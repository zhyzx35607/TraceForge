import copy
import hashlib
import json
from pathlib import Path

import pytest

from traceforge.reports import build_report, render_markdown
from traceforge.store import patch_digest


def sample():
    # Recorded real Docker observer output, including identities and outcomes.
    path = Path(__file__).resolve().parents[1] / 'docs' / 'evidence' / 'docker-validation.json'
    verification = json.loads(path.read_text(encoding='utf-8'))
    # Observer execution above is recorded; this isolated unit-test provenance is synthetic.
    manifest = {'pricing.py': 'a' * 64}
    verification['source_manifest'] = dict(manifest)
    patch = {'path': 'pricing.py', 'before': 'return subtotal', 'after': 'return subtotal * quantity',
             'reason': '修复商品数量遗漏', 'evidence_ids': ['e1']}
    digest = patch_digest([patch])
    project = {'id': 'p1', 'name': 'Checkout', 'root': r'C:\private\checkout',
               'test_command': ['private-command'], 'github_repo': 'private/repo'}
    run = {'id': 'r1', 'project_id': 'p1', 'title': '数量错误', 'query': '3件商品金额错误',
           'mode': 'replay', 'status': 'completed', 'created_at': '2026-11-07T00:00:00Z',
           'updated_at': '2026-11-07T00:01:00Z', 'state': {
               'diagnosis': {'summary': '遗漏数量', 'root_cause': '只累计单价', 'confidence': 'high',
                             'limitations': ['固定样例'], 'evidence_ids': ['e1'], 'patches': [patch]},
               'evidence': [{'id': 'e1', 'path': 'pricing.py', 'source': 'repository', 'kind': 'code',
                             'line_start': 1, 'line_end': 3, 'sha256': 'a' * 64, 'content': 'private-source'}],
               'approval': {'decision': 'approve', 'patch_digest': digest, 'approved_at': '2026-11-07T00:00:30Z'},
               'patch_digest': digest, 'verification': verification, 'source_manifest': manifest,
               'usage': {'provider_calls': 0, 'input_tokens': 0, 'output_tokens': 0},
               'agent_messages': [{'content': 'private-continuation'}], 'lease': 'private-lease',
           }}
    events = [{'id': 1, 'run_id': 'r1', 'kind': 'verification.completed', 'created_at': run['updated_at'],
               'payload': {'private': 'private-event-body'}}]
    return run, project, events


def test_real_observer_structure_is_verified_and_digest_recomputes():
    run, project, events = sample()
    before = copy.deepcopy((run, project, events))
    report = build_report(run, project, events)
    assert report['verification']['verified'] is True
    assert report['verification']['regression_fixed'] is True
    assert report['verification']['baseline']['tests']['failed'] == 4
    assert report['verification']['patched']['tests']['passed'] == 8
    assert report['verification']['baseline']['tests']['valid_observer']
    assert report['approval']['patch_digest'] == run['state']['patch_digest']
    assert report['patches'][0]['before_sha256'] == hashlib.sha256(b'return subtotal').hexdigest()
    digest = report.pop('content_sha256')
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    assert hashlib.sha256(encoded.encode()).hexdigest() == digest
    assert (run, project, events) == before
    assert build_report(run, project, events)['content_sha256'] == digest


def test_snapshot_omits_all_non_allowlisted_content():
    run, project, events = sample()
    result = run['state']['verification']
    result.update(diff='private-diff', artifact_path='private-artifact', source_unchanged=True)
    for phase in ('baseline', 'patched'):
        result[phase].update(stdout='private-stdout', stderr='private-stderr', output_summary={'private': 'private-summary'})
    encoded = json.dumps(build_report(run, project, events), ensure_ascii=False)
    for value in ('private-source', 'private-continuation', 'private-lease', 'private-event-body',
                  'private-command', 'private/repo', 'private-diff', 'private-artifact',
                  'private-stdout', 'private-stderr', 'private-summary', 'return subtotal',
                  'test_pricing.PricingTests', 'agent_messages', 'artifact_path', 'output_summary'):
        assert value not in encoded


def test_partial_line_remains_explicit_in_portable_report():
    run, project, events = sample()
    run['state']['evidence'][0]['partial_line'] = True
    report = build_report(run, project, events)
    assert report['evidence'][0]['partial_line'] is True
    assert '部分行片段' in render_markdown(report)


@pytest.mark.parametrize('status', ['queued', 'investigating', 'awaiting_approval', 'verifying', 'failed', 'cancelled', 'rejected'])
def test_non_completed_run_cannot_export_verified(status):
    run, project, events = sample()
    run['status'] = status
    result = build_report(run, project, events)['verification']
    assert result['verified'] is result['regression_fixed'] is False
    assert result['reported_regression_fixed'] is True


@pytest.mark.parametrize('mutation', ['legacy', 'missing', 'reduced', 'new_skip', 'duplicate', 'inconsistent',
                                    'framework', 'timeout', 'exit_code', 'not_completed', 'approval', 'patch_changed'])
def test_incomplete_or_inconsistent_verification_is_not_verified(mutation):
    run, project, events = sample()
    state = run['state']
    patched = state['verification']['patched']
    tests = patched['tests']
    if mutation == 'legacy':
        tests['source'] = 'stdout'
    elif mutation == 'missing':
        patched.pop('tests')
    elif mutation == 'reduced':
        identity = tests['identities'].pop()
        tests['outcomes'].pop(identity)
        tests['executed'] -= 1
        tests['passed'] -= 1
    elif mutation == 'new_skip':
        tests['outcomes'][tests['identities'][0]] = 'skipped'
        tests['passed'] -= 1
        tests['skipped'] += 1
    elif mutation == 'duplicate':
        tests['identities'][-1] = tests['identities'][0]
    elif mutation == 'inconsistent':
        tests['passed'] += 1
    elif mutation == 'framework':
        tests['framework'] = 'pytest'
    elif mutation == 'timeout':
        patched['timed_out'] = True
    elif mutation == 'exit_code':
        patched['exit_code'] = True
    elif mutation == 'not_completed':
        tests['completed'] = False
    elif mutation == 'approval':
        state['approval']['patch_digest'] = 'b' * 64
    elif mutation == 'patch_changed':
        state['diagnosis']['patches'][0]['after'] = 'return altered'
    assert build_report(run, project, events)['verification']['verified'] is False


@pytest.mark.parametrize(('mode', 'label'), [('replay', '离线固定样例'), ('investigate', '仅检索调查'), ('live', '真实模型模式')])
def test_mode_labels_and_usage_limits_are_explicit(mode, label):
    run, project, events = sample()
    run['mode'] = mode
    report = build_report(run, project, events)
    assert label in report['run']['mode_label']
    assert '零值不代表零消耗' in '\n'.join(report['limitations'])
    markdown = render_markdown(report)
    assert label in markdown
    assert '不是签名或外部证明' in markdown
    assert '不能用于校验 Markdown 文件本身' in markdown
    assert '匿名化保证' in markdown
    assert '成功率：' not in markdown
    if mode == 'investigate':
        assert report['verification']['verified'] is False


def test_no_patch_completed_diagnosis_is_not_a_verified_fix():
    run, project, events = sample()
    run['state']['diagnosis']['patches'] = []
    run['state'].pop('verification')
    report = build_report(run, project, events)
    assert report['verification']['available'] is False
    assert report['verification']['verified'] is False
    assert '尚未证实回归修复' in render_markdown(report)


def test_known_secrets_and_both_root_slash_forms_are_removed_before_truncation():
    run, project, events = sample()
    secret = 'actual-private-credential'
    text = secret + ' C:\\private\\checkout/file.py C:/private/checkout/other.py'
    run.update(id=text, title=text, query='x' * 5995 + secret, created_at=text, updated_at=text)
    project.update(id=text, name=text)
    diagnosis = run['state']['diagnosis']
    diagnosis.update(summary=text, root_cause=text, confidence=text, limitations=[text], evidence_ids=[text])
    diagnosis['patches'][0].update(path=text, reason=text, evidence_ids=[text], before=secret, after=text)
    run['state']['evidence'][0].update(id=text, path=text, source=text, kind=text, sha256='a' * 64)
    run['state']['approval'].update(decision=text, approved_at=text)
    run['state']['verification'].update(runner=text, limitations=[text])
    run['state']['verification']['baseline'].update(observation_status=text)
    run['state']['verification']['baseline']['tests'].update(source=text, framework=text)
    events[0].update(kind=text, created_at=text)
    report = build_report(run, project, events, secrets=(secret, 'a' * 64))
    encoded = json.dumps(report, ensure_ascii=False)
    assert secret not in encoded and 'actual-' not in report['run']['query']
    assert 'private\\\\checkout' not in encoded and 'private/checkout' not in encoded
    assert 'a' * 64 not in encoded
    assert '[REDACTED]' in encoded and '[PROJECT_ROOT]' in encoded


def test_unknown_containers_are_not_stringified():
    run, project, events = sample()
    unknown = {'nested': 'DO_NOT_STRINGIFY'}
    run.update(title=unknown, query=[unknown], mode=unknown)
    project['name'] = unknown
    diagnosis = run['state']['diagnosis']
    diagnosis.update(summary=unknown, root_cause=[unknown], limitations=[unknown], evidence_ids=[unknown])
    diagnosis['patches'][0].update(before=unknown, after=[unknown], reason=unknown)
    run['state']['evidence'][0].update(path=unknown, source=[unknown])
    run['state']['verification']['patched']['tests']['framework'] = unknown
    events[0].update(kind=unknown)
    report = build_report(run, project, events)
    assert 'DO_NOT_STRINGIFY' not in json.dumps(report)
    assert report['run']['mode'] == 'unknown'
    assert report['verification']['verified'] is False


def test_markdown_escapes_html_images_links_reference_and_autolinks():
    run, project, events = sample()
    attack = '<img src="https://evil.example/pixel"> ![x](https://evil.example/image) [x][r]\n[r]: https://evil.example/ref'
    run['title'] = attack
    run['state']['diagnosis']['root_cause'] = '<script>alert(1)</script>\n# injected\n```html'
    run['state']['evidence'][0].update(path=attack, id=attack)
    run['state']['diagnosis']['evidence_ids'] = [attack]
    events[0]['kind'] = attack
    markdown = render_markdown(build_report(run, project, events))
    for token in ('<img', '<script', '![', '](', '[r]:', 'https://', '\n# injected', '```'):
        assert token not in markdown
    assert '&#60;img' in markdown
    assert '&#33;&#91;x&#93;' in markdown


def test_export_limits_and_external_event_truncation_are_visible():
    run, project, events = sample()
    run['state']['evidence'] *= 105
    run['state']['diagnosis']['patches'] *= 12
    events *= 1002
    report = build_report(run, project, events)
    assert len(report['evidence']) == 100 and len(report['patches']) == 10 and len(report['events']) == 1000
    assert report['events_truncated'] and report['evidence_truncated'] and report['patches_truncated']
    assert report['counts']['events_supplied'] == 1002
    assert report['counts']['events_exported'] == 1000
    assert report['verification']['verified'] is False
    assert '列表已截断' in render_markdown(report)
    assert build_report(run, project, [], events_truncated=True)['events_truncated']


def test_cited_evidence_is_retained_ahead_of_uncited_and_missing_ids_are_explicit():
    run, project, events = sample()
    evidence = run['state']['evidence'][0]
    run['state']['evidence'] = [{**evidence, 'id': f'uncited-{number}'} for number in range(150)] + [evidence]
    run['state']['diagnosis']['evidence_ids'] = ['e1', 'missing']
    report = build_report(run, project, events)
    assert report['evidence'][0]['id'] == 'e1'
    assert len(report['evidence']) == 100
    assert report['citations']['complete'] is False
    assert report['citations']['unavailable_evidence_ids'] == ['missing']
    assert '未包含或不存在的引用：missing' in render_markdown(report)


def test_too_many_cited_evidence_ids_cannot_look_complete():
    run, project, events = sample()
    evidence = run['state']['evidence'][0]
    patches = run['state']['diagnosis']['patches']
    run['state']['diagnosis']['patches'] = [{**patches[0], 'evidence_ids': [f'e{number}']}
                                          for number in range(10)]
    for index, patch in enumerate(run['state']['diagnosis']['patches']):
        patch['evidence_ids'] = [f'e{index * 30 + number}' for number in range(30)]
    run['state']['evidence'] = [{**evidence, 'id': f'e{number}'} for number in range(300)]
    report = build_report(run, project, events)
    assert len(report['evidence']) == 100
    assert len(report['citations']['unavailable_evidence_ids']) == 200
    assert report['citations']['complete'] is False


def test_unbounded_or_nonfinite_fields_do_not_break_json():
    run, project, events = sample()
    run['query'] = 'x' * 100001
    run['state']['verification']['baseline']['duration_ms'] = float('inf')
    run['state']['verification']['patched']['duration_ms'] = 10**500
    run['state']['usage']['input_tokens'] = True
    report = build_report(run, project, events)
    assert report['run']['query'] == '[字段超过导出上限]'
    assert report['verification']['baseline']['duration_ms'] is None
    assert report['verification']['patched']['duration_ms'] is None
    assert report['usage']['input_tokens'] is None
    json.dumps(report, allow_nan=False)


def reapprove(run):
    digest = patch_digest(run['state']['diagnosis']['patches'])
    run['state']['patch_digest'] = digest
    run['state']['approval']['patch_digest'] = digest


@pytest.mark.parametrize('mutation', ['missing-investigation', 'missing-verification', 'empty-investigation',
                                    'empty-verification', 'invalid-hash', 'different-hash', 'different-files',
                                    'nonmapping', 'duplicate-path', 'unsafe-path', 'invalid-unicode'])
def test_missing_or_conflicting_persisted_manifests_downgrade_old_success(mutation):
    run, project, events = sample()
    state, tested = run['state'], run['state']['verification']
    if mutation == 'missing-investigation':
        state.pop('source_manifest')
    elif mutation == 'missing-verification':
        tested.pop('source_manifest')
    elif mutation == 'empty-investigation':
        state['source_manifest'] = {}
    elif mutation == 'empty-verification':
        tested['source_manifest'] = {}
    elif mutation == 'invalid-hash':
        state['source_manifest']['pricing.py'] = 'invalid'
    elif mutation == 'different-hash':
        tested['source_manifest']['pricing.py'] = 'b' * 64
    elif mutation == 'different-files':
        tested['source_manifest']['other.py'] = 'b' * 64
    elif mutation == 'nonmapping':
        tested['source_manifest'] = ['pricing.py']
    elif mutation == 'duplicate-path':
        state['source_manifest']['./pricing.py'] = 'a' * 64
    elif mutation == 'unsafe-path':
        state['source_manifest']['../private-file.py'] = 'b' * 64
    elif mutation == 'invalid-unicode':
        state['source_manifest']['\ud800.py'] = 'b' * 64
    report = build_report(run, project, events)
    verification = report['verification']
    assert verification['verified'] is verification['regression_fixed'] is False
    assert verification['reported_regression_fixed'] is True
    assert verification['provenance']['manifests_match'] is False
    assert any('文件清单' in reason for reason in verification['limitations'])
    assert '尚未证实回归修复' in render_markdown(report)


@pytest.mark.parametrize('mutation', ['stale-cited', 'stale-uncited', 'missing-hash', 'unknown-path',
                                    'duplicate-id', 'missing-diagnosis-id', 'missing-patch-id',
                                    'empty-diagnosis-ids', 'wrong-patch-target', 'git-only-patch-reference'])
def test_stale_or_unbound_evidence_cannot_be_exported_as_verified(mutation):
    run, project, events = sample()
    state = run['state']
    evidence = state['evidence'][0]
    patch = state['diagnosis']['patches'][0]
    if mutation == 'stale-cited':
        evidence['sha256'] = 'b' * 64
    elif mutation == 'stale-uncited':
        state['evidence'].append({**evidence, 'id': 'uncited-stale', 'sha256': 'b' * 64})
    elif mutation == 'missing-hash':
        evidence.pop('sha256')
    elif mutation == 'unknown-path':
        evidence['path'] = '../pricing.py'
    elif mutation == 'duplicate-id':
        state['evidence'].append(dict(evidence))
    elif mutation == 'missing-diagnosis-id':
        state['diagnosis']['evidence_ids'] = ['missing']
    elif mutation == 'missing-patch-id':
        patch['evidence_ids'] = ['missing']
    elif mutation == 'empty-diagnosis-ids':
        state['diagnosis']['evidence_ids'] = []
    elif mutation == 'wrong-patch-target':
        state['source_manifest']['other.py'] = state['verification']['source_manifest']['other.py'] = 'b' * 64
        state['evidence'].append({**evidence, 'id': 'other', 'path': 'other.py', 'sha256': 'b' * 64})
        patch['evidence_ids'] = ['other']
    elif mutation == 'git-only-patch-reference':
        state['evidence'].append({**evidence, 'id': 'git-observation', 'source': 'git'})
        patch['evidence_ids'] = ['git-observation']
    reapprove(run)
    result = build_report(run, project, events)['verification']
    assert result['provenance']['manifests_match'] is True
    assert result['verified'] is False
    assert any('证据' in reason for reason in result['limitations'])
    assert not any('批准记录' in reason for reason in result['limitations'])


def test_persisted_path_aliases_normalize_without_reading_current_checkout(monkeypatch):
    run, project, events = sample()
    state = run['state']
    state['source_manifest'] = {'./pkg\\pricing.py': 'a' * 64}
    state['verification']['source_manifest'] = {'pkg/pricing.py': 'a' * 64}
    state['evidence'][0]['path'] = './pkg/./pricing.py'
    state['diagnosis']['patches'][0]['path'] = 'pkg\\pricing.py'
    reapprove(run)

    def no_disk(*args, **kwargs):
        pytest.fail('portable report must use persisted metadata only')

    for method in ('read_text', 'read_bytes', 'resolve', 'exists', 'stat'):
        monkeypatch.setattr(Path, method, no_disk)
    report = build_report(run, project, events)
    assert report['verification']['verified']
    assert report['verification']['provenance']['manifests_match']
    assert report['verification']['provenance']['patch_targets_cited']


@pytest.mark.parametrize('mutation', ['manifest-case', 'evidence-case', 'patch-case'])
def test_portable_paths_never_assume_case_insensitive_filesystems(mutation):
    run, project, events = sample()
    state = run['state']
    if mutation == 'manifest-case':
        state['verification']['source_manifest'] = {'Pricing.py': 'a' * 64}
    elif mutation == 'evidence-case':
        state['evidence'][0]['path'] = 'Pricing.py'
    else:
        state['diagnosis']['patches'][0]['path'] = 'Pricing.py'
    reapprove(run)
    assert build_report(run, project, events)['verification']['verified'] is False


def test_manifest_summary_is_deterministic_and_does_not_export_uncited_paths():
    run, project, events = sample()
    manifest = {'pricing.py': 'a' * 64, 'Pricing.py': 'b' * 64,
                'private-unrelated-module.py': 'c' * 64}
    run['state']['source_manifest'] = dict(manifest)
    run['state']['verification']['source_manifest'] = dict(reversed(list(manifest.items())))
    before = copy.deepcopy(run)
    report = build_report(run, project, events)
    provenance = report['verification']['provenance']
    expected = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                        separators=(',', ':')).encode('utf-8')).hexdigest()
    assert report['verification']['verified'] is True
    assert provenance['investigation_manifest_sha256'] == provenance['verification_manifest_sha256'] == expected
    assert provenance['investigation_file_count'] == provenance['verification_file_count'] == 3
    assert 'private-unrelated-module.py' not in json.dumps(report)
    assert 'Pricing.py' not in json.dumps(report)
    assert 'source_manifest' not in json.dumps(report)
    assert '调查与验证文件清单：一致' in render_markdown(report)
    assert run == before


def test_old_partial_metadata_reports_specific_unconfirmed_reason():
    run, project, events = sample()
    run['state'].pop('source_manifest')
    run['state']['verification'].pop('source_manifest')
    report = build_report(run, project, events)
    assert report['verification']['verified'] is False
    assert any('旧记录' in reason for reason in report['verification']['limitations'])
    assert report['verification']['provenance']['investigation_manifest_sha256'] is None
    assert '调查与验证文件清单：未确认' in render_markdown(report)
