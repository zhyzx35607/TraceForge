import sys
from pathlib import Path

import pytest

from traceforge.config import Settings
from traceforge.runner import (
    VerificationCancelled,
    VerificationError,
    _docker_command,
    assess_regression,
    clean_environment,
    observed_command,
    parse_test_counts,
    repository_manifest,
    run_verification,
    validate_patch,
)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'main.py').write_text('def answer():\n    return 1\n', encoding='utf-8')
    (root / 'test_main.py').write_text(
        'import unittest\nfrom main import answer\nclass Test(unittest.TestCase):\n'
        '    def test_answer(self): self.assertEqual(answer(), 2)\n', encoding='utf-8')
    settings = Settings(data_dir=tmp_path / 'state', allowed_roots=[root], test_timeout=5)
    project = {'root': str(root), 'runner': 'local', 'trusted': True,
               'test_command': [sys.executable, '-m', 'unittest', 'discover', '-v']}
    patch = {'path': 'main.py', 'before': 'return 1', 'after': 'return 2',
             'reason': 'Fix regression', 'evidence_ids': ['source']}
    return root, settings, project, patch


def test_real_baseline_and_patch_never_modify_source(repository):
    root, settings, project, patch = repository
    original = (root / 'main.py').read_bytes()
    (root / '.env').write_text('PRIVATE_TOKEN=hidden')
    result = run_verification(project, [patch], settings, 'demo')
    assert result['baseline']['exit_code'] == 1
    assert result['patched']['exit_code'] == 0
    assert result['regression_fixed']
    assert {key: result['baseline']['tests'][key] for key in ['framework', 'executed', 'passed', 'failed', 'skipped']} == {
        'framework': 'unittest', 'executed': 1, 'passed': 0, 'failed': 1, 'skipped': 0}
    assert result['baseline']['tests']['source'] == 'external_observer'
    assert result['patched']['tests']['passed'] == 1
    assert '-    return 1' in result['diff']
    assert Path(result['artifact_path']).is_file()
    assert (root / 'main.py').read_bytes() == original
    assert '.env' not in result['source_manifest']
    assert not list((settings.data_dir / 'runs' / 'demo').glob('verification-*'))


def test_green_baseline_is_not_proof_of_fix(repository):
    _root, settings, project, patch = repository
    project['test_command'] = [sys.executable, '-c', 'print("always green")']
    result = run_verification(project, [patch], settings, 'green')
    assert result['baseline']['exit_code'] == result['patched']['exit_code'] == 0
    assert result['regression_fixed'] is False


@pytest.mark.parametrize('path', ['../main.py', '/main.py', 'C:\\main.py', '.env',
                                  'test_main.py', 'tests/main.py', 'package-lock.json', '.git/config', 'uv.lock',
                                  'conftest.py', 'pyproject.toml', 'package.json', 'vitest.config.ts', 'sitecustomize.py'])
def test_rejects_unsafe_or_test_patches(repository, path):
    root, _settings, _project, patch = repository
    with pytest.raises(VerificationError):
        validate_patch(root, {**patch, 'path': path})


def test_rejects_untrusted_and_changed_source(repository):
    root, settings, project, patch = repository
    with pytest.raises(VerificationError, match='trusted'):
        run_verification({**project, 'trusted': False}, [patch], settings, 'untrusted')
    project['_expected_manifest'] = repository_manifest(root, settings)
    (root / 'test_main.py').write_text('changed after approval')
    with pytest.raises(VerificationError, match='审批后'):
        run_verification(project, [patch], settings, 'changed')


def test_docker_missing_never_falls_back(repository, monkeypatch):
    _, settings, project, patch = repository
    monkeypatch.setattr('traceforge.runner.shutil.which', lambda _: None)
    with pytest.raises(VerificationError, match='Docker'):
        run_verification({**project, 'runner': 'docker'}, [patch], settings, 'docker')


def test_docker_sandbox_configuration(repository):
    root, settings, project, _ = repository
    command = _docker_command(project, root, settings, 'unit')
    for option in ['--read-only', '--cap-drop', '--security-opt', '--network', '--memory', '--cpus', '--pids-limit']:
        assert option in command
    assert command[command.index('--network') + 1] == 'none'
    assert command[command.index('--cap-drop') + 1] == 'ALL'
    assert '--privileged' not in command


def test_clean_environment_and_bounded_output(repository, monkeypatch):
    root, settings, project, patch = repository
    monkeypatch.setenv('TRACEFORGE_API_KEY', 'private-test-key')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'private-cloud-key')
    monkeypatch.setenv('PYTHONPATH', 'malicious-path')
    environment = clean_environment(root)
    assert not {'TRACEFORGE_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'PYTHONPATH'} & environment.keys()
    project['test_command'] = [sys.executable, '-c', 'print("x" * 100000)']
    result = run_verification(project, [patch], settings, 'output')
    assert len(result['baseline']['stdout']) < 25000
    assert '[output truncated]' in result['baseline']['stdout']


def test_timeout_and_cancellation(repository):
    _, settings, project, patch = repository
    settings.test_timeout = 0.15
    project['test_command'] = [sys.executable, '-c', 'import time;time.sleep(20)']
    with pytest.raises(VerificationError, match='超时'):
        run_verification(project, [patch], settings, 'timeout')
    with pytest.raises(VerificationCancelled):
        run_verification(project, [patch], settings, 'cancelled', cancelled=lambda: True)


def test_cancellation_during_process_terminates_promptly(repository):
    import time
    _, settings, project, patch = repository
    project['test_command'] = [sys.executable, '-c', 'import time;time.sleep(20)']
    started = time.monotonic()
    with pytest.raises(VerificationCancelled):
        run_verification(project, [patch], settings, 'cancel-process',
                         cancelled=lambda: time.monotonic() - started > 0.2)
    assert time.monotonic() - started < 5


def test_cannot_patch_test_command_entrypoint(repository):
    _, settings, project, patch = repository
    project['test_command'] = [sys.executable, 'main.py']
    with pytest.raises(VerificationError, match='test_command'):
        run_verification(project, [patch], settings, 'entrypoint')


def test_symlink_patch_rejected(repository):
    root, _settings, _project, patch = repository
    try:
        (root / 'linked.py').symlink_to(root / 'main.py')
    except OSError:
        pytest.skip('OS does not permit unprivileged symlinks')
    with pytest.raises(VerificationError):
        validate_patch(root, {**patch, 'path': 'linked.py'})


def test_pytest_counts_and_fewer_tests_cannot_prove_fix():
    before = {'exit_code': 1, 'stdout': '=== 2 failed, 3 passed, 1 skipped in 0.12s ===', 'stderr': ''}
    after = {'exit_code': 0, 'stdout': '=== 1 passed, 1 skipped in 0.03s ===', 'stderr': ''}
    before['tests'], after['tests'] = parse_test_counts(before), parse_test_counts(after)
    assert before['tests'] == {'framework': 'pytest', 'executed': 6, 'passed': 3, 'failed': 2, 'skipped': 1}
    fixed, limits = assess_regression({'baseline': before, 'patched': after})
    assert not fixed
    assert any('数量减少' in item for item in limits)


def test_additional_skips_and_zero_tests_cannot_prove_fix():
    before = {'exit_code': 1, 'stdout': '', 'stderr': 'Ran 2 tests in 0.1s\n\nFAILED (failures=1)'}
    after = {'exit_code': 0, 'stdout': '', 'stderr': 'Ran 2 tests in 0.1s\n\nOK (skipped=1)'}
    before['tests'], after['tests'] = parse_test_counts(before), parse_test_counts(after)
    assert not assess_regression({'baseline': before, 'patched': after})[0]
    after['stderr'] = 'Ran 0 tests in 0.1s\n\nOK'
    after['tests'] = parse_test_counts(after)
    assert after['tests']['executed'] == 0
    assert not assess_regression({'baseline': before, 'patched': after})[0]


def test_unknown_framework_requires_count_evidence():
    results = {'baseline': {'exit_code': 1, 'tests': None}, 'patched': {'exit_code': 0, 'tests': None}}
    fixed, limitations = assess_regression(results)
    assert not fixed
    assert any('观察报告' in item for item in limitations)


def test_forged_stdout_and_os_exit_cannot_prove_fix(repository):
    root, settings, project, patch = repository
    original = (root / 'main.py').read_text()
    patch = {**patch, 'before': original,
             'after': 'import os\nprint("Ran 100 tests in 0.01s\\n\\nOK", flush=True)\nos._exit(0)\n' + original}
    result = run_verification(project, [patch], settings, 'spoof')
    assert result['baseline']['tests']['failed'] == 1
    assert result['patched']['exit_code'] == 0
    assert result['patched']['output_summary']['executed'] == 100
    assert result['patched']['tests'] is None
    assert result['patched']['observation_status'] == 'missing_or_invalid'
    assert not result['regression_fixed']
    assert (root / 'main.py').read_text() == original


@pytest.mark.parametrize('style', ['python-module', 'pytest-command'])
def test_pytest_observer_reports_real_outcomes(repository, style):
    _root, settings, project, patch = repository
    # This verifies observer outcomes, not cold-start performance. Windows
    # interpreter/plugin startup was measured near the old five-second cap;
    # test_timeout_and_cancellation separately enforces the short hard limit.
    settings.test_timeout = 15
    project['test_command'] = ([sys.executable, '-m', 'pytest', '-q'] if style == 'python-module'
                               else ['pytest', '-q'])
    result = run_verification(project, [patch], settings, 'pytest-' + style)
    assert result['regression_fixed'], result['limitations']
    assert result['baseline']['tests']['framework'] == 'pytest'
    assert result['baseline']['tests']['identities'] == result['patched']['tests']['identities']
    assert result['patched']['tests']['completed']


def test_test_identity_removal_rejected_even_if_count_increases():
    before = {'framework': 'unittest', 'source': 'external_observer', 'completed': True,
              'executed': 1, 'passed': 0, 'failed': 1, 'skipped': 0, 'identities': ['original.test'],
              'outcomes': {'original.test': 'failed'}}
    after = {'framework': 'unittest', 'source': 'external_observer', 'completed': True,
             'executed': 2, 'passed': 2, 'failed': 0, 'skipped': 0, 'identities': ['replacement.one', 'replacement.two'],
             'outcomes': {'replacement.one': 'passed', 'replacement.two': 'passed'}}
    fixed, limitations = assess_regression({'baseline': {'exit_code': 1, 'tests': before},
                                           'patched': {'exit_code': 0, 'tests': after}})
    assert not fixed
    assert any('身份集合' in item for item in limitations)


def test_docker_observer_is_readonly_with_separate_report_mount(repository):
    root, settings, project, _patch = repository
    command = _docker_command(project, root, settings, 'unit', observer_dir=root / 'observer', reports_dir=root / 'reports')
    assert any('target=/traceforge-observer,readonly' in argument for argument in command)
    assert any('target=/traceforge-reports' in argument for argument in command)
    wrapped, framework = observed_command(['python', '-m', 'unittest', 'discover'],
                                          '/traceforge-observer/observer.py', '/traceforge-reports/baseline.json', docker=True)
    assert framework == 'unittest'
    assert wrapped[:4] == ['python', '-I', '-B', '/traceforge-observer/observer.py']


def test_unknown_command_runs_but_cannot_use_stdout_to_verify(repository):
    _root, settings, project, patch = repository
    project['test_command'] = [sys.executable, '-c', 'print("Ran 8 tests in 0.1s\\n\\nOK")']
    result = run_verification(project, [patch], settings, 'unsupported')
    assert result['patched']['exit_code'] == 0
    assert result['patched']['output_summary']['executed'] == 8
    assert result['patched']['tests'] is None
    assert result['patched']['observation_status'] == 'unsupported_command'
    assert not result['regression_fixed']
