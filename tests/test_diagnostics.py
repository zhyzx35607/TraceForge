import asyncio
import time

import httpx
import pytest

from traceforge.config import Settings
from traceforge.diagnostics import classify_response, diagnose_provider


def test_classifies_gateway_concurrency_as_access_capacity():
    result = classify_response(429, {"error": {"code": "gateway_concurrency_limit", "message": "Concurrency limit exceeded"}})
    assert result["category"] == "concurrency_limit"
    assert "并发" in result["action"]


@pytest.mark.asyncio
async def test_doctor_does_not_generate_without_smoke():
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})
    report = await diagnose_provider(Settings(api_key="private-test-value", model="test-model"), transport=httpx.MockTransport(handle))
    assert len(seen) == 1 and seen[0].method == "GET"
    assert report["checks"][0]["selected_model_listed"]
    assert "private-test-value" not in str(report)


@pytest.mark.asyncio
async def test_smoke_is_explicit_and_redacts_upstream_echo():
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(403, json={"error": {"message": "Echo private-test-value"}})
    report = await diagnose_provider(Settings(api_key="private-test-value", model="m"), smoke=True, transport=httpx.MockTransport(handle))
    assert len(report["checks"]) == 2
    assert report["checks"][1]["category"] == "access_denied"
    assert "private-test-value" not in str(report)


@pytest.mark.asyncio
async def test_missing_config_never_makes_network_call():
    def handle(request):
        raise AssertionError("unexpected network")
    report = await diagnose_provider(Settings(), transport=httpx.MockTransport(handle))
    assert report["checks"][0]["category"] == "configuration"


class DiagnosticStream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.read_count, self.closed = chunks, delay, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize('data', [[], {'data': None}, {'data': 'bad'}, {'data': [1]}, {'data': [{'id': []}]}])
async def test_doctor_malformed_models_is_reported(data):
    report = await diagnose_provider(Settings(api_key='key', model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=data)))
    assert report['checks'][0]['category'] == 'format'
    assert report['checks'][0]['ok'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('usage', [[], 'bad', {'input_tokens': 'secret'}, {'input_tokens': True}])
async def test_doctor_malformed_usage_is_reported(usage):
    def handle(request):
        return httpx.Response(200, json={'data': []} if request.method == 'GET' else {'usage': usage})
    report = await diagnose_provider(Settings(api_key='secret', model='m'), smoke=True,
                                     transport=httpx.MockTransport(handle))
    assert report['checks'][1]['category'] == 'format'
    assert 'secret' not in str(report)


@pytest.mark.asyncio
async def test_doctor_overlong_stream_closes_and_does_not_exhaust():
    stream = DiagnosticStream([b'x' * 16384] * 100)
    report = await diagnose_provider(Settings(api_key='key', model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=stream)))
    assert report['checks'][0]['category'] == 'budget'
    assert stream.closed and stream.read_count < 20


@pytest.mark.asyncio
async def test_doctor_slow_stream_respects_total_deadline():
    stream = DiagnosticStream([b' '] * 100, delay=0.02)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=stream)
    started = time.monotonic()
    report = await diagnose_provider(Settings(api_key='key', model='m', request_timeout=0.08), smoke=True,
                                     transport=httpx.MockTransport(handle))
    assert time.monotonic() - started < 0.5
    assert len(calls) == 1
    assert all(check['category'] == 'timeout' for check in report['checks'])
    assert stream.closed


@pytest.mark.asyncio
async def test_doctor_error_body_is_never_read():
    stream = DiagnosticStream([b'private-test-value'])
    report = await diagnose_provider(Settings(api_key='private-test-value', model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(429, stream=stream)))
    assert report['checks'][0]['category'] == 'rate_limit'
    assert stream.closed and stream.read_count == 0
    assert 'private-test-value' not in str(report)


@pytest.mark.asyncio
async def test_doctor_usage_fields_are_allowlisted_and_redacts_quoted_key():
    key = 'a"b\\c'
    def handle(request):
        return httpx.Response(200, json={'data': [{'id': key}]} if request.method == 'GET' else {
            'usage': {'input_tokens': 2, 'output_tokens': 1, 'private': key}, 'private': key})
    report = await diagnose_provider(Settings(api_key=key, model='m'), smoke=True, transport=httpx.MockTransport(handle))
    assert report['checks'][0]['listed_models'] == ['[REDACTED]']
    assert report['checks'][1]['usage'] == {'input_tokens': 2, 'output_tokens': 1}
