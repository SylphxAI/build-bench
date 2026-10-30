import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import simulator_envelope as envelope


class EnvelopeTests(unittest.TestCase):
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
