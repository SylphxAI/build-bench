#!/usr/bin/env python3
"""Print the benchmark tables from the raw runs in results/.

results/github/<run id>.jobs.json  the Actions jobs API answer of one
                                   workflow run (scripts/github-fetch.sh)
results/github/<run id>.cache.tsv  target, n, cache line of each job (from
                                   the run's log)
results/sylphx/*.jsonl             one line per run (scripts/sylphx-bench.sh)

Per cell: p50 and p90 (nearest rank; with five runs p90 is the slowest run)
of the wall time from request to result, of the queue wait (request to the
machine starting the job), and of the build command alone; and how many runs
restored a cache.
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


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


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
        run = json.load(open(f))
        cache_file = f.replace(".jobs.json", ".cache.tsv")
        hits = {}
        if os.path.exists(cache_file):
            for line in open(cache_file):
                name, rest = line.rstrip("\n").split("\t", 1)
                hits[name] = rest.replace('"', "").replace("rust= ", "").replace("go= ", "").replace("pnpm= ", "").strip()
        for j in run["jobs"]:
            if not j["name"].startswith("run (") or j["conclusion"] != "success":
                continue
            target, n = j["name"][5:-1].split(", ")
            build = next(s for s in j["steps"] if s["name"] in (f"build {target}", "build image") and s["conclusion"] == "success")
            rows.append({
                "side": "GitHub ubuntu-latest (4 vCPU)",
                "run_id": run["run_id"],
                "target": target,
                "cache": run["cache"],
                "n": int(n),
                "start": j["created_at"],
                "wall": ts(j["completed_at"]) - ts(j["created_at"]),
                "queue": ts(j["started_at"]) - ts(j["created_at"]),
                "command": ts(build["completed_at"]) - ts(build["started_at"]),
                "hit": hits.get(j["name"]),
                "source": f"https://github.com/SylphxAI/build-bench/actions/runs/{run['run_id']}/job/{j['id']}",
            })
    return rows


def sylphx():
    rows = []
    for f in sorted(glob.glob(os.path.join(ROOT, "sylphx", "*.jsonl"))):
        for line in open(f):
            r = json.loads(line)
            if not r.get("counted"):
                continue
            rows.append({
                "side": f"Sylphx Build {r['size']} ({r['vcpu']} vCPU)",
                "target": r["target"],
                "cache": r["cache"],
                "n": r["n"],
                "start": r["start"],
                "wall": r["wall"],
                "queue": r["queue"],
                "command": r["command"],
                "hit": r.get("hit"),
                "source": os.path.basename(f),
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
    files stay in results/github/)."""
    newest = {}
    for r in rows:
        k = (r["target"], r["cache"])
        newest[k] = max(newest.get(k, 0), r["run_id"])
    return [r for r in rows if r["run_id"] == newest[(r["target"], r["cache"])]]


def main():
    rows = latest(github()) + sylphx()
    cells = {}
    for r in rows:
        cells.setdefault((r["target"], r["cache"], r["side"]), []).append(r)
    print("| Target | Cache | Runner | n | Wall p50 | Wall p90 | Queue p50 | Queue p90 | Command p50 | Command p90 | Cache restored |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    order = {t: i for i, t in enumerate(TARGETS)}
    for (target, cache, side), rs in sorted(cells.items(), key=lambda k: (order.get(k[0][0], 9), k[0][1], k[0][2])):
        w = [r["wall"] for r in rs]
        q = [r["queue"] for r in rs]
        c = [r["command"] for r in rs]
        hits = [r["hit"] for r in rs if r["hit"]]
        hit = f"{sum(1 for h in hits if restored(h))}/{len(rs)}" if hits else "-"
        print(f"| {target} | {cache} | {side} | {len(rs)} | {fmt(pct(w, 50))} | {fmt(pct(w, 90))} | {fmt(pct(q, 50))} | {fmt(pct(q, 90))} | {fmt(pct(c, 50))} | {fmt(pct(c, 90))} | {hit} |")
    if "--runs" in sys.argv:
        print()
        print("| Target | Cache | Runner | Run | Start (UTC) | Wall s | Queue s | Command s | Cache | Source |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in sorted(rows, key=lambda r: (order.get(r["target"], 9), r["cache"], r["side"], r["n"])):
            print(f"| {r['target']} | {r['cache']} | {r['side']} | {r['n']} | {r['start']} | {r['wall']:.0f} | {r['queue']:.0f} | {r['command']:.0f} | {r['hit'] or '-'} | {r['source']} |")


if __name__ == "__main__":
    main()
