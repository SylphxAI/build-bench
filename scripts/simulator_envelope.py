#!/usr/bin/env python3
"""One-use authenticated transport for private simulator inputs and results."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

MAGIC = b"SIMENV1\x00"


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


def receive(url, digest, directory):
    key = os.environ["SIMULATOR_ENVELOPE_KEY"].encode()
    if len(key) != 64 or any(c not in b"0123456789abcdef" for c in key):
        raise ValueError("invalid operation key")
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
        with zipfile.ZipFile(root / "results.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(output.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(output))
        seal(key, "results", root / "results.zip", root / "results.enc")
    print("Encrypted simulator results prepared.")
    return code


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "receive":
        raise SystemExit("usage: simulator_envelope.py receive HTTPS_URL SHA256 DIRECTORY")
    try:
        raise SystemExit(receive(*sys.argv[2:]))
    except Exception:
        raise SystemExit("Envelope receiver failed; no plaintext output was published.")
