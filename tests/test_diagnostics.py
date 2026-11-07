import asyncio
import json
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
    assert report["checks"][1]["message"] == "Echo [REDACTED]"
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
async def test_doctor_invalid_error_body_is_not_exposed():
    stream = DiagnosticStream([b'private-test-value'])
    report = await diagnose_provider(Settings(api_key='private-test-value', model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(429, stream=stream)))
    assert report['checks'][0]['category'] == 'rate_limit'
    assert stream.closed and stream.read_count == 1
    assert report['checks'][0]['http_status'] == 429
    assert report['checks'][0]['message'] is None
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


@pytest.mark.asyncio
async def test_doctor_reads_allowlisted_concurrency_metadata():
    data = {'error': {'code': 'gateway_concurrency_limit', 'type': 'capacity_error',
                      'message': 'Concurrency limit exceeded', 'request': 'private request'},
            'internal': 'private response'}
    report = await diagnose_provider(Settings(api_key='key', model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(429, json=data)))
    check = report['checks'][0]
    assert check['category'] == 'concurrency_limit'
    assert check['code'] == 'gateway_concurrency_limit'
    assert check['upstream_type'] == 'capacity_error'
    assert check['message'] == 'Concurrency limit exceeded'
    assert 'private' not in str(report)


@pytest.mark.asyncio
async def test_doctor_metadata_never_stringifies_nested_values():
    data = {'error': {'code': {'nested': 'private code'}, 'type': ['private type'],
                      'message': {'nested': 'private message'}},
            'message': ['private fallback'], 'detail': {'private': 'detail'},
            'code': ['private code'], 'type': {'private': 'type'}}
    report = await diagnose_provider(Settings(api_key='key', model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(403, json=data)))
    check = report['checks'][0]
    assert check['category'] == 'access_denied'
    assert check['code'] is check['message'] is check['upstream_type'] is None
    assert 'private' not in str(report)


@pytest.mark.asyncio
async def test_doctor_redacts_before_truncating_metadata():
    key = 'quoted"private\\credential'
    data = {'error': {'code': 'x' * 115 + key, 'type': key,
                      'message': 'x' * 590 + key + ' api_key=secondary-secret-value'}}
    report = await diagnose_provider(Settings(api_key=key, model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(403, json=data)))
    check = report['checks'][0]
    assert check['upstream_type'] == '[REDACTED]'
    assert 'quoted' not in str(report)
    assert 'secondary-secret-value' not in str(report)


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [b'<html>private body</html>', b'not json', b'[]', b'null',
                                 b'{' + b'[' * 2000, b'{"error": NaN}'])
async def test_doctor_unusable_error_json_preserves_status(body):
    stream = DiagnosticStream([body])
    report = await diagnose_provider(Settings(api_key='key', model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(403, stream=stream)))
    check = report['checks'][0]
    assert check['category'] == 'access_denied'
    assert check['http_status'] == 403 and check['message'] is None
    assert 'private' not in str(report)
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [{'Content-Length': '4097'}, {'Content-Length': '-1'},
                                    {'Content-Length': 'invalid'}, {'Content-Encoding': 'gzip'}])
async def test_doctor_unacceptable_error_headers_skip_body(headers):
    stream = DiagnosticStream([b'private body'])
    report = await diagnose_provider(Settings(api_key='key', model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(403, stream=stream, headers=headers)))
    assert report['checks'][0]['category'] == 'access_denied'
    assert report['checks'][0]['http_status'] == 403
    assert stream.closed and stream.read_count == 0


@pytest.mark.asyncio
async def test_doctor_error_body_cannot_exceed_4k_and_closes():
    stream = DiagnosticStream([b'x' * 1024] * 100)
    report = await diagnose_provider(Settings(api_key='key', model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(429, stream=stream)))
    assert report['checks'][0]['category'] == 'rate_limit'
    assert report['checks'][0]['http_status'] == 429
    assert stream.closed and stream.read_count == 5


@pytest.mark.asyncio
async def test_doctor_error_body_accepts_exact_4k():
    body = json.dumps({'error': {'code': 'gateway_concurrency_limit'}}).encode()
    stream = DiagnosticStream([body + b' ' * (4096 - len(body))])
    report = await diagnose_provider(Settings(api_key='key', model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(429, stream=stream)))
    assert report['checks'][0]['category'] == 'concurrency_limit'
    assert stream.closed


@pytest.mark.asyncio
async def test_doctor_slow_error_stream_preserves_http_status_with_total_deadline():
    stream = DiagnosticStream([b' '] * 100, delay=0.02)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(403, stream=stream)
    started = time.monotonic()
    report = await diagnose_provider(Settings(api_key='key', model='m', request_timeout=0.08), smoke=True,
                                     transport=httpx.MockTransport(handle))
    assert time.monotonic() - started < 0.5
    assert len(calls) == 1
    assert report['checks'][0]['category'] == 'access_denied'
    assert report['checks'][0]['http_status'] == 403
    assert report['checks'][1]['category'] == 'timeout'
    assert stream.closed and stream.read_count < 10


@pytest.mark.asyncio
async def test_doctor_never_follows_redirect_or_exposes_location():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={'Location': 'https://untrusted.example/private'})
    report = await diagnose_provider(Settings(api_key='key', model='m'), transport=httpx.MockTransport(handle))
    assert len(calls) == 1
    assert report['checks'][0]['category'] == 'request_rejected'
    assert report['checks'][0]['http_status'] == 302
    assert 'untrusted' not in str(report)


@pytest.mark.asyncio
async def test_doctor_truncated_error_stream_preserves_status():
    class BrokenStream(DiagnosticStream):
        async def __aiter__(self):
            yield b'{"error":'
            raise httpx.RemoteProtocolError('private transport detail')
    stream = BrokenStream([])
    report = await diagnose_provider(Settings(api_key='key', model='m'), transport=httpx.MockTransport(
        lambda _: httpx.Response(503, stream=stream)))
    assert report['checks'][0]['category'] == 'upstream_unavailable'
    assert report['checks'][0]['http_status'] == 503
    assert report['checks'][0]['message'] is None
    assert stream.closed and 'private' not in str(report)


@pytest.mark.asyncio
async def test_doctor_external_cancellation_is_not_swallowed():
    stream = DiagnosticStream([b' '] * 100, delay=0.02)
    started = asyncio.Event()
    def handle(request):
        started.set()
        return httpx.Response(403, stream=stream)
    task = asyncio.create_task(diagnose_provider(Settings(api_key='key', model='m'),
                                                transport=httpx.MockTransport(handle)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
