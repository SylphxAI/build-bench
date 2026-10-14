#!/usr/bin/env bash
# The Sylphx Build side of the benchmark: one cell (target, size, cache) of
# RUNS runs, one at a time, each through `sylphx build run` from a work tree
# at the pinned commit. One JSON line per run is appended to OUT.
#
#   scripts/sylphx-bench.sh <ripgrep|caddy|nextjs|image> <standard|large> <cold|warm|workspace> [RUNS] [OUT]
#
# cold       --fresh --no-cache: an empty workspace, no shared build cache and
#            no package mirrors; packages come from the public registries, as
#            on a GitHub runner.
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
# BENCH_GOPROXY=public: the caddy target fetches modules from the public Go
# proxy in every state, bypassing the build cache's Go mirror (it answered
# 502 for larger module zips on 2026-10-07, so warm caddy runs failed).
goproxy=${BENCH_GOPROXY:-mirror}
variant=; [ "$goproxy" = public ] && variant=-publicproxy
out=${5:-$here/results/sylphx/$(date -u +%Y%m%dT%H%M%SZ)-$target-$size-$cache$variant.jsonl}
SRC=${SRC:-$here/.src}
logs=${out%.jsonl}.logs
mkdir -p "$(dirname "$out")" "$SRC" "$logs"

RIPGREP_SHA=e89fff89ac9af12e8d4ce9d5fd07beb408ca730f # 15.2.0
CADDY_SHA=72dd0fb067f6d7826c7f79907670ba4a713bfe37   # v2.11.7

fetch() { # <dir> <url> <sha>
  [ -d "$SRC/$1/.git" ] || { git init -q "$SRC/$1" && git -C "$SRC/$1" fetch -q --depth 1 "$2" "$3" && git -C "$SRC/$1" checkout -q FETCH_HEAD; }
  [ "$(git -C "$SRC/$1" rev-parse HEAD)" = "$3" ] || { echo "$1 is not at $3" >&2; exit 2; }
  # The clone runs in this directory's linked org, project and env.
  [ -f "$SRC/$1/.sylphx/project.json" ] || { mkdir -p "$SRC/$1/.sylphx" && cp "$here/.sylphx/project.json" "$SRC/$1/.sylphx/"; }
}

# The command runs under `sh -c` in the lease; BENCH_CMD_SECONDS is the build
# alone, timed inside the machine.
timed() { printf 't0=$(date +%%s.%%N); %s; rc=$?; t1=$(date +%%s.%%N); echo "BENCH_CMD_SECONDS=$(awk "BEGIN{print $t1-$t0}")"; exit $rc' "$1"; }

# The build machine's image carries Rust, Bun and BuildKit but no Go or Node
# (a GitHub runner's image has both in its tool cache), so those targets
# download the release toolchain first, from its official host. That time is
# in the wall time, not in the command time, and is printed on its own.
GO_VERSION=1.26.0     # caddy's go.mod `go` line, what setup-go installs
NODE_VERSION=24.21.0  # the newest Node 24 on 2026-10-07
tool() { printf 'u0=$(date +%%s.%%N); T=/tmp/bench-tools; mkdir -p $T; %s || exit 125; u1=$(date +%%s.%%N); echo "BENCH_TOOL_SECONDS=$(awk "BEGIN{print $u1-$u0}")"; ' "$1"; }
go_tool=$(tool "curl -fsSL https://dl.google.com/go/go$GO_VERSION.linux-amd64.tar.gz | tar xz -C \$T && export PATH=\$T/go/bin:\$PATH")
PNPM_VERSION=10.34.6  # nextjs/package.json packageManager
node_tool=$(tool "curl -fsSL https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz | tar xJ -C \$T && export PATH=\$T/node-v$NODE_VERSION-linux-x64/bin:\$PATH && npm install -g -s pnpm@$PNPM_VERSION")

# next/font downloads the app's Google fonts during `next build`, on GitHub too.
npm_hosts=(--allow-host registry.npmjs.org --allow-host fonts.googleapis.com --allow-host fonts.gstatic.com)
case $target in
  ripgrep)
    fetch ripgrep https://github.com/BurntSushi/ripgrep $RIPGREP_SHA; dir=$SRC/ripgrep; path=.
    cmd=$(timed 'cargo build --release --locked; s=$?; sccache --show-stats 2>/dev/null | grep -E "^(Compile requests|Cache hits|Cache misses) " | sed "s/^/BENCH_SCCACHE /"; [ $s = 0 ]')
    extra=() ;;
  caddy)
    fetch caddy https://github.com/caddyserver/caddy $CADDY_SHA; dir=$SRC/caddy; path=cmd/caddy
    cmd=$go_tool$(timed 'go build -trimpath -o caddy .')
    extra=(--allow-host dl.google.com)
    if [ "$goproxy" = public ]; then
      cmd="export GOPROXY=https://proxy.golang.org; $cmd"
      extra+=(--allow-host proxy.golang.org --allow-host sum.golang.org --allow-host storage.googleapis.com)
    fi ;;
  nextjs)
    dir=$here; path=nextjs
    cmd=$node_tool$(timed 'pnpm install --frozen-lockfile && pnpm build')
    extra=("${npm_hosts[@]}" --allow-host nodejs.org) ;;
  image)
    # `sylphx build image` of the upstream Dockerfile, from its own git
    # repository: a new repository per cold run (a new workspace, so an empty
    # BuildKit state), one repository per size for the warm runs (BuildKit's
    # layers and cache mounts stay on its warm workspace, its only cache).
    path=.; cmd=
    extra=("${npm_hosts[@]}" --allow-host registry-1.docker.io --allow-host auth.docker.io --allow-host production.cloudflare.docker.com --allow-host production.cloudfront.docker.com) ;;
  *) echo "unknown target $target" >&2; exit 2 ;;
esac
# --no-cache also drops the build cache's package mirrors, so a cold run
# fetches from the public registries, as a GitHub runner does.
public=()
case $target in
  ripgrep) public=(--allow-host index.crates.io --allow-host static.crates.io) ;;
  caddy) public=(--allow-host proxy.golang.org --allow-host sum.golang.org --allow-host storage.googleapis.com) ;;
esac
case $cache in
  cold) flags=(--fresh --no-cache "${public[@]}") ;;
  warm) flags=(--fresh) ;;
  workspace) flags=() ;;
  *) echo "unknown cache $cache" >&2; exit 2 ;;
esac
vcpu=$([ "$size" = standard ] && echo 8 || echo 16)
[ "$target" = image ] && flags=()

# A new git repository holding a copy of nextjs/ at one commit.
image_repo() { # <dir>
  rm -rf "$1"; mkdir -p "$1"; cp -r "$here/nextjs/." "$1/"
  rm -rf "$1/.sylphx" "$1/node_modules" "$1/.next"
  git -C "$1" init -q && git -C "$1" add -A \
    && GIT_AUTHOR_DATE="$(date -u +%FT%T)Z" GIT_COMMITTER_DATE="$(date -u +%FT%T)Z" \
       git -C "$1" -c user.name=build-bench -c user.email=build-bench@sylphx.com commit -qm "nextjs with-docker"
  mkdir -p "$1/.sylphx"; cp "$here/.sylphx/project.json" "$1/.sylphx/"; echo .sylphx/ >>"$1/.git/info/exclude"
}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
if [ "$target" = image ] && [ "$cache" != cold ]; then dir=$SRC/image-$cache-$size-$stamp; image_repo "$dir"; fi

one() { # <n> <counted 0|1>: one run, one JSON line
  local n=$1 counted=$2 log attempt=0 start t0 rc
  log=$(mktemp "${TMPDIR:-/tmp}/sylphx-bench.XXXXXX")
  start=$(date -u +%Y-%m-%dT%H:%M:%SZ); t0=$(date +%s.%N)
  while :; do
    attempt=$((attempt + 1))
    # Each event line gets the second it arrived, so the queue wait is the
    # time from the call to the `running` event.
    if [ "$target" = image ]; then
      [ "$cache" = cold ] && { dir=$SRC/image-cold-$size-$stamp-$n-$attempt; image_repo "$dir"; }
      run=(sylphx build image --size "$size" --queue-timeout 30m -o json "${extra[@]}" .)
    else
      run=(sylphx build run --size "$size" --queue-timeout 30m -o json "${flags[@]}" "${extra[@]}" -- sh -c "$cmd")
    fi
    ( cd "$dir/$path" && timeout 3600 "${run[@]}" ) 2>&1 \
      | while IFS= read -r line; do printf '%s\t%s\n' "$(date +%s.%N)" "$line"; done >"$log"
    rc=${PIPESTATUS[0]}
    # Retry a platform failure that never reached the command: one the CLI
    # marks retryable, or a workspace it picked while that was being deleted.
    grep -q '"outcome":"platform_error"' "$log" && grep -qE '"retryable":true|is deleting' "$log" && [ $attempt -lt 6 ] || break
    cat "$log" >>"$logs/$n.attempts.log"
    sleep 20
  done
  python3 - "$log" "$t0" "$start" "$target" "$size" "$vcpu" "$cache" "$n" "$counted" "$attempt" "$rc" <<'PY' >>"$out"
import json, os, re, sys
log, t0, start, target, size, vcpu, cache, n, counted, attempts, rc = sys.argv[1:]
t0 = float(t0); ev = {}; cmd = tool = None; text = []; sc = {}; cached = steps = 0
for line in open(log, errors="replace"):
    t, _, body = line.rstrip("\n").partition("\t")
    if body.startswith("{"):
        try:
            e = json.loads(body); ev.setdefault(e.get("type"), (float(t), e))
            if e.get("type") == "result": ev["result"] = (float(t), e)
            if e.get("type") in ("stdout", "stderr"): text.append(e.get("data", ""))
        except ValueError: pass
    else:
        text.append(body + "\n")
for body in "".join(text).splitlines():
    m = re.search(r"BENCH_CMD_SECONDS=([0-9.]+)", body)
    if m: cmd = float(m.group(1))
    m = re.search(r"BENCH_TOOL_SECONDS=([0-9.]+)", body)
    if m: tool = float(m.group(1))
    m = re.match(r"BENCH_SCCACHE (Compile requests|Cache hits|Cache misses)\s+(\d+)", body)
    if m: sc[m.group(1)] = int(m.group(2))
    if re.match(r"#\d+ CACHED", body): cached += 1
res = ev.get("result", (None, {}))[1]
run = ev.get("running")
if target == "image" and run and "result" in ev:
    cmd = ev["result"][0] - run[0]  # the build on the machine, from `running` to `result`
hit = None
if target == "ripgrep" and sc.get("Compile requests"):
    hit = f"sccache {sc.get('Cache hits', 0)}/{sc['Compile requests']}"
elif target == "image":
    hit = f"buildkit cached {cached}"
print(json.dumps({
    "target": target, "goproxy": os.environ.get("BENCH_GOPROXY", "mirror") if target == "caddy" else None, "size": size, "vcpu": int(vcpu), "cache": cache, "n": int(n), "counted": counted == "1",
    "start": start, "attempts": int(attempts), "exit_code": res.get("exit_code", int(rc)), "outcome": res.get("outcome"),
    "wall": (ev["result"][0] - t0) if "result" in ev else None, "wall_cli": res.get("duration_ms", 0) / 1000, "queue": (run[0] - t0) if run else None, "command": cmd, "toolchain": tool,
    "workspace": res.get("workspace"), "image_manifest_digest": (res.get("image") or {}).get("image_manifest_digest"), "bytes_up": res.get("bytes_up"), "lease": (run[1].get("lease", "").rsplit("/", 1)[-1] if run else None),
    "hit": hit, "error": res.get("error"),
}))
PY
  tail -n 1 "$out"
  mv "$log" "$logs/$n.log"
}

[ "$cache" = cold ] || one 0 0
for n in $(seq 1 "$runs"); do one "$n" 1; done
