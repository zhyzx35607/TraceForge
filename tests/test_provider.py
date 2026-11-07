import asyncio
import json
import time

import httpx
import pytest

from traceforge.config import Settings
from traceforge.provider import OpenAIProvider, ProviderError


def settings(**kwargs):
    return Settings(api_key='unit-test-placeholder', model='test-model', **kwargs)


@pytest.mark.asyncio
async def test_chat_real_usage_and_wire_format():
    events, requests = [], []
    def handle(request):
        requests.append(json.loads(request.content))
        assert request.url.path == '/v1/chat/completions'
        return httpx.Response(200, json={'model': 'actual-model', 'usage': {'prompt_tokens': 42, 'completion_tokens': 7},
            'choices': [{'finish_reason': 'tool_calls', 'message': {'content': None, 'tool_calls': [
                {'id': 'one', 'type': 'function', 'function': {'name': 'search', 'arguments': '{"query":"error","limit":4}'}}]}}]})
    provider = OpenAIProvider(settings(), transport=httpx.MockTransport(handle),
                              on_event=lambda kind, payload: events.append((kind, payload)))
    result = await provider.complete([{'role': 'user', 'content': 'Find cause'}], remaining_seconds=10)
    assert result['tool_calls'][0]['name'] == 'search'
    assert requests[0]['tools'][2]['function']['strict']
    assert sum(event[1].get('usage', {}).get('provider_calls', 0) for event in events) == 1
    assert events[-1][1]['usage'] == {'provider_calls': 0, 'input_tokens': 42, 'output_tokens': 7}


@pytest.mark.asyncio
async def test_responses_roundtrip_preserves_function_call_ids():
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        assert request.url.path == '/v1/responses'
        return httpx.Response(200, json={'status': 'completed', 'usage': {'input_tokens': 11, 'output_tokens': 5},
            'output': [{'id': 'rs_unit', 'type': 'reasoning', 'summary': [], 'encrypted_content': 'opaque'},
                       {'type': 'function_call', 'id': 'fc_item', 'call_id': 'call_abc', 'name': 'read',
                        'arguments': '{"path":"main.py","start":1,"end":20}'}]})
    provider = OpenAIProvider(settings(api_style='responses'), transport=httpx.MockTransport(handle))
    first = await provider.complete([{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'issue'}],
                                    remaining_seconds=10)
    await provider.complete([first['message'], {'role': 'tool', 'tool_call_id': 'call_abc', 'content': '{"evidence":[]}'}],
                            remaining_seconds=10)
    assert requests[0]['instructions'] == 'rules'
    assert requests[1]['input'][-1]['call_id'] == 'call_abc'
    assert requests[1]['input'][0]['encrypted_content'] == 'opaque'
    assert requests[0]['store'] is False
    assert requests[0]['tools'][0]['name'] == 'search'


@pytest.mark.asyncio
async def test_transient_retries_only_and_count_attempts(monkeypatch):
    calls = []
    async def no_sleep(_):
        pass
    monkeypatch.setattr('traceforge.provider.asyncio.sleep', no_sleep)
    events = []
    def handle(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, json={'error': 'private body'})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'done'}}]})
    provider = OpenAIProvider(settings(), transport=httpx.MockTransport(handle),
                              on_event=lambda kind, payload: events.append((kind, payload)))
    await provider.complete([], remaining_seconds=5)
    assert len(calls) == 3
    assert sum(event[1].get('usage', {}).get('provider_calls', 0) for event in events) == 3
    assert sum(event[1].get('usage', {}).get('input_tokens', 0) for event in events) == 0
    calls.clear()
    def reject(request):
        calls.append(request)
        return httpx.Response(401, text='unit-test-placeholder')
    provider.transport = httpx.MockTransport(reject)
    with pytest.raises(ProviderError) as error:
        await provider.complete([], remaining_seconds=5)
    assert len(calls) == 1
    assert 'unit-test-placeholder' not in str(error.value)


@pytest.mark.asyncio
async def test_truncated_output_is_rejected():
    provider = OpenAIProvider(settings(), transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]})))
    with pytest.raises(ProviderError, match='截断'):
        await provider.complete([], remaining_seconds=5)


@pytest.mark.asyncio
async def test_no_configuration_or_expired_budget():
    provider = OpenAIProvider(Settings())
    with pytest.raises(ProviderError, match='TRACEFORGE_API_KEY'):
        await provider.complete([], remaining_seconds=5)
    provider = OpenAIProvider(settings())
    with pytest.raises(ProviderError, match='时间预算'):
        await provider.complete([], remaining_seconds=0)


@pytest.mark.asyncio
async def test_remaining_token_budget_is_sent_as_output_cap():
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})
    provider = OpenAIProvider(settings(), transport=httpx.MockTransport(handle))
    await provider.complete([], remaining_seconds=5, remaining_tokens=17)
    assert requests[0]['max_completion_tokens'] == 17


@pytest.mark.asyncio
async def test_provider_fault_types_are_structured():
    provider = OpenAIProvider(settings(), transport=httpx.MockTransport(
        lambda _: httpx.Response(403, text='private body')))
    with pytest.raises(ProviderError) as caught:
        await provider.complete([], remaining_seconds=5)
    assert caught.value.fault_type == 'permission'


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [[], {'usage': 'wrong', 'choices': []}])
async def test_malformed_provider_json_has_format_fault(payload):
    provider = OpenAIProvider(settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    with pytest.raises(ProviderError) as caught:
        await provider.complete([], remaining_seconds=5)
    assert caught.value.fault_type == 'format'


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay = chunks, delay
        self.read_count, self.closed = 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_stream_over_limit_stops_reading_and_closes():
    stream = TrackedStream([b'x' * 16384] * 200)
    provider = OpenAIProvider(settings(), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=stream)))
    with pytest.raises(ProviderError) as caught:
        await provider.complete([], remaining_seconds=5)
    assert caught.value.fault_type == 'budget'
    assert stream.read_count < 65
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(('request_timeout', 'remaining_seconds'), [(5, 0.08), (0.05, 0.3)])
async def test_slow_drip_obeys_whole_deadline_and_closes(request_timeout, remaining_seconds):
    stream = TrackedStream([b' '] * 100, delay=0.02)
    provider = OpenAIProvider(settings(request_timeout=request_timeout), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=stream)))
    started = time.monotonic()
    with pytest.raises(ProviderError) as caught:
        await provider.complete([], remaining_seconds=remaining_seconds)
    assert caught.value.fault_type == 'timeout'
    assert time.monotonic() - started < 0.5
    assert stream.closed
    assert stream.read_count < 10


@pytest.mark.asyncio
async def test_error_body_never_read_and_stream_closed():
    stream = TrackedStream([b'unit-test-placeholder private body'])
    provider = OpenAIProvider(settings(), transport=httpx.MockTransport(
        lambda _: httpx.Response(403, stream=stream)))
    with pytest.raises(ProviderError) as caught:
        await provider.complete([], remaining_seconds=5)
    assert caught.value.fault_type == 'permission'
    assert stream.read_count == 0
    assert stream.closed


@pytest.mark.asyncio
async def test_provider_never_follows_redirect_or_persists_error_metadata():
    requests, events = [], []
    stream = TrackedStream([b'{"error":{"message":"unit-test-placeholder"}}'])
    def handle(request):
        requests.append(request)
        return httpx.Response(307, stream=stream, headers={'Location': 'https://untrusted.example/responses'})
    provider = OpenAIProvider(settings(), transport=httpx.MockTransport(handle),
                              on_event=lambda kind, payload: events.append((kind, payload)))
    with pytest.raises(ProviderError) as caught:
        await provider.complete([], remaining_seconds=5)
    assert len(requests) == 1
    assert stream.closed and stream.read_count == 0
    assert 'HTTP 307' in str(caught.value)
    assert 'unit-test-placeholder' not in str(events)
    assert 'untrusted' not in str(events)


@pytest.mark.asyncio
async def test_declared_oversize_rejected_before_read():
    stream = TrackedStream([b'should not read'])
    provider = OpenAIProvider(settings(), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=stream, headers={'Content-Length': '1000001'})))
    with pytest.raises(ProviderError) as caught:
        await provider.complete([], remaining_seconds=5)
    assert caught.value.fault_type == 'budget'
    assert stream.read_count == 0
    assert stream.closed


@pytest.mark.asyncio
async def test_compressed_response_not_decoded():
    stream = TrackedStream([b'not a gzip stream'])
    provider = OpenAIProvider(settings(), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=stream, headers={'Content-Encoding': 'gzip'})))
    with pytest.raises(ProviderError) as caught:
        await provider.complete([], remaining_seconds=5)
    assert caught.value.fault_type == 'format'
    assert stream.read_count == 0
    assert stream.closed
