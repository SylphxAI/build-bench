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

## Gate

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every pull
request and merge-queue group and publishes the required check `ci-ok`: it
syntax-checks and compiles `scripts/`, prints the README's tables with
`scripts/report.py`, and builds `nextjs/` exactly as `bench.yml` does
(`pnpm install --frozen-lockfile && pnpm build`). `bench.yml` is
dispatch-only — it times pinned upstream commits — so it reports on nothing
here and cannot be the required check.

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

For non-image targets, `--no-cache` also turns off the build cache's package
mirrors, so a cold run reaches the public registries (crates.io, the Go proxy,
npm), as a GitHub runner does. On 2026-10-07 the build cache's Go mirror
answered 502 for larger module zips, so every warm caddy run through it failed; those runs are kept,
and `BENCH_GOPROXY=public` measures the same cell with the public Go proxy
(files named `*-publicproxy.jsonl`).

Package downloads on GitHub use public registries. For the non-image Sylphx
targets, cold runs disable the shared cache and its package mirrors; warm and
workspace runs use the mirrors unless `BENCH_GOPROXY=public` overrides Go.
Image cold runs use a new workspace with empty BuildKit state; the image
command does not pass `--no-cache`, so this does not establish that its package
downloads bypass the mirrors.

## Measures

- **Wall**: GitHub: the job's `created_at` to
  `completed_at` (the API's own times). Sylphx: the CLI call to its `result`
  event, retries of a retryable platform failure included. The GitHub span
  excludes workflow dispatch and the warm priming job; it is not the time from
  dispatch to the final result.
- **Queue**: GitHub: the job's
  `created_at` to `started_at`. Sylphx: the CLI call to its `running` event
  (the machine booted and its workspace attached).
- **Command**: for `ripgrep`, `caddy` and `nextjs`, GitHub uses the build
  step's start and end; Sylphx times the command inside the machine. For
  `image`, GitHub times `docker/build-push-action` (including its cache
  import/export), while Sylphx uses the `running` to `result` event interval
  (including BuildKit startup, attestations and OCI export). GitHub sets
  `push: false` with no output; Sylphx produces an OCI image. These image spans
  measure different work and cannot establish a like-for-like build speedup.
- p50 and p90 are nearest-rank; with five runs p90 is the slowest run.

These are elapsed times for the listed runner sizes and cache systems, not
equal-resource comparisons: GitHub has 4 vCPU and Sylphx has 8 or 16 vCPU.
The data does not measure CPU utilization, peak memory, output size or cost
per build, so it cannot establish efficiency or price-performance ratios.

The report selects the newest GitHub workflow for each target/cache cell,
including failed workflows. Failed, skipped, cancelled and missing matrix
jobs remain in `Runs ok`'s denominator; percentiles use successful runs only.
Missing slots in the five-run matrix are shown as `missing` in `--runs`.
Priming-job names identify the target even when its counted jobs never start.
If an archive has no identifiable target, reporting fails with its run ID
rather than silently showing an older cell. A targeted rerun replaces only
the targets it names; older raw files remain available.

## Running it

The separate [per-operation simulator transport](scripts/simulator-transport.md),
including explicit retirement when a terminal run publishes no output,
keeps private simulator inputs and results encrypted on this public repository.

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
