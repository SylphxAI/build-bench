import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse
import zipfile

import simulator_envelope as envelope
import simulator_transport as transport

SOURCE = Path(__file__).resolve().parents[1]
SHA = "a" * 40
REPO = "example/public"


class Provider(transport.GitHub):
    """A shared provider simulator; local git/crypto remain real."""
    def __init__(self):
        self.lock = threading.RLock()
        self.secrets = {"SIMULATOR_ENVELOPE_KEY": b"0" * 64}
        self.metadata = {"SIMULATOR_ENVELOPE_KEY": {"name": "SIMULATOR_ENVELOPE_KEY"}}
        self.refs, self.blobs, self.artifacts, self.ciphertexts = {}, {}, {}, {}
        self.run_records = {1: {"id": 1, "head_branch": "legacy-active", "head_sha": "b" * 40,
                                "status": "in_progress", "run_attempt": 1}}
        self.mutations = []
        self.pending = {}
        self.lag = 0
        self.fail_dispatch = False
        self.before_mutation = lambda name: None
        self.private = False

    def get(self, endpoint):
        with self.lock:
            path, _, query = endpoint.partition("?")
            suffix = path.removeprefix(f"repos/{REPO}").lstrip("/")
            if path in self.pending:
                self.pending[path] -= 1
                if self.pending[path] <= 0:
                    self.pending.pop(path)
                    self.remove(path)
            if not suffix:
                return {"private": self.private}
            if suffix.startswith("commits/"):
                return {"sha": SHA}
            if suffix.startswith("contents/"):
                return {"content": base64.b64encode((SOURCE / suffix[9:]).read_bytes()).decode()}
            if suffix.startswith("actions/secrets/"):
                value = self.metadata.get(suffix.split("/")[-1])
            elif suffix.startswith("git/ref/"):
                value = self.refs.get(suffix[8:])
            elif suffix == "actions/workflows/simulator-envelope.yml/runs":
                page = int(urllib.parse.parse_qs(query).get("page", [1])[0])
                items = list(self.run_records.values())
                return {"total_count": len(items), "workflow_runs": copy.deepcopy(items[(page-1)*100:page*100])}
            elif re.fullmatch(r"actions/runs/\d+/artifacts", suffix):
                run_id = int(suffix.split("/")[2])
                items = [value for value in self.artifacts.values() if value["workflow_run"]["id"] == run_id]
                return {"total_count": len(items), "artifacts": copy.deepcopy(items)}
            elif re.fullmatch(r"actions/runs/\d+", suffix):
                value = self.run_records.get(int(suffix.split("/")[-1]))
            elif suffix.startswith("actions/artifacts/"):
                value = self.artifacts.get(int(suffix.split("/")[-1]))
            else:
                raise AssertionError("Unexpected read: " + endpoint)
            if value is None:
                raise transport.ApiError(404)
            return copy.deepcopy(value)

    def set_secret(self, repo, name, key):
        with self.lock:
            self.before_mutation("create-secret")
            self.mutations.append(("secret", name))
            self.secrets[name] = key
            self.metadata[name] = {"name": name, "created_at": "2026-10-30T00:00:00Z", "updated_at": "2026-10-30T00:00:00Z"}
            return {"cli_exit": 0}

    def push(self, repo, branch, directory):
        with self.lock:
            self.before_mutation("create-branch")
            self.mutations.append(("push", branch))
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=directory, text=True).strip()
            self.refs["heads/" + branch] = {"object": {"sha": commit}}
            self.blobs[commit] = (directory / "payload.enc").read_bytes()
            return {"cli_exit": 0}

    def fetch(self, url):
        return self.blobs[url.split("/")[-2]]

    def remove(self, endpoint):
        suffix = endpoint.removeprefix(f"repos/{REPO}/")
        if suffix.startswith("actions/secrets/"):
            name = suffix.split("/")[-1]
            self.secrets.pop(name, None)
            self.metadata.pop(name, None)
        elif suffix.startswith("actions/artifacts/"):
            self.artifacts.pop(int(suffix.split("/")[-1]), None)
        elif suffix.startswith("git/ref/"):
            self.refs.pop(suffix[8:], None)

    def api(self, method, endpoint, body=None):
        with self.lock:
            suffix = endpoint.removeprefix(f"repos/{REPO}/")
            if method == "DELETE":
                self.before_mutation("delete-" + ("secret" if "secrets/" in suffix else "artifact" if "artifacts/" in suffix
                                                 else "tag" if "/tags/" in suffix else "branch"))
                self.mutations.append((method, endpoint))
                read_path = endpoint.replace("git/refs/", "git/ref/")
                if self.lag:
                    self.pending[read_path] = self.lag
                else:
                    self.remove(read_path)
                return {"status": 204, "body": None}
            self.mutations.append((method, endpoint))
            if suffix == "git/refs":
                self.before_mutation("create-tag")
                self.refs[body["ref"][5:]] = {"object": {"sha": body["sha"]}}
                return {"status": 201, "body": None}
            if suffix.endswith("/dispatches"):
                self.before_mutation("dispatch")
                op = body["ref"]
                key = self.secrets[body["inputs"]["key_secret_name"]]
                run_id = max(self.run_records) + 1
                self.run_records[run_id] = {"id": run_id, "head_branch": op, "head_sha": self.refs["tags/"+op]["object"]["sha"],
                                            "event": "workflow_dispatch", "run_attempt": 1, "status": "completed",
                                            "conclusion": "failure", "html_url": "https://github.com/example/public/actions/runs/"+str(run_id)}
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / "payload.enc").write_bytes(self.fetch(body["inputs"]["payload_url"]))
                    envelope.unseal(key, "payload", root / "payload.enc", root / "payload.zip")
                    with zipfile.ZipFile(root / "payload.zip") as archive:
                        manifest = archive.read("manifest.json")
                    with zipfile.ZipFile(root / "results.zip", "w") as archive:
                        archive.writestr("payload-manifest.json", manifest)
                        archive.writestr("driver.log", "private output for " + op)
                        archive.writestr("nested/", b"")
                        archive.writestr("nested/result.bin", b"verified bytes")
                    envelope.seal(key, "results", root / "results.zip", root / "results.enc")
                    self.ciphertexts[run_id] = (root / "results.enc").read_bytes()
                self.artifacts[run_id] = {"id": run_id, "name": f"simulator-results-{run_id}", "expired": False,
                                          "workflow_run": {"id": run_id, "head_sha": SHA, "head_branch": op}}
                if self.fail_dispatch:
                    raise transport.TransportError("Response lost after accepted dispatch.")
                return {"status": 204, "body": None}
            raise AssertionError("Unexpected mutation: " + endpoint)

    def download(self, repo, run_id, name, directory):
        (directory / "results.enc").write_bytes(self.ciphertexts[int(run_id)])


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app, self.script = self.root / "app.zip", self.root / "run.sh"
        self.app.write_bytes(b"synthetic app")
        self.script.write_bytes(b"exit 7\n")
        self.provider = Provider()
        identity = patch.dict(os.environ, {"GIT_AUTHOR_NAME": "Kyle Tse", "GIT_AUTHOR_EMAIL": "hi@kylet.se",
                                          "GIT_COMMITTER_NAME": "Kyle Tse", "GIT_COMMITTER_EMAIL": "hi@kylet.se"})
        identity.start()
        self.addCleanup(identity.stop)
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def operation(self):
        return transport.Transport(repo=REPO, receiver_ref=SHA, script=self.script,
                                   script_sha256=transport.sha256(self.script), app=self.app,
                                   app_sha256=transport.sha256(self.app), work_root=self.root / "ops",
                                   github=self.provider, fetcher=self.provider.fetch)

    def ready(self, op):
        op.prepare()
        op.publish()
        op.refresh()

    def completed(self, op):
        self.ready(op)
        op.dispatch()
        run_id = op.runs()[0]["id"]
        op.decrypt(run_id)
        return run_id

    def test_concurrent_operations_keep_distinct_keys_and_cleanup_ownership(self):
        first, second = self.operation(), self.operation()
        original_keys = [first.key, second.key]
        self.assertEqual(len({first.operation, second.operation}), 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(self.completed, [first, second]))
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(len(first.key), 64)
        self.assertEqual(len(bytes.fromhex(first.key.decode())), 32)
        self.assertEqual(self.provider.secrets["SIMULATOR_ENVELOPE_KEY"], b"0" * 64)
        for own, other, run_id in [(first, second, runs[0]), (second, first, runs[1])]:
            inventory = own.receipt["inventory"]
            self.assertEqual((inventory["member_count"], inventory["file_count"]), (4, 3))
            self.assertEqual(inventory["head_sha"], SHA)
            self.assertEqual(inventory["head_branch"], own.operation)
            self.assertEqual(inventory["conclusion"], "failure")  # Transport authentication is not product acceptance.
            with self.assertRaises(ValueError):
                envelope.unseal(other.key, "results", own.work / "encrypted-results/results.enc", self.root / "wrong.zip")
            for member in inventory["members"]:
                if not member["directory"]:
                    path = own.work / "decrypted" / member["name"]
                    self.assertEqual((path.stat().st_size, transport.sha256(path)), (member["bytes"], member["sha256"]))
        second_metadata = copy.deepcopy(self.provider.metadata[second.secret])
        self.assertTrue(first.cleanup(runs[0], "consumer-receipt-first"))
        self.assertIsNone(first.key)
        self.assertEqual(self.provider.metadata[second.secret], second_metadata)
        self.assertIn("tags/" + second.operation, self.provider.refs)
        self.assertIn(runs[1], self.provider.artifacts)
        self.assertEqual(self.provider.run_records[1]["status"], "in_progress")
        self.assertTrue(second.cleanup(runs[1], "consumer-receipt-second"))
        self.assertEqual(set(self.provider.secrets), {"SIMULATOR_ENVELOPE_KEY"})
        for op, key in zip((first, second), original_keys):
            self.assertNotIn("0" * 64, (op.work / "receipt.json").read_text())
            self.assertNotIn(key.decode(), (op.work / "receipt.json").read_text())
            self.assertNotIn(key.decode(), self.output.getvalue())
            self.assertEqual(op.work.stat().st_mode & 0o777, 0o700)

    def test_prepare_dispatch_and_decrypt_cannot_replay_and_journal_precedes_effect(self):
        op = self.operation()
        def check_journal(name):
            receipt = json.loads((op.work / "receipt.json").read_text())
            self.assertEqual(receipt["effects"][name]["state"], "submitted")
        self.provider.before_mutation = check_journal
        run_id = self.completed(op)
        before = list(self.provider.mutations)
        for method in [op.prepare, op.dispatch, lambda: op.decrypt(run_id)]:
            with self.assertRaises(transport.TransportError):
                method()
        self.assertEqual(self.provider.mutations, before)
        self.assertTrue(op.cleanup(run_id, "receipt"))

    def test_lagging_delete_readback_never_repeats_accepted_deletes(self):
        op = self.operation()
        run_id = self.completed(op)
        self.provider.lag = 2
        self.assertFalse(op.cleanup(run_id, "receipt"))
        deletes = [item for item in self.provider.mutations if item[0] == "DELETE"]
        self.assertEqual(len(deletes), 4)
        self.assertIsNotNone(op.key)
        self.assertTrue(op.cleanup(run_id, "receipt"))
        self.assertEqual([item for item in self.provider.mutations if item[0] == "DELETE"], deletes)

    def test_unknown_dispatch_outcome_is_not_replayed(self):
        op = self.operation()
        self.ready(op)
        self.provider.fail_dispatch = True
        with self.assertRaises(transport.TransportError):
            op.dispatch()
        submission = [event for event in op.receipt["events"] if event["action"] == "mutation-submitting" and event["effect"] == "dispatch"]
        before = list(self.provider.mutations)
        with self.assertRaises(transport.TransportError):
            op.dispatch()
        self.assertEqual(self.provider.mutations, before)
        runs = op.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(op.receipt["effects"]["dispatch"]["state"], "accepted")
        self.assertEqual([event for event in op.receipt["events"] if event["action"] == "mutation-submitting" and event["effect"] == "dispatch"], submission)
        op.decrypt(runs[0]["id"])
        self.assertTrue(op.cleanup(runs[0]["id"], "receipt-after-reconciliation"))

    def test_download_timeout_and_post_unseal_failure_can_resume_without_second_unseal(self):
        op = self.operation()
        self.ready(op)
        op.dispatch()
        run_id = op.runs()[0]["id"]
        with patch.object(self.provider, "download", side_effect=transport.TransportError("Read timeout.")):
            with self.assertRaises(transport.TransportError):
                op.decrypt(run_id)
        self.assertNotIn("unseal", op.receipt["phases"])
        with patch.object(envelope, "unseal", wraps=envelope.unseal) as unseal:
            with patch.object(zipfile.ZipFile, "extractall", side_effect=OSError("Synthetic disk failure.")):
                with self.assertRaises(OSError):
                    op.decrypt(run_id)
            self.assertEqual(op.receipt["phases"]["unseal"], "complete")
            op.decrypt(run_id)
            self.assertEqual(unseal.call_count, 1)
        self.assertTrue(op.cleanup(run_id, "receipt-after-read-recovery"))

    def test_missing_selected_secret_does_not_use_shared_secret(self):
        op = self.operation()
        self.ready(op)
        self.provider.metadata.pop(op.secret)
        self.provider.secrets.pop(op.secret)
        before = list(self.provider.mutations)
        with self.assertRaises(transport.ApiError):
            op.dispatch()
        self.assertEqual(self.provider.mutations, before)
        self.assertIn("SIMULATOR_ENVELOPE_KEY", self.provider.secrets)
        workflow = (SOURCE / transport.WORKFLOW).read_text()
        expression = re.search(r"SIMULATOR_ENVELOPE_KEY: \$\{\{ (.+) \}\}", workflow)[1]
        self.assertEqual(expression, "secrets[inputs.key_secret_name || 'SIMULATOR_ENVELOPE_KEY']")

    def test_private_repo_and_changed_pinned_receiver_refuse_secret_mutation(self):
        op = self.operation()
        self.provider.private = True
        with self.assertRaises(transport.TransportError):
            op.prepare()
        self.assertEqual(self.provider.mutations, [])
        self.provider.private = False
        op = self.operation()
        original = self.provider.get
        def changed(endpoint):
            if "/contents/" in endpoint:
                return {"content": base64.b64encode(b"wrong receiver").decode()}
            return original(endpoint)
        with patch.object(self.provider, "get", side_effect=changed):
            with self.assertRaises(transport.TransportError):
                op.prepare()
        self.assertEqual(self.provider.mutations, [])

    def test_cleanup_requires_receipt_preserved_hashes_and_owned_objects(self):
        op = self.operation()
        run_id = self.completed(op)
        before = list(self.provider.mutations)
        with self.assertRaises(transport.TransportError):
            op.cleanup(run_id, "")
        path = op.work / "decrypted/driver.log"
        original = path.read_bytes()
        path.write_bytes(b"changed")
        with self.assertRaises(transport.TransportError):
            op.cleanup(run_id, "receipt")
        self.assertEqual(self.provider.mutations, before)
        path.write_bytes(original)
        self.provider.metadata[op.secret]["updated_at"] = "changed"
        with self.assertRaises(transport.TransportError):
            op.cleanup(run_id, "receipt")
        self.assertEqual(self.provider.mutations, before)

    def test_wrong_run_identity_and_duplicate_zip_members_are_rejected(self):
        op = self.operation()
        self.ready(op)
        op.dispatch()
        run_id = op.runs()[0]["id"]
        self.provider.run_records[run_id]["head_sha"] = "f" * 40
        with self.assertRaises(transport.TransportError):
            op.decrypt(run_id)
        self.provider.run_records[run_id]["head_sha"] = SHA
        with zipfile.ZipFile(self.root / "duplicate.zip", "w") as archive:
            archive.writestr("a", b"first")
            with self.assertWarns(UserWarning):
                archive.writestr("a", b"second")
        envelope.seal(op.key, "results", self.root / "duplicate.zip", self.root / "duplicate.enc")
        self.provider.ciphertexts[run_id] = (self.root / "duplicate.enc").read_bytes()
        with self.assertRaises(transport.TransportError):
            op.decrypt(run_id)


class AdapterTests(unittest.TestCase):
    def test_http_statuses_multiple_headers_and_error_redaction(self):
        cases = [(b'HTTP/2.0 200 OK\r\nX-Test: ok\r\n\r\n{"value":1}', 0, 200),
                 (b'HTTP/2.0 302 Found\r\n\r\nHTTP/2.0 200 OK\r\n\r\n{"value":1}', 0, 200),
                 (b'HTTP/2.0 204 No Content\r\n\r\n', 0, 204)]
        for output, exit_code, status in cases:
            with self.subTest(status=status), patch.object(transport, "command", return_value=subprocess.CompletedProcess([], exit_code, output)):
                self.assertEqual(transport.GitHub().api("GET", "example")["status"], status)
        private = b"private-key-must-not-appear"
        with patch.object(transport, "command", return_value=subprocess.CompletedProcess([], 1, b'HTTP/2.0 404 Not Found\n\n'+private, private)):
            with self.assertRaises(transport.ApiError) as failure:
                transport.GitHub().api("GET", "example")
            self.assertNotIn(private.decode(), str(failure.exception))
        with patch.object(transport, "command", return_value=subprocess.CompletedProcess([], 1, private, private)):
            with self.assertRaises(transport.TransportError) as failure:
                transport.GitHub().set_secret(REPO, "OP", private)
            self.assertNotIn(private.decode(), str(failure.exception))

    def test_full_pagination_and_changed_census(self):
        api = transport.GitHub()
        responses = [{"total_count": 205, "items": list(range(i, min(i+100,205)))} for i in range(0,205,100)]
        with patch.object(api, "get", side_effect=responses) as get:
            self.assertEqual(api.pages("example", "items"), list(range(205)))
            self.assertTrue(get.call_args_list[-1].args[0].endswith("page=3"))
        with patch.object(api, "get", return_value={"total_count": 2, "items": [1]}):
            with self.assertRaises(transport.TransportError):
                api.pages("example", "items")


if __name__ == "__main__":
    unittest.main()
