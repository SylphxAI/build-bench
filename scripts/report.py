#!/usr/bin/env python3
"""Print the benchmark tables from the raw runs in results/.

results/github/<run id>.jobs.json  the Actions jobs API answer of one
                                   workflow run (scripts/github-fetch.sh)
results/github/<run id>.cache.tsv  target, n, cache line of each job (from
                                   the run's log)
results/sylphx/*.jsonl             one line per run (scripts/sylphx-bench.sh)

Per cell: p50 and p90 (nearest rank; with five runs p90 is the slowest run)
of the wall, queue and command spans described in README.md (image spans
differ between runners); and how many runs restored a cache.
"""
import glob
import json
import math
import os
import re
import sys
from datetime import datetime

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
TARGETS = ["ripgrep", "caddy", "nextjs", "image"]
# The counted matrix in bench.yml; a missing job is not a successful sample.
RUN_NUMBERS = range(1, 6)
REPO = "https://github.com/SylphxAI/build-bench"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def elapsed(start, end):
    return ts(end) - ts(start) if start and end else None


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[max(0, math.ceil(p / 100 * len(xs)) - 1)]


def fmt(s):
    if s is None:
        return "-"
    s = round(s)
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


def github():
    rows = []
    for f in sorted(glob.glob(os.path.join(ROOT, "github", "*.jobs.json"))):
        with open(f) as source:
            run = json.load(source)
        cache_file = f.replace(".jobs.json", ".cache.tsv")
        hits = {}
        if os.path.exists(cache_file):
            for line in open(cache_file):
                name, rest = line.rstrip("\n").split("\t", 1)
                hits[name] = rest.replace('"', "").replace("rust= ", "").replace("go= ", "").replace("pnpm= ", "").strip()
        jobs = {}
        targets = set()
        for j in run["jobs"]:
            counted = re.fullmatch(r"run \(([^,]+), (\d+)\)", j["name"])
            prime = re.fullmatch(r"prime \(([^,]+)\)", j["name"])
            if counted:
                target, n = counted.groups()
                jobs[(target, int(n))] = j
                targets.add(target)
            elif prime:
                targets.add(prime.group(1))
        if not targets:
            raise ValueError(f"run {run['run_id']}: cannot identify benchmark targets from jobs")
        for target, n in sorted({(t, n) for t in targets for n in RUN_NUMBERS} | set(jobs)):
            j = jobs.get((target, n), {})
            build = next((s for s in j.get("steps", []) if s["name"] == f"build {target}" and s["conclusion"] != "skipped"), {})
            result = (j.get("conclusion") or j.get("status") or "unknown") if j else "missing"
            source = f"{REPO}/actions/runs/{run['run_id']}"
            if j:
                source += f"/job/{j['id']}"
            rows.append({
                "side": "GitHub ubuntu-latest (4 vCPU)",
                "run_id": run["run_id"],
                "ok": result == "success",
                "result": "ok" if result == "success" else result,
                "toolchain": None,
                "target": target,
                "cache": run["cache"],
                "n": n,
                "start": j.get("created_at"),
                "wall": elapsed(j.get("created_at"), j.get("completed_at")),
                "queue": elapsed(j.get("created_at"), j.get("started_at")),
                "command": elapsed(build.get("started_at"), build.get("completed_at")),
                "hit": hits.get(j.get("name")),
                "source": source,
            })
    return rows


def sylphx():
    rows = []
    for f in sorted(glob.glob(os.path.join(ROOT, "sylphx", "*.jsonl"))):
        for line in open(f):
            r = json.loads(line)
            if not r.get("counted"):
                continue
            side = f"Sylphx Build {r['size']} ({r['vcpu']} vCPU)"
            if r.get("goproxy") == "public":
                side += ", public Go proxy"
            rows.append({
                "side": side,
                "ok": r.get("outcome") == "succeeded",
                "toolchain": r.get("toolchain"),
                "target": r["target"],
                "cache": r["cache"],
                "n": r["n"],
                "start": r["start"],
                "wall": r["wall"],
                "queue": r["queue"],
                "command": r["command"],
                "hit": r.get("hit"),
                "source": f"{REPO}/blob/main/results/sylphx/{os.path.basename(f)}",
            })
    return rows


def restored(h):
    """Whether a run's cache line shows a restored cache: an Actions cache
    hit, BuildKit steps found cached, or sccache hits."""
    if "=true" in h:
        return True
    m = re.search(r"(?:buildkit-cached=|buildkit cached )(\d+)", h)
    if m:
        return int(m.group(1)) > 0
    m = re.match(r"sccache (\d+)/", h)
    return bool(m and int(m.group(1)) > 0)


def latest(rows):
    """Per target and cache state, only the runs of the newest workflow run:
    a cell re-run after a workflow fix replaces the earlier one (whose raw
    files stay in results/github/). Failed and missing jobs participate too,
    so a failed rerun never falls back to an older successful cell."""
    newest = {}
    for r in rows:
        k = (r["target"], r["cache"])
        newest[k] = max(newest.get(k, 0), r["run_id"])
    return [r for r in rows if r["run_id"] == newest[(r["target"], r["cache"])]]


def cell_link(url):
    """The raw source of a whole cell: its workflow run, or its runs file."""
    if "/actions/runs/" in url:
        run = url.split("/actions/runs/")[1].split("/")[0]
        return f"[run {run}]({REPO}/actions/runs/{run})"
    return f"[{url.rsplit('/', 1)[1]}]({url})"


def link(url):
    return f"[{'job' if '/job/' in url else 'run'}]({url})"


def main():
    rows = latest(github()) + sylphx()
    cells = {}
    for r in rows:
        cells.setdefault((r["target"], r["cache"], r["side"]), []).append(r)
    print("| Target | Cache | Runner | Runs ok | Wall p50 | Wall p90 | Queue p50 | Queue p90 | Command p50 | Command p90 | Cache restored | Raw |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    order = {t: i for i, t in enumerate(TARGETS)}
    for (target, cache, side), rs in sorted(cells.items(), key=lambda k: (order.get(k[0][0], 9), k[0][1], k[0][2])):
        ok = [r for r in rs if r["ok"]]
        w = [r["wall"] for r in ok]
        q = [r["queue"] for r in ok]
        c = [r["command"] for r in ok]
        hits = [r["hit"] for r in ok if r["hit"]]
        hit = f"{sum(1 for h in hits if restored(h))}/{len(ok)}" if hits else "-"
        raw = ", ".join(sorted({cell_link(r["source"]) for r in rs}))
        print(f"| {target} | {cache} | {side} | {len(ok)}/{len(rs)} | {fmt(pct(w, 50))} | {fmt(pct(w, 90))} | {fmt(pct(q, 50))} | {fmt(pct(q, 90))} | {fmt(pct(c, 50))} | {fmt(pct(c, 90))} | {hit} | {raw} |")
    print("\nPercentiles use successful runs only; Runs ok includes failed, skipped, cancelled and missing samples. "
          "GitHub uses the newest workflow per target/cache, including failures. "
          "Image command spans differ: GitHub times the build action; Sylphx times running to result, "
          "including BuildKit startup and OCI export. These are elapsed times on different CPU sizes and cache systems, "
          "not equal-resource speedups.")
    if "--runs" in sys.argv:
        print()
        print("| Target | Cache | Runner | Run | Start (UTC) | Result | Wall s | Queue s | Command s | Toolchain s | Cache | Raw |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        num = lambda x: "-" if x is None else f"{x:.0f}"
        for r in sorted(rows, key=lambda r: (order.get(r["target"], 9), r["cache"], r["side"], r["n"])):
            result = r.get("result", "ok" if r["ok"] else "failed")
            print(f"| {r['target']} | {r['cache']} | {r['side']} | {r['n']} | {r['start'] or '-'} | {result} | {num(r['wall'])} | {num(r['queue'])} | {num(r['command'])} | {num(r['toolchain'])} | {r['hit'] or '-'} | {link(r['source'])} |")


if __name__ == "__main__":
    main()
