from __future__ import annotations

import asyncio
import hmac
import json
import logging
import shutil
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import DEMO_ROOT, PACKAGE_ROOT, Settings
from .models import Approval, ProjectCreate, RunCreate
from .store import TERMINAL, ConflictError, LeaseLostError, Store, encode

log = logging.getLogger("traceforge")


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                              timeout=4, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


async def worker_loop(store: Store, settings: Settings, stopped: asyncio.Event, slot: int) -> None:
    from .engine import Engine
    engine = Engine(store, settings)
    owner = f"{uuid.uuid4().hex}:{slot}"
    try:
        while not stopped.is_set():
            job = await asyncio.to_thread(store.claim_job, owner)
            if not job:
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=0.4)
                except TimeoutError:
                    pass
                continue

            async def heartbeat(current_job=job):
                while True:
                    await asyncio.sleep(8)
                    owned = await asyncio.to_thread(store.renew_job, current_job["run_id"], current_job["phase"], owner)
                    if not owned:
                        return

            lease = asyncio.create_task(heartbeat())
            try:
                with store.fenced_job(job["run_id"], job["phase"], owner):
                    try:
                        if job["attempts"] > 3:
                            store.update_run(job["run_id"], status="failed", error="进程恢复已超过 3 次，请检查故障原因后创建新任务")
                        elif job["phase"] == "investigate":
                            await engine.investigate(job["run_id"])
                        else:
                            await engine.verify(job["run_id"])
                    except (asyncio.CancelledError, LeaseLostError):
                        raise
                    except Exception as exc:  # noqa: BLE001 - durable worker boundary
                        from .retrieval import redact
                        message = redact(f"{type(exc).__name__}: {exc}")[:4000]
                        log.error("run %s failed: %s", job["run_id"], message)
                        store.add_event(job["run_id"], "run.error", {"message": message})
                        store.update_run(job["run_id"], status="failed", error=message)
            except asyncio.CancelledError:
                raise
            except LeaseLostError:
                log.warning("Worker lost lease for run %s; writes fenced", job["run_id"])
            finally:
                lease.cancel()
                with suppress(asyncio.CancelledError):
                    await lease
            await asyncio.to_thread(store.finish_job, job["run_id"], job["phase"], owner)
    finally:
        store.release_owner(owner)


def create_app(settings: Settings | None = None, *, start_workers: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir / "traceforge.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.docker_available = await asyncio.to_thread(docker_available)
        stopped = asyncio.Event()
        tasks = [asyncio.create_task(worker_loop(store, settings, stopped, slot)) for slot in range(2)] if start_workers else []
        yield
        stopped.set()
        # Durable leases let interrupted work resume on restart. Engine/runner
        # cancellation terminates child processes before releasing ownership.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="TraceForge", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.state.settings = settings
    app.state.docker_available = False

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        if request.scope["path"].startswith("/api/"):
            origin = request.headers.get("origin")
            try:
                host_parts = urlsplit("//" + request.headers.get("host", ""))
                host = host_parts.hostname
                if host_parts.username or host_parts.password:
                    raise ValueError("invalid host")
                origin_parts = urlsplit(origin) if origin else None
                origin_host = origin_parts.hostname if origin_parts else None
            except ValueError:
                return JSONResponse({"detail": "无效的 Host 或 Origin"}, status_code=400)
            if not settings.api_token and host not in {"127.0.0.1", "localhost", "testserver", "::1"}:
                return JSONResponse({"detail": "远程访问必须配置 TRACEFORGE_API_TOKEN"}, status_code=403)
            if (origin_parts and origin_host not in {"127.0.0.1", "localhost", "::1"}
                    and origin_parts.netloc != request.headers.get("host")):
                return JSONResponse({"detail": "不允许跨站访问本地工作台"}, status_code=403)
            if settings.api_token:
                expected = f"Bearer {settings.api_token}"
                if not hmac.compare_digest(request.headers.get("authorization", "").encode("utf-8"), expected.encode("utf-8")):
                    return JSONResponse({"detail": "需要有效的 API Token"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def require_run(run_id: str) -> dict:
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404, "任务不存在")
        return run

    def public_run(run: dict) -> dict:
        # Provider continuation items and internal checkpoints stay server-side.
        state = {key: value for key, value in run["state"].items()
                 if key not in {"agent_messages", "tool_repetitions"}}
        return {**run, "state": state}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__,
                "provider": {"configured": settings.provider_configured, "model": settings.model,
                             "api_style": settings.api_style},
                "capabilities": {"docker_available": app.state.docker_available},
                "allowed_roots": [str(p) for p in settings.allowed_roots]}

    @app.get("/api/projects")
    def list_projects():
        return store.list_projects()

    @app.post("/api/projects", status_code=201)
    def create_project(data: ProjectCreate):
        try:
            root = settings.validate_root(data.root)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if data.runner == "local" and not data.trusted:
            raise HTTPException(400, "本机执行仅允许已明确标记为可信的仓库；其他仓库请选择 Docker")
        return store.create_project({**data.model_dump(), "root": str(root)})

    @app.post("/api/demo", status_code=201)
    def create_demo():
        project = next((p for p in store.list_projects() if Path(p["root"]) == DEMO_ROOT.resolve()), None)
        if project is None:
            project = store.create_project({"name": "Checkout · 数量计算回归", "root": str(DEMO_ROOT.resolve()),
                                            "test_command": [sys.executable, "-m", "unittest", "discover", "-v"],
                                            "runner": "local", "trusted": True, "github_repo": None})
        run = store.create_run({"project_id": project["id"], "title": "结算金额遗漏商品数量",
                                "query": "购买3件19.90元的商品，calculate_total返回19.90，预期59.70。CI的数量、折扣、多行和零数量测试失败。请定位并修复，保留现有测试。",
                                "mode": "replay"})
        return {"project": project, "run": public_run(run)}

    @app.get("/api/runs")
    def list_runs():
        return [public_run(run) for run in store.list_runs()]

    @app.post("/api/runs", status_code=201)
    def create_run(data: RunCreate, idempotency_key: str | None = Header(None)):
        project = store.get_project(data.project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        if data.mode == "live" and not settings.provider_configured:
            raise HTTPException(400, "请在服务端设置 TRACEFORGE_API_KEY 和 TRACEFORGE_MODEL")
        if data.mode == "replay" and Path(project["root"]).resolve() != DEMO_ROOT.resolve():
            raise HTTPException(400, "离线回放只支持内置 Checkout 样例；真实项目请选择检索调查或真实模型")
        if idempotency_key and len(idempotency_key) > 200:
            raise HTTPException(400, "Idempotency-Key 最长 200 字符")
        return public_run(store.create_run(data.model_dump(), idempotency_key=idempotency_key))

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return public_run(require_run(run_id))

    @app.get("/api/runs/{run_id}/events")
    def get_events(run_id: str, after: int = 0):
        require_run(run_id)
        return store.events(run_id, max(0, after))

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request, after: int = 0, last_event_id: str | None = Header(None)):
        require_run(run_id)
        try:
            cursor = max(after, int(last_event_id or 0))
        except ValueError:
            raise HTTPException(400, "Last-Event-ID 必须是整数")

        async def events():
            nonlocal cursor
            while not await request.is_disconnected():
                rows = await asyncio.to_thread(store.events, run_id, cursor)
                for event in rows:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: trace\ndata: {encode(event)}\n\n"
                run = await asyncio.to_thread(store.get_run, run_id)
                if run["status"] in TERMINAL and len(rows) < 1000:
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: str, body: Approval):
        require_run(run_id)
        return public_run(store.decide(run_id, body.decision, body.patch_digest))

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        run = require_run(run_id)
        if run["status"] not in TERMINAL:
            run = store.update_run(run_id, status="cancelled", state={"cancel_requested": True})
            store.add_event(run_id, "run.cancelled", {"requested_by": "user"})
        return public_run(run)

    @app.get("/api/runs/{run_id}/artifact")
    def artifact(run_id: str):
        run = require_run(run_id)
        raw = run["state"].get("verification", {}).get("artifact_path")
        if not raw:
            raise HTTPException(404, "当前任务尚无补丁文件")
        path = Path(raw).resolve()
        if not path.is_relative_to(settings.data_dir.resolve()) or not path.is_file():
            raise HTTPException(404, "补丁文件不可用")
        return FileResponse(path, media_type="text/x-diff", filename=f"traceforge-{run_id[:8]}.patch")

    @app.get("/api/metrics")
    def metrics():
        return store.metrics()

    @app.get("/api/memories")
    def memories(project_id: str):
        if not store.get_project(project_id):
            raise HTTPException(404, "项目不存在")
        return store.memories(project_id)

    @app.get("/api/evaluation")
    def evaluation():
        path = settings.data_dir / "evaluation.json"
        if not path.is_file():
            path = PACKAGE_ROOT / "evals" / "results" / "offline.json"
        if path.is_file():
            return {"available": True, **json.loads(path.read_text(encoding="utf-8"))}
        return {"available": False}

    @app.get("/api/benchmarks/latest")
    def benchmark_latest():
        path = settings.data_dir / "benchmark" / "latest.json"
        if not path.is_file():
            return JSONResponse({"available": False, "message": "尚未运行多故障基准；请执行 traceforge benchmark"}, status_code=404)
        try:
            return {"available": True, **json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, ValueError) as exc:
            raise HTTPException(500, f"基准报告不可读：{type(exc).__name__}") from exc

    dist = PACKAGE_ROOT / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    else:
        @app.get("/")
        def index():
            return {"name": "TraceForge", "message": "运行 web 目录的 npm install && npm run build，或启动 Vite 开发服务器", "docs": "/docs"}
    return app
