import asyncio
import copy
import hashlib
import json
import sys

import pytest

from traceforge.config import DEMO_ROOT, Settings
from traceforge.engine import Engine, EvidenceVersionError, _json
from traceforge.provider import ProviderError, _responses_input
from traceforge.retrieval import RepositoryIndex
from traceforge.store import LeaseLostError, Store, patch_digest


@pytest.fixture
def demo(tmp_path):
    settings = Settings(data_dir=tmp_path / 'state', allowed_roots=[DEMO_ROOT])
    store = Store(settings.data_dir / 'test.sqlite')
    project = store.create_project({'name': 'demo', 'root': str(DEMO_ROOT), 'runner': 'local',
                                    'trusted': True, 'test_command': [sys.executable, '-m', 'unittest', 'discover', '-v']})
    return settings, store, project, Engine(store, settings)


def new_run(store, project, mode='replay'):
    return store.create_run({'project_id': project['id'], 'title': 'Quantity regression',
                             'query': 'quantity 3 returns 19.90; expected 59.70', 'mode': mode})


@pytest.mark.asyncio
async def test_replay_approval_and_real_fix(demo):
    _settings, store, project, engine = demo
    run = new_run(store, project)
    before = (DEMO_ROOT / 'pricing.py').read_bytes()
    await engine.investigate(run['id'])
    run = store.get_run(run['id'])
    assert run['status'] == 'awaiting_approval', run.get('error')
    assert 'REPLAY' in run['state']['execution_label']
    assert run['state']['usage']['provider_calls'] == 0
    assert run['state']['patch_digest'] == patch_digest(run['state']['diagnosis']['patches'])
    await engine.investigate(run['id'])
    assert store.get_run(run['id'])['status'] == 'awaiting_approval'
    store.decide(run['id'], 'approve', run['state']['patch_digest'])
    await engine.verify(run['id'])
    run = store.get_run(run['id'])
    assert run['status'] == 'completed', run.get('error')
    assert run['state']['verification']['regression_fixed']
    assert 'FAILED (failures=4)' in run['state']['verification']['baseline']['stderr']
    assert 'Ran 8 tests' in run['state']['verification']['patched']['stderr']
    assert len(store.memories(project['id'])) == 1
    assert (DEMO_ROOT / 'pricing.py').read_bytes() == before


@pytest.mark.asyncio
async def test_missing_or_tampered_approval_never_executes(demo, monkeypatch):
    _settings, store, project, engine = demo
    run = new_run(store, project)
    await engine.investigate(run['id'])
    called = []
    monkeypatch.setattr('traceforge.engine.run_verification', lambda *a, **k: called.append(True))
    await engine.verify(run['id'])
    assert store.get_run(run['id'])['status'] == 'failed'
    assert not called
    run = new_run(store, project)
    await engine.investigate(run['id'])
    ready = store.get_run(run['id'])
    store.decide(run['id'], 'approve', ready['state']['patch_digest'])
    changed = copy.deepcopy(ready['state']['diagnosis'])
    changed['patches'][0]['after'] += ' # tampered'
    store.update_run(run['id'], state={'diagnosis': changed})
    await engine.verify(run['id'])
    assert store.get_run(run['id'])['status'] == 'failed'
    assert not called


@pytest.mark.asyncio
async def test_investigate_has_no_model_claims_or_memory(demo):
    _settings, store, project, engine = demo
    run = new_run(store, project, 'investigate')
    await engine.investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'completed', actual.get('error')
    assert actual['state']['diagnosis']['patches'] == []
    assert '未确认' in actual['state']['diagnosis']['root_cause']
    assert actual['state']['evidence']
    assert store.memories(project['id']) == []


@pytest.mark.asyncio
async def test_replay_rejects_other_repository(demo, tmp_path):
    settings, store, project, engine = demo
    root = tmp_path / 'other'
    root.mkdir()
    (root / 'main.py').write_text('print(1)')
    settings.allowed_roots.append(root)
    project = store.create_project({**project, 'name': 'other', 'root': str(root)})
    run = new_run(store, project)
    await engine.investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'failed'
    assert 'replay' in actual['error']


def test_citations_and_patch_target_are_strict(demo):
    _settings, _store, _project, engine = demo
    index = RepositoryIndex(DEMO_ROOT)
    index.build()
    evidence = index.read('pricing.py')
    diagnosis = {'summary': 'finding', 'root_cause': 'unknown', 'confidence': 'low',
                 'evidence_ids': ['missing'], 'limitations': [], 'patches': []}
    with pytest.raises(ValueError, match='引用'):
        engine._validate(diagnosis, {evidence['id']: evidence}, DEMO_ROOT)
    diagnosis['evidence_ids'] = [evidence['id']]
    diagnosis['patches'] = [{'path': 'test_pricing.py', 'before': 'import unittest', 'after': '# removed',
                             'reason': 'bad', 'evidence_ids': [evidence['id']]}]
    with pytest.raises(ValueError, match='目标文件'):
        engine._validate(diagnosis, {evidence['id']: evidence}, DEMO_ROOT)


def test_finish_cannot_cite_unseen_evidence(demo):
    _settings, _store, _project, engine = demo
    evidence = {'seen': {'id': 'seen', 'path': 'pricing.py'}, 'hidden': {'id': 'hidden', 'path': 'pricing.py'}}
    diagnosis = {'summary': 'finding', 'root_cause': 'cause', 'confidence': 'low',
                 'evidence_ids': ['hidden'], 'limitations': [], 'patches': []}
    with pytest.raises(ValueError, match='尚未看到'):
        engine._validate(diagnosis, evidence, DEMO_ROOT, visible_ids={'seen'})


def test_context_compaction_keeps_pair_and_factual_catalog(demo):
    settings, _store, _project, engine = demo
    settings.max_context_chars = 1800
    evidence = {'ev1': {'id': 'ev1', 'path': 'pricing.py', 'line_start': 1, 'line_end': 2,
                        'kind': 'code', 'sha256': 'a' * 64, 'content': 'x' * 5000}}
    messages = [
        {'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'symptom'},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function',
            'function': {'name': 'read', 'arguments': '{"path":"pricing.py","start":1,"end":2}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': json.dumps({'evidence': [evidence['ev1']]})},
    ]
    compact = engine._compress_messages(messages, evidence, {'ev1'})
    assert len(_json(compact)) <= settings.max_context_chars
    assert compact[0]['role'] == 'system'
    assert compact[-1]['role'] == 'tool'
    assert json.loads(compact[-1]['content'])['evidence'][0]['truncated']
    assert len(evidence['ev1']['content']) == 5000
    catalog = json.loads(compact[2]['content'])
    assert catalog['evidence_catalog'][0]['id'] == 'ev1'
    assert 'chain' not in compact[2]['content'].lower()


def test_context_compaction_preserves_responses_opaque_and_pairs(demo):
    settings, _store, _project, engine = demo
    settings.max_context_chars = 2100
    evidence = {'ev1': {'id': 'ev1', 'path': 'pricing.py', 'content': 'x' * 5000}}
    output = [{'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'encrypted_content': 'opaque-bytes'},
              {'type': 'function_call', 'call_id': 'call_1', 'name': 'read', 'arguments': '{}'}]
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': '{"symptom":"original"}'},
                {'role': 'assistant', 'content': None, '_responses_output': output},
                {'role': 'tool', 'tool_call_id': 'call_1', 'content': _json({'evidence': [evidence['ev1']]})}]
    compact = engine._compress_messages(messages, evidence, {'ev1'})
    assert compact[-2]['_responses_output'] == output
    _, request = _responses_input(compact)
    assert request[-1]['call_id'] == 'call_1'
    assert request[-3]['encrypted_content'] == 'opaque-bytes'
    assert len(_json(compact)) <= settings.max_context_chars


def test_compaction_never_splits_pending_tool_call(demo):
    settings, _store, _project, engine = demo
    settings.max_context_chars = 500
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'symptom'},
                {'role': 'assistant', 'content': 'x' * 900, 'tool_calls': [
                    {'id': 'pending', 'function': {'name': 'read', 'arguments': '{}'}}]}]
    with pytest.raises(ProviderError, match='配对') as caught:
        engine._compress_messages(messages, {}, set())
    assert caught.value.fault_type == 'format'


@pytest.mark.asyncio
async def test_token_budget_stops_before_provider(demo, monkeypatch):
    settings, store, project, engine = demo
    settings.max_total_tokens = 100
    run = new_run(store, project, 'live')
    store.update_run(run['id'], state={'usage': {'provider_calls': 1, 'input_tokens': 80, 'output_tokens': 20}})
    class NeverProvider:
        def __init__(self, *args, **kwargs):
            pass
        async def complete(self, *args, **kwargs):
            raise AssertionError('token budget must be checked before request')
    monkeypatch.setattr('traceforge.engine.OpenAIProvider', NeverProvider)
    await engine.investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'failed'
    assert actual['state']['fault_type'] == 'budget'
    assert actual['state']['usage']['provider_calls'] == 1


@pytest.mark.asyncio
async def test_large_search_compacts_and_recovers_seen_references(demo, monkeypatch):
    settings, store, project, engine = demo
    settings.max_context_chars = 5000
    generated = []
    def large_search(index, query, limit=8):
        items = [{'id': f'large-{query}-{n}', 'path': 'pricing.py', 'source': 'repository',
                  'line_start': 1, 'line_end': 120, 'kind': 'code',
                  'sha256': hashlib.sha256((DEMO_ROOT / 'pricing.py').read_bytes()).hexdigest(),
                  'score': 1, 'content': query + ':' + 'evidence ' * 900} for n in range(limit)]
        generated.extend(items)
        return items
    monkeypatch.setattr(RepositoryIndex, 'search', large_search)
    request_count, all_requests = 0, []
    class InterruptibleProvider:
        def __init__(self, *args, **kwargs):
            pass
        async def complete(self, messages, **kwargs):
            nonlocal request_count
            request_count += 1
            all_requests.append(copy.deepcopy(messages))
            assert len(_json(messages)) <= settings.max_context_chars
            if request_count == 5:
                raise asyncio.CancelledError()
            if request_count < 5:
                name, args = 'search', {'query': f'probe{request_count}', 'limit': 10}
            else:
                visible = engine._shown_ids(messages)
                assert visible
                name, args = 'finish', {'summary': 'Retrieved evidence', 'root_cause': 'Unconfirmed',
                    'confidence': 'low', 'evidence_ids': [min(visible)], 'limitations': ['No tests'], 'patches': []}
            call_id, raw = str(request_count), _json(args)
            return {'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': raw}}]},
                'tool_calls': [{'id': call_id, 'name': name, 'arguments': raw}]}
    monkeypatch.setattr('traceforge.engine.OpenAIProvider', InterruptibleProvider)
    run = new_run(store, project, 'live')
    with pytest.raises(asyncio.CancelledError):
        await engine.investigate(run['id'])
    interrupted = store.get_run(run['id'])
    assert interrupted['status'] == 'investigating'
    assert interrupted['state']['seen_evidence_ids']
    await Engine(store, settings).investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'completed', actual.get('error')
    assert set(actual['state']['diagnosis']['evidence_ids']) <= set(actual['state']['seen_evidence_ids'])
    assert any(len(item['content']) > 6000 for item in actual['state']['evidence'])
    assert len(actual['state']['evidence']) > len(actual['state']['seen_evidence_ids'])
    events = [event for event in store.events(run['id']) if event['kind'] == 'context.compacted']
    assert events
    assert all(event['payload']['before_chars'] > event['payload']['after_chars'] for event in events)
    assert all_requests[0][1]['content'].find(run['query']) >= 0


@pytest.mark.asyncio
async def test_live_tool_loop_usage_and_final_validation(demo, monkeypatch):
    settings, store, project, engine = demo
    settings.api_key, settings.model = 'placeholder', 'test-model'
    calls = []
    class FakeProvider:
        def __init__(self, settings, on_event):
            self.on_event = on_event
        async def complete(self, messages, **kwargs):
            self.on_event('provider.attempt', {'usage': {'provider_calls': 1, 'input_tokens': 0, 'output_tokens': 0}})
            self.on_event('provider.usage', {'usage': {'provider_calls': 0, 'input_tokens': 12, 'output_tokens': 4}})
            calls.append(messages)
            if len(calls) == 1:
                name, args = 'read', {'path': 'pricing.py', 'start': 1, 'end': 30}
            else:
                item = json.loads(messages[-1]['content'])['evidence'][0]
                name, args = 'finish', {'summary': 'Evidence collected', 'root_cause': 'Still unconfirmed',
                    'confidence': 'low', 'evidence_ids': [item['id']], 'limitations': ['No tests executed'], 'patches': []}
            tool = {'id': str(len(calls)), 'name': name, 'arguments': json.dumps(args)}
            return {'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': tool['id'], 'type': 'function', 'function': {'name': name, 'arguments': tool['arguments']}}]},
                'tool_calls': [tool]}
    monkeypatch.setattr('traceforge.engine.OpenAIProvider', FakeProvider)
    run = new_run(store, project, 'live')
    await engine.investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'completed', actual.get('error')
    assert len(calls) == 2
    assert actual['state']['usage'] == {'provider_calls': 2, 'input_tokens': 24, 'output_tokens': 8}
    assert actual['state']['provider_identity'] == {
        'model': 'test-model', 'api_style': 'chat',
        'endpoint_sha256': hashlib.sha256(settings.base_url.rstrip('/').encode()).hexdigest()}
    assert any(event['kind'] == 'tool.result' for event in store.events(run['id']))


@pytest.mark.asyncio
async def test_live_repeat_guard(demo, monkeypatch):
    _settings, store, project, engine = demo
    class Repeater:
        def __init__(self, *args, **kwargs):
            pass
        async def complete(self, messages, **kwargs):
            arguments = '{"path":"pricing.py","start":1,"end":20}'
            return {'message': {'role': 'assistant', 'content': None},
                    'tool_calls': [{'id': str(len(messages)), 'name': 'read', 'arguments': arguments}]}
    monkeypatch.setattr('traceforge.engine.OpenAIProvider', Repeater)
    run = new_run(store, project, 'live')
    await engine.investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'failed'
    assert '重复工具' in actual['error']
    assert actual['state']['agent_steps'] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['model', 'api_style', 'base_url', 'api_key'])
async def test_live_recovery_binds_model_and_endpoint_but_allows_key_rotation(demo, monkeypatch, change):
    settings, store, project, engine = demo
    settings.model, settings.api_key = 'original-model', 'original-key'
    requests = []

    class InterruptedProvider:
        def __init__(self, *args, **kwargs):
            pass

        async def complete(self, messages, **kwargs):
            requests.append(messages)
            if len(requests) == 1:
                raise asyncio.CancelledError
            identity = json.loads(messages[1]['content'])['evidence'][0]['id']
            arguments = {'summary': 'Insufficient evidence', 'root_cause': 'Unconfirmed',
                         'confidence': 'low', 'evidence_ids': [identity], 'limitations': [], 'patches': []}
            return {'message': {'role': 'assistant', 'content': None}, 'tool_calls': [
                {'id': 'finish', 'name': 'finish', 'arguments': json.dumps(arguments)}]}

    monkeypatch.setattr('traceforge.engine.OpenAIProvider', InterruptedProvider)
    run = new_run(store, project, 'live')
    with pytest.raises(asyncio.CancelledError):
        await engine.investigate(run['id'])
    original = copy.deepcopy(store.get_run(run['id'])['state']['provider_identity'])
    setattr(settings, change, {'model': 'different-model', 'api_style': 'responses',
                              'base_url': 'https://different.invalid/v1', 'api_key': 'rotated-key'}[change])
    await Engine(store, settings).investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['state']['provider_identity'] == original
    assert 'original-key' not in json.dumps(actual) and 'rotated-key' not in json.dumps(actual)
    if change == 'api_key':
        assert actual['status'] == 'completed' and len(requests) == 2
    else:
        assert actual['status'] == 'failed' and len(requests) == 1
        assert actual['state']['fault_type'] == 'configuration'


@pytest.mark.asyncio
async def test_failed_patch_does_not_write_memory(demo, monkeypatch):
    _settings, store, project, engine = demo
    run = new_run(store, project)
    await engine.investigate(run['id'])
    ready = store.get_run(run['id'])
    store.decide(run['id'], 'approve', ready['state']['patch_digest'])
    monkeypatch.setattr('traceforge.engine.run_verification', lambda *a, **k: {
        'regression_fixed': False, 'baseline': {'exit_code': 0}, 'patched': {'exit_code': 0}})
    await engine.verify(run['id'])
    assert store.get_run(run['id'])['status'] == 'completed'
    assert store.memories(project['id']) == []


@pytest.mark.asyncio
async def test_context_budget_fails_before_model_call(demo, monkeypatch):
    settings, store, project, engine = demo
    settings.max_context_chars = 100
    class UnexpectedProvider:
        def __init__(self, *args, **kwargs):
            pass
        async def complete(self, *args, **kwargs):
            raise AssertionError('provider must not be called')
    monkeypatch.setattr('traceforge.engine.OpenAIProvider', UnexpectedProvider)
    run = new_run(store, project, 'live')
    await engine.investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'failed'
    assert '字符预算' in actual['error']
    assert actual['state']['usage']['provider_calls'] == 0


@pytest.mark.asyncio
async def test_recovered_live_run_retains_step_budget(demo, monkeypatch):
    settings, store, project, engine = demo
    run = new_run(store, project, 'live')
    store.update_run(run['id'], status='investigating', state={'agent_steps': settings.max_steps,
        'usage': {'provider_calls': 7, 'input_tokens': 21, 'output_tokens': 8}})
    await engine.investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'failed'
    assert '最大工具步骤预算' in actual['error']
    assert actual['state']['usage']['provider_calls'] == 7


@pytest.mark.asyncio
async def test_recovered_verification_rechecks_manifest(demo, monkeypatch):
    _settings, store, project, engine = demo
    run = new_run(store, project)
    await engine.investigate(run['id'])
    ready = store.get_run(run['id'])
    store.decide(run['id'], 'approve', ready['state']['patch_digest'])
    manifest = copy.deepcopy(ready['state']['source_manifest'])
    manifest['pricing.py'] = '0' * 64
    store.update_run(run['id'], state={'source_manifest': manifest, 'phase': 'verifying'})
    await engine.verify(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'failed'
    assert '证据版本' in actual['error']
    assert store.memories(project['id']) == []


@pytest.mark.asyncio
async def test_cancel_between_baseline_and_patch_prevents_memory(demo):
    _settings, store, project, engine = demo
    run = new_run(store, project)
    await engine.investigate(run['id'])
    ready = store.get_run(run['id'])
    store.decide(run['id'], 'approve', ready['state']['patch_digest'])
    real_add_event = store.add_event
    def event(run_id, kind, payload):
        result = real_add_event(run_id, kind, payload)
        if kind == 'verification.result' and payload['phase'] == 'baseline':
            store.update_run(run_id, status='cancelled')
        return result
    store.add_event = event
    await engine.verify(run['id'])
    assert store.get_run(run['id'])['status'] == 'cancelled'
    assert store.memories(project['id']) == []
    assert not any(item['kind'] == 'verification.result' and item['payload']['phase'] == 'patched'
                   for item in store.events(run['id']))


@pytest.mark.asyncio
async def test_expired_worker_stops_verification_and_preserves_recovery(demo):
    import time
    _settings, store, project, engine = demo
    run = new_run(store, project)
    await engine.investigate(run['id'])
    ready = store.get_run(run['id'])
    store.decide(run['id'], 'approve', ready['state']['patch_digest'])
    job = store.claim_job('old-worker', lease_seconds=30)
    assert job['phase'] == 'verify'
    real_add_event = store.add_event
    def event(run_id, kind, payload):
        result = real_add_event(run_id, kind, payload)
        if kind == 'verification.result' and payload['phase'] == 'baseline':
            with store.connect() as db:
                db.execute('UPDATE jobs SET lease_until=? WHERE run_id=? AND phase=?',
                           (time.time() - 1, run_id, 'verify'))
        return result
    store.add_event = event
    with store.fenced_job(run['id'], 'verify', 'old-worker'), pytest.raises(LeaseLostError):
        await engine.verify(run['id'])
    assert store.get_run(run['id'])['status'] == 'verifying'
    assert store.memories(project['id']) == []
    next_job = store.claim_job('new-worker')
    assert next_job['run_id'] == run['id']
    assert next_job['attempts'] == 2
    assert not any(item['kind'] == 'verification.result' and item['payload']['phase'] == 'patched'
                   for item in store.events(run['id']))


@pytest.mark.parametrize('changed', [{'path': 'missing.py'}, {'sha256': 'b' * 64}, {'sha256': None}])
def test_repository_evidence_must_match_fixed_revision(changed):
    item = {'source': 'repository', 'path': 'main.py', 'sha256': 'a' * 64}
    Engine._require_evidence_versions([item], {'main.py': 'a' * 64})
    with pytest.raises(EvidenceVersionError, match='证据版本'):
        Engine._require_evidence_versions([{**item, **changed}], {'main.py': 'a' * 64})
    # A historical commit or user log is not a claim about current file bytes.
    Engine._require_evidence_versions([{**item, **changed, 'source': 'git'}], {})


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['read', 'search'])
async def test_transient_repository_revision_never_reaches_model(tmp_path, monkeypatch, action):
    root = tmp_path / 'repo'
    root.mkdir()
    target = root / 'main.py'
    original = b'def answer():\n    return 1\n# revision A\n'
    target.write_bytes(original)
    settings = Settings(data_dir=tmp_path / 'state', allowed_roots=[root])
    store = Store(settings.data_dir / 'test.sqlite')
    project = store.create_project({'name': 'revision test', 'root': str(root), 'runner': 'local',
                                   'trusted': True, 'test_command': [sys.executable, '-m', 'unittest']})
    run = new_run(store, project, 'live')
    engine = Engine(store, settings)
    original_read, original_search = RepositoryIndex.read, RepositoryIndex.search
    transient_observations = []

    def changed_observation(index, *args, **kwargs):
        # Reproduce A -> B -> A entirely inside one observation. Comparing only
        # the manifests before and after investigation cannot detect this.
        target.write_bytes(original.replace(b'revision A', b'revision B'))
        try:
            value = (original_read if action == 'read' else original_search)(index, *args, **kwargs)
            transient_observations.extend([value] if action == 'read' else value)
            return value
        finally:
            target.write_bytes(original)

    requests = []

    class OneObservationProvider:
        def __init__(self, *args, **kwargs):
            pass

        async def complete(self, messages, **kwargs):
            requests.append(copy.deepcopy(messages))
            assert len(requests) == 1, 'A mismatched observation must stop before another model call'
            monkeypatch.setattr(RepositoryIndex, action, changed_observation)
            arguments = {'path': 'main.py', 'start': 1, 'end': 3} if action == 'read' else {
                'query': 'answer', 'limit': 5}
            raw = _json(arguments)
            return {'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'observe', 'type': 'function', 'function': {'name': action, 'arguments': raw}}]},
                'tool_calls': [{'id': 'observe', 'name': action, 'arguments': raw}]}

    monkeypatch.setattr('traceforge.engine.OpenAIProvider', OneObservationProvider)
    await engine.investigate(run['id'])
    actual = store.get_run(run['id'])
    assert target.read_bytes() == original
    assert transient_observations and transient_observations[0]['sha256'] != hashlib.sha256(original).hexdigest()
    assert actual['status'] == 'failed'
    assert actual['state']['fault_type'] == 'source_changed'
    assert len(requests) == 1
    assert not actual['state'].get('diagnosis')
    assert not any(item['sha256'] == transient_observations[0]['sha256']
                   for item in actual['state'].get('evidence', []))
    assert store.memories(project['id']) == []


@pytest.mark.asyncio
async def test_recovered_evidence_revision_rejected_before_provider(demo, monkeypatch):
    _settings, store, project, engine = demo
    run = new_run(store, project, 'live')
    item = RepositoryIndex(DEMO_ROOT).read('pricing.py')
    store.update_run(run['id'], status='investigating', state={'evidence': [{**item, 'sha256': '0' * 64}]})

    def unexpected_provider(*args, **kwargs):
        pytest.fail('Recovered stale observations must not reach provider construction')

    monkeypatch.setattr('traceforge.engine.OpenAIProvider', unexpected_provider)
    await engine.investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'failed'
    assert actual['state']['fault_type'] == 'source_changed'
    assert store.memories(project['id']) == []


@pytest.mark.asyncio
async def test_verification_rechecks_evidence_from_old_checkpoint(demo, monkeypatch):
    _settings, store, project, engine = demo
    run = new_run(store, project)
    await engine.investigate(run['id'])
    ready = store.get_run(run['id'])
    store.decide(run['id'], 'approve', ready['state']['patch_digest'])
    observations = copy.deepcopy(ready['state']['evidence'])
    next(item for item in observations if item['source'] == 'repository')['sha256'] = '0' * 64
    store.update_run(run['id'], state={'evidence': observations})
    called = []
    monkeypatch.setattr('traceforge.engine.run_verification', lambda *a, **k: called.append(True))
    await engine.verify(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'failed'
    assert actual['state']['fault_type'] == 'source_changed'
    assert not called
    assert store.memories(project['id']) == []


def test_patch_cannot_use_historical_evidence_as_current_file(demo):
    _settings, _store, _project, engine = demo
    item = RepositoryIndex(DEMO_ROOT).read('pricing.py')
    item['source'] = 'git'
    diagnosis = {'summary': 'finding', 'root_cause': 'unknown', 'confidence': 'low',
                 'evidence_ids': [item['id']], 'limitations': [], 'patches': [
                     {'path': 'pricing.py', 'before': 'subtotal =', 'after': 'subtotal =',
                      'reason': 'historical reference is insufficient', 'evidence_ids': [item['id']]}]}
    with pytest.raises(ValueError, match='目标文件'):
        engine._validate(diagnosis, {item['id']: item}, DEMO_ROOT, source_manifest={})


@pytest.mark.asyncio
async def test_recovery_rejects_old_message_when_catalog_has_newer_same_id(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    path = root / 'main.py'
    version_a = b'answer = 42\n# revision A\n'
    path.write_bytes(version_a.replace(b'revision A', b'revision B'))
    old = RepositoryIndex(root).read('main.py', 1, 1)
    path.write_bytes(version_a)
    current = RepositoryIndex(root).read('main.py', 1, 1)
    assert old['id'] == current['id'] and old['sha256'] != current['sha256']
    settings = Settings(data_dir=tmp_path / 'state', allowed_roots=[root])
    store = Store(settings.data_dir / 'test.sqlite')
    project = store.create_project({'name': 'old messages', 'root': str(root), 'runner': 'local',
                                   'trusted': True, 'test_command': [sys.executable, '-m', 'unittest']})
    run = new_run(store, project, 'live')
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': _json({'symptom': 'check'})}]
    for number, item in enumerate([old, current]):
        messages.extend([
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': str(number), 'type': 'function', 'function': {'name': 'read',
                 'arguments': _json({'path': 'main.py', 'start': 1, 'end': 1})}}]},
            {'role': 'tool', 'tool_call_id': str(number), 'content': _json({'evidence': [item]})}])
    store.update_run(run['id'], status='investigating', state={
        'source_manifest': {'main.py': current['sha256']}, 'evidence': [current],
        'agent_messages': messages, 'seen_evidence_ids': [current['id']], 'agent_steps': 2})
    calls = []

    class UnexpectedProvider:
        def __init__(self, *args, **kwargs):
            pass

        async def complete(self, *args, **kwargs):
            calls.append(True)
            raise AssertionError('An old observation must not be replayed to a provider')

    monkeypatch.setattr('traceforge.engine.OpenAIProvider', UnexpectedProvider)
    await Engine(store, settings).investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'failed' and actual['state']['fault_type'] == 'source_changed'
    assert not calls
    assert path.read_bytes() == version_a


def test_legacy_relative_alias_binds_to_real_file(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    path = root / 'main.py'
    path.write_text('answer = 42\n', encoding='utf-8')
    item = RepositoryIndex(root).read('main.py')
    item['path'] = './main.py'
    Engine._require_evidence_versions([item], {'main.py': item['sha256']}, root)
    if sys.platform == 'win32':
        item['path'] = 'MAIN.PY'
        Engine._require_evidence_versions([item], {'main.py': item['sha256']}, root)


@pytest.mark.asyncio
async def test_empty_repository_checkpoint_does_not_adopt_new_files(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    settings = Settings(data_dir=tmp_path / 'state', allowed_roots=[root])
    store = Store(settings.data_dir / 'test.sqlite')
    project = store.create_project({'name': 'empty revision', 'root': str(root), 'runner': 'local',
                                   'trusted': True, 'test_command': [sys.executable, '-m', 'unittest']})
    run = new_run(store, project, 'live')
    calls = []

    class InterruptedProvider:
        def __init__(self, *args, **kwargs):
            pass

        async def complete(self, *args, **kwargs):
            calls.append(True)
            raise asyncio.CancelledError()

    monkeypatch.setattr('traceforge.engine.OpenAIProvider', InterruptedProvider)
    with pytest.raises(asyncio.CancelledError):
        await Engine(store, settings).investigate(run['id'])
    interrupted = store.get_run(run['id'])
    assert interrupted['state']['source_manifest'] == {}
    (root / 'main.py').write_text('answer = 42\n', encoding='utf-8')
    await Engine(store, settings).investigate(run['id'])
    actual = store.get_run(run['id'])
    assert actual['status'] == 'failed' and actual['state']['fault_type'] == 'source_changed'
    assert actual['state']['source_manifest'] == {}
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fail', [False, True])
async def test_collection_shares_one_fresh_search_and_keeps_symptom(demo, monkeypatch, fail):
    _settings, store, project, engine = demo
    run = new_run(store, project, 'investigate')
    searches = []
    item = {'id': 'log-observation', 'path': 'incident.log', 'kind': 'log', 'content': 'failure'}

    class CountingIndex:
        def search(self, query, limit):
            searches.append((query, limit))
            if fail:
                raise OSError('temporary read failure')
            return [item]

    monkeypatch.setattr('traceforge.engine.collect_git_evidence', lambda *args, **kwargs: [])
    evidence = await engine._collect(run, project, CountingIndex())
    assert searches == [(run['query'], 8)]
    assert any(value['source'] == 'submitted_symptom' for value in evidence.values() if 'source' in value)
    assert ('log-observation' in evidence) is not fail
    events = store.events(run['id'])
    assert any(event['kind'] == 'specialist.completed' and event['payload']['specialist'] == 'logs'
               for event in events)
