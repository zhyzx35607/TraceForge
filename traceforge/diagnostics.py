"""Explicit, bounded connectivity diagnosis. Never logs keys or full provider bodies."""
from __future__ import annotations

import asyncio
import platform
import shutil
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .provider import ERROR_METADATA_TIMEOUT, ProviderError, bounded_json_request, error_metadata, usage_counts
from .retrieval import redact

DIAGNOSTIC_RESPONSE_BYTES = 256_000


def _safe_report(value, api_key: str):
    if isinstance(value, str):
        return redact(value.replace(api_key, '[REDACTED]') if api_key else value)
    if isinstance(value, dict):
        return {key: _safe_report(item, api_key) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_report(item, api_key) for item in value]
    return value


def classify_response(status: int, data: object) -> dict:
    metadata = error_metadata(data)
    code = redact(metadata.get("code", ""))[:120]
    upstream_type = redact(metadata.get("type", ""))[:120]
    message = redact(metadata.get("message", ""))[:600]
    if status == 429:
        hints = f'{code} {upstream_type} {message}'.lower()
        category = "concurrency_limit" if "concurrency" in hints else "rate_limit"
        action = "服务端并发或速率已达上限。减少同账号并发，稍后重试；不要把它计为模型推理失败。"
    elif status == 401:
        category, action = "authentication", "服务端拒绝凭据，请检查当前环境中的 Key 和接口地址。"
    elif status == 403:
        category, action = "access_denied", "检查服务端分组、模型权限或网关访问规则；不会自动绕过访问限制。"
    elif status == 404:
        category, action = "not_found", "确认模型名与 API 路径是否由当前服务支持。"
    elif status >= 500:
        category, action = "upstream_unavailable", "上游服务故障，稍后检查服务状态。"
    elif status >= 300:
        category, action = "request_rejected", "检查 API_STYLE、模型能力和请求格式；不会静默替换模型。"
    else:
        category, action = "ok", "接口已响应；模型列表存在不代表工具调用一定可用。"
    return {"category": category, "code": code or None, "upstream_type": upstream_type or None,
            "message": message or None, "action": action}


async def diagnose_provider(settings: Settings, *, smoke: bool = False,
                            transport: httpx.AsyncBaseTransport | None = None) -> dict:
    report = {"configured": settings.provider_configured, "model": settings.model,
              "api_style": settings.api_style, "host": urlsplit(settings.base_url).hostname,
              "checked_at": datetime.now(UTC).isoformat(), "checks": []}
    if not settings.provider_configured:
        report["checks"].append({"name": "configuration", "ok": False, "category": "configuration",
                                "action": "设置 TRACEFORGE_API_KEY 和 TRACEFORGE_MODEL 后重试。"})
        return _safe_report(report, settings.api_key)
    headers = {"Authorization": "Bearer " + settings.api_key, "User-Agent": "TraceForge/0.1"}
    timeout = httpx.Timeout(min(settings.request_timeout, 45), connect=min(settings.request_timeout, 10))
    deadline = time.monotonic() + min(settings.request_timeout, 45)
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=timeout) as client:
        jobs = [("models", "GET", "/models", None)]
        if smoke:
            body = {"model": settings.model}
            if settings.api_style == "responses":
                body.update(input="Reply with the single word OK.", max_output_tokens=64, store=False)
                path = "/responses"
            else:
                body.update(messages=[{"role": "user", "content": "Reply with the single word OK."}], max_completion_tokens=64)
                path = "/chat/completions"
            jobs.append(("minimal_generation", "POST", path, body))
        for name, method, path, body in jobs:
            started = time.monotonic()
            try:
                status, _response_headers, data = await bounded_json_request(
                    client, method, settings.base_url.rstrip("/") + path, headers=headers, payload=body,
                    timeout=min(settings.request_timeout, 45), deadline=deadline, max_bytes=DIAGNOSTIC_RESPONSE_BYTES,
                    read_error_metadata=True)
                if data is ERROR_METADATA_TIMEOUT:
                    # An HTTP error still has a useful status, but a body timeout
                    # must close the shared budget without consulting timer drift.
                    deadline = min(deadline, time.monotonic())
                    data = None
                # Remove the actual credential before truncating any upstream field.
                # The request helper returns only allowlisted error metadata on non-2xx.
                if not 200 <= status < 300:
                    data = _safe_report(data, settings.api_key)
                check = {"name": name, "ok": status == 200, "http_status": status,
                         **classify_response(status, data if not 200 <= status < 300 else {})}
                if name == "models" and status == 200:
                    listed = data.get("data")
                    if not isinstance(listed, list) or any(not isinstance(item, dict)
                                                         or not isinstance(item.get('id'), str) for item in listed):
                        raise ProviderError('模型列表响应结构无效', fault_type='format')
                    ids = [_safe_report(item['id'], settings.api_key)[:200] for item in listed]
                    check.update(listed_models=ids[:100],
                                 selected_model_listed=any(settings.model == item['id'] for item in listed))
                if smoke and name == "minimal_generation" and status == 200:
                    check["usage"] = usage_counts(data)
                    check["action"] = "最小生成请求成功；此检查没有验证 Function Calling 或修复效果。"
            except ProviderError as exc:
                check = {"name": name, "ok": False, "category": exc.fault_type, "error_type": type(exc).__name__,
                         "action": "服务响应未通过格式或大小校验；检查接口兼容性与响应限制。"}
            except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
                if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
                    deadline = min(deadline, time.monotonic())
                check = {"name": name, "ok": False,
                         "category": "timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else "transport",
                         "error_type": type(exc).__name__,
                         "action": "未收到有效服务响应。检查网络、TLS、代理配置或服务可达性。"}
            check["latency_ms"] = round((time.monotonic() - started) * 1000)
            report["checks"].append(check)
    # A non-standard upstream could put the authorization string in its error fields.
    return _safe_report(report, settings.api_key)


async def doctor(settings: Settings, *, smoke: bool = False) -> dict:
    from .api import docker_available
    return {"python": platform.python_version(), "git_available": bool(shutil.which("git")),
            "github_cli_available": bool(shutil.which("gh")),
            "docker_daemon_available": await asyncio.to_thread(docker_available),
            "data_dir": str(settings.data_dir), "allowed_roots": [str(p) for p in settings.allowed_roots],
            "provider": await diagnose_provider(settings, smoke=smoke)}
