import copy
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import simulator_retirement as retirement
import simulator_transport as transport
import test_simulator_transport as support

Provider, REPO, SHA = support.Provider, support.REPO, support.SHA


class RetirementProvider(Provider):
    def get(self, endpoint):
        suffix = endpoint.partition('?')[0].removeprefix(f'repos/{REPO}/')
        if suffix == 'actions/secrets':
            return {'total_count': len(self.metadata), 'secrets': copy.deepcopy(list(self.metadata.values()))}
        if suffix == 'git/matching-refs/':
            return [{'ref': 'refs/' + name, **copy.deepcopy(obj)} for name, obj in self.refs.items()]
        if suffix.endswith('/attempts/1/jobs'):
            run_id = int(suffix.split('/')[2])
            run = self.run_records[run_id]
            return {'total_count': 1, 'jobs': [{'id': 42, 'run_id': run_id, 'head_sha': run['head_sha'],
                                               'status': 'completed', 'conclusion': run['conclusion']}]}
        if suffix.endswith('/attempts/1'):
            return super().get(endpoint.removesuffix('/attempts/1'))
        return super().get(endpoint)


@unittest.skipUnless(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'Linux pidfd required')
class RetirementTests(unittest.TestCase):
    operation = support.TransportTests.operation
    ready = support.TransportTests.ready
    # Reuse the real-crypto/provider fixture, not its test methods.
    def setUp(self):
        support.TransportTests.setUp(self)
        self.provider = RetirementProvider()
        self.op = self.operation()
        self.ready(self.op)
        self.op.dispatch()
        self.run_id = self.op.runs()[0]['id']
        self.provider.artifacts.clear()
        self.program = self.root / 'fixture.py'
        self.program.write_text('import sys\nsys.stdin.read()\n')
        argv = [sys.executable, str(self.program), '--repo', REPO, '--receiver-ref', SHA,
                '--script', str(self.script), '--script-sha256', transport.sha256(self.script),
                '--app', str(self.app), '--app-sha256', transport.sha256(self.app),
                '--work-root', str(self.op.work.parent), '--prefix', 'simulator']
        self.child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_child)
        # Wait for the fixture to reach its idle read without an arbitrary sleep.
        for _ in range(1000):
            try:
                retirement.process_identity(self.child.pid)
                break
            except transport.TransportError:
                os.sched_yield()
        self.op.receipt['pid'] = self.child.pid
        self.op.receipt['events'][0]['utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.op.save()
        self.evidence = self.root / 'provider-diagnostics.json'
        self.evidence.write_text(json.dumps({'run_id': self.run_id, 'payload': 'cancelled', 'upload': 'failure'}))
        self.provider.refs['heads/unrelated'] = {'object': {'sha': 'b' * 40}}
        self.request = {'disposition': 'terminal-without-output', 'authorization': 'independent-consumer-no-output-receipt',
                        'receipt': str(self.op.work / 'receipt.json'), 'run_id': self.run_id, 'job_ids': [42],
                        'code_files': [str(self.program)], 'evidence_files': [str(self.evidence)], 'preserve': []}
        self.journal = retirement.canonical_journal(self.op.work / 'receipt.json')

    def stop_child(self):
        if self.child.poll() is None:
            self.child.terminate()
        self.child.communicate(timeout=5)

    def prepare_retirement(self):
        controller = retirement.Retirement(self.journal, self.provider)
        controller.prepare(self.request)
        return controller

    def rejected(self, change):
        change()
        before = list(self.provider.mutations)
        with self.assertRaises(transport.TransportError):
            self.prepare_retirement()
        self.assertEqual(self.provider.mutations, before)
        self.assertIsNone(self.child.poll())

    def test_terminal_no_output_retires_only_owned_resources_and_original_pid(self):
        original = (self.op.work / 'receipt.json').read_bytes()
        controller = self.prepare_retirement()
        preserved = copy.deepcopy(controller.data['preserved'])
        parallel = copy.deepcopy(controller.data['parallel_resources'])
        self.assertTrue(controller.apply())
        self.assertEqual(self.child.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual((self.op.work / 'receipt.json').read_bytes(), original)
        self.assertEqual(retirement.census(controller.data['preserved_roots'], exclude=[self.journal, self.journal.with_suffix('.lock')]), preserved)
        self.assertEqual(controller.parallel(controller.resources()), parallel)
        self.assertEqual(controller.data['disposition'], 'retired-without-output')
        self.assertNotIn('decrypt', controller.data['original']['phases'])
        self.assertNotIn('cleanup', controller.data['original']['phases'])
        self.assertEqual([x for x in self.provider.mutations if x[0] == 'DELETE'],
                         [('DELETE', p.replace('git/ref/', 'git/refs/')) for p, _ in controller.objects(self.op.receipt).values()])
        with self.assertRaises(transport.TransportError):
            controller.apply()

    def test_nonterminal_run_refuses(self):
        self.rejected(lambda: self.provider.run_records[self.run_id].update(status='in_progress'))

    def test_success_without_output_requires_a_distinct_investigation(self):
        self.rejected(lambda: self.provider.run_records[self.run_id].update(conclusion='success'))

    def test_wrong_head_refuses(self):
        self.rejected(lambda: self.provider.run_records[self.run_id].update(head_sha='b' * 40))

    def test_wrong_attempt_refuses(self):
        self.rejected(lambda: self.provider.run_records[self.run_id].update(run_attempt=2))

    def test_wrong_ref_refuses(self):
        self.rejected(lambda: self.provider.run_records[self.run_id].update(head_branch='other'))

    def test_wrong_run_refuses(self):
        self.rejected(lambda: self.request.update(run_id=1))

    def test_wrong_jobs_refuse(self):
        self.rejected(lambda: self.request.update(job_ids=[43]))

    def test_contradictory_job_attempt_or_ref_refuses(self):
        get = self.provider.get
        for field, value in (('run_attempt', 2), ('head_branch', 'another-operation')):
            def contradictory(endpoint):
                result = get(endpoint)
                if '/attempts/1/jobs' in endpoint:
                    result['jobs'][0][field] = value
                return result
            with self.subTest(field=field), patch.object(self.provider, 'get', side_effect=contradictory):
                before = list(self.provider.mutations)
                with self.assertRaises(transport.TransportError):
                    self.prepare_retirement()
                self.assertEqual(self.provider.mutations, before)
            self.assertIsNone(self.child.poll())

    def test_any_artifact_including_expired_refuses(self):
        self.rejected(lambda: self.provider.artifacts.update({self.run_id: {'id': self.run_id, 'expired': True,
                             'workflow_run': {'id': self.run_id}}}))

    def test_duplicate_run_refuses(self):
        self.rejected(lambda: self.provider.run_records.update({3: {**self.provider.run_records[self.run_id], 'id': 3}}))

    def test_unknown_dispatch_refuses(self):
        def change():
            self.op.receipt['effects']['dispatch']['state'] = 'submitted'
            self.op.save()
        self.rejected(change)

    def test_started_unseal_refuses(self):
        def change():
            self.op.receipt['phases']['unseal'] = 'started'
            self.op.save()
        self.rejected(change)

    def test_partial_local_output_refuses(self):
        self.rejected(lambda: (self.op.work / 'encrypted-results').mkdir())

    def test_missing_diagnostics_refuses(self):
        self.rejected(lambda: self.request.update(evidence_files=[]))

    def test_wrong_process_arguments_refuse(self):
        def change():
            self.op.receipt['pid'] = os.getpid()
            self.op.save()
        self.rejected(change)

    def test_changed_owned_object_refuses(self):
        self.rejected(lambda: self.provider.refs['tags/' + self.op.operation]['object'].update(sha='b' * 40))

    def test_changed_preserved_byte_refuses_before_first_delete(self):
        controller = self.prepare_retirement()
        before = list(self.provider.mutations)
        self.evidence.write_text('changed')
        with self.assertRaises(transport.TransportError):
            controller.apply()
        self.assertEqual(self.provider.mutations, before)
        self.assertIsNone(self.child.poll())

    def test_late_artifact_stops_cleanup_and_preserves_key_process(self):
        controller = self.prepare_retirement()
        def late(name):
            if name == 'delete-secret':
                self.provider.artifacts[self.run_id] = {'id': self.run_id, 'workflow_run': {'id': self.run_id}}
        self.provider.before_mutation = late
        with self.assertRaises(transport.TransportError):
            controller.apply()
        self.assertIsNone(self.child.poll())
        self.assertIn('tags/' + self.op.operation, self.provider.refs)
        self.assertNotIn('retire-process', controller.data['effects'])

    def test_parallel_change_stops_without_signalling(self):
        controller = self.prepare_retirement()
        def changed(name):
            if name == 'delete-secret':
                self.provider.refs['heads/unrelated']['object']['sha'] = 'c' * 40
        self.provider.before_mutation = changed
        with self.assertRaises(transport.TransportError):
            controller.apply()
        self.assertIsNone(self.child.poll())

    def test_unknown_delete_reconciles_absence_without_replay(self):
        controller = self.prepare_retirement()
        api = self.provider.api
        def lost(method, endpoint, body=None):
            response = api(method, endpoint, body)
            if method == 'DELETE' and '/secrets/' in endpoint:
                raise transport.TransportError('Lost DELETE response.')
            return response
        with patch.object(self.provider, 'api', side_effect=lost):
            with self.assertRaises(transport.TransportError):
                controller.apply()
        self.assertTrue(retirement.Retirement(self.journal, self.provider).apply())
        self.assertEqual(sum(x[0] == 'DELETE' and '/secrets/' in x[1] for x in self.provider.mutations), 1)

    def test_visibility_lag_never_replays_accepted_delete(self):
        controller = self.prepare_retirement()
        self.provider.lag = 3
        self.assertFalse(controller.apply())
        self.provider.lag = 0
        for _ in range(10):
            if controller.apply():
                break
        self.assertEqual(controller.data['disposition'], 'retired-without-output')
        self.assertEqual(sum(x[0] == 'DELETE' for x in self.provider.mutations), 3)

    def test_lost_signal_response_reconciles_without_second_signal(self):
        controller = self.prepare_retirement()
        actual = retirement.Custodian.terminate
        def lost(process):
            actual(process)
            self.child.wait(timeout=5)
            raise transport.TransportError('Signal response lost.')
        with patch.object(retirement.Custodian, 'terminate', lost):
            with self.assertRaises(transport.TransportError):
                controller.apply()
        with patch.object(retirement.Custodian, 'terminate', side_effect=AssertionError('signal replay')):
            self.assertTrue(retirement.Retirement(self.journal, self.provider).apply())

    def test_changed_process_birth_refuses_before_pidfd_signal(self):
        controller = self.prepare_retirement()
        controller.data['process']['start_ticks'] += 1
        retirement.save(self.journal, controller.data)
        with self.assertRaises(transport.TransportError):
            controller.apply()
        self.assertIsNone(self.child.poll())

    def test_relative_inputs_resolve_in_original_custodian_directory(self):
        self.stop_child()
        self.child = subprocess.Popen([sys.executable, str(self.program), '--repo', REPO, '--receiver-ref', SHA,
            '--script', self.script.name, '--script-sha256', transport.sha256(self.script),
            '--app', self.app.name, '--app-sha256', transport.sha256(self.app),
            '--work-root', 'ops', '--prefix', 'simulator'], cwd=self.root,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(10000):
            try:
                retirement.process_identity(self.child.pid)
                break
            except transport.TransportError:
                os.sched_yield()
        self.op.receipt['pid'] = self.child.pid
        self.op.receipt['events'][0]['utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.op.save()
        decoys = self.root / 'decoys'
        decoys.mkdir()
        for p in (self.app, self.script):
            (decoys / p.name).write_bytes(p.read_bytes())
        # The wrong resolution has same-hash decoys but no owned work root.
        (decoys / 'ops').symlink_to(self.op.work.parent, target_is_directory=True)
        with contextlib.chdir(decoys):
            controller = self.prepare_retirement()
            self.assertIn(str(self.app), controller.data['preserved'])
            self.assertIn(str(self.script), controller.data['preserved'])
            self.app.write_bytes(b'original input changed')
            before = list(self.provider.mutations)
            with self.assertRaises(transport.TransportError):
                controller.apply()
            self.assertEqual(self.provider.mutations, before)
        self.assertIsNone(self.child.poll())

    def test_new_journal_cannot_replay_unknown_delete(self):
        controller = self.prepare_retirement()
        with patch.object(self.provider, 'api', side_effect=transport.TransportError('Outcome unknown')):
            with self.assertRaises(transport.TransportError):
                controller.apply()
        before = list(self.provider.mutations)
        alternate = retirement.Retirement(self.root / 'another-journal.json', self.provider)
        with self.assertRaises(transport.TransportError):
            alternate.prepare(self.request)
        alternate.data = json.loads(self.journal.read_text())
        retirement.save(alternate.journal, alternate.data)
        with self.assertRaises(transport.TransportError):
            alternate.apply()
        self.assertEqual(self.provider.mutations, before)
        self.assertIsNone(self.child.poll())

    def test_stale_controller_reloads_unknown_effect_instead_of_replaying(self):
        controller = self.prepare_retirement()
        stale = retirement.Retirement(self.journal, self.provider)
        with patch.object(self.provider, 'api', side_effect=transport.TransportError('Outcome unknown')):
            with self.assertRaises(transport.TransportError):
                controller.apply()
        before = list(self.provider.mutations)
        self.assertFalse(stale.apply())
        self.assertEqual(self.provider.mutations, before)
        self.assertIsNone(self.child.poll())

    def test_operation_lock_refuses_parallel_invocation(self):
        import fcntl
        controller = self.prepare_retirement()
        with self.journal.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                controller.apply()
        self.assertIsNone(self.child.poll())

    def test_existing_evidence_inside_state_directory_is_preserved(self):
        self.journal.parent.mkdir()
        evidence = self.journal.parent / 'original-provider-evidence.txt'
        evidence.write_bytes(b'original evidence')
        controller = self.prepare_retirement()
        self.assertIn(str(evidence), controller.data['preserved'])
        evidence.write_bytes(b'changed evidence')
        before = list(self.provider.mutations)
        with self.assertRaises(transport.TransportError):
            controller.apply()
        self.assertEqual(self.provider.mutations, before)
        self.assertIsNone(self.child.poll())

    def test_state_directory_association_is_fsynced_before_effects(self):
        actual_open, actual_fsync = os.open, os.fsync
        opened, synced = {}, []
        def open_record(path, *args, **kwargs):
            fd = actual_open(path, *args, **kwargs)
            opened[fd] = Path(path)
            return fd
        def fsync_record(fd):
            synced.append(opened.get(fd))
            return actual_fsync(fd)
        with patch.object(os, 'open', side_effect=open_record), patch.object(os, 'fsync', side_effect=fsync_record):
            controller = self.prepare_retirement()
            self.assertIn(self.op.work, synced)
            synced.clear()
            self.provider.before_mutation = lambda name: self.assertIn(self.op.work, synced)
            self.assertTrue(controller.apply())

    def test_pid_recycle_during_open_refuses(self):
        expected = retirement.process_identity(self.child.pid)
        other = {**expected, 'start_ticks': expected['start_ticks'] + 1}
        with patch.object(retirement, 'process_identity', side_effect=[expected, other]), \
                patch.object(os, 'pidfd_open', return_value=123), patch.object(os, 'close') as close:
            with self.assertRaises(transport.TransportError):
                retirement.Custodian(expected)
        close.assert_called_once_with(123)


if __name__ == '__main__':
    unittest.main()
