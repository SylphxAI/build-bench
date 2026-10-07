# build-bench

The same four builds, at the same pinned commits, timed on GitHub-hosted
runners and on [Sylphx Build](https://sylphx.com/build): cold and warm, five
runs per cell, with every raw run kept in [`results/`](results/).

| Target | Source | Command |
| --- | --- | --- |
| `ripgrep` | [BurntSushi/ripgrep](https://github.com/BurntSushi/ripgrep) 15.2.0 (`e89fff89`) | `cargo build --release --locked` |
| `caddy` | [caddyserver/caddy](https://github.com/caddyserver/caddy) v2.11.7 (`72dd0fb0`) | `go build -trimpath -o caddy ./cmd/caddy` |
| `nextjs` | [`nextjs/`](nextjs/): vercel/next.js v16.4.0 `examples/with-docker` | `pnpm install --frozen-lockfile && pnpm build` |
| `image` | the same app's upstream `Dockerfile` | a BuildKit image build, not pushed (`sylphx build image` on Sylphx Build) |

`nextjs/` is the upstream example unchanged (MIT, Vercel) except for a
`packageManager` field that pins pnpm 10.34.6, so both sides install the same
pnpm, and a `/target` line in `.dockerignore` (a Sylphx Build workspace links
`target/` into the tree; it is not in the tree anywhere else).

Sylphx build machines carry Rust, Bun and BuildKit but no Go or Node, so the
`caddy` and `nextjs` runs there download Go 1.26.0 (caddy's `go` line, what
`setup-go` installs) and Node 24.21.0 plus pnpm first. That download is in the
wall time and not in the command time; each run records it as `toolchain`.

## Runners

- **GitHub**: `ubuntu-latest` on this public repository (4 vCPU, 16 GB; free
  for public repositories), [`.github/workflows/bench.yml`](.github/workflows/bench.yml).
  Every job is skipped if the repository is ever made private.
- **Sylphx Build**: `sylphx build run` on a `standard` (8 vCPU) and a `large`
  (16 vCPU) build machine, [`scripts/sylphx-bench.sh`](scripts/sylphx-bench.sh).

## Cache states

| State | GitHub | Sylphx Build |
| --- | --- | --- |
| cold | a fresh runner, no cache step | `--fresh --no-cache`: an empty workspace and no shared build cache |
| warm | a fresh runner restoring GitHub's standard cache actions (`Swatinem/rust-cache`, `setup-go` cache, `setup-node` pnpm cache plus `.next/cache`, BuildKit `type=gha`), filled by one priming run that is not counted | `--fresh` (a clean tree on an empty workspace) with the shared build cache warm, after one priming run that is not counted; the image keeps BuildKit's layers on its warm workspace, its only cache |
| workspace | - | the default re-run: the warm workspace and the unchanged tree |

The image target on Sylphx Build is the product command, `sylphx build image`,
from its own git repository holding a copy of `nextjs/`: cold is a new
repository per run (a new workspace, so an empty BuildKit state), warm is one
repository per size rebuilt unchanged after a priming build (BuildKit's layers
and cache mounts stay on the warm workspace, its only cache).

`--no-cache` also turns off the build cache's package mirrors, so a cold run
reaches the public registries (crates.io, the Go proxy, npm), as a GitHub
runner does. On 2026-10-07 the build cache's Go mirror answered 502 for larger
module zips, so every warm caddy run through it failed; those runs are kept,
and `BENCH_GOPROXY=public` measures the same cell with the public Go proxy
(files named `*-publicproxy.jsonl`).

Package downloads go to the public registries on GitHub and to the build
cache's mirrors (crates, Go modules, npm) on Sylphx Build in every state.

## Measures

- **Wall**: from the request to the result. GitHub: the job's `created_at` to
  `completed_at` (the API's own times). Sylphx: the CLI call to its `result`
  event, retries of a retryable platform failure included.
- **Queue**: from the request to a machine starting the job. GitHub: the job's
  `created_at` to `started_at`. Sylphx: the CLI call to its `running` event
  (the machine booted and its workspace attached).
- **Command**: the build command alone. GitHub: the build step's start and end.
  Sylphx: timed inside the machine around the command.
- p50 and p90 are nearest-rank; with five runs p90 is the slowest run.

## Running it

```sh
# GitHub side: one workflow run per cache state
gh workflow run bench.yml -f cache=cold
gh workflow run bench.yml -f cache=warm
scripts/github-fetch.sh <run id> <cold|warm>

# Sylphx side: one cell at a time, from a directory linked with `sylphx link`
# (its .sylphx/project.json is copied into each source tree)
scripts/sylphx-bench.sh ripgrep large cold

python3 scripts/report.py --runs   # results/README.md
```

The results of 2026-10-07 are in [`results/README.md`](results/README.md).
