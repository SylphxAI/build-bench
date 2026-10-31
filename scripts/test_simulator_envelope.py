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
    def test_small_diagnostic_is_available_before_any_bulk_packaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'private-output'
            output.mkdir()
            manifest = {'schema': 1, 'files': {'run.sh': 'a' * 64, 'simulator-app.zip': 'b' * 64}}
            (output / 'payload-manifest.json').write_text(json.dumps(manifest))
            (output / 'capture-progress.json').write_text(json.dumps({'step': 'native-observation', 'last_screenshot': 'last.png', 'last_log': 'native-test.log'}))
            image = b'\x89PNG\r\n\x1a\n' + b'unchanged-image-bytes'
            (output / 'last.png').write_bytes(image)
            (output / 'driver.log').write_bytes(b'private-driver-output')
            (output / 'native-test.log').write_bytes(b'private-prefix' * 20000 + b'private-tail-marker')
            with (output / 'large-bulk.bin').open('wb') as stream:
                stream.truncate(1500 * 1024 * 1024)
            key = b'4' * 64
            envelope.diagnostic_summary(key, root, 7)
            self.assertFalse((root / 'results.zip').exists())
            self.assertLess((root / 'diagnostic.enc').stat().st_size, 128 * 1024)
            envelope.unseal(key, 'diagnostic', root / 'diagnostic.enc', root / 'opened.zip')
            with zipfile.ZipFile(root / 'opened.zip') as archive:
                self.assertEqual(set(archive.namelist()), {'summary.json', 'driver-tail.log', 'last-screenshot.png'})
                summary = json.loads(archive.read('summary.json'))
                self.assertEqual(summary['failing_step'], 'native-observation')
                self.assertEqual(summary['exit_code'], 7)
                self.assertEqual(summary['log_source'], 'native-test.log')
                self.assertEqual(summary['payload_manifest'], manifest)
                self.assertEqual(archive.read('last-screenshot.png'), image)
                self.assertEqual(len(archive.read('driver-tail.log')), envelope.SUMMARY_TEXT_LIMIT)
                self.assertTrue(archive.read('driver-tail.log').endswith(b'private-tail-marker'))
            with self.assertRaises(ValueError):
                envelope.unseal(key, 'results', root / 'diagnostic.enc', root / 'wrong-purpose')

    def test_diagnostic_does_not_follow_an_external_image_and_caps_large_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'private-output'
            output.mkdir()
            outside = root / 'outside.png'
            outside.write_bytes(b'\x89PNG\r\n\x1a\nprivate-outside')
            (output / 'image.png').symlink_to(outside)
            (output / 'capture-progress.json').write_text(json.dumps({'step': 'failed', 'last_screenshot': 'image.png'}))
            key = b'5' * 64
            for case in ['external-link', 'large-image']:
                if case == 'large-image':
                    (output / 'image.png').unlink()
                    with (output / 'image.png').open('wb') as stream:
                        stream.truncate(envelope.SUMMARY_IMAGE_LIMIT + 1)
                envelope.diagnostic_summary(key, root, 1)
                envelope.unseal(key, 'diagnostic', root / 'diagnostic.enc', root / 'opened.zip')
                with zipfile.ZipFile(root / 'opened.zip') as archive:
                    self.assertEqual(set(archive.namelist()), {'summary.json', 'driver-tail.log'})
                    self.assertIsNone(json.loads(archive.read('summary.json'))['screenshot'])

    def test_summary_first_receiver_preserves_failure_and_defers_bulk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'app').write_bytes(b'synthetic input')
            (root / 'script').write_text('printf "private failure detail\\n"\nexit 7\n')
            envelope.package(root / 'app', root / 'script', root / 'payload.zip')
            key = b'6' * 64
            envelope.seal(key, 'payload', root / 'payload.zip', root / 'payload.enc')
            data = (root / 'payload.enc').read_bytes()
            public = io.StringIO()
            with patch.dict(os.environ, {'SIMULATOR_ENVELOPE_KEY': key.decode()}), \
                    patch.object(envelope.urllib.request, 'urlopen', return_value=io.BytesIO(data)), \
                    patch.object(envelope, 'package_results') as bulk, contextlib.redirect_stdout(public):
                code = envelope.receive('https://example.invalid/input', hashlib.sha256(data).hexdigest(), root / 'received', defer_bulk=True)
            bulk.assert_not_called()
            self.assertEqual(code, 7)
            self.assertTrue((root / 'received/diagnostic.enc').exists())
            self.assertNotIn('private failure detail', public.getvalue())
            self.assertNotIn(key.decode(), public.getvalue())
            envelope.package_results(key, root / 'received')
            envelope.unseal(key, 'results', root / 'received/results.enc', root / 'bulk.zip')
            with zipfile.ZipFile(root / 'bulk.zip') as archive:
                self.assertIn(b'private failure detail', archive.read('driver.log'))

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
