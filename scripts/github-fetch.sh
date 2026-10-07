#!/usr/bin/env bash
# Save one workflow run of bench.yml into results/github/: the jobs API answer
# (times of every job and step) and, from the run's log archive, each job's
# cache line and the BuildKit steps it found cached. Two API calls.
#
#   scripts/github-fetch.sh <run id> <cold|warm>
set -eu
run=${1:?run id}; cache=${2:?cold or warm}
repo=${REPO:-SylphxAI/build-bench}
dir=$(cd "$(dirname "$0")/.." && pwd)/results/github
mkdir -p "$dir"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
gh api "repos/$repo/actions/runs/$run/jobs?per_page=100" >"$tmp/jobs.json"
python3 - "$tmp/jobs.json" "$run" "$cache" >"$dir/$run.jobs.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(json.dumps({"run_id": int(sys.argv[2]), "cache": sys.argv[3], "jobs": d["jobs"]}, indent=1))
PY
gh api "repos/$repo/actions/runs/$run/logs" >"$tmp/logs.zip"
python3 - "$tmp/logs.zip" >"$dir/$run.cache.tsv" <<'PY'
import re, sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
jobs = {}
for name in z.namelist():
    # Archive layout: "<job name>.txt" per job and "<job name>/<n>_<step>.txt"
    # per step; read the whole-job files only.
    if "/" in name or not name.endswith(".txt"):
        continue
    job = re.sub(r"^\d+_", "", name[:-4])
    text = z.read(name).decode("utf-8", "replace")
    line = re.search(r"BENCH target=\S+ cache=\S+ (.*)", text)
    cached = len(re.findall(r"#\d+ CACHED", text))
    if line:
        hits = line.group(1).strip()
        if "target=image" in line.group(0):
            hits += f" buildkit-cached={cached}"
        jobs[job] = hits
for job, hits in sorted(jobs.items()):
    print(f"{job}\t{hits}")
PY
echo "saved $dir/$run.jobs.json and $dir/$run.cache.tsv ($(wc -l <"$dir/$run.cache.tsv") jobs with a cache line)"
