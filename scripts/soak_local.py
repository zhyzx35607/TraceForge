"""Bounded local two-worker acceptance using synthetic replay and real HTTP.

This is a correctness/latency sample, not a production load test or LLM score.
Only the standard library is needed by this driver. The child interpreter must
have this checkout's runtime dependencies installed. No existing server is used.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import platform
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

TERMINAL = {'completed', 'rejected', 'cancelled', 'failed'}
PLAN = ['approve'] * 6 + ['reject'] * 3 + ['cancel'] * 3
BOOTSTRAP = r'''
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
if sys.argv[2] == 'probe':
    import traceforge
    from traceforge.paths import DEMO_ROOT
    digest = hashlib.sha256()
    files = sorted(path for path in DEMO_ROOT.rglob('*') if path.is_file())
    for path in files:
        digest.update(path.relative_to(DEMO_ROOT).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
    print(json.dumps({'version': traceforge.__version__, 'fixture_files': len(files),
                      'fixture_sha256': digest.hexdigest(), 'python': list(sys.version_info[:3])}))
else:
    from traceforge.__main__ import main
    sys.argv = ['traceforge', 'serve', '--host', '127.0.0.1', '--port', sys.argv[3]]
    raise SystemExit(main())
'''


class CheckFailure(RuntimeError):
    """Messages contain only driver-owned labels, never raw API/log content."""


def require(condition, code):
    if not condition:
        raise CheckFailure(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def stamp(value):
    require(isinstance(value, str), 'event_timestamp_missing')
    return datetime.fromisoformat(value).timestamp()


def milliseconds(value):
    return round(value * 1000, 3)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CheckFailure('unexpected_http_redirect')


class LocalAcceptance:
    def __init__(self, python: Path, root: Path, source: Path, timeout: float, report: dict):
        self.python, self.root, self.source, self.report = python, root, source, report
        self.started = time.monotonic()
        self.deadline = self.started + timeout
        self.work_deadline = self.deadline - 25  # Allow the engine's 15-second child cleanup, then force termination.
        self.state = root / 'state'
        self.cwd = root / 'empty-cwd'
        self.cwd.mkdir()
        keep = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'SYSTEMDRIVE'}
        self.env = {key: value for key, value in os.environ.items() if key.upper() in keep}
        self.env.update(HOME=str(root), USERPROFILE=str(root), TMP=str(root), TEMP=str(root), TMPDIR=str(root),
                        LOCALAPPDATA=str(root), XDG_DATA_HOME=str(root), PYTHONNOUSERSITE='1',
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0',
                        TRACEFORGE_DATA_DIR=str(self.state), TRACEFORGE_TEST_TIMEOUT='20',
                        TRACEFORGE_MAX_RUN_SECONDS='90', TRACEFORGE_BASE_URL='http://127.0.0.1:1/v1')
        self.lock = threading.Lock()
        self.process = None
        self.base_url = ''
        self.project_id = None
        self.runs = {}
        self.request_rows = report['http_requests']

    def remaining(self, maximum=3, *, cleanup=False):
        left = (self.deadline if cleanup else self.work_deadline) - time.monotonic()
        require(left > 0, 'overall_deadline_exceeded')
        return min(maximum, left)

    def child_command(self, mode, *arguments):
        return [str(self.python), '-I', '-B', '-X', 'utf8', '-c', BOOTSTRAP, str(self.source), mode, *arguments]

    def launch(self, mode, log, *arguments):
        return subprocess.Popen(self.child_command(mode, *arguments), cwd=self.cwd, env=self.env,
                                stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                start_new_session=os.name != 'nt')

    def owned_process_groups(self, process):
        # runner starts its test processes in new sessions. Snapshot only this
        # launcher's descendants, so a forced API exit cannot orphan those groups.
        inspected = subprocess.run(['ps', '-eo', 'pid=,ppid=,pgid='], capture_output=True,
                                   timeout=max(0.1, min(3, self.deadline - time.monotonic())), check=False)
        require(inspected.returncode == 0, 'cleanup_process_inventory_failed')
        rows = []
        for line in inspected.stdout.decode('ascii', errors='strict').splitlines():
            fields = line.split()
            require(len(fields) == 3 and all(field.isdigit() for field in fields), 'cleanup_process_inventory_invalid')
            rows.append(tuple(map(int, fields)))
        owned = {process.pid}
        while True:
            children = {pid for pid, parent, _group in rows if parent in owned}
            if children <= owned:
                break
            owned.update(children)
        return {process.pid, *(group for pid, _parent, group in rows if pid in owned and group in owned)}

    def stop(self, process):
        if process is None or process.poll() is not None:
            return
        # Cleanup must still attempt termination if the work exhausted its budget.
        # Never target an existing app: this is the PID/session created by launch().
        cleanup_time = max(0.1, min(18, self.deadline - time.monotonic()))
        groups = {process.pid}
        inventory_failed = False
        try:
            if os.name != 'nt':
                try:
                    groups = self.owned_process_groups(process)
                except (OSError, ValueError, subprocess.TimeoutExpired, CheckFailure):
                    inventory_failed = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=cleanup_time)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
            else:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                               timeout=min(5, cleanup_time), check=False,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        finally:
            if os.name != 'nt':
                for group in groups:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if process.poll() is None:
                process.kill()
            process.wait(timeout=max(0.1, min(2, self.deadline - time.monotonic())))
        require(not inventory_failed, 'cleanup_process_inventory_failed')

    def probe(self, name):
        path = self.root / (name + '.log')
        with path.open('wb') as log:
            process = self.launch('probe', log)
            try:
                code = process.wait(timeout=self.remaining(10))
            finally:
                self.stop(process)
        require(code == 0 and path.stat().st_size < 20_000, 'fixture_probe_failed')
        value = json.loads(path.read_text(encoding='utf-8'))
        require(isinstance(value, dict) and type(value.get('fixture_files')) is int
                and isinstance(value.get('version'), str) and re.fullmatch(r'[0-9A-Za-z.+-]{1,60}', value['version'])
                and isinstance(value.get('fixture_sha256'), str)
                and re.fullmatch(r'[a-f0-9]{64}', value['fixture_sha256'])
                and isinstance(value.get('python'), list) and len(value['python']) == 3
                and all(type(part) is int for part in value['python']), 'fixture_probe_invalid')
        return value

    def request(self, route, *, label='instance', action=None, payload=None, expected=(200,), path=None, headers=None):
        started = time.monotonic()
        row = {'label': label, 'action': action or route, 'method': 'POST' if payload is not None else 'GET',
               'endpoint': route, 'started_ms': milliseconds(started - self.started), 'status': None}
        data = canonical(payload) if payload is not None else None
        request = Request(self.base_url + (path or route), data=data,
                          headers={**({'Content-Type': 'application/json'} if data is not None else {}), **(headers or {})})
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            try:
                response = opener.open(request, timeout=self.remaining(4))
            except HTTPError as error:
                response = error
            with response:
                row['status'] = response.status
                body = response.read(2_000_001)
                require(len(body) <= 2_000_000, 'http_response_exceeds_limit')
                require(response.status in expected, f'{label}:{row["action"]}:unexpected_http_status')
                return response.status, json.loads(body)
        except (OSError, URLError, TimeoutError):
            row['transport_error'] = True
            raise
        finally:
            row['duration_ms'] = milliseconds(time.monotonic() - started)
            with self.lock:
                self.request_rows.append(row)

    def run_request(self, label, suffix='', **kwargs):
        run_id = self.runs[label]['id']
        return self.request('/api/runs/{run}' + suffix, label=label, path=f'/api/runs/{run_id}' + suffix, **kwargs)

    def parallel(self, calls):
        # The outstanding fanout is fixed and each socket uses the overall budget.
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(call) for call in calls]
            return [future.result(timeout=self.remaining(30)) for future in futures]

    def poll(self, targets):
        pending = set(targets)
        while pending:
            self.remaining()
            require(self.process.poll() is None, 'server_exited_during_work')
            labels = sorted(pending)
            fetched = self.parallel([lambda label=label: self.run_request(label, action='poll')[1] for label in labels])
            for label, run in zip(labels, fetched, strict=True):
                status = run.get('status')
                self.runs[label]['latest'] = run
                if status == targets[label]:
                    pending.remove(label)
                    self.runs[label]['observed_target_ms'] = milliseconds(time.monotonic() - self.started)
                else:
                    require(status not in TERMINAL, f'{label}:unexpected_terminal_status')
            if pending:
                time.sleep(min(0.1, self.remaining()))

    def register(self, label, run, action):
        require(run.get('mode') == 'replay' and run.get('project_id') == self.project_id, 'unexpected_created_run')
        with self.lock:
            self.runs[label] = {'id': run['id'], 'action': action, 'created_status': run['status'], 'latest': run}

    def create_run(self, label, action):
        payload = {'project_id': self.project_id, 'title': 'Synthetic local acceptance ' + label,
                   'query': 'Fixture replay: 3 items at 19.90 must total 59.70. Preserve the fixture tests.', 'mode': 'replay'}
        _, run = self.request('/api/runs', label=label, action='create', payload=payload, expected=(201,),
                              headers={'Idempotency-Key': 'local-soak-' + label})
        self.register(label, run, action)
        if action == 'cancel':
            _, cancelled = self.run_request(label, '/cancel', action='cancel', payload={})
            require(cancelled['status'] == 'cancelled', f'{label}:cancel_not_applied')
            _, repeated = self.run_request(label, '/cancel', action='repeat-cancel', payload={})
            require(repeated['status'] == 'cancelled', f'{label}:repeat_cancel_changed_terminal')

    def decide(self, label, digest, action):
        return self.run_request(label, '/approve', action='duplicate-' + action,
                                payload={'decision': action, 'patch_digest': digest}, expected=(200, 409))[0]

    def exercise(self):
        _, initial = self.request('/api/metrics')
        require(initial['total_runs'] == 0 and initial['provider_calls'] == 0, 'instance_not_empty')
        _, demo = self.request('/api/demo', action='seed-demo', payload={}, expected=(201,))
        self.project_id = demo['project']['id']
        require(demo['project']['runner'] == 'local' and demo['project']['trusted'] is True,
                'demo_is_not_explicitly_trusted_local_fixture')
        self.register('run-01', demo['run'], PLAN[0])
        self.parallel([lambda index=index, action=action: self.create_run(f'run-{index:02d}', action)
                       for index, action in enumerate(PLAN[1:], 2)])
        targets = {label: 'cancelled' if item['action'] == 'cancel' else 'awaiting_approval'
                   for label, item in self.runs.items()}
        self.poll(targets)
        require(len(self.runs) == 12 and len({item['id'] for item in self.runs.values()}) == 12, 'run_ids_not_unique')
        waiting = sorted(label for label, item in self.runs.items() if item['action'] != 'cancel')
        for label in waiting:
            events = self.run_request(label, '/events', action='preapproval-events')[1]
            require(not any(item['kind'].startswith('verification.') for item in events), f'{label}:verified_before_approval')
            digest = self.runs[label]['latest']['state']['patch_digest']
            require(isinstance(digest, str) and len(digest) == 64, 'approval_digest_missing')
            wrong = ('0' if digest[0] != '0' else '1') + digest[1:]
            self.run_request(label, '/approve', action='wrong-digest', payload={'decision': 'approve', 'patch_digest': wrong},
                             expected=(409,))
            unchanged = self.run_request(label, action='after-wrong-digest')[1]
            require(unchanged['status'] == 'awaiting_approval' and 'approval' not in unchanged['state'],
                    f'{label}:wrong_digest_mutated_state')
        calls, decisions = [], []
        for label in waiting:
            item = self.runs[label]
            for _ in range(2):
                calls.append(lambda label=label, item=item: self.decide(label, item['latest']['state']['patch_digest'],
                                                                       item['action']))
                decisions.append(label)
        statuses = self.parallel(calls)
        for label in waiting:
            actual = sorted(status for owner, status in zip(decisions, statuses, strict=True) if owner == label)
            require(actual == [200, 409], f'{label}:duplicate_decision_was_not_atomic')
            self.runs[label]['decision_responses'] = actual
        self.poll({label: {'approve': 'completed', 'reject': 'rejected', 'cancel': 'cancelled'}[item['action']]
                   for label, item in self.runs.items()})
        for label, item in self.runs.items():
            digest = item['latest']['state'].get('patch_digest', '0' * 64)
            self.run_request(label, '/approve', action='terminal-approval',
                             payload={'decision': 'approve', 'patch_digest': digest}, expected=(409,))

    def jobs(self):
        uri = (self.state / 'traceforge.db').as_uri() + '?mode=ro'
        with sqlite3.connect(uri, uri=True, timeout=self.remaining()) as database:
            database.row_factory = sqlite3.Row
            return [dict(row) for row in database.execute('SELECT run_id,phase,status,owner,attempts FROM jobs')]

    def wait_jobs(self):
        while True:
            jobs = self.jobs()
            if jobs and all(row['status'] == 'done' for row in jobs):
                return jobs
            require(self.process.poll() is None, 'server_exited_before_jobs_drained')
            time.sleep(min(0.1, self.remaining()))

    @staticmethod
    def event_durations(run, events):
        origin = stamp(run['created_at'])
        statuses = {item['payload']['to']: stamp(item['created_at']) for item in events if item['kind'] == 'run.status'}
        result = {'created_to_terminal_ms': milliseconds(stamp(run['updated_at']) - origin)}
        for key, start, finish in [('queue_ms', origin, statuses.get('investigating')),
                                   ('investigate_ms', statuses.get('investigating'), statuses.get('awaiting_approval')),
                                   ('decision_wait_ms', statuses.get('awaiting_approval'), statuses.get('verifying')),
                                   ('approved_to_completed_ms', statuses.get('verifying'), statuses.get('completed'))]:
            if start is not None and finish is not None:
                require(finish >= start, 'persisted_timestamps_out_of_order')
                result[key] = milliseconds(finish - start)
        starts = {item['payload']['phase']: stamp(item['created_at']) for item in events
                  if item['kind'] == 'verification.started'}
        if 'baseline' in starts and 'verifying' in statuses:
            result['verification_queue_ms'] = milliseconds(starts['baseline'] - statuses['verifying'])
        return result

    def audit_run(self, label, memories):
        item = self.runs[label]
        run = self.run_request(label, action='audit-run')[1]
        events = self.run_request(label, '/events', action='audit-events')[1]
        require(len(events) < 1000, f'{label}:events_truncated')
        require(all(event['run_id'] == item['id'] for event in events), f'{label}:foreign_event')
        ids = [event['id'] for event in events]
        require(ids == sorted(set(ids)), f'{label}:event_order_or_identity_invalid')
        counts = Counter(event['kind'] for event in events)
        require(counts['run.created'] == 1 and not counts['run.error'] and not counts['run.recovered'],
                f'{label}:unexpected_runtime_event')
        transitions = [event['payload'] for event in events if event['kind'] == 'run.status']
        current = 'queued'
        allowed = {'queued': {'investigating', 'cancelled'}, 'investigating': {'awaiting_approval', 'cancelled'},
                   'awaiting_approval': {'verifying', 'rejected', 'cancelled'}, 'verifying': {'completed'}}
        for transition in transitions:
            require(transition['from'] == current and transition['to'] in allowed.get(current, set()),
                    f'{label}:invalid_persisted_transition')
            current = transition['to']
        require(current == run['status'], f'{label}:events_and_state_disagree')
        state = run['state']
        usage = state.get('usage', {})
        require(all(usage.get(key, 0) == 0 for key in ('provider_calls', 'input_tokens', 'output_tokens')),
                f'{label}:provider_usage_not_zero')
        require(not any(kind.startswith('provider.') for kind in counts), f'{label}:provider_event_present')
        stored_memories = [memory for memory in memories if memory['run_id'] == item['id']]
        approval = state.get('approval', {})
        expected_decision = item['action']
        require(counts['approval.decided'] == (0 if expected_decision == 'cancel' else 1),
                f'{label}:approval_count_wrong')
        if expected_decision != 'cancel':
            digest = hashlib.sha256(canonical(state['diagnosis']['patches'])).hexdigest()
            require(approval.get('decision') == expected_decision and approval.get('patch_digest') == digest
                    and state['patch_digest'] == digest, f'{label}:approval_digest_not_bound')
            decided = next(event for event in events if event['kind'] == 'approval.decided')
            require(decided['payload'] == {'decision': expected_decision, 'patch_digest': digest},
                    f'{label}:approval_event_disagrees')
        else:
            require(not approval and counts['run.cancelled'] == 1, f'{label}:cancel_audit_not_idempotent')
        phases = {}
        if expected_decision == 'approve':
            verification = state.get('verification', {})
            require(verification.get('regression_fixed') is True, f'{label}:fix_not_verified')
            require(state.get('source_manifest') == verification.get('source_manifest')
                    and bool(state.get('source_manifest')), f'{label}:source_manifest_mismatch')
            require(counts['verification.started'] == counts['verification.result'] == 2
                    and counts['verification.completed'] == 1, f'{label}:verification_event_count_wrong')
            approval_id = next(event['id'] for event in events if event['kind'] == 'approval.decided')
            require(all(event['id'] > approval_id for event in events if event['kind'].startswith('verification.')),
                    f'{label}:verification_event_precedes_approval')
            for phase in ('baseline', 'patched'):
                observation = verification[phase]
                tests = observation['tests']
                require(observation['observation_status'] == 'completed' and not observation['timed_out']
                        and tests['source'] == 'external_observer' and tests['completed'] is True
                        and tests['executed'] == 8, f'{label}:{phase}:observer_incomplete')
                require(len(tests['identities']) == len(set(tests['identities'])) == 8
                        and set(tests['identities']) == set(tests['outcomes']), f'{label}:{phase}:identities_invalid')
                require(Counter(tests['outcomes'].values()) == Counter({key: tests[key] for key in
                        ('passed', 'failed', 'skipped') if tests[key]}), f'{label}:{phase}:outcome_counts_wrong')
                phases[phase] = {key: tests[key] for key in ('executed', 'passed', 'failed', 'skipped')}
                phases[phase].update(exit_code=observation['exit_code'], duration_ms=observation['duration_ms'])
            require(phases['baseline']['exit_code'] == 1 and phases['baseline']['failed'] == 4
                    and phases['patched']['exit_code'] == 0 and phases['patched']['passed'] == 8,
                    f'{label}:unexpected_fixture_outcome')
            require(verification['baseline']['tests']['identities'] == verification['patched']['tests']['identities'],
                    f'{label}:test_identities_changed')
            require(len(stored_memories) == 1 and stored_memories[0]['project_id'] == self.project_id
                    and stored_memories[0]['evidence_ids'] == state['diagnosis']['evidence_ids'],
                    f'{label}:memory_not_exactly_once')
        else:
            require(not any(kind.startswith('verification.') for kind in counts) and not state.get('verification')
                    and not stored_memories, f'{label}:unapproved_execution_or_memory')
        document = self.run_request(label, '/report?format=json', action='audit-export')[1]
        digest = document.pop('content_sha256')
        require(hashlib.sha256(canonical(document)).hexdigest() == digest, f'{label}:report_digest_invalid')
        require(document['verification']['verified'] is (expected_decision == 'approve'), f'{label}:report_claim_mismatch')
        row = {'label': label, 'action': expected_decision, 'status': run['status'], 'provider_calls': 0,
               'created_status': item['created_status'], 'decision_responses': item.get('decision_responses', []),
               'transitions': [transition['to'] for transition in transitions], 'event_counts': dict(sorted(counts.items())),
               'memory_count': len(stored_memories), 'phases': phases, 'durations': self.event_durations(run, events),
               'report_verified': document['verification']['verified'], 'consistency_passed': True}
        self.report['runs'].append(row)
        if expected_decision == 'approve':
            start = min(stamp(event['created_at']) for event in events if event['kind'] == 'verification.started')
            end = next(stamp(event['created_at']) for event in events if event['kind'] == 'verification.completed')
            return start, end
        return None

    def audit(self):
        jobs = self.wait_jobs()
        memories = self.request('/api/memories?project_id={project}', action='audit-memories',
                                path='/api/memories?project_id=' + self.project_id)[1]
        require(len(memories) == 6 and len({memory['run_id'] for memory in memories}) == 6, 'memory_total_wrong')
        intervals = [interval for label in sorted(self.runs) if (interval := self.audit_run(label, memories))]
        expected_ids = {item['id'] for item in self.runs.values()}
        require({row['run_id'] for row in jobs} == expected_ids, 'job_run_set_mismatch')
        owners = {row['owner'] for row in jobs if row['owner'] is not None}
        require(len(owners) == 2 and {owner.rsplit(':', 1)[-1] for owner in owners} == {'0', '1'},
                'two_worker_owners_not_observed')
        require(all(row['attempts'] <= 1 for row in jobs), 'unexpected_job_retry')
        for label, item in self.runs.items():
            actual = [row['phase'] for row in jobs if row['run_id'] == item['id']]
            require(sorted(actual) == (['investigate', 'verify'] if item['action'] == 'approve' else ['investigate']),
                    f'{label}:job_multiplicity_wrong')
        metrics = self.request('/api/metrics', action='audit-metrics')[1]
        expected = {'total_runs': 12, 'completed_runs': 6, 'verified_fixes': 6, 'failed_runs': 0,
                    'approval_pending': 0, 'provider_calls': 0, 'input_tokens': 0, 'output_tokens': 0}
        require(metrics == expected, 'aggregate_metrics_wrong')
        active = peak = 0
        for _at, change in sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals]):
            active += change
            peak = max(peak, active)
        require(peak == 2, 'two_overlapping_verifications_not_observed')
        self.report.update(metrics=expected, queue={'job_count': len(jobs), 'all_done': True,
                           'max_attempts': max(row['attempts'] for row in jobs), 'observed_worker_owners': len(owners),
                           'peak_overlapping_verifications': peak}, provider_calls=0)

    def execute(self):
        before = self.probe('before')
        self.report['environment'].update(application_version=before['version'], child_python=before['python'])
        self.report['fixture'] = {'files': before['fixture_files'], 'before_sha256': before['fixture_sha256']}
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        self.base_url = f'http://127.0.0.1:{port}'
        with (self.root / 'server.log').open('wb') as log:
            self.process = self.launch('serve', log, str(port))
            try:
                ready_deadline = time.monotonic() + self.remaining(25)
                while True:
                    require(time.monotonic() < ready_deadline, 'server_startup_deadline_exceeded')
                    require(self.process.poll() is None, 'server_exited_before_ready')
                    try:
                        health = self.request('/api/health', action='startup-health')[1]
                        break
                    except (URLError, TimeoutError, ConnectionError):
                        time.sleep(min(0.1, self.remaining()))
                require(health['status'] == 'ok' and health['provider']['configured'] is False
                        and not health['provider']['model'], 'model_provider_was_not_cleared')
                self.report['startup_ms'] = milliseconds(time.monotonic() - self.started)
                self.exercise()
                self.audit()
            finally:
                try:
                    self.stop(self.process)
                finally:
                    self.report['own_server_stopped'] = self.process.poll() is not None
                    self.report['run_progress'] = [
                        {'label': label, 'action': item['action'],
                         'last_observed_status': item['latest']['status']
                         if item['latest'].get('status') in TERMINAL | {'queued', 'investigating', 'awaiting_approval', 'verifying'}
                         else 'unknown'} for label, item in sorted(self.runs.items())]
        after = self.probe('after')
        self.report['fixture'].update(after_sha256=after['fixture_sha256'],
                                      source_unchanged=before['fixture_sha256'] == after['fixture_sha256'])
        require(self.report['fixture']['source_unchanged'], 'fixture_resources_changed')


def summarize_requests(rows):
    completed = sorted(row['duration_ms'] for row in rows if row['status'] is not None)
    groups = {}
    for row in rows:
        key = row['method'] + ' ' + row['endpoint']
        group = groups.setdefault(key, {'count': 0, 'statuses': Counter(), 'duration_ms': []})
        group['count'] += 1
        group['statuses'][str(row['status'])] += 1
        group['duration_ms'].append(row['duration_ms'])
    for group in groups.values():
        durations = group.pop('duration_ms')
        group.update(min_ms=min(durations), max_ms=max(durations), mean_ms=round(sum(durations) / len(durations), 3))
        group['statuses'] = dict(group['statuses'])
    return {'count': len(rows), 'completed_http_count': len(completed), 'by_endpoint': groups,
            'p50_ms': completed[math.ceil(len(completed) * 0.5) - 1] if completed else None,
            'p95_ms': completed[math.ceil(len(completed) * 0.95) - 1] if completed else None,
            'quantile_method': 'nearest-rank; HTTP responses only; expected 409 responses included'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, default=Path(sys.executable), help='Python with project dependencies installed')
    parser.add_argument('--report', type=Path, default=Path('.traceforge/local-concurrency/report.json'))
    parser.add_argument('--timeout', type=float, default=180, help='Total seconds, including a 25-second cleanup reserve (60..600)')
    args = parser.parse_args()
    started = time.monotonic()
    report = {'schema_version': 1, 'kind': 'synthetic-local-concurrency-acceptance', 'status': 'failed',
              'checked_at': datetime.now(UTC).isoformat(), 'mode': 'replay', 'model_capability': 'not_measured',
              'production_capacity': 'not_measured', 'expected_worker_slots': 2, 'planned_runs': 12,
              'planned_actions': dict(Counter(PLAN)),
              'timeout_seconds': args.timeout if math.isfinite(args.timeout) else None,
              'environment': {'os': platform.system(), 'architecture': platform.machine(),
                              'driver_python': list(sys.version_info[:3])},
              'provider_calls': None, 'own_server_stopped': False, 'runs': [], 'http_requests': [],
              'limitations': ['Fixed synthetic replay; no model call or model quality measurement.',
                              'One local process with two durable worker loops; not a production capacity or endurance test.',
                              'Stage times use persisted wall-clock events; HTTP times use a monotonic clock.',
                              'Cancellation is requested immediately after creation; the exact pre-cancel stage is observed, not forced.',
                              'No credentials, source bodies, server logs, original paths, or raw API responses are exported.']}
    try:
        require(math.isfinite(args.timeout) and 60 <= args.timeout <= 600, 'timeout_must_be_between_60_and_600')
        python = args.python.expanduser().absolute()
        require(python.is_file(), 'selected_python_not_found')
        with tempfile.TemporaryDirectory(prefix='traceforge-local-concurrency-') as temporary:
            acceptance = LocalAcceptance(python, Path(temporary), Path(__file__).resolve().parents[1], args.timeout, report)
            acceptance.execute()
        report['status'] = 'passed'
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - bounded acceptance always writes failure evidence
        report['failure'] = {'type': type(exc).__name__,
                             'code': str(exc) if isinstance(exc, CheckFailure) else 'driver_or_transport_failure'}
    report['duration_ms'] = milliseconds(time.monotonic() - started)
    report['http_requests'].sort(key=lambda row: row['started_ms'])
    report['http_summary'] = summarize_requests(report['http_requests'])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'report_filename': args.report.name,
                      'duration_ms': report['duration_ms'], 'failure': report.get('failure')}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
