#!/usr/bin/env python3
"""One-use authenticated transport for private simulator inputs and results."""
import hashlib
import hmac
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import signal
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

MAGIC = b"SIMENV1\x00"
SUMMARY_IMAGE_LIMIT = 8 * 1024 * 1024
SUMMARY_TEXT_LIMIT = 64 * 1024


def crypt(key, source, target, decrypt=False):
    openssl = os.environ.get("OPENSSL_BIN", "openssl")
    command = [openssl, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-pass", "stdin", "-in", str(source), "-out", str(target)]
    help_text = subprocess.run([openssl, "enc", "-help"], capture_output=True, text=True).stderr
    if "-saltlen" in help_text:
        command += ["-saltlen", "8"]
    if decrypt:
        command.append("-d")
    subprocess.run(command, input=key + b"\n", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)


def seal(key, purpose, source, target):
    header = MAGIC + purpose.encode().ljust(16, b"\x00")
    with tempfile.TemporaryDirectory() as temporary:
        encrypted = Path(temporary) / "encrypted"
        crypt(key, source, encrypted)
        ciphertext = encrypted.read_bytes()
    auth_key = hmac.digest(key, b"simulator-envelope-auth-v1", "sha256")
    tag = hmac.digest(auth_key, header + ciphertext, "sha256")
    Path(target).write_bytes(header + tag + ciphertext)


def unseal(key, purpose, source, target):
    envelope = Path(source).read_bytes()
    header = MAGIC + purpose.encode().ljust(16, b"\x00")
    if len(envelope) < 72 or envelope[:24] != header:
        raise ValueError("invalid envelope header")
    auth_key = hmac.digest(key, b"simulator-envelope-auth-v1", "sha256")
    tag = hmac.digest(auth_key, header + envelope[56:], "sha256")
    if not hmac.compare_digest(tag, envelope[24:56]):
        raise ValueError("envelope authentication failed")
    with tempfile.TemporaryDirectory() as temporary:
        ciphertext = Path(temporary) / "ciphertext"
        ciphertext.write_bytes(envelope[56:])
        crypt(key, ciphertext, target, decrypt=True)


def package(app, script, target):
    manifest = {"schema": 1, "files": {"simulator-app.zip": hashlib.sha256(Path(app).read_bytes()).hexdigest(), "run.sh": hashlib.sha256(Path(script).read_bytes()).hexdigest()}}
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.write(app, "simulator-app.zip")
        archive.write(script, "run.sh")
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
    return manifest


def unpack(source, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        if sorted(archive.namelist()) != ["manifest.json", "run.sh", "simulator-app.zip"]:
            raise ValueError("unexpected payload members")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema") != 1 or set(manifest["files"]) != {"run.sh", "simulator-app.zip"}:
            raise ValueError("invalid manifest")
        for name, digest in manifest["files"].items():
            data = archive.read(name)
            if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
                raise ValueError("payload hash mismatch")
            (directory / name).write_bytes(data)
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def operation_key():
    key = os.environ["SIMULATOR_ENVELOPE_KEY"].encode()
    if len(key) != 64 or any(c not in b"0123456789abcdef" for c in key):
        raise ValueError("invalid operation key")
    return key


def private_output_file(output, name):
    if not isinstance(name, str):
        return None
    relative = PurePosixPath(name)
    path = output / name
    if (not relative.is_absolute() and ".." not in relative.parts and "\\" not in name
            and str(relative) == name and path.is_file() and not path.is_symlink()
            and path.resolve().is_relative_to(output.resolve())):
        return path
    return None


def diagnostic_summary(key, root, code):
    """Seal bounded diagnostics without reading or compressing the bulk output."""
    output = root / "private-output"
    diagnostic = {"schema": "simulator-diagnostic-v1", "exit_code": code,
                  "failing_step": "driver", "screenshot": None}
    progress = output / "capture-progress.json"
    if progress.is_file() and not progress.is_symlink() and progress.stat().st_size <= SUMMARY_TEXT_LIMIT:
        try:
            record = json.loads(progress.read_text())
            if isinstance(record, dict):
                diagnostic["capture_progress"] = record
                diagnostic["failing_step"] = record.get("step", "driver")
        except (ValueError, UnicodeError):
            diagnostic["progress_error"] = "invalid capture progress"
    manifest = output / "payload-manifest.json"
    if manifest.is_file():
        diagnostic["payload_manifest"] = json.loads(manifest.read_text())
    log_name = diagnostic.get("capture_progress", {}).get("last_log", "driver.log")
    log = private_output_file(output, log_name) or private_output_file(output, "driver.log")
    tail = b""
    if log:
        diagnostic["log_source"] = str(log.relative_to(output))
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - SUMMARY_TEXT_LIMIT))
            tail = stream.read(SUMMARY_TEXT_LIMIT)
    screenshot = diagnostic.get("capture_progress", {}).get("last_screenshot")
    image = None
    path = private_output_file(output, screenshot)
    if path and path.stat().st_size <= SUMMARY_IMAGE_LIMIT:
        with path.open("rb") as stream:
            image = stream.read(SUMMARY_IMAGE_LIMIT + 1)
        if len(image) > SUMMARY_IMAGE_LIMIT or not image.startswith(b"\x89PNG\r\n\x1a\n"):
            image = None
        else:
            diagnostic["screenshot"] = {"source": screenshot, "bytes": len(image),
                "sha256": hashlib.sha256(image).hexdigest()}
    with zipfile.ZipFile(root / "diagnostic.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("summary.json", json.dumps(diagnostic, sort_keys=True))
        archive.writestr("driver-tail.log", tail)
        if image is not None:
            archive.writestr("last-screenshot.png", image)
    seal(key, "diagnostic", root / "diagnostic.zip", root / "diagnostic.enc")
    if (root / "diagnostic.enc").stat().st_size > 9 * 1024 * 1024:
        raise ValueError("diagnostic envelope exceeds its bound")


def package_results(key, root):
    root = Path(root).resolve()
    output = root / "private-output"
    with zipfile.ZipFile(root / "results.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output))
    seal(key, "results", root / "results.zip", root / "results.enc")


def receive(url, digest, directory, *, defer_bulk=False):
    key = operation_key()
    if not url.startswith("https://"):
        raise ValueError("HTTPS payload required")
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    output = root / "private-output"
    output.mkdir()
    code = 1
    try:
        with urllib.request.urlopen(url, timeout=90) as response:
            encrypted = response.read()
        if not hmac.compare_digest(hashlib.sha256(encrypted).hexdigest(), digest):
            raise ValueError("ciphertext hash mismatch")
        (root / "payload.enc").write_bytes(encrypted)
        unseal(key, "payload", root / "payload.enc", root / "payload.zip")
        manifest = unpack(root / "payload.zip", root / "payload")
        (output / "payload-manifest.json").write_text(json.dumps(manifest, indent=2))
        environment = {k: v for k, v in os.environ.items() if k not in {"SIMULATOR_ENVELOPE_KEY", "GITHUB_TOKEN", "GH_TOKEN"}}
        with (output / "driver.log").open("wb") as log:
            try:
                process = subprocess.Popen(["bash", str(root / "payload/run.sh"), str(root / "payload/simulator-app.zip"), str(output)], cwd=root / "payload", env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                code = process.wait(timeout=2400)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                log.write(b"\nPrivate script exceeded the forty-minute bound.\n")
                code = 124
    except Exception as error:
        (output / "receiver-error.txt").write_text(type(error).__name__ + ": " + str(error))
    finally:
        (output / "exit-code.json").write_text(json.dumps({"exit_code": code}))
        if defer_bulk:
            diagnostic_summary(key, root, code)
        else:
            package_results(key, root)
    print("Encrypted simulator results prepared.")
    return code


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "package-results":
            package_results(operation_key(), Path(sys.argv[2]))
        elif len(sys.argv) == 5 and sys.argv[1] in {"receive", "receive-summary-first"}:
            raise SystemExit(receive(*sys.argv[2:], defer_bulk=sys.argv[1] == "receive-summary-first"))
        else:
            raise ValueError("invalid receiver command")
    except Exception:
        raise SystemExit("Envelope receiver failed; no plaintext output was published.")
