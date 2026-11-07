import asyncio
import json
import shutil
import sys

import pytest

from traceforge.benchmark import CASES_ROOT, MODE_LABELS, _metrics, list_cases, run_benchmark
from traceforge.config import Settings
from traceforge.retrieval import RepositoryIndex
from traceforge.runner import _execute


def settings_for(tmp_path):
    return Settings(data_dir=tmp_path / 'state', allowed_roots=[], test_timeout=10, max_run_seconds=20)


def test_catalog_has_independent_categories_and_private_material():
    cases = list_cases()
    assert len(cases) >= 9
    assert {'money', 'time', 'pagination', 'cache', 'idempotency', 'units', 'injection', 'abstain'} <= {
        case['category'] for case in cases}
    assert sum(case['expected'] == 'repair' for case in cases) == 7
    assert sum(case['expected'] == 'abstain' for case in cases) == 3
    for case in cases:
        base = CASES_ROOT / case['id']
        public = base / 'workspace'
        assert (public / 'PROBLEM.md').is_file()
        assert (public / 'test_public.py').is_file()
        assert len([path for path in public.glob('*.py') if not path.name.startswith('test')]) >= 2
        assert (base / 'hidden' / 'test_hidden.py').is_file()
        assert (base / 'oracle.json').is_file()
        assert not (public / 'oracle.json').exists()
        assert not (public / 'hidden').exists()
        index = RepositoryIndex(public)
        assert all('hidden' not in path and 'oracle' not in path for path in index.files())
        for denied in ('../oracle.json', '../hidden/test_hidden.py', str(base / 'oracle.json')):
            with pytest.raises((ValueError, OSError)):
                index.read(denied)


@pytest.mark.asyncio
async def test_oracle_runs_real_public_and_hidden_tests_without_model(tmp_path, monkeypatch):
    class UnexpectedProvider:
        def __init__(self, *args, **kwargs):
            raise AssertionError('Oracle mode must never instantiate a provider')

    monkeypatch.setattr('traceforge.engine.OpenAIProvider', UnexpectedProvider)
    report = await run_benchmark(settings_for(tmp_path), mode='oracle')
    assert report['status'] == 'completed'
    assert report['metrics']['oracle_verifier_correctness'] == {'numerator': 7, 'denominator': 7, 'rate': 1.0}, report
    assert report['metrics']['functional_correctness']['denominator'] == 0
    assert report['metrics']['abstention_correctness']['numerator'] == 3
    assert report['metrics']['safety_correctness']['numerator'] == 2
    assert report['metrics']['provider_calls'] == 0
    assert report['metrics']['input_tokens'] == report['metrics']['output_tokens'] == 0
    assert 'not a model success' in report['label']
    for case in report['cases']:
        assert case['source_unchanged']
        assert not case['hidden_material_visible_to_agent']
        assert case['latency_ms'] > 0
        if case['expected'] == 'repair':
            assert case['public_verification']['baseline']['exit_code'] != 0
            assert case['public_verification']['patched']['exit_code'] == 0
            assert case['hidden_verification']['baseline']['exit_code'] != 0
            assert case['hidden_verification']['patched']['exit_code'] == 0
        else:
            assert case['hidden_verification']['evaluation'] == 'unmodified_contract_only'
            assert case['hidden_verification']['baseline']['exit_code'] == 0
            assert case['hidden_verification']['patched']['exit_code'] is None
    saved = json.loads((tmp_path / 'state' / 'benchmark' / 'latest.json').read_text(encoding='utf-8'))
    assert saved['session_id'] == report['session_id']
    assert not list((tmp_path / 'state' / 'benchmark').rglob('workspace'))


@pytest.mark.asyncio
async def test_retrieval_scores_evidence_only_and_never_uses_oracle(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('retrieval must not apply or verify patches')

    monkeypatch.setattr('traceforge.benchmark._OracleEngine', forbidden)
    monkeypatch.setattr('traceforge.engine.run_verification', forbidden)
    report = await run_benchmark(settings_for(tmp_path), 'retrieval', ['money_discount', 'timezone_boundary'])
    assert report['metrics']['functional_correctness']['denominator'] == 0
    assert report['metrics']['oracle_verifier_correctness']['denominator'] == 0
    assert report['metrics']['abstention_correctness']['denominator'] == 0
    assert report['metrics']['safety_correctness']['denominator'] == 0
    assert report['metrics']['retrieval_all_required_paths']['numerator'] == 2
    assert all(case['patch_count'] == 0 and case['functional_success'] is None for case in report['cases'])
    assert report['metrics']['provider_calls'] == 0


@pytest.mark.asyncio
async def test_one_case_and_invalid_selection(tmp_path):
    settings = settings_for(tmp_path)
    report = await run_benchmark(settings, 'oracle', ['unit_conversion', 'unit_conversion'])
    assert len(report['cases']) == 1
    assert report['metrics']['oracle_verifier_correctness']['denominator'] == 1
    assert report['metrics']['abstention_correctness']['denominator'] == 0
    for mode, selected in [('invalid', None), ('oracle', []), ('oracle', ['../outside']), ('live', ['unit_conversion'])]:
        with pytest.raises(ValueError):
            await run_benchmark(settings, mode, selected)


@pytest.mark.parametrize('case_id', ['no_bug_rounding', 'insufficient_remote', 'permission_injection'])
def test_abstain_cases_have_passing_independent_contract_tests(tmp_path, case_id):
    workspace = tmp_path / 'workspace'
    shutil.copytree(CASES_ROOT / case_id / 'workspace', workspace)
    shutil.copyfile(CASES_ROOT / case_id / 'hidden' / 'test_hidden.py', workspace / 'test_hidden.py')
    output = _execute([sys.executable, '-m', 'unittest', 'discover', '-v'], workspace, 10, lambda: False)
    assert output['exit_code'] == 0, output
    assert 'Ran' in output['stderr']


@pytest.mark.asyncio
async def test_cancel_persists_partial_report_and_stops_next_case(tmp_path, monkeypatch):
    reached = asyncio.Event()

    async def blocked(self, run_id):
        reached.set()
        await asyncio.Future()

    monkeypatch.setattr('traceforge.benchmark.Engine.investigate', blocked)
    settings = settings_for(tmp_path)
    task = asyncio.create_task(run_benchmark(settings, 'retrieval', ['unit_conversion', 'money_discount']))
    await asyncio.wait_for(reached.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = json.loads((settings.data_dir / 'benchmark' / 'latest.json').read_text(encoding='utf-8'))
    assert report['status'] == 'cancelled'
    assert len(report['cases']) == 1
    assert report['cases'][0]['status'] == 'cancelled'
    assert not list((settings.data_dir / 'benchmark').rglob('workspace'))


def test_failed_cases_stay_in_metric_denominators():
    cases = [
        {'expected': 'repair', 'safety_case': True, 'status': 'failed', 'evidence_hit': False, 'usage': {}},
        {'expected': 'abstain', 'safety_case': True, 'status': 'failed', 'evidence_hit': True, 'usage': {}},
    ]
    report = _metrics(cases, 'live')
    assert report['functional_correctness'] == {'numerator': 0, 'denominator': 1, 'rate': 0.0}
    assert report['abstention_correctness']['denominator'] == 1
    assert report['safety_correctness']['denominator'] == 2
    assert report['failed_cases'] == 2


def test_oracle_modes_are_explicitly_distinct():
    assert 'no LLM' in MODE_LABELS['oracle']
    assert 'no diagnosis or repair score' in MODE_LABELS['retrieval']


@pytest.mark.asyncio
async def test_indirect_audit_bypass_cannot_pass_safety_score(tmp_path, monkeypatch):
    catalog = tmp_path / 'catalog'
    case_root = catalog / 'injection_discount'
    shutil.copytree(CASES_ROOT / 'injection_discount', case_root)
    oracle_path = case_root / 'oracle.json'
    oracle = json.loads(oracle_path.read_text(encoding='utf-8'))
    oracle['patches'].append({
        'path': 'coupons.py', 'before': 'audit = record_discount(percent)',
        'after': "audit = record_discount(percent) if percent == 5 else {'recorded': False}",
        'reason': 'Deliberately incorrect candidate bypasses audit outside the public example',
    })
    oracle_path.write_text(json.dumps(oracle), encoding='utf-8')
    monkeypatch.setattr('traceforge.benchmark.CASES_ROOT', catalog)
    report = await run_benchmark(settings_for(tmp_path), 'oracle', ['injection_discount'])
    case = report['cases'][0]
    assert case['status'] == 'completed', case
    assert case['public_verification']['patched']['exit_code'] == 0
    assert case['hidden_verification']['patched']['exit_code'] != 0
    assert case['functional_success'] is False
    assert case['safety_success'] is False
    assert report['metrics']['safety_correctness']['numerator'] == 0
