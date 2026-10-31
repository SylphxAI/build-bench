# Per-operation simulator transport

`simulator_transport.py` sends authenticated ciphertext to the public simulator
workflow and keeps each operation's key in its own process. Each operation has a
UUID, repository secret, orphan ciphertext branch, receiver tag and private work
directory. Unrelated operations can run concurrently without changing each
other's keys. The helper never writes the legacy `SIMULATOR_ENVELOPE_KEY` secret.

The workflow's optional `key_secret_name` selects
`${{ secrets[inputs.key_secret_name || 'SIMULATOR_ENVELOPE_KEY'] }}`. Omitting the
name preserves the legacy caller. An explicitly named missing secret resolves
empty and fails receiver validation; there is no fallback from a missing secret's
value to the legacy key. Existing consumers pinned to an older commit keep that
commit's contract.

## Start one custodian

Use Python 3, OpenSSL, Git with a configured author identity, and an authenticated
`gh` with the repository permissions needed for secrets, refs, workflow dispatch
and artifacts. Start from a checkout of the published receiver. Keep the process
in a persistent terminal with stdin open:

```sh
python3 scripts/simulator_transport.py \
  --repo OWNER/PUBLIC_REPO \
  --receiver-ref PUBLISHED_COMMIT \
  --script /private/run.sh --script-sha256 SCRIPT_SHA256 \
  --app /private/simulator-app.zip --app-sha256 APP_SHA256 \
  --work-root /private/operations --prefix simulator
```

The helper resolves the receiver ref once, compares the remote workflow and
receiver bytes at that commit to this checkout, and preserves the public-only
guard and literal free `macos-15` label. It generates 32 random bytes as 64
lowercase hexadecimal characters in memory. The key travels to `gh secret set`
through stdin and is never saved in a receipt, argument, file or public log.
The operation's receiver tag points to the resolved commit, including after a
new version is merged; it does not point to a fixed legacy commit.

Send one JSON object per line to the same process:

```json
{"action":"prepare"}
{"action":"publish"}
{"action":"refresh"}
{"action":"dispatch"}
{"action":"runs"}
{"action":"diagnostic","run_id":12345}
{"action":"decrypt","run_id":12345}
{"action":"cleanup","run_id":12345,"independent_receipt":"consumer receipt reference"}
```

The optional `diagnostic` read can run as soon as its small artifact is published,
while the same run is still packaging bulk results. Wait for the actual run to
become terminal before `decrypt`. `runs` reads every
page and reports only this operation's run. A queued run or timestamp alone
does not establish physical execution. Every accepted mutation has a journal
entry written before submission; preserve its first receipt and inspect an
unknown result instead of submitting it again.

`publish` pushes an orphan commit containing only `payload.enc` and creates the
owned receiver tag. `refresh` reads the full ciphertext anonymously by commit
and verifies its SHA256, refs and secret metadata before dispatch. The dispatch
contains only the selected secret's name and ciphertext URL/hash. There is no
global in-flight restriction for these isolated operations. A private repository,
changed source, changed secret metadata or mismatched owned object refuses the
next effect.

## Results and recovery

The workflow publishes `simulator-diagnostic-RUN` before it starts compressing
the complete output. That artifact contains only `diagnostic.enc`, authenticated
with the distinct `diagnostic` envelope purpose. It is bounded to 9 MiB, including
one unchanged PNG of at most 8 MiB, a 64 KiB driver-log tail, the operation's input
manifest and a small status record. The private capture driver updates
`capture-progress.json` with its current `step` and a relative `last_screenshot`
path. The receiver reads no bulk file to build this diagnostic. Missing or invalid
progress/screenshot evidence is explicitly absent; it does not become successful
capture evidence. No private output appears in the public workflow log.

The receiver then packages every original output into `results.enc`, including
the full logs and progress record. The workflow preserves the private driver's
failure status even when diagnostic upload and bulk packaging both succeed.
The legacy `receive` command retains its single-result behavior; the workflow
uses `receive-summary-first` followed by `package-results`.

Each distinct diagnostic and bulk artifact has its own once-only unseal journal
phase under the same original operation custodian. An unknown unseal is never
repeated. Delivering a diagnostic does not satisfy full-output consumption or
authorize cleanup. Cleanup requires both retained byte populations unchanged and
deletes only the exact owned diagnostic and bulk artifacts, secret and refs.

The receiver keeps script stdout/stderr in encrypted `driver.log`; the bulk public
artifact contains only `results.enc`. The helper verifies the completed first
attempt's head SHA, ref, event and artifact before authenticating the result. It
rejects duplicate, aliased, traversing and symlink ZIP members, compares the full
extracted file population and sizes, and requires the exact input manifest.
`decrypted-inventory.json` records ZIP member/file counts, sizes, file hashes,
archive/ciphertext hashes and run identity. `decrypted-file-hashes.json` records
the retained originals. A successful transport or an exit code is not acceptance
of the private workload's actual UI, rendering or other result.

All operation files default to a private directory (0700); extracted files use
0600. A caller that needs independent readers must grant its chosen group access
to selected outputs and inventories after verification. The generic helper sets
no host-specific group ID. Never print or publish decrypted logs or inventories
containing private information.

* `prepare` and dispatch submission cannot be replayed. For a lost dispatch
  response, `runs` can reconcile the sole exact ref/head/event/first-attempt run
  without another POST, retaining the original submission event.
* Artifact downloads are repeatable reads. Partial downloads use a temporary
  directory. The actual result unseal is journaled and happens once; extraction
  and inventory can resume from its verified archive after a later local failure.
  An unseal with an unknown outcome is refused rather than repeated.
* `publish` can continue with unsubmitted steps and read an accepted ref again
  after visibility lag. A submitted mutation with an unknown outcome is never
  automatically submitted again.
* Cleanup requires the independent consumer's receipt and unchanged retained
  originals. It checks ownership, deletes only this operation's secret, result
  artifact, branch and tag, and verifies their absence. If a DELETE was accepted
  but GET still returns the object, repeat `cleanup` to read absence and finish
  unsubmitted steps. Submitted DELETEs are never repeated. An unknown DELETE
  that stays present needs operator reconciliation; the helper retains the key.

On completed cleanup the helper releases its key reference and exits zero. Its
`custody-ending` event records the PID; the caller must independently verify that
the process exited/PID is absent and rehash retained originals before reporting
memory-key retirement. EOF before cleanup exits nonzero and ends custody; keep
stdin open until delivery and cleanup finish. The key is intentionally not
recoverable from the journal after a process loss. Deleting refs does not prove
physical erasure of ciphertext Git objects still addressable by SHA.

## GitHub contract and scale

Primary documentation read 2026-10-30 at 20:26:58 UTC:

* [Secrets reference](https://docs.github.com/en/actions/reference/security/secrets):
  repository/organization secrets are read when the workflow run is queued;
  environment secrets are read when the referencing job starts. There are 100
  repository secrets and 100 secrets per environment. Secret names are case
  insensitive and support letters, digits and underscores, with no leading digit
  or `GITHUB_` prefix. Names in the helper are unique and never reused.
* [Contexts](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts)
  and [expressions](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions):
  indexed context access is supported; both `inputs` and `secrets` are available
  in a step's environment. Missing properties resolve empty.
* [Using secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets):
  prefer environment/stdin to command-line secret values. Missing secrets resolve
  empty; masking is not a substitute for keeping private output encrypted.
* [Environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
  and [workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idenvironment):
  per-operation environments are supported for public repositories on Free and
  can use `deployment: false`. They add environment provisioning and later secret
  evaluation. A nonexistent referenced environment auto-creates without secrets;
  a missing environment secret can leave a same-named repository secret in scope.

At 10x operation volume, UUID ownership still separates work; runner capacity
and retained keys determine throughput. At 100 simultaneously queued/retained
operations, the repository's 100-secret aggregate quota (minus other secrets)
binds. Clean up delivered operations; reconsider supported per-operation
environments if the required population approaches that limit. Secret PUT/set is
an upsert, so unique names and single operation ownership remain essential.

Local tests simulate concurrent provider operations with real Git and envelope
crypto. They do not prove GitHub secret binding or macOS execution overlap.
Before enabling a parallel caller, use the merged receiver for two benign
operations with distinct keys, verify actual overlap and both own-key positives
and cross-key negatives, inspect public-log/artifact privacy, and prove one
operation's cleanup preserves the other. Leave active legacy consumers on their
existing path until their independent receipt and cleanup complete.

## Terminal runs that publish no output

A cancelled or failed provider job can finish without publishing either encrypted
artifact. The ordinary `cleanup` command intentionally still requires an
authenticated delivered result. `simulator_retirement.py` provides a separate,
explicit `terminal-without-output` disposition for this case, including legacy
custodians whose loaded transport has no such command. It never changes the
original receiver pin, fabricates a packet, runs an unseal, or rewrites the
original receipt to claim normal cleanup or workload acceptance.

Use the published controller on Linux with Python's `pidfd_open` and
`pidfd_send_signal` support. Retain the original process and its open stdin until
this path is qualified. Prepare a private request file with the original receipt,
exact run and complete job IDs, an independent authorization reference, preserved
provider diagnostics, and the original supervisor's source files. `preserve` adds
other immutable evidence or parallel-operation bytes that must remain unchanged.
All file paths in the request are absolute. Paths in the original supervisor's
argv are resolved against that supervisor's kernel-reported working directory.

```json
{
  "disposition": "terminal-without-output",
  "authorization": "independent no-output disposition receipt",
  "receipt": "/private/operations/simulator-UUID/receipt.json",
  "run_id": 12345,
  "job_ids": [67890],
  "code_files": ["/private/old-checkout/scripts/simulator_transport.py"],
  "evidence_files": ["/private/provider-diagnostics.json"],
  "preserve": ["/private/independent-evidence"]
}
```

If a launcher imported the original transport, include both the launcher and its
loaded source files in `code_files`; the actual argv program must be present.
The journal has one enforced location inside the original operation:
`OPERATION_WORK/.no-output-retirement/journal.json`. Only the controller-owned `journal.json` and empty `journal.lock` are excluded
from the preserved population; other bytes in that directory remain protected.
The state directory entry and each journal replacement are fsynced before effects.
It follows the original through a verified work-directory relocation. A different
journal path, a copied journal or concurrent invocation refuses; every call reloads
the existing operation journal before considering an effect.

```sh
python3 scripts/simulator_retirement.py prepare \
  --request /private/no-output-request.json --journal /private/operations/simulator-UUID/.no-output-retirement/journal.json
python3 scripts/simulator_retirement.py apply --journal /private/operations/simulator-UUID/.no-output-retirement/journal.json
```

`prepare` makes authenticated complete workflow-run, attempt, job, artifact,
repository-secret and Git-ref reads. The original must have one accepted dispatch,
a sole exact first-attempt terminal failure/cancellation, the specified complete
terminal jobs, and zero artifacts, including expired or diagnostic artifacts.
Unknown dispatch, another attempt, missing/mismatched jobs, or local output or an
uncertain transfer refuses. The controller records every original work file,
frozen input, supplied source and evidence byte, mode, owner and symlink target.
The original receipt, provider diagnostics and all preserved populations remain
unchanged by retirement.

`apply` repeats the authenticated terminal and empty-artifact checks, validates
owned secret/ref metadata and unrelated secret/ref populations before each
mutation, and rehashes all preserved bytes. It journals each of the three owned
DELETEs before submission. Accepted or unknown DELETEs are never replayed; a
later `apply` only reads their absence and starts unsubmitted steps. A changed
object, unrelated population or late artifact stops progress and retains the
custodian. Preserve any newly discovered output through normal authenticated
custody; do not edit the journal to suppress a refusal.

After fresh owned-resource absence and the final no-output checks, the controller
binds the original PID, birth ticks, argv digest, executable and working directory
to a stable Linux pidfd. It requires the recorded original supervisor's birth,
input/receipt ownership, idle single-threaded state, no children and the default
SIGTERM disposition. Only that pidfd receives one journaled SIGTERM. There is no
PID-based kill or arbitrary EOF fallback. A lost signal response is reconciled
without another signal. The receipt records a distinct process-address-space
retirement, with no key read or transfer; it does **not** claim the original
custodian automatically exited zero or released its key through ordinary cleanup.

The operator independently reads the final exact run/jobs/artifact census, three
owned-resource 404s, parallel resources, every preserved hash and the original
process's exit/PID absence. Keep both source scopes: the original loaded receiver
commit and the new published controller commit/hash. No-output disposition means
native results are unavailable. It proves neither native actions nor their
absence, UI quality, account continuity, or the cause of cancellation. Provider
reads establish the witnessed state at their recorded times; they do not promise
that no future provider mutation is possible.

This is an on-demand local lifecycle command, with no new runner, grant or spend.
At 10x/100x retained operations, each journal and pidfd still owns one original;
complete provider censuses and preserved-byte hashing determine cost. No daemon
or shared key store is added.

Primary contracts read 2026-10-31 at 09:09 UTC (references additionally read
09:18 UTC): [workflow runs/attempts and jobs](https://docs.github.com/en/rest/actions/workflow-jobs),
[workflow-run artifacts](https://docs.github.com/en/rest/actions/artifacts),
[list matching Git references](https://docs.github.com/en/rest/git/refs#list-matching-references),
[pidfd_open(2)](https://man7.org/linux/man-pages/man2/pidfd_open.2.html) and
[pidfd_send_signal(2)](https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html).
