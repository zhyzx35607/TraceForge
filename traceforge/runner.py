from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath

from .retrieval import is_denied_path, redact, safe_path

OUTPUT_LIMIT = 24000
DIFF_LIMIT = 100000
REPORT_LIMIT = 500_000
OBSERVER_LIMITATION = '测试报告来自仓库外观察器；同一 Python 进程内的反射或 monkey-patching 不构成密码学防篡改保证。'
IMPLEMENTATION_SUFFIXES = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.mjs', '.cjs', '.go', '.rs', '.java', '.cs',
    '.c', '.cc', '.cpp', '.h', '.hpp', '.rb', '.php', '.swift', '.kt', '.vue', '.svelte',
    '.sql', '.sh', '.html', '.css', '.scss', '.ex', '.exs',
}


class VerificationError(ValueError):
    def __init__(self, message: str, *, fault_type: str = 'validation'):
        super().__init__(message)
        self.fault_type = fault_type


class VerificationCancelled(VerificationError):
    pass


def _linked(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def validate_patch(root: Path, patch: dict) -> Path:
    raw = patch.get('path', '')
    normalized = raw.replace('\\', '/')
    parts = PurePosixPath(normalized).parts
    if (not raw or '\x00' in raw or ':' in raw or PureWindowsPath(raw).is_absolute()
            or PurePosixPath(normalized).is_absolute() or '..' in parts or not parts):
        raise VerificationError('补丁路径必须是仓库内的相对路径')
    lower = [x.casefold() for x in parts]
    name = lower[-1]
    if (any(x in {'test', 'tests', '__tests__', 'spec', 'specs', '__snapshots__'} for x in lower)
            or name.startswith(('test_', 'test.'))
            or re.search(r'(^|[._-])(test|spec)([._-]|$)', name)
            or name.endswith(('_test.py', '_test.go', '.lock', '-lock.json', '.snap'))
            or name in {'poetry.lock', 'uv.lock', 'requirements.lock', 'cargo.lock', 'go.sum',
                        'conftest.py', 'setup.py', 'sitecustomize.py', 'usercustomize.py', 'pytest.py', 'unittest.py'}
            or any(name.startswith(prefix) for prefix in ('jest.config.', 'vitest.config.',
                                                          'playwright.config.', 'karma.conf.'))):
        raise VerificationError('禁止修改测试、快照断言或锁文件')
    if Path(name).suffix not in IMPLEMENTATION_SUFFIXES:
        raise VerificationError('补丁必须修改支持的实现或配置文件')
    try:
        target = safe_path(root, raw)
    except (ValueError, OSError) as exc:
        raise VerificationError(f'不安全的补丁路径：{raw}') from exc
    before, after = patch.get('before'), patch.get('after')
    if not isinstance(before, str) or not before or not isinstance(after, str) or before == after:
        raise VerificationError('补丁必须包含不同的非空原文与替换文本')
    if max(len(before), len(after)) > 30000 or '\x00' in before + after:
        raise VerificationError('补丁内容超出限制')
    if redact(before) != before or redact(after) != after:
        raise VerificationError('禁止在补丁中读取或写入凭据内容')
    text = target.read_bytes().decode('utf-8')
    # RepositoryIndex returns universal newlines; normalize for exact matching across OSes.
    text = text.replace('\r\n', '\n')
    if text.count(before) != 1:
        raise VerificationError(f'{raw} 中待替换文本必须恰好出现一次')
    return target


def repository_manifest(root: Path, settings) -> dict[str, str]:
    root = root.resolve(strict=True)
    manifest = {}
    for directory, dirs, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if not is_denied_path((base / d).relative_to(root))
                         and not _linked(base / d))
        for name in sorted(filenames):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if is_denied_path(relative) or _linked(path) or not path.is_file():
                continue
            if path.stat().st_size > settings.max_file_bytes:
                raise VerificationError(f'快照文件过大：{relative}；请调整边界或明确排除该文件')
            data = path.read_bytes()
            if b'\x00' in data:
                continue
            try:
                data.decode('utf-8')
            except UnicodeDecodeError:
                continue
            manifest[relative] = hashlib.sha256(data).hexdigest()
            if len(manifest) > settings.max_files:
                raise VerificationError('仓库超过快照文件数量预算')
    return manifest


def _snapshot(root: Path, destination: Path, manifest: dict[str, str]) -> None:
    for relative, expected in manifest.items():
        source = safe_path(root, relative)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise VerificationError(f'审批后源文件已改变：{relative}')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(stat.S_IMODE(source.stat().st_mode) | stat.S_IRUSR | stat.S_IWUSR)


def clean_environment(workspace: Path) -> dict[str, str]:
    # Do not inherit tokens, proxy credentials, cloud credentials or Python injection settings.
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'SYSTEMDRIVE'}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    home = workspace / '.traceforge-home'
    home.mkdir(exist_ok=True)
    result.update({'HOME': str(home), 'USERPROFILE': str(home), 'TMP': str(home), 'TEMP': str(home),
                   'TMPDIR': str(home), 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1', 'LANG': 'C.UTF-8',
                   'LC_ALL': 'C.UTF-8', 'CI': '1', 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'})
    return result


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                           timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def _execute(command: list[str], workspace: Path, timeout: float,
             cancelled: Callable[[], bool]) -> dict:
    started = time.monotonic()
    streams: dict[str, bytearray] = {'stdout': bytearray(), 'stderr': bytearray()}
    truncated = {'stdout': False, 'stderr': False}
    process = subprocess.Popen(command, cwd=workspace, env=clean_environment(workspace), shell=False,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=os.name != 'nt',
                               creationflags=(getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                                              | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
                               if os.name == 'nt' else 0)

    def drain(name, stream):
        while data := stream.read(4096):
            capacity = OUTPUT_LIMIT - len(streams[name])
            streams[name].extend(data[:max(0, capacity)])
            truncated[name] |= len(data) > capacity
        stream.close()

    readers = [threading.Thread(target=drain, args=(name, getattr(process, name)), daemon=True)
               for name in streams]
    for reader in readers:
        reader.start()
    timed_out = was_cancelled = False
    try:
        while process.poll() is None:
            was_cancelled = cancelled()
            timed_out = time.monotonic() - started >= timeout
            if timed_out or was_cancelled:
                _kill_tree(process)
                break
            time.sleep(0.04)
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            _kill_tree(process)
        for reader in readers:
            reader.join(timeout=2)
    if was_cancelled:
        raise VerificationCancelled('验证已取消')
    result = {name: redact(bytes(value).decode('utf-8', errors='replace'))[:OUTPUT_LIMIT]
              + ('\n[output truncated]' if truncated[name] else '') for name, value in streams.items()}
    result.update(exit_code=124 if timed_out else process.returncode,
                  duration_ms=round((time.monotonic() - started) * 1000), timed_out=timed_out)
    if timed_out:
        result['stderr'] += '\n测试执行超时，已终止进程树。'
    return result


def _docker_command(project: dict, snapshot: Path, settings, name: str, *,
                    observer_dir: Path | None = None, reports_dir: Path | None = None) -> list[str]:
    mounts = []
    if observer_dir is not None:
        mounts.extend(['--mount', f'type=bind,source={observer_dir.resolve()},target=/traceforge-observer,readonly'])
    if reports_dir is not None:
        mounts.extend(['--mount', f'type=bind,source={reports_dir.resolve()},target=/traceforge-reports'])
    return ['docker', 'run', '--rm', '--name', name, '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '512m',
            '--memory-swap', '512m', '--cpus', '1', '--pids-limit', '128', '--user', '65534:65534',
            '--mount', f'type=bind,source={snapshot.resolve()},target=/workspace',
            *mounts,
            '--workdir', '/workspace', '--env', 'HOME=/workspace/.traceforge-home',
            '--env', 'TMPDIR=/workspace/.traceforge-home', '--env', 'PYTHONDONTWRITEBYTECODE=1',
            '--env', 'PYTHONNOUSERSITE=1', '--env', 'PYTHONUNBUFFERED=1', settings.docker_image,
            *project['test_command']]


def observed_command(command: list[str], observer: str, report: str, *, docker: bool = False) -> tuple[list[str], str | None]:
    executable = Path(command[0]).name.casefold()
    python_name = bool(re.fullmatch(r'(?:python|python3|python3\.\d+|pypy3?)(?:\.exe)?', executable))
    if python_name and len(command) >= 3 and command[1] == '-m' and command[2] in {'unittest', 'pytest'}:
        framework, interpreter, arguments = command[2], command[0], command[3:]
    elif executable in {'pytest', 'pytest.exe', 'py.test', 'py.test.exe'}:
        framework, arguments = 'pytest', command[1:]
        if docker:
            interpreter = 'python'
        elif Path(command[0]).is_absolute():
            sibling = Path(command[0]).with_name('python.exe' if os.name == 'nt' else 'python')
            interpreter = str(sibling) if sibling.is_file() else sys.executable
        else:
            interpreter = sys.executable
    else:
        return command, None
    return [interpreter, '-I', observer, framework, report, *arguments], framework


def read_observation(report_path: Path, framework: str, result: dict) -> dict | None:
    try:
        if _linked(report_path) or report_path.stat().st_size > REPORT_LIMIT:
            return None
        with report_path.open('rb') as stream:
            raw = stream.read(REPORT_LIMIT + 1)
        if len(raw) > REPORT_LIMIT:
            return None
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get('schema') != 1 or data.get('completed') is not True:
        return None
    if data.get('framework') != framework or data.get('exit_code') != result['exit_code']:
        return None
    entries = data.get('tests')
    if not isinstance(entries, list) or len(entries) > 2500:
        return None
    if any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
           or not item['id'] or len(item['id']) > 500 or item.get('outcome') not in {'passed', 'failed', 'skipped'}
           for item in entries):
        return None
    identities = [item['id'] for item in entries]
    if len(set(identities)) != len(identities):
        return None
    counts = Counter(item['outcome'] for item in entries)
    if data.get('executed') != len(entries) or any(data.get(name) != counts[name] for name in ('passed', 'failed', 'skipped')):
        return None
    return {'framework': framework, 'completed': True, 'source': 'external_observer',
            'executed': len(entries), 'passed': counts['passed'], 'failed': counts['failed'], 'skipped': counts['skipped'],
            'identities': sorted(identities),
            'outcomes': {item['id']: item['outcome'] for item in entries}}


def parse_test_counts(result: dict) -> dict | None:
    """Parse recorded framework summaries; never infer test counts from exit status."""
    output = result.get('stdout', '') + '\n' + result.get('stderr', '')
    output = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', output)
    matches = list(re.finditer(r'(?m)^Ran (\d+) tests? in [^\r\n]+', output))
    if matches:
        summary = output[matches[-1].end():]
        executed = int(matches[-1].group(1))
        counts = {key: int(value) for key, value in re.findall(
            r'(?:[,(])\s*(failures|errors|skipped|expected failures|unexpected successes)=(\d+)', summary)}
        failed = counts.get('failures', 0) + counts.get('errors', 0) + counts.get('unexpected successes', 0)
        skipped = counts.get('skipped', 0) + counts.get('expected failures', 0)
        if not re.search(r'(?m)^(?:OK|FAILED)(?:\s|$|\()', summary):
            return None
        return {'framework': 'unittest', 'executed': executed,
                'passed': max(0, executed - failed - skipped), 'failed': failed, 'skipped': skipped}
    summaries = []
    for line in output.splitlines():
        if not re.search(r'\bin \d+(?:\.\d+)?s\b', line):
            continue
        counts = re.findall(r'\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b', line)
        if counts:
            summaries.append(counts)
    if summaries:
        counts = {name: int(value) for value, name in summaries[-1]}
        passed = counts.get('passed', 0) + counts.get('xpassed', 0)
        failed = counts.get('failed', 0) + counts.get('error', 0) + counts.get('errors', 0)
        skipped = counts.get('skipped', 0) + counts.get('xfailed', 0)
        return {'framework': 'pytest', 'executed': passed + failed + skipped,
                'passed': passed, 'failed': failed, 'skipped': skipped}
    return None


def assess_regression(results: dict) -> tuple[bool, list[str]]:
    baseline, patched = results['baseline'], results['patched']
    limits = []
    if baseline['exit_code'] == 0:
        limits.append('基线测试已经通过，不能证明本次补丁修复了回归。')
    elif baseline['exit_code'] in {124, 125, 126, 127, None}:
        limits.append('基线属于执行器或命令启动失败，不能作为回归证据。')
    if patched['exit_code'] != 0:
        limits.append('补丁后测试未通过。')
    before, after = baseline.get('tests'), patched.get('tests')
    if not before or not after:
        limits.append('无法确认完整的仓库外 unittest/pytest 观察报告；stdout 与退出码不足以确认修复。')
    else:
        if any(item.get('source') != 'external_observer' or item.get('completed') is not True for item in (before, after)):
            limits.append('缺少完整的仓库外观察报告；stdout 解析只用于展示。')
        if before['framework'] != after['framework']:
            limits.append('前后测试框架不一致。')
        if before['failed'] <= 0:
            limits.append('基线输出未记录实际失败的测试。')
        if after['executed'] <= 0 or after['passed'] <= 0:
            limits.append('补丁后没有实际通过的测试。')
        if after['executed'] < before['executed']:
            limits.append('补丁后执行的测试数量减少，不能确认完整回归验证。')
        if after['skipped'] > before['skipped']:
            limits.append('补丁后跳过的测试数量增加，不能确认修复。')
        if after['failed']:
            limits.append('补丁后输出仍包含失败测试。')
        if not set(before.get('identities', [])) <= set(after.get('identities', [])):
            limits.append('补丁后测试身份集合减少或改变，不能确认原测试已回归验证。')
        if any(before.get('outcomes', {}).get(identity) != 'skipped'
               and after.get('outcomes', {}).get(identity) == 'skipped' for identity in before.get('identities', [])):
            limits.append('原先执行的测试在补丁后被跳过。')
    return not limits, limits


def run_verification(project: dict, patches: list[dict], settings, run_id: str, *,
                     progress: Callable[[str, dict], None] | None = None,
                     cancelled: Callable[[], bool] | None = None) -> dict:
    progress = progress or (lambda kind, payload: None)
    cancelled = cancelled or (lambda: False)
    root = Path(project['root']).resolve(strict=True)
    settings.validate_root(str(root))
    command = project.get('test_command')
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x or '\x00' in x for x in command):
        raise VerificationError('test_command 必须为非空参数数组')
    runner = project.get('runner', 'docker')
    if runner == 'local' and project.get('trusted') is not True:
        raise VerificationError('local runner 仅允许显式 trusted 的仓库')
    if runner not in {'local', 'docker'}:
        raise VerificationError('未知 runner')
    if not patches:
        raise VerificationError('没有待验证补丁')
    for patch in patches:
        validate_patch(root, patch)
        if any(arg.replace('\\', '/').removeprefix('./') == patch['path'].replace('\\', '/')
               for arg in command):
            raise VerificationError('禁止修改 test_command 直接执行的文件')
    if cancelled():
        raise VerificationCancelled('验证已取消')
    manifest = repository_manifest(root, settings)
    expected = project.get('_expected_manifest')
    if expected is not None and manifest != expected:
        raise VerificationError('审批后仓库文件已改变；请创建新的调查并重新审批')
    if runner == 'docker':
        if shutil.which('docker') is None:
            raise VerificationError('Docker 不可用；请安装并启动 Docker，或为可信项目显式选择 local runner')
        try:
            available = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                                       capture_output=True, timeout=8, check=False,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VerificationError('Docker daemon 不可用；请启动 Docker 后重试') from exc
        if available.returncode:
            raise VerificationError('Docker daemon 不可用；不会回退至本地执行')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id):
        raise VerificationError('无效的运行标识符')
    artifact_dir = settings.data_dir / 'runs' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    diff_parts = []
    with tempfile.TemporaryDirectory(prefix='verification-', dir=artifact_dir) as temporary:
        temporary = Path(temporary)
        baseline_root, patched_root = temporary / 'baseline', temporary / 'patched'
        observer_dir, reports_dir = temporary / 'observer', temporary / 'reports'
        observer_dir.mkdir()
        reports_dir.mkdir()
        observer_path = observer_dir / 'observer.py'
        observer_path.write_bytes(Path(__file__).with_name('test_observer.py').read_bytes())
        observer_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        if os.name != 'nt':
            observer_dir.chmod(0o755)
            reports_dir.chmod(0o777)
        baseline_root.mkdir()
        patched_root.mkdir()
        _snapshot(root, baseline_root, manifest)
        _snapshot(root, patched_root, manifest)
        for patch in patches:
            target = validate_patch(patched_root, patch)
            original_bytes = target.read_bytes()
            original = original_bytes.decode('utf-8').replace('\r\n', '\n')
            changed = original.replace(patch['before'], patch['after'], 1)
            diff_parts.extend(difflib.unified_diff(original.splitlines(keepends=True),
                                                 changed.splitlines(keepends=True),
                                                 fromfile='a/' + patch['path'], tofile='b/' + patch['path']))
            newline = '\r\n' if b'\r\n' in original_bytes else '\n'
            target.write_bytes(changed.replace('\n', newline).encode('utf-8'))
        diff = ''.join(diff_parts)
        if len(diff) > DIFF_LIMIT:
            raise VerificationError('补丁差异超过审计大小限制')
        artifact = artifact_dir / 'patch.diff'
        artifact.write_text(redact(diff), encoding='utf-8')
        results = {}
        for phase, snapshot in [('baseline', baseline_root), ('patched', patched_root)]:
            if cancelled():
                raise VerificationCancelled('验证已取消')
            container = 'traceforge-' + uuid.uuid4().hex
            clean_environment(snapshot)
            if runner == 'docker' and os.name != 'nt':
                for directory, dirs, files in os.walk(snapshot):
                    Path(directory).chmod(0o777)
                    for filename in files:
                        path = Path(directory) / filename
                        path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o666)
            report_path = reports_dir / (phase + '.json')
            wrapped, framework = observed_command(command,
                '/traceforge-observer/observer.py' if runner == 'docker' else str(observer_path),
                '/traceforge-reports/' + report_path.name if runner == 'docker' else str(report_path),
                docker=runner == 'docker')
            actual = wrapped if runner == 'local' else _docker_command({**project, 'test_command': wrapped},
                snapshot, settings, container, observer_dir=observer_dir if framework else None,
                reports_dir=reports_dir if framework else None)
            progress('verification.started', {'phase': phase, 'runner': runner, 'command': command,
                                              'observation': framework or 'unsupported'})
            try:
                results[phase] = _execute(actual, snapshot, settings.test_timeout, cancelled)
            finally:
                if runner == 'docker':
                    subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=10,
                                   check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            results[phase]['tests'] = read_observation(report_path, framework, results[phase]) if framework else None
            results[phase]['output_summary'] = parse_test_counts(results[phase])
            results[phase]['observation_status'] = ('completed' if results[phase]['tests'] else
                                                    'missing_or_invalid' if framework else 'unsupported_command')
            progress('verification.result', {'phase': phase, **results[phase]})
            if results[phase].get('timed_out'):
                raise VerificationError(f'{phase} 测试超时；不能据此判断修复有效', fault_type='timeout')
        # Windows cannot unlink a read-only file during TemporaryDirectory cleanup.
        # Only the trusted observer copy is made writable after both processes exit.
        observer_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    fixed, limitations = assess_regression(results)
    return {**results, 'regression_fixed': fixed, 'artifact_path': str(artifact), 'diff': redact(diff),
            'runner': runner, 'source_manifest': manifest,
            'confidence': 'high' if fixed else 'low', 'limitations': [*limitations, OBSERVER_LIMITATION]}
