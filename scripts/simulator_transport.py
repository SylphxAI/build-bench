#!/usr/bin/env python3
"""Memory-only key custody and operation-scoped GitHub simulator transport."""
import argparse
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile

import simulator_envelope as envelope

WORKFLOW = ".github/workflows/simulator-envelope.yml"
RECEIVER = "scripts/simulator_envelope.py"


class TransportError(Exception):
    """A failure whose message is safe to include in the local receipt."""


class ApiError(TransportError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub request failed (HTTP {status}).")


def require(condition, message):
    if not condition:
        raise TransportError(message)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def command(arguments, *, cwd=None, data=None):
    # Never include stdout/stderr, argv, or input in an exception: a failed
    # credential command may echo its stdin in diagnostics.
    try:
        result = subprocess.run(arguments, cwd=cwd, input=data, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        raise TransportError("External command did not complete; inspect the journal.") from None
    return result


def checked(arguments, *, cwd=None, data=None):
    result = command(arguments, cwd=cwd, data=data)
    require(result.returncode == 0, "External command failed; response withheld.")
    return result.stdout


class GitHub:
    def api(self, method, endpoint, body=None):
        args = ["gh", "api", "--include", "--method", method, endpoint]
        data = None
        if body is not None:
            args += ["--input", "-"]
            data = json.dumps(body).encode()
        result = command(args, data=data)
        payload = result.stdout.replace(b"\r\n", b"\n")
        match = None
        # gh can expose an intermediate response when following a redirect.
        # Consume header blocks only while the remaining bytes start with HTTP/.
        while payload.startswith(b"HTTP/"):
            headers, separator, payload = payload.partition(b"\n\n")
            require(bool(separator), "GitHub response headers incomplete.")
            match = re.match(rb"HTTP/\S+ (\d+)", headers)
        require(bool(match), "GitHub response status unavailable; inspect the journal.")
        status = int(match[1])
        if result.returncode or status >= 400:
            raise ApiError(status)
        return {"status": status, "body": json.loads(payload) if payload.strip() else None}

    def get(self, endpoint):
        return self.api("GET", endpoint)["body"]

    def optional(self, endpoint):
        try:
            return self.get(endpoint)
        except ApiError as error:
            if error.status != 404:
                raise
            return None

    def pages(self, endpoint, member=None):
        result = []
        page = 1
        while True:
            separator = "&" if "?" in endpoint else "?"
            obj = self.get(f"{endpoint}{separator}per_page=100&page={page}")
            require(not isinstance(obj, dict) or not obj.get("incomplete_results"), "Incomplete GitHub census.")
            items = obj[member] if member else obj
            require(isinstance(items, list), "Invalid GitHub census.")
            result.extend(items)
            if len(items) < 100:
                if isinstance(obj, dict) and "total_count" in obj:
                    require(len(result) == obj["total_count"], "GitHub census changed; read it again.")
                return result
            page += 1

    def set_secret(self, repo, name, key):
        checked(["gh", "secret", "set", name, "--repo", repo], data=key)
        return {"cli_exit": 0}

    def push(self, repo, branch, directory):
        checked(["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                 "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=directory)
        return {"cli_exit": 0}

    def download(self, repo, run_id, name, directory):
        checked(["gh", "run", "download", str(run_id), "--repo", repo, "--name", name, "--dir", str(directory)])


def fetch(url):
    with urllib.request.urlopen(url, timeout=90) as response:
        require(response.status == 200, "Anonymous ciphertext read failed.")
        return response.read()


class Transport:
    def __init__(self, *, repo, receiver_ref, script, script_sha256, app, app_sha256,
                 work_root, prefix="simulator", github=None, fetcher=fetch, source_root=None):
        require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)), "Invalid repository.")
        require(bool(re.fullmatch(r"[a-z][a-z0-9-]{0,39}", prefix)), "Invalid operation prefix.")
        require(bool(re.fullmatch(r"[0-9a-f]{64}", script_sha256)), "Invalid script SHA256.")
        require(bool(re.fullmatch(r"[0-9a-f]{64}", app_sha256)), "Invalid app SHA256.")
        self.repo, self.receiver_ref = repo, receiver_ref
        self.script, self.app = Path(script).resolve(), Path(app).resolve()
        self.script_sha, self.app_sha = script_sha256, app_sha256
        self.github, self.fetcher = github or GitHub(), fetcher
        self.source_root = Path(source_root) if source_root else Path(__file__).resolve().parents[1]
        self.operation = f"{prefix}-{uuid.uuid4().hex}"
        self.secret = "SIMULATOR_ENVELOPE_KEY_" + self.operation.replace("-", "_").upper()
        self.branch = "transport/" + self.operation
        self.work = Path(work_root).resolve() / self.operation
        self.work.mkdir(parents=True, mode=0o700)
        os.chmod(self.work, 0o700)
        self.key = secrets.token_hex(32).encode()
        self.receipt = {"operation": self.operation, "repo": repo, "secret_name": self.secret,
                        "pid": os.getpid(), "script_sha256": script_sha256, "app_sha256": app_sha256,
                        "effects": {}, "phases": {}, "events": []}
        self.event("supervisor-alive", work=str(self.work))

    def save(self):
        target = self.work / "receipt.json"
        with tempfile.NamedTemporaryFile(mode="w", dir=self.work, delete=False) as output:
            json.dump(self.receipt, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
            temporary = output.name
        os.replace(temporary, target)
        descriptor = os.open(self.work, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def event(self, action, **values):
        item = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "action": action, **values}
        self.receipt["events"].append(item)
        self.save()
        print(json.dumps(item), flush=True)

    def once(self, phase):
        require(phase not in self.receipt["phases"], f"{phase} already started; inspect instead of replaying.")
        self.receipt["phases"][phase] = "started"
        self.event(phase + "-started")

    def effect(self, name, endpoint, operation):
        require(name not in self.receipt["effects"], "Mutation already submitted; inspect instead of replaying.")
        self.receipt["effects"][name] = {"state": "submitted", "endpoint": endpoint}
        self.event("mutation-submitting", effect=name, endpoint=endpoint)
        response = operation()
        self.receipt["effects"][name].update(state="accepted", response=response)
        self.event("mutation-accepted", effect=name)

    def path(self, suffix):
        return f"repos/{self.repo}/{suffix}".rstrip("/")

    def public_source(self):
        metadata = self.github.get(self.path(""))
        require(metadata["private"] is False, "Only a public repository is permitted.")
        for path in (WORKFLOW, RECEIVER):
            obj = self.github.get(self.path(f"contents/{path}?ref={self.receipt['source_sha']}"))
            require(base64.b64decode(obj["content"]) == (self.source_root / path).read_bytes(),
                    "Pinned receiver bytes differ from this checkout.")
        workflow = (self.source_root / WORKFLOW).read_text()
        require("    if: ${{ !github.event.repository.private }}\n" in workflow and
                "    runs-on: macos-15\n" in workflow and
                "secrets[inputs.key_secret_name || 'SIMULATOR_ENVELOPE_KEY']" in workflow,
                "Public/free isolated receiver contract is missing.")

    def prepare(self):
        self.once("prepare")
        require(sha256(self.script) == self.script_sha and sha256(self.app) == self.app_sha,
                "Private input SHA256 mismatch.")
        ref = urllib.parse.quote(self.receiver_ref, safe="")
        self.receipt["source_sha"] = self.github.get(self.path("commits/" + ref))["sha"]
        require(bool(re.fullmatch(r"[0-9a-f]{40}", self.receipt["source_sha"])), "Invalid receiver commit.")
        self.public_source()
        secret_path = self.path("actions/secrets/" + self.secret)
        require(self.github.optional(secret_path) is None, "Operation secret already exists; refusing overwrite.")
        with tempfile.TemporaryDirectory(dir=self.work) as temporary:
            payload = Path(temporary) / "payload.zip"
            self.receipt["manifest"] = envelope.package(self.app, self.script, payload)
            envelope.seal(self.key, "payload", payload, self.work / "payload.enc")
        self.receipt["payload_sha256"] = sha256(self.work / "payload.enc")
        self.effect("create-secret", secret_path, lambda: self.github.set_secret(self.repo, self.secret, self.key))
        self.receipt["secret_metadata"] = self.github.get(secret_path)
        require(self.receipt["secret_metadata"]["name"] == self.secret, "Secret metadata mismatch.")
        self.receipt["phases"]["prepare"] = "complete"
        self.event("prepared", source_sha=self.receipt["source_sha"], ciphertext_sha256=self.receipt["payload_sha256"])

    def refs(self):
        return [("branch", self.path("git/ref/heads/" + self.branch), self.receipt["ciphertext_commit"]),
                ("tag", self.path("git/ref/tags/" + self.operation), self.receipt["source_sha"])]

    def publish(self):
        require(self.receipt["phases"].get("prepare") == "complete", "Prepare must complete first.")
        self.public_source()
        require(sha256(self.work / "payload.enc") == self.receipt["payload_sha256"], "Ciphertext changed.")
        directory = self.work / "ciphertext-git"
        if "ciphertext_commit" not in self.receipt:
            directory.mkdir(mode=0o700)
            os.link(self.work / "payload.enc", directory / "payload.enc")
            checked(["git", "init", "--initial-branch", self.branch], cwd=directory)
            for setting in ("user.name", "user.email"):
                configured = command(["git", "config", "--get", setting], cwd=self.source_root)
                if configured.returncode == 0:
                    checked(["git", "config", setting, configured.stdout.decode().strip()], cwd=directory)
            checked(["git", "remote", "add", "origin", f"https://github.com/{self.repo}.git"], cwd=directory)
            checked(["git", "add", "payload.enc"], cwd=directory)
            checked(["git", "commit", "-m", "Encrypted simulator validation input"], cwd=directory)
            commit = checked(["git", "rev-parse", "HEAD"], cwd=directory).decode().strip()
            require(checked(["git", "ls-tree", "--name-only", "-r", "HEAD"], cwd=directory).decode().splitlines()
                    == ["payload.enc"], "Publication contains unexpected files.")
            require(checked(["git", "rev-list", "--parents", "-n", "1", "HEAD"], cwd=directory).decode().split()
                    == [commit], "Publication must have no private history.")
            self.receipt["ciphertext_commit"] = commit
            self.save()
        for kind, endpoint, expected in self.refs():
            effect = "create-" + kind
            state = self.receipt["effects"].get(effect)
            if state is None:
                require(self.github.optional(endpoint) is None, "Operation ref already exists; refusing overwrite.")
                if kind == "branch":
                    self.effect(effect, endpoint, lambda: self.github.push(self.repo, self.branch, directory))
                else:
                    self.effect(effect, endpoint, lambda: self.github.api("POST", self.path("git/refs"),
                                {"ref": "refs/tags/" + self.operation, "sha": expected}))
            else:
                require(state["state"] == "accepted", "Publication outcome unknown; do not resubmit.")
            actual = self.github.optional(endpoint)
            require(actual is not None and actual["object"]["sha"] == expected,
                    "Published ref not visible at expected SHA; retry reads, never the mutation.")
        self.receipt["phases"]["publish"] = "complete"
        self.event("published", ciphertext_commit=self.receipt["ciphertext_commit"])

    def refresh(self):
        require(self.receipt["phases"].get("publish") == "complete", "Publish must complete first.")
        for _, endpoint, expected in self.refs():
            obj = self.github.get(endpoint)
            require(obj["object"]["sha"] == expected, "Operation ref changed.")
        url = f"https://raw.githubusercontent.com/{self.repo}/{self.receipt['ciphertext_commit']}/payload.enc"
        data = self.fetcher(url)
        require(hashlib.sha256(data).hexdigest() == self.receipt["payload_sha256"], "Anonymous ciphertext SHA256 mismatch.")
        metadata = self.github.get(self.path("actions/secrets/" + self.secret))
        require(metadata == self.receipt["secret_metadata"], "Operation secret metadata changed.")
        self.receipt["payload_url"] = url
        self.event("refreshed", ciphertext_sha256=self.receipt["payload_sha256"], ciphertext_bytes=len(data))

    def all_runs(self):
        return self.github.pages(self.path("actions/workflows/simulator-envelope.yml/runs"), "workflow_runs")

    def dispatch(self):
        require("payload_url" in self.receipt, "Refresh must complete first.")
        require(not any(run["head_branch"] == self.operation for run in self.all_runs()),
                "Operation already has a run; inspect instead of dispatching.")
        self.public_source()
        self.refresh()
        self.once("dispatch")
        endpoint = self.path("actions/workflows/simulator-envelope.yml/dispatches")
        body = {"ref": self.operation, "inputs": {"payload_url": self.receipt["payload_url"],
                "payload_sha256": self.receipt["payload_sha256"], "key_secret_name": self.secret}}
        self.effect("dispatch", endpoint, lambda: self.github.api("POST", endpoint, body))
        self.receipt["phases"]["dispatch"] = "complete"
        self.event("dispatched")

    def runs(self):
        matches = [run for run in self.all_runs() if run["head_branch"] == self.operation]
        require(len(matches) <= 1, "Multiple operation runs; inspect without resubmitting.")
        require(all(run["head_sha"] == self.receipt["source_sha"] and run["event"] == "workflow_dispatch" and
                    run["run_attempt"] == 1 for run in matches), "Operation run identity mismatch.")
        if matches and self.receipt["phases"].get("dispatch") == "started":
            require("dispatch" in self.receipt["effects"], "Run has no recorded dispatch submission.")
            self.receipt["effects"]["dispatch"].update(state="accepted", reconciled_run_id=matches[0]["id"])
            self.receipt["phases"]["dispatch"] = "complete"
            self.event("dispatch-reconciled", run_id=matches[0]["id"])
        self.event("runs", runs=[{key: run.get(key) for key in
                   ("id", "run_attempt", "head_branch", "head_sha", "status", "conclusion", "html_url")} for run in matches])
        return matches

    def owned_run(self, run_id):
        run = self.github.get(self.path(f"actions/runs/{int(run_id)}"))
        require(run["head_branch"] == self.operation and run["head_sha"] == self.receipt["source_sha"] and
                run["event"] == "workflow_dispatch" and run["run_attempt"] == 1 and run["status"] == "completed",
                "Run is not this operation's completed first attempt.")
        return run

    def hashes(self):
        root = self.work / "decrypted"
        return {str(path.relative_to(root)): sha256(path) for path in sorted(root.rglob("*")) if path.is_file()}

    def decrypt(self, run_id):
        if self.receipt["phases"].get("dispatch") == "started":
            self.runs()
        require(self.receipt["phases"].get("dispatch") == "complete", "Dispatch must complete first.")
        require(self.receipt["phases"].get("decrypt") != "complete", "Decrypt already completed; use retained results.")
        run = self.owned_run(run_id)
        artifacts = self.github.pages(self.path(f"actions/runs/{int(run_id)}/artifacts"), "artifacts")
        name = f"simulator-results-{int(run_id)}"
        matches = [artifact for artifact in artifacts if artifact["name"] == name and not artifact["expired"]]
        require(len(matches) == 1, "Expected one unexpired encrypted result artifact.")
        scope = matches[0].get("workflow_run", {})
        require(scope.get("id") == int(run_id) and scope.get("head_sha") == self.receipt["source_sha"] and
                scope.get("head_branch") == self.operation, "Result artifact run scope differs.")
        directory = self.work / "encrypted-results"
        directory.mkdir(mode=0o700, exist_ok=True)
        if not (directory / "results.enc").exists():
            # Downloads are read-only and may be repeated after a timeout. A
            # partial download is never mistaken for the completed ciphertext.
            with tempfile.TemporaryDirectory(dir=self.work) as temporary:
                attempt = Path(temporary)
                self.github.download(self.repo, run_id, name, attempt)
                require(sorted(str(path.relative_to(attempt)) for path in attempt.rglob("*")) == ["results.enc"],
                        "Result artifact contains unexpected files.")
                os.replace(attempt / "results.enc", directory / "results.enc")
        require(sorted(str(path.relative_to(directory)) for path in directory.rglob("*")) == ["results.enc"],
                "Result artifact contains unexpected files.")
        ciphertext_sha = sha256(directory / "results.enc")
        require(self.receipt.get("result_ciphertext_sha256", ciphertext_sha) == ciphertext_sha, "Downloaded ciphertext changed.")
        self.receipt["result_ciphertext_sha256"] = ciphertext_sha
        if "unseal" not in self.receipt["phases"]:
            self.once("unseal")
            envelope.unseal(self.key, "results", directory / "results.enc", self.work / "results.zip")
            self.receipt["phases"]["unseal"] = "complete"
            self.receipt["result_archive_sha256"] = sha256(self.work / "results.zip")
            self.event("unsealed")
        require(self.receipt["phases"].get("unseal") == "complete", "Unseal outcome unknown; never repeat it.")
        require(sha256(self.work / "results.zip") == self.receipt["result_archive_sha256"], "Retained result archive changed.")
        output = self.work / "decrypted"
        output.mkdir(mode=0o700, exist_ok=True)
        with zipfile.ZipFile(self.work / "results.zip") as archive:
            names = archive.namelist()
            require(len(names) == len(set(names)), "Duplicate result archive members.")
            members = []
            for item in archive.infolist():
                path = PurePosixPath(item.filename)
                require(str(path) == item.filename.rstrip("/") and str(path) != "." and
                        not path.is_absolute() and ".." not in path.parts and "\\" not in item.filename and
                        not stat.S_ISLNK(item.external_attr >> 16), "Unsafe result archive member.")
                members.append({"name": str(path), "directory": item.is_dir(), "bytes": item.file_size})
            require(len({member["name"] for member in members}) == len(members), "Aliased result archive members.")
            archive.extractall(output)
        actual = {str(path.relative_to(output)): path for path in output.rglob("*") if path.is_file()}
        files = [member for member in members if not member["directory"]]
        require(set(actual) == {member["name"] for member in files}, "Extracted ZIP population differs.")
        for member in files:
            path = actual[member["name"]]
            require(path.stat().st_size == member["bytes"], "Extracted ZIP member size differs.")
            member["sha256"] = sha256(path)
        manifest = json.loads((output / "payload-manifest.json").read_text())
        require(manifest == self.receipt["manifest"], "Result payload manifest differs from this operation.")
        for path in output.rglob("*"):
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        inventory = {"run_id": int(run_id), "head_sha": run["head_sha"], "head_branch": run["head_branch"],
                     "run_attempt": run["run_attempt"], "conclusion": run["conclusion"],
                     "archive_bytes": (self.work / "results.zip").stat().st_size,
                     "archive_sha256": sha256(self.work / "results.zip"),
                     "ciphertext_sha256": sha256(directory / "results.enc"),
                     "member_count": len(members), "file_count": len(files), "members": members,
                     "payload_manifest": manifest}
        self.receipt.update(run_id=int(run_id), artifact=matches[0], file_hashes=self.hashes(), inventory=inventory)
        (self.work / "decrypted-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
        (self.work / "decrypted-file-hashes.json").write_text(json.dumps(self.receipt["file_hashes"], indent=2) + "\n")
        self.receipt["phases"]["decrypt"] = "complete"
        self.event("decrypted", run_id=int(run_id), file_count=len(self.receipt["file_hashes"]), output=str(output))

    def delete_owned(self, name, read_endpoint, delete_endpoint, validate):
        obj = self.github.optional(read_endpoint)
        if obj is None:
            self.event("absence-readback", object=name)
            return True
        require(validate(obj), "Cleanup object changed ownership; refusing deletion.")
        if name not in self.receipt["effects"]:
            self.effect(name, delete_endpoint, lambda: self.github.api("DELETE", delete_endpoint))
        # Accepted or unknown DELETEs are never replayed, including when GET is
        # lagging. A later cleanup command only polls and starts unsubmitted steps.
        absent = self.github.optional(read_endpoint) is None
        self.event("cleanup-readback", object=name, absent=absent)
        return absent

    def cleanup(self, run_id, independent_receipt):
        require(isinstance(independent_receipt, str) and bool(independent_receipt.strip()), "Independent consumer receipt required.")
        require(self.receipt["phases"].get("decrypt") == "complete" and self.receipt["run_id"] == int(run_id),
                "Only this operation's authenticated delivered run can be cleaned.")
        require(self.hashes() == self.receipt["file_hashes"], "Retained originals changed; preserve before cleanup.")
        self.owned_run(run_id)
        previous = self.receipt.get("independent_receipt")
        require(previous in (None, independent_receipt), "Consumer receipt changed.")
        self.receipt["independent_receipt"] = independent_receipt
        self.save()
        secret_path = self.path("actions/secrets/" + self.secret)
        complete = [self.delete_owned("delete-secret", secret_path, secret_path,
                    lambda obj: obj == self.receipt["secret_metadata"])]
        artifact = self.receipt["artifact"]
        endpoint = self.path(f"actions/artifacts/{artifact['id']}")
        complete.append(self.delete_owned("delete-artifact", endpoint, endpoint,
                        lambda obj: obj["id"] == artifact["id"] and obj["name"] == artifact["name"] and
                        obj.get("workflow_run") == artifact["workflow_run"]))
        for kind, endpoint, expected in self.refs():
            complete.append(self.delete_owned("delete-" + kind, endpoint, endpoint.replace("git/ref/", "git/refs/"),
                            lambda obj: obj["object"]["sha"] == expected))
        if all(complete):
            require(self.hashes() == self.receipt["file_hashes"], "Retained originals changed during cleanup.")
            self.receipt["phases"]["cleanup"] = "complete"
            self.key = None
            self.event("cleanup-complete", pid=os.getpid(), originals_sha256_file=str(self.work / "decrypted-file-hashes.json"))
            return True
        self.event("cleanup-pending", reason="Read absence again; submitted deletions must not be repeated.")
        return False

    def handle(self, request):
        action = request["action"]
        if action in ("prepare", "publish", "refresh", "dispatch", "runs"):
            getattr(self, action)()
        elif action == "decrypt":
            self.decrypt(request["run_id"])
        elif action == "cleanup":
            return self.cleanup(request["run_id"], request.get("independent_receipt"))
        else:
            raise TransportError("Unknown command.")
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "receiver-ref", "script", "script-sha256", "app", "app-sha256", "work-root"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--prefix", default="simulator")
    options = vars(parser.parse_args())
    os.umask(0o077)
    transport = Transport(**options)
    complete = False
    try:
        for line in sys.stdin:
            try:
                if transport.handle(json.loads(line)):
                    complete = True
                    break
            except Exception as error:
                # Unexpected parser/library errors can include private values.
                message = str(error) if isinstance(error, TransportError) else "Command failed; private response withheld."
                transport.event("command-failed-key-retained", error=message)
    finally:
        transport.key = None
        transport.event("custody-ending", cleanup_complete=complete, pid=os.getpid())
    return 0 if complete else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit("Transport stopped; private response withheld.")
