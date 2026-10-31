#!/usr/bin/env python3
"""Explicit no-output retirement for a terminal simulator custodian (Linux)."""
import argparse
import datetime
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import stat
import tempfile

from simulator_transport import GitHub, TransportError, require, sha256

SCHEMA = 'simulator-no-output-retirement-v1'
TERMINAL_FAILURES = {'cancelled', 'failure', 'timed_out', 'startup_failure'}


def save(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def census(paths, exclude=()):
    """Preserve every byte, mode and symlink target; never traverse symlinks."""
    result = {}
    for root in paths:
        root = Path(root)
        require(root.is_absolute() and root.exists() and not root.is_symlink(), 'Preserved root must exist without a symlink.')
        for path in [root, *sorted(root.rglob('*'))] if root.is_dir() else [root]:
            if any(path == omitted or omitted in path.parents for omitted in exclude):
                continue
            info = path.lstat()
            value = {'mode': info.st_mode, 'uid': info.st_uid, 'gid': info.st_gid}
            if stat.S_ISREG(info.st_mode):
                value.update(bytes=info.st_size, sha256=sha256(path))
            elif stat.S_ISLNK(info.st_mode):
                value['target'] = os.readlink(path)
            else:
                require(stat.S_ISDIR(info.st_mode), 'Unexpected preserved special file.')
            result[str(path)] = value
    return result


def process_identity(pid):
    """No environment or memory access; identity uses kernel metadata only."""
    root = Path('/proc') / str(pid)
    stage = 'process stat'
    try:
        fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
        stage = 'process status'
        status = dict(line.split(':', 1) for line in (root / 'status').read_text().splitlines() if ':' in line)
        stage = 'process arguments'
        argv = (root / 'cmdline').read_bytes()
        stage = 'process children'
        require(fields[0] == 'S' and not (root / 'task' / str(pid) / 'children').read_text().strip(),
                'Custodian must be idle without child processes.')
        require(int(status['Threads']) == 1 and int(status['Uid'].split()[0]) == os.geteuid(), 'Custodian ownership differs.')
        mask = 1 << (signal.SIGTERM - 1)
        require(not any(int(status[name], 16) & mask for name in ('SigBlk', 'SigIgn', 'SigCgt')),
                'Custodian must retain default SIGTERM disposition.')
        stage = 'kernel boot time'
        boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
        stage = 'process birth and executable'
        return {'pid': int(pid), 'start_ticks': int(fields[19]),
                'started_epoch': boot + int(fields[19]) / os.sysconf('SC_CLK_TCK'),
                'cmdline_sha256': hashlib.sha256(argv).hexdigest(),
                'executable': str((root / 'exe').resolve()), 'cwd': str((root / 'cwd').resolve())}
    except (OSError, ValueError, StopIteration):
        raise TransportError(f'Custodian kernel identity unavailable ({stage}).') from None


class Custodian:
    def __init__(self, expected):
        require(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'Stable Linux pidfd support required.')
        self.expected = expected
        require(process_identity(expected['pid']) == expected, 'Custodian PID identity changed.')
        self.fd = os.pidfd_open(expected['pid'])
        try:
            require(process_identity(expected['pid']) == expected, 'Custodian changed while opening pidfd.')
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    def check(self):
        require(process_identity(self.expected['pid']) == self.expected, 'Custodian identity changed.')

    def terminate(self):
        self.check()
        signal.pidfd_send_signal(self.fd, signal.SIGTERM)

    def exited(self):
        poll = select.poll()
        poll.register(self.fd, select.POLLIN)
        return bool(poll.poll(5000))


def absent_process(identity):
    path = Path('/proc') / str(identity['pid']) / 'stat'
    if not path.exists():
        return True
    # A reused PID is not the original address space. Never signal the new one.
    fields = path.read_text().rsplit(')', 1)[1].split()
    return int(fields[19]) != identity['start_ticks'] or fields[0] == 'Z'


def serialized(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        self.journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Persist the operation's new state-directory association, not only the
        # journal entry inside it, before a provider effect can be submitted.
        parent = os.open(self.journal.parent.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        lock_path = self.journal.with_suffix('.lock')
        require(not lock_path.is_symlink() and (not lock_path.exists() or
                (lock_path.is_file() and lock_path.stat().st_size == 0)), 'Unexpected operation lock contents.')
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # A stale controller instance must observe another invocation's effects.
            self.data = json.loads(self.journal.read_text()) if self.journal.exists() else None
            return method(self, *args, **kwargs)
    return invoke


def canonical_journal(receipt):
    return Path(receipt).resolve().parent / '.no-output-retirement' / 'journal.json'


class Retirement:
    def __init__(self, journal, github=None, custodian=Custodian):
        self.journal = Path(journal).resolve()
        self.github = github or GitHub()
        self.custodian = custodian
        self.data = json.loads(self.journal.read_text()) if self.journal.exists() else None

    def path(self, suffix):
        return f"repos/{self.data['original']['repo']}/{suffix}"

    def record(self, event, **values):
        self.data['events'].append({'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'event': event, **values})
        save(self.journal, self.data)

    def original(self):
        request = self.data['request']
        require(census(self.data['preserved_roots'], exclude=[self.journal, self.journal.with_suffix('.lock')]) == self.data['preserved'], 'Preserved population or bytes changed.')
        receipt = json.loads(Path(request['receipt']).read_text())
        require(receipt == self.data['original'], 'Original receipt changed.')
        return receipt

    def objects(self, receipt):
        op = receipt['operation']
        return {'secret': (self.path('actions/secrets/' + receipt['secret_name']), receipt['secret_metadata']),
                'branch': (self.path('git/ref/heads/transport/' + op), {'sha': receipt['ciphertext_commit']}),
                'tag': (self.path('git/ref/tags/' + op), {'sha': receipt['source_sha']})}

    def resources(self):
        # These provider APIs return the complete repository secret/ref populations.
        secrets = self.github.pages(self.path('actions/secrets'), 'secrets')
        refs = self.github.get(self.path('git/matching-refs/'))
        require(isinstance(refs, list), 'Incomplete reference census.')
        require(len({s['name'] for s in secrets}) == len(secrets) and len({r['ref'] for r in refs}) == len(refs),
                'Duplicate provider resources.')
        return {'secrets': {s['name']: s for s in secrets}, 'refs': {r['ref']: r for r in refs}}

    def parallel(self, resources):
        result = json.loads(json.dumps(resources))
        rec = self.data['original']
        result['secrets'].pop(rec['secret_name'], None)
        for name in ('refs/heads/transport/' + rec['operation'], 'refs/tags/' + rec['operation']):
            result['refs'].pop(name, None)
        return result

    def remote(self):
        rec, request = self.original(), self.data['request']
        require(self.github.get(self.path(''))['private'] is False, 'Public repository required.')
        run_id = request['run_id']
        matches = [r for r in self.github.pages(self.path('actions/workflows/simulator-envelope.yml/runs'), 'workflow_runs')
                   if r['head_branch'] == rec['operation']]
        run = self.github.get(self.path(f'actions/runs/{run_id}'))
        attempt = self.github.get(self.path(f'actions/runs/{run_id}/attempts/1'))
        for item in [*matches, run, attempt]:
            require(item['id'] == run_id and item['run_attempt'] == 1 and item['head_sha'] == rec['source_sha'] and
                    item['head_branch'] == rec['operation'] and item['event'] == 'workflow_dispatch' and
                    item['status'] == 'completed' and item['conclusion'] in TERMINAL_FAILURES,
                    'Exact original terminal failure/attempt is not established.')
        require(len(matches) == 1, 'Expected one sole original run.')
        jobs = self.github.pages(self.path(f'actions/runs/{run_id}/attempts/1/jobs'), 'jobs')
        require({j['id'] for j in jobs} == set(request['job_ids']) and len(jobs) == len(request['job_ids']) and
                all(j['run_id'] == run_id and j['head_sha'] == rec['source_sha'] and j['status'] == 'completed' and
                    j.get('run_attempt', 1) == 1 and j.get('head_branch', rec['operation']) == rec['operation']
                    for j in jobs),
                'Exact terminal job census differs.')
        artifacts = self.github.pages(self.path(f'actions/runs/{run_id}/artifacts'), 'artifacts')
        require(artifacts == [], 'An artifact exists; authenticate and preserve it through normal custody.')
        resources = self.resources()
        require(self.parallel(resources) == self.data['parallel_resources'], 'Unrelated resources changed; preserve and reconcile.')
        for kind, (endpoint, expected) in self.objects(rec).items():
            obj = self.github.optional(endpoint)
            effect = self.data['effects'].get('delete-' + kind)
            if obj is None:
                require(effect is not None, 'Owned object disappeared before its journaled retirement.')
            else:
                require((obj == expected if kind == 'secret' else obj['object']['sha'] == expected['sha']),
                        'Owned object metadata changed.')
        return {'run': run, 'attempt': attempt, 'jobs': jobs, 'artifacts': artifacts}

    @serialized
    def prepare(self, request):
        require(self.data is None and not self.journal.exists(), 'Use the existing retirement journal.')
        require(request.get('disposition') == 'terminal-without-output' and bool(request.get('authorization')),
                'Explicit no-output authorization required.')
        receipt_path = Path(request['receipt']).resolve()
        require(self.journal == canonical_journal(receipt_path), 'Use the operation-owned canonical retirement journal.')
        rec = json.loads(receipt_path.read_text())
        require(rec['phases'] == {'prepare': 'complete', 'publish': 'complete', 'dispatch': 'complete'} and
                set(rec['effects']) == {'create-secret', 'create-branch', 'create-tag', 'dispatch'} and
                all(e['state'] == 'accepted' for e in rec['effects'].values()), 'Accepted original custody is uncertain.')
        require(rec['secret_name'] == 'SIMULATOR_ENVELOPE_KEY_' + rec['operation'].replace('-', '_').upper(), 'Secret ownership differs.')
        require(isinstance(request['run_id'], int) and request['run_id'] > 0 and request['job_ids'] and
                len(set(request['job_ids'])) == len(request['job_ids']), 'Exact run and job IDs required.')
        require(not any((receipt_path.parent / p).exists() for p in
                        ('encrypted-results', 'results.zip', 'decrypted', 'encrypted-diagnostic', 'diagnostic.zip', 'diagnostic')),
                'Local output or an uncertain transfer requires authenticated custody.')
        identity = process_identity(rec['pid'])
        alive = [e for e in rec['events'] if e['action'] == 'supervisor-alive']
        require(len(alive) == 1 and 0 <= datetime.datetime.fromisoformat(alive[0]['utc']).timestamp() - identity['started_epoch'] < 60,
                'Process birth does not match original supervisor event.')
        argv = (Path('/proc') / str(rec['pid']) / 'cmdline').read_bytes().decode().strip('\0').split('\0')
        def option(name):
            require(argv.count(name) == 1, 'Original process argument binding differs.')
            return argv[argv.index(name) + 1]
        require(option('--repo') == rec['repo'] and option('--receiver-ref') == rec['source_sha'] and
                option('--script-sha256') == rec['script_sha256'] and option('--app-sha256') == rec['app_sha256'] and
                Path(identity['cwd'], option('--work-root'), rec['operation']).resolve() == receipt_path.parent,
                'Original process does not own the supplied receipt.')
        program = Path(identity['cwd'], argv[1]).resolve()
        require(str(program) in request['code_files'] and request['code_files'], 'Original supervisor source binding required.')
        inputs = [str(Path(identity['cwd'], option(flag)).resolve()) for flag in ('--script', '--app')]
        require([sha256(p) for p in inputs] == [rec['script_sha256'], rec['app_sha256']], 'Frozen input bytes changed.')
        require(request['evidence_files'] and all(Path(p).is_file() for p in request['evidence_files']), 'Preserved provider diagnostics required.')
        request = {**request, 'receipt': str(receipt_path)}
        roots = sorted(set([str(receipt_path.parent), *inputs, *request['code_files'], *request['evidence_files'], *request.get('preserve', [])]))
        require(not any(Path(p) == self.journal.parent or self.journal.parent in Path(p).parents for p in roots),
                'Controller state must remain separate from original evidence roots.')
        self.data = {'schema': SCHEMA, 'request': request, 'original': rec, 'process': identity,
                     'controller_sha256': sha256(__file__), 'preserved_roots': roots, 'preserved': census(roots, exclude=[self.journal, self.journal.with_suffix('.lock')]),
                     'effects': {}, 'events': [], 'disposition': 'pending'}
        self.data['parallel_resources'] = self.parallel(self.resources())
        observed = self.remote()
        self.record('qualified-no-output', observed=observed)

    @serialized
    def apply(self):
        require(self.data and self.data['schema'] == SCHEMA and self.data['controller_sha256'] == sha256(__file__), 'Retirement controller/version differs.')
        require(self.journal == canonical_journal(self.data['request']['receipt']), 'Original retirement journal association differs.')
        require(self.data['disposition'] != 'retired-without-output', 'Already retired; use the retained receipt.')
        self.remote()
        process = None
        try:
            if 'retire-process' not in self.data['effects'] or not absent_process(self.data['process']):
                process = self.custodian(self.data['process'])
            for kind, (endpoint, _) in self.objects(self.data['original']).items():
                self.remote()
                name = 'delete-' + kind
                if name not in self.data['effects']:
                    require(process is not None, 'Original custodian ended before qualified resource retirement.')
                    process.check()
                    self.data['effects'][name] = {'state': 'submitted', 'endpoint': endpoint}
                    self.record('mutation-submitting', effect=name)
                    delete = endpoint.replace('git/ref/', 'git/refs/')
                    response = self.github.api('DELETE', delete)
                    require(response['status'] == 204, 'Deletion response requires reconciliation.')
                    self.data['effects'][name]['state'] = 'accepted'
                    self.record('mutation-accepted', effect=name)
                if self.github.optional(endpoint) is not None:
                    self.record('retirement-pending', effect=name)
                    return False
            observed = self.remote()
            require(all(self.github.optional(p) is None for p, _ in self.objects(self.data['original']).values()), 'Owned absence not established.')
            if 'retire-process' not in self.data['effects']:
                process.check()
                self.data['effects']['retire-process'] = {'state': 'submitted', 'method': 'pidfd-SIGTERM'}
                self.record('process-retirement-submitting', final_observation=observed)
                process.terminate()
            require((process is None or process.exited()) and absent_process(self.data['process']), 'Process retirement pending; never repeat the signal.')
            observed = self.remote()
            self.data['effects']['retire-process']['state'] = 'settled'
            self.data['disposition'] = 'retired-without-output'
            self.record('retired-without-output', final_observation=observed,
                        key_retirement='Original process address space ended; no key was read or transferred.',
                        native_verdict='unavailable', ordinary_cleanup=False)
            return True
        finally:
            if process is not None:
                process.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'apply'])
    parser.add_argument('--journal', required=True)
    parser.add_argument('--request')
    args = parser.parse_args()
    os.umask(0o077)
    path = Path(args.journal).resolve()
    controller = Retirement(path)
    if args.action == 'prepare':
        require(bool(args.request), 'Immutable request file required.')
        controller.prepare(json.loads(Path(args.request).read_text()))
        print(json.dumps({'disposition': 'qualified-no-output', 'journal': str(path)}))
        return 0
    done = controller.apply()
    print(json.dumps({'disposition': controller.data['disposition'], 'journal': str(path)}))
    return 0 if done else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        raise SystemExit(str(error) if isinstance(error, TransportError) else 'Retirement refused; private response withheld.')
