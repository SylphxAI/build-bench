#!/usr/bin/env bash
# The Sylphx Build side of the benchmark: one cell (target, size, cache) of
# RUNS runs, one at a time, each through `sylphx build run` from a work tree
# at the pinned commit. One JSON line per run is appended to OUT.
#
#   scripts/sylphx-bench.sh <ripgrep|caddy|nextjs|image> <standard|large> <cold|warm|workspace> [RUNS] [OUT]
#
# cold       --fresh --no-cache: an empty workspace and no shared build cache;
#            only the package mirrors (crates, Go, npm) answer, as the public
#            registries do for a GitHub runner.
# warm       --fresh after one priming run that is not counted: a clean tree on
#            an empty workspace with the shared build cache (sccache) warm, the
#            counterpart of a fresh GitHub runner restoring its Actions cache.
#            The image target keeps BuildKit's layers on the workspace, which is
#            its only cache, so its warm runs take the warm workspace.
# workspace  the default re-run after one priming run: the warm workspace with
#            the unchanged tree (what `sylphx build run` does on a second call).
#
# Needs SYLPHX_API_KEY (or `sylphx login`) and SRC, a directory holding the
# ripgrep and caddy clones (made here if missing). A platform failure the CLI
# marks retryable is retried up to 5 times; the run's line counts the attempts.
# Each run's raw output, one timestamped line per event, is kept in OUT's
# .logs directory.
set -u
target=${1:?target}; size=${2:?size}; cache=${3:?cache}; runs=${4:-5}
here=$(cd "$(dirname "$0")/.." && pwd)
out=${5:-$here/results/sylphx/$(date -u +%Y%m%dT%H%M%SZ)-$target-$size-$cache.jsonl}
SRC=${SRC:-$here/.src}
logs=${out%.jsonl}.logs
mkdir -p "$(dirname "$out")" "$SRC" "$logs"

RIPGREP_SHA=e89fff89ac9af12e8d4ce9d5fd07beb408ca730f # 15.2.0
CADDY_SHA=72dd0fb067f6d7826c7f79907670ba4a713bfe37   # v2.11.7

fetch() { # <dir> <url> <sha>
  [ -d "$SRC/$1/.git" ] || { git init -q "$SRC/$1" && git -C "$SRC/$1" fetch -q --depth 1 "$2" "$3" && git -C "$SRC/$1" checkout -q FETCH_HEAD; }
  [ "$(git -C "$SRC/$1" rev-parse HEAD)" = "$3" ] || { echo "$1 is not at $3" >&2; exit 2; }
}

# The command runs under `sh -c` in the lease; BENCH_CMD_SECONDS is the build
# alone, timed inside the machine.
timed() { printf 't0=$(date +%%s.%%N); %s; rc=$?; t1=$(date +%%s.%%N); echo "BENCH_CMD_SECONDS=$(awk "BEGIN{print $t1-$t0}")"; exit $rc' "$1"; }

npm_hosts=(--allow-host registry.npmjs.org)
case $target in
  ripgrep)
    fetch ripgrep https://github.com/BurntSushi/ripgrep $RIPGREP_SHA; dir=$SRC/ripgrep; path=.
    cmd=$(timed 'cargo build --release --locked; s=$?; sccache --show-stats 2>/dev/null | grep -E "^(Compile requests|Cache hits|Cache misses) " | sed "s/^/BENCH_SCCACHE /"; [ $s = 0 ]')
    extra=() ;;
  caddy)
    fetch caddy https://github.com/caddyserver/caddy $CADDY_SHA; dir=$SRC/caddy; path=cmd/caddy
    cmd=$(timed 'go build -trimpath -o caddy .')
    extra=() ;;
  nextjs)
    dir=$here; path=nextjs
    cmd=$(timed 'corepack enable --install-directory "$HOME/.local/bin" pnpm >/dev/null 2>&1; PATH=$HOME/.local/bin:$PATH; pnpm install --frozen-lockfile && pnpm build')
    extra=("${npm_hosts[@]}") ;;
  image)
    dir=$here; path=nextjs
    # sylphx build image's own guest command, with the BuildKit state on the
    # workspace (warm) or on the machine's disk (cold).
    state='$PWD/../../buildkit'; [ "$cache" = cold ] && state=/tmp/buildkit-cold
    cmd=$(timed "/usr/local/bin/sylphx-image-build --context . --state $state --metadata /tmp/image.json")
    extra=("${npm_hosts[@]}" --allow-host registry-1.docker.io --allow-host auth.docker.io --allow-host production.cloudflare.docker.com) ;;
  *) echo "unknown target $target" >&2; exit 2 ;;
esac
case $cache in
  cold) flags=(--fresh --no-cache) ;;
  warm) if [ "$target" = image ]; then flags=(); else flags=(--fresh); fi ;;
  workspace) flags=() ;;
  *) echo "unknown cache $cache" >&2; exit 2 ;;
esac
vcpu=$([ "$size" = standard ] && echo 8 || echo 16)

one() { # <n> <counted 0|1>: one run, one JSON line
  local n=$1 counted=$2 log attempt=0 start t0 rc
  log=$(mktemp "${TMPDIR:-/tmp}/sylphx-bench.XXXXXX")
  start=$(date -u +%Y-%m-%dT%H:%M:%SZ); t0=$(date +%s.%N)
  while :; do
    attempt=$((attempt + 1))
    # Each event line gets the second it arrived, so the queue wait is the
    # time from the call to the `running` event.
    ( cd "$dir/$path" && timeout 3600 sylphx build run --size "$size" --queue-timeout 30m -o json "${flags[@]}" "${extra[@]}" -- sh -c "$cmd" ) 2>&1 \
      | while IFS= read -r line; do printf '%s\t%s\n' "$(date +%s.%N)" "$line"; done >"$log"
    rc=${PIPESTATUS[0]}
    # Retry a platform failure that never reached the command: one the CLI
    # marks retryable, or a workspace it picked while that was being deleted.
    grep -q '"outcome":"platform_error"' "$log" && grep -qE '"retryable":true|is deleting' "$log" && [ $attempt -lt 6 ] || break
    cat "$log" >>"$logs/$n.attempts.log"
    sleep 20
  done
  python3 - "$log" "$t0" "$start" "$target" "$size" "$vcpu" "$cache" "$n" "$counted" "$attempt" "$rc" <<'PY' >>"$out"
import json, re, sys
log, t0, start, target, size, vcpu, cache, n, counted, attempts, rc = sys.argv[1:]
t0 = float(t0); ev = {}; cmd = None; sc = {}; cached = steps = 0
for line in open(log, errors="replace"):
    t, _, body = line.rstrip("\n").partition("\t")
    if body.startswith("{"):
        try:
            e = json.loads(body); ev.setdefault(e.get("type"), (float(t), e))
            if e.get("type") == "result": ev["result"] = (float(t), e)
        except ValueError: pass
    m = re.search(r"BENCH_CMD_SECONDS=([0-9.]+)", body)
    if m: cmd = float(m.group(1))
    m = re.match(r"BENCH_SCCACHE (Compile requests|Cache hits|Cache misses)\s+(\d+)", body)
    if m: sc[m.group(1)] = int(m.group(2))
    if re.match(r"#\d+ \[", body) and " RUN " in body or re.match(r"#\d+ \[.*\] (COPY|RUN|FROM)", body): steps += 1
    if re.match(r"#\d+ CACHED", body): cached += 1
res = ev.get("result", (None, {}))[1]
run = ev.get("running")
hit = None
if target == "ripgrep" and sc.get("Compile requests"):
    hit = f"sccache {sc.get('Cache hits', 0)}/{sc['Compile requests']}"
elif target == "image":
    hit = f"buildkit cached {cached}"
print(json.dumps({
    "target": target, "size": size, "vcpu": int(vcpu), "cache": cache, "n": int(n), "counted": counted == "1",
    "start": start, "attempts": int(attempts), "exit_code": res.get("exit_code", int(rc)), "outcome": res.get("outcome"),
    "wall": (ev["result"][0] - t0) if "result" in ev else None, "wall_cli": res.get("duration_ms", 0) / 1000, "queue": (run[0] - t0) if run else None, "command": cmd,
    "workspace": res.get("workspace"), "bytes_up": res.get("bytes_up"), "lease": (run[1].get("lease", "").rsplit("/", 1)[-1] if run else None),
    "hit": hit, "error": res.get("error"),
}))
PY
  tail -n 1 "$out"
  mv "$log" "$logs/$n.log"
}

[ "$cache" = cold ] || one 0 0
for n in $(seq 1 "$runs"); do one "$n" 1; done
