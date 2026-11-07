"""Black-box wheel acceptance using an already installed, separate virtualenv.

Only the standard library is required in the invoking Python. All application
commands run through the selected interpreter with -I from an empty directory.
This is an offline replay/oracle acceptance, never a model capability score.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PROBE = r'''
import hashlib, json, sys
from pathlib import Path
import traceforge
from traceforge.paths import MODULE_ROOT, RESOURCE_ROOT, DEMO_ROOT, CASES_ROOT, WEB_ROOT, default_data_dir
from traceforge.benchmark import list_cases
from traceforge.config import Settings
digest = hashlib.sha256()
files = sorted(path for path in RESOURCE_ROOT.rglob('*') if path.is_file())
for path in files:
    digest.update(path.relative_to(RESOURCE_ROOT).as_posix().encode())
    digest.update(b'\0')
    digest.update(path.read_bytes())
cases = list_cases()
canaries = sorted(CASES_ROOT.glob('*/workspace/.env'))
required = [DEMO_ROOT / 'pricing.py', DEMO_ROOT / 'test_pricing.py', WEB_ROOT / 'index.html']
for case in cases:
    root = CASES_ROOT / case['id']
    required.extend([root / 'case.json', root / 'oracle.json', root / 'workspace' / 'test_public.py',
                     root / 'hidden' / 'test_hidden.py'])
print(json.dumps({
    'module': str(Path(traceforge.__file__).resolve()), 'prefix': sys.prefix,
    'base_prefix': sys.base_prefix, 'executable': sys.executable, 'isolated': bool(sys.flags.isolated),
    'module_root': str(MODULE_ROOT), 'resource_root': str(RESOURCE_ROOT),
    'default_data_dir': str(default_data_dir()), 'provider_configured': Settings.from_env().provider_configured,
    'case_ids': [case['id'] for case in cases], 'canary_count': len(canaries),
    'canaries_valid': all('TRACEFORGE_PRIVATE_CANARY_DO_NOT_SURFACE' in p.read_text() for p in canaries),
    'missing_resources': [str(p.relative_to(RESOURCE_ROOT)) for p in required if not p.is_file()],
    'resource_files': len(files), 'resource_sha256': digest.hexdigest(),
}))
'''


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError(f'Unexpected HTTP redirect ({code})')


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets: set[str] = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        url = values.get('src') if tag == 'script' else values.get('href') if tag == 'link' else None
        if url and urlsplit(url).path.endswith(('.js', '.css')):
            parts = urlsplit(url)
            require(not parts.scheme and not parts.netloc and url.startswith('/'), 'Asset URL is not local')
            self.assets.add(url)


class ReleaseSmoke:
    def __init__(self, python: Path, root: Path, timeout: float):
        self.python, self.root = python, root
        self.cwd = root / 'empty-cwd'
        self.cwd.mkdir()
        self.deadline = time.monotonic() + timeout
        self.env = {key: value for key, value in os.environ.items()
                    if not key.upper().startswith('TRACEFORGE_') and key.upper() != 'PYTHONPATH'}
        self.env.update(TMPDIR=str(root), TEMP=str(root), TMP=str(root))
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.base_url = ''

    def remaining(self, maximum: float) -> float:
        value = min(maximum, self.deadline - time.monotonic())
        require(value > 0, 'Release smoke exceeded its overall time budget')
        return value

    def command(self, *args: str) -> list[str]:
        return [str(self.python), '-I', '-B', '-X', 'utf8', *args]

    def child_env(self, name: str) -> dict[str, str]:
        return {**self.env, 'TRACEFORGE_DATA_DIR': str(self.root / name), 'TRACEFORGE_TEST_TIMEOUT': '25'}

    def start(self, args: list[str], env: dict[str, str], log):
        return subprocess.Popen(self.command(*args), cwd=self.cwd, env=env, stdout=log, stderr=log,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                start_new_session=os.name != 'nt')

    @staticmethod
    def stop(process) -> None:
        if process.poll() is not None:
            return
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                           timeout=10, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name != 'nt':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)

    def json_command(self, args: list[str], name: str, *, clean_env: bool = False) -> dict:
        output = self.root / f'{name}.log'
        with output.open('wb') as log:
            process = self.start(args, self.env if clean_env else self.child_env(name), log)
            try:
                code = process.wait(timeout=self.remaining(90))
            finally:
                self.stop(process)
        raw = output.read_text(encoding='utf-8', errors='replace')
        require(code == 0, f'{name} exited {code}: {raw[-2000:]}')
        value = json.loads(raw)
        require(isinstance(value, dict), f'{name} did not return a JSON object')
        return value

    def request(self, path: str, payload: dict | None = None, *, raw: bool = False):
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        request = Request(self.base_url + path, data=data,
                          headers={'Content-Type': 'application/json'} if data is not None else {})
        with self.opener.open(request, timeout=self.remaining(3)) as response:
            body = response.read(2_000_001)
            require(len(body) <= 2_000_000, f'HTTP response too large: {path}')
            require(response.status in {200, 201}, f'Unexpected HTTP status: {response.status}')
            return body if raw else json.loads(body)

    def poll(self, process, run_id: str, target: str) -> dict:
        deadline = time.monotonic() + self.remaining(70)
        while time.monotonic() < deadline:
            require(process.poll() is None, 'API process exited during verification')
            run = self.request(f'/api/runs/{run_id}')
            if run['status'] == target:
                return run
            require(run['status'] not in {'failed', 'cancelled', 'rejected', 'completed'},
                    f'Run stopped at {run["status"]}: {run.get("error")}')
            time.sleep(0.15)
        raise RuntimeError(f'Run did not reach {target} within its deadline')

    def api_acceptance(self) -> dict:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.base_url = f'http://127.0.0.1:{port}'
        with (self.root / 'server.log').open('wb') as log:
            process = self.start(['-m', 'traceforge', 'serve', '--port', str(port)], self.child_env('api-state'), log)
            try:
                ready_deadline = time.monotonic() + self.remaining(25)
                while time.monotonic() < ready_deadline:
                    require(process.poll() is None, 'Installed API exited before becoming ready')
                    try:
                        health = self.request('/api/health')
                        break
                    except (URLError, TimeoutError):
                        time.sleep(0.15)
                else:
                    raise RuntimeError('Installed API did not become ready')
                require(not health['provider']['configured'], 'Unexpected configured model provider')
                html = self.request('/', raw=True)
                require(b'TraceForge' in html and b'id="root"' in html, 'Workbench HTML missing')
                parser = AssetParser()
                parser.feed(html.decode('utf-8'))
                require(any(url.endswith('.js') for url in parser.assets), 'Built JS asset missing')
                require(any(url.endswith('.css') for url in parser.assets), 'Built CSS asset missing')
                assets = []
                for url in sorted(parser.assets):
                    body = self.request(url, raw=True)
                    require(len(body) > 100 and not body.lstrip().lower().startswith(b'<!doctype'),
                            f'Asset is empty or an HTML fallback: {url}')
                    assets.append({'path': url, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
                created = self.request('/api/demo', {})['run']
                require(created['mode'] == 'replay', 'Demo did not explicitly use replay mode')
                ready = self.poll(process, created['id'], 'awaiting_approval')
                digest = ready['state']['patch_digest']
                require(len(digest) == 64, 'Approval digest missing')
                events = self.request(f'/api/runs/{created["id"]}/events')
                require(not any(event['kind'] == 'verification.started' for event in events),
                        'Verification ran before approval')
                self.request(f'/api/runs/{created["id"]}/approve', {'decision': 'approve', 'patch_digest': digest})
                completed = self.poll(process, created['id'], 'completed')
                verification = completed['state']['verification']
                require(verification['regression_fixed'], 'Installed demo did not verify a regression fix')
                baseline, patched = verification['baseline'], verification['patched']
                require(baseline['exit_code'] != 0 and patched['exit_code'] == 0, 'Unexpected test exit codes')
                for phase in (baseline, patched):
                    require(phase['observation_status'] == 'completed', 'External observer did not complete')
                    require(phase['tests']['source'] == 'external_observer', 'Test results are not observer data')
                    require(phase['tests']['executed'] == 8 and phase['tests']['completed'], 'Expected 8 completed tests')
                require(baseline['tests']['failed'] == 4, 'Baseline must have 4 failures')
                require(patched['tests']['passed'] == 8 and patched['tests']['failed'] == 0, 'Patched suite must pass 8 tests')
                require(set(baseline['tests']['identities']) == set(patched['tests']['identities']),
                        'Test identities changed across verification')
                metrics = self.request('/api/metrics')
                require(metrics['provider_calls'] == 0, 'Replay unexpectedly called a provider')
                document = self.request(f'/api/runs/{created["id"]}/report?format=json')
                content_hash = document.pop('content_sha256')
                calculated = hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                                        separators=(',', ':')).encode('utf-8')).hexdigest()
                require(content_hash == calculated, 'Exported report content digest does not match')
                require(document['verification']['verified'] and document['run']['mode'] == 'replay',
                        'Exported report did not preserve the verified replay outcome')
                require(all('content' not in item for item in document['evidence']), 'Report exported source bodies')
                markdown = self.request(f'/api/runs/{created["id"]}/report?format=markdown', raw=True)
                require(b'# TraceForge' in markdown and content_hash.encode() in markdown, 'Markdown report missing')
                return {'run_id': created['id'], 'mode': 'replay', 'status': completed['status'],
                        'approval_digest': digest, 'baseline': baseline['tests'], 'patched': patched['tests'],
                        'regression_fixed': True, 'provider_calls': metrics['provider_calls'], 'assets': assets,
                        'report_export': {'json': True, 'markdown': True, 'content_sha256': content_hash,
                                          'digest_verified': True}}
            finally:
                self.stop(process)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', required=True, type=Path, help='Python executable in a separate installed-wheel venv')
    parser.add_argument('--report', required=True, type=Path, help='Write concise JSON evidence here')
    parser.add_argument('--timeout', type=float, default=240, help='Overall execution budget in seconds')
    args = parser.parse_args()
    started = time.monotonic()
    report = {'kind': 'installed-wheel-offline-acceptance', 'status': 'failed',
              'checked_at': datetime.now(UTC).isoformat(), 'model_capability': 'not_measured',
              'limitations': ['Replay and oracle only; this does not measure model repair ability or Docker isolation.']}
    try:
        require(args.timeout > 0, '--timeout must be positive')
        python = args.python.expanduser().absolute()  # Keep the venv path even when its executable is a symlink.
        require(python.is_file(), 'Target Python executable does not exist')
        with tempfile.TemporaryDirectory(prefix='traceforge-release-') as raw:
            smoke = ReleaseSmoke(python, Path(raw), args.timeout)
            before = smoke.json_command(['-c', PROBE], 'probe-before', clean_env=True)
            prefix, module = Path(before['prefix']).resolve(), Path(before['module']).resolve()
            source = Path(__file__).resolve().parents[1]
            require(prefix != Path(before['base_prefix']).resolve(), 'Target interpreter is not a virtualenv')
            require(prefix == python.parent.parent.resolve(), 'Target virtualenv prefix does not match --python')
            require(module.is_relative_to(prefix) and not module.is_relative_to(source / 'traceforge'),
                    'Application was imported from the source checkout instead of the target virtualenv')
            require(before['isolated'], 'Target Python did not run in isolated mode')
            require(Path(before['resource_root']).resolve() == module.parent / '_assets', 'Wheel resources not selected')
            require(not Path(before['default_data_dir']).resolve().is_relative_to(prefix),
                    'Default writable data directory is inside the installed virtualenv')
            require(len(before['case_ids']) == 10 and len(set(before['case_ids'])) == 10, 'Expected 10 benchmark cases')
            require(before['canary_count'] == 2 and before['canaries_valid'], 'Expected two packaged synthetic canaries')
            require(not before['missing_resources'], f'Missing wheel resources: {before["missing_resources"]}')
            require(not before['provider_configured'], 'Inherited provider configuration was not cleared')
            report['installation'] = before
            report['api'] = smoke.api_acceptance()
            evaluation = smoke.json_command(['-m', 'traceforge', 'evaluate'], 'evaluation')
            require(evaluation['kind'] == 'offline-harness', 'Evaluation label is not offline-harness')
            require(evaluation['passed_cases'] == evaluation['total_cases'] == 10, 'Evaluation did not pass 10/10')
            report['evaluation'] = {key: evaluation[key] for key in ('kind', 'passed_cases', 'total_cases')}
            benchmark = smoke.json_command(['-m', 'traceforge', 'benchmark', '--mode', 'oracle',
                                            '--case', 'unit_conversion'], 'benchmark')
            metrics = benchmark['metrics']
            require(benchmark['mode'] == 'oracle' and benchmark['status'] == 'completed', 'Oracle benchmark did not complete')
            require(metrics['oracle_verifier_correctness']['numerator'] ==
                    metrics['oracle_verifier_correctness']['denominator'] == 1, 'Oracle benchmark did not pass 1/1')
            require(metrics['provider_calls'] == 0 and metrics['functional_correctness']['denominator'] == 0,
                    'Oracle benchmark incorrectly reports model capability')
            require(all(case['source_unchanged'] for case in benchmark['cases']), 'Benchmark modified a source fixture')
            report['benchmark'] = {key: benchmark[key] for key in ('mode', 'status', 'selected_case_ids', 'metrics')}
            after = smoke.json_command(['-c', PROBE], 'probe-after', clean_env=True)
            require(after['resource_sha256'] == before['resource_sha256'], 'Installed resources were modified')
            report['resources_unchanged'] = True
            report['status'] = 'passed'
    except Exception as exc:  # noqa: BLE001 - always leave a machine-readable failure report
        report['error'] = f'{type(exc).__name__}: {exc}'[:2500]
    report['duration_ms'] = round((time.monotonic() - started) * 1000)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'report': str(args.report.absolute()),
                      'duration_ms': report['duration_ms'], 'error': report.get('error')}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
