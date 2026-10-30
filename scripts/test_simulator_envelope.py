import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import simulator_envelope as envelope


class EnvelopeTests(unittest.TestCase):
    def test_receiver_keeps_driver_output_private_and_removes_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'app').write_bytes(b'synthetic input')
            (root / 'script').write_text('printf "private-driver-marker\\n"\n'
                                        'printf "%s/%s/%s" "${SIMULATOR_ENVELOPE_KEY-unset}" '
                                        '"${GITHUB_TOKEN-unset}" "${GH_TOKEN-unset}" > "$2/environment.txt"\n'
                                        'exit 7\n')
            envelope.package(root / 'app', root / 'script', root / 'payload.zip')
            key = b'3' * 64
            envelope.seal(key, 'payload', root / 'payload.zip', root / 'payload.enc')
            data = (root / 'payload.enc').read_bytes()
            public = io.StringIO()
            with patch.dict(os.environ, {'SIMULATOR_ENVELOPE_KEY': key.decode(), 'GITHUB_TOKEN': 'synthetic-token', 'GH_TOKEN': 'synthetic-token'}), \
                 patch.object(envelope.urllib.request, 'urlopen', return_value=io.BytesIO(data)), contextlib.redirect_stdout(public):
                code = envelope.receive('https://example.invalid/input', hashlib.sha256(data).hexdigest(), root / 'received')
            self.assertEqual(code, 7)
            self.assertNotIn('private-driver-marker', public.getvalue())
            self.assertNotIn(key.decode(), public.getvalue())
            envelope.unseal(key, 'results', root / 'received/results.enc', root / 'results.zip')
            with zipfile.ZipFile(root / 'results.zip') as archive:
                self.assertIn(b'private-driver-marker', archive.read('driver.log'))
                self.assertEqual(archive.read('environment.txt'), b'unset/unset/unset')

    def test_empty_selected_key_fails_before_any_network_or_payload_execution(self):
        with patch.dict(os.environ, {'SIMULATOR_ENVELOPE_KEY': ''}), patch.object(envelope.urllib.request, 'urlopen') as fetch:
            with self.assertRaises(ValueError):
                envelope.receive('https://example.invalid/input', '0' * 64, 'unused')
            fetch.assert_not_called()

    def test_round_trip_and_wrong_purpose(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'plain').write_bytes(b'private test bytes')
            key = b'1' * 64
            envelope.seal(key, 'payload', root / 'plain', root / 'sealed')
            envelope.unseal(key, 'payload', root / 'sealed', root / 'restored')
            self.assertEqual((root / 'restored').read_bytes(), b'private test bytes')
            with self.assertRaises(ValueError):
                envelope.unseal(key, 'results', root / 'sealed', root / 'rejected')

    def test_tampering_is_rejected_before_decryption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'plain').write_bytes(b'private test bytes')
            key = b'2' * 64
            envelope.seal(key, 'payload', root / 'plain', root / 'sealed')
            data = bytearray((root / 'sealed').read_bytes())
            data[-1] ^= 1
            (root / 'sealed').write_bytes(data)
            with patch.object(envelope, 'crypt') as crypt:
                with self.assertRaises(ValueError):
                    envelope.unseal(key, 'payload', root / 'sealed', root / 'rejected')
                crypt.assert_not_called()

    def test_private_manifest_hashes_and_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'app').write_bytes(b'test archive')
            (root / 'script').write_bytes(b'exit 0\n')
            expected = envelope.package(root / 'app', root / 'script', root / 'package')
            self.assertEqual(envelope.unpack(root / 'package', root / 'unpacked'), expected)
            with zipfile.ZipFile(root / 'bad', 'w') as archive:
                archive.writestr('simulator-app.zip', b'wrong bytes')
                archive.writestr('run.sh', b'exit 0\n')
                archive.writestr('manifest.json', json.dumps(expected))
            with self.assertRaises(ValueError):
                envelope.unpack(root / 'bad', root / 'rejected')
            with zipfile.ZipFile(root / 'traversal', 'w') as archive:
                archive.writestr('../outside', b'rejected')
            with self.assertRaises(ValueError):
                envelope.unpack(root / 'traversal', root / 'rejected')


if __name__ == '__main__':
    unittest.main()
