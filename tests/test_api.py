from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traceforge.api import create_app
from traceforge.config import DEMO_ROOT, Settings
from traceforge.store import patch_digest


@pytest.fixture(autouse=True)
def isolated_docker_capability(monkeypatch):
    # API contract tests do not depend on the host daemon. Real Docker startup
    # and snapshot execution are exercised by the separate acceptance run.
    monkeypatch.setattr("traceforge.api.docker_available", lambda: False)


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", allowed_roots=[tmp_path, DEMO_ROOT])
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        yield client


def test_health_does_not_return_provider_key(client):
    body = client.get("/api/health").json()
    assert body["provider"]["configured"] is False
    assert "api_key" not in str(body)


def test_register_rejects_unapproved_root_and_untrusted_local(client, tmp_path):
    body = {"name": "bad", "root": str(Path.home()), "runner": "docker"}
    assert client.post("/api/projects", json=body).status_code == 400
    assert client.post("/api/projects", json={**body, "root": str(tmp_path), "runner": "local"}).status_code == 400


def test_demo_queue_and_cancel(client):
    response = client.post("/api/demo")
    assert response.status_code == 201
    run = response.json()["run"]
    assert run["mode"] == "replay"
    assert client.get("/api/runs").json()[0]["id"] == run["id"]
    assert client.post(f"/api/runs/{run['id']}/cancel").json()["status"] == "cancelled"
    assert client.get("/api/metrics").json()["total_runs"] == 1


def test_live_mode_requires_configuration(client):
    demo = client.post("/api/demo").json()
    body = {"project_id": demo["project"]["id"], "title": "live", "query": "failure", "mode": "live"}
    assert client.post("/api/runs", json=body).status_code == 400


def test_approval_digest_gate_via_api(client):
    run = client.post("/api/demo").json()["run"]
    store = client.app.state.store
    patches = [{"path": "pricing.py", "before": "bad", "after": "good", "reason": "proof", "evidence_ids": ["e1"]}]
    digest = patch_digest(patches)
    store.update_run(run["id"], status="investigating")
    store.update_run(run["id"], status="awaiting_approval", state={"diagnosis": {"patches": patches}, "patch_digest": digest})
    url = f"/api/runs/{run['id']}/approve"
    assert client.post(url, json={"decision": "approve", "patch_digest": "0" * 64}).status_code == 409
    assert client.post(url, json={"decision": "approve", "patch_digest": digest}).json()["status"] == "verifying"
    assert client.post(url, json={"decision": "approve", "patch_digest": digest}).status_code == 409


def test_cross_site_and_dns_rebinding_are_denied(client):
    assert client.post("/api/demo", headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 403


@pytest.mark.parametrize("origin", ["http://127.0.0.1:9999", "http://localhost:8710", "https://127.0.0.1:8710"])
def test_other_loopback_origins_cannot_create_or_cancel_runs(client, origin):
    run = client.post("/api/demo").json()["run"]
    store = client.app.state.store
    before_run = store.get_run(run["id"])
    before_events = store.events(run["id"])
    before_runs = store.list_runs()
    headers = {"Host": "127.0.0.1:8710", "Origin": origin, "Sec-Fetch-Mode": "no-cors"}
    assert client.post("/api/demo", headers=headers).status_code == 403
    assert client.post(f"/api/runs/{run['id']}/cancel", headers=headers).status_code == 403
    assert store.get_run(run["id"]) == before_run
    assert store.events(run["id"]) == before_events
    assert store.list_runs() == before_runs


@pytest.mark.parametrize(("host", "origin"), [
    ("localhost:8710", "http://localhost:8710"),
    ("LOCALHOST:8710", "HTTP://localhost:8710"),
    ("127.0.0.1", "http://127.0.0.1:80"),
    ("127.0.0.1:80", "http://127.0.0.1"),
    ("[::1]:8710", "http://[::1]:8710"),
    ("[0:0:0:0:0:0:0:1]:8710", "http://[::1]:8710"),
    # Vite's default proxy keeps the browser's Host, despite using backend port 8710.
    ("localhost:5173", "http://localhost:5173"),
])
def test_same_origin_and_vite_preserved_host_are_allowed(client, host, origin):
    response = client.post("/api/demo", headers={"Host": host, "Origin": origin})
    assert response.status_code == 201
    assert client.app.state.store.get_run(response.json()["run"]["id"]) is not None


@pytest.mark.parametrize("origin", [
    "", "null", "localhost", "http:localhost", "//localhost", "ftp://localhost", "file://localhost",
    "http://localhost/", "http://localhost/path", "http://localhost?", "http://localhost?q=x",
    "http://localhost#", "http://localhost#fragment", "http://user@localhost", "http://@localhost",
    "http://user:password@localhost", "http://localhost:", "http://localhost:no", "http://localhost:65536",
    "http://[broken", "http://[::1]junk", "http://local\\host", "http://local\thost",
    " http://localhost", "http://localhost ", "http://localhost,http://attacker.example",
])
def test_malformed_origin_is_rejected_without_creating_state(client, origin):
    assert client.post("/api/demo", headers={"Host": "localhost", "Origin": origin}).status_code == 400
    assert client.app.state.store.list_projects() == []
    assert client.app.state.store.list_runs() == []


@pytest.mark.parametrize("host", ["", "localhost:", "localhost:no", "localhost:65536", "user@localhost",
                                 "localhost/path", "localhost?", "localhost#", "local\thost", "[::1]junk"])
def test_malformed_host_is_rejected(client, host):
    assert client.post("/api/demo", headers={"Host": host}).status_code == 400
    assert client.app.state.store.list_runs() == []


def test_duplicate_origin_or_host_headers_are_rejected(client):
    assert client.post("/api/demo", headers=[("Host", "localhost"), ("Host", "localhost")]).status_code == 400
    assert client.post("/api/demo", headers=[("Origin", "http://testserver"),
                                            ("Origin", "http://testserver")]).status_code == 400
    assert client.app.state.store.list_runs() == []


def test_token_remote_same_origin_https_and_cli_remain_supported(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token="test-only-token"), start_workers=False)
    with TestClient(app, base_url="https://workbench.example") as remote:
        headers = {"Authorization": "Bearer test-only-token"}
        assert remote.get("/api/health", headers=headers).status_code == 200
        assert remote.post("/api/demo", headers={**headers, "Origin": "https://workbench.example:443"}).status_code == 201
        assert remote.get("/api/health", headers={**headers, "Host": "workbench.example:443",
                                                "Origin": "https://workbench.example"}).status_code == 200
        for origin in ("http://workbench.example", "https://workbench.example:8443", "http://localhost:5173"):
            assert remote.post("/api/demo", headers={**headers, "Origin": origin}).status_code == 403
        assert len(app.state.store.list_runs()) == 1
        assert remote.get("/api/health", headers={"Origin": "https://workbench.example"}).status_code == 401


def test_optional_api_token(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token="test-only-token"), start_workers=False)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 401
        assert client.get("/api/health", headers={"Authorization": "Bearer test-only-token"}).status_code == 200
        assert client.get("/api/health", headers={b"Authorization": b"Bearer \xff"}).status_code == 401


def test_ipv6_local_host_and_malformed_origin(client):
    assert client.get("/api/health", headers={"Host": "[::1]:8710"}).status_code == 200
    assert client.get("/api/health", headers={"Origin": "http://[broken"}).status_code == 400


@pytest.mark.asyncio
async def test_worker_exception_after_lease_loss_cannot_fail_run(tmp_path, monkeypatch):
    import asyncio

    from traceforge.api import worker_loop
    from traceforge.store import Store

    settings = Settings(data_dir=tmp_path)
    store = Store(tmp_path / "worker.db")
    project = store.create_project({"name": "lease", "root": str(tmp_path)})
    run = store.create_run({"project_id": project["id"], "title": "race", "query": "error", "mode": "investigate"})
    stopped = asyncio.Event()

    async def lose_lease_then_raise(self, run_id):
        self.store.update_run(run_id, status="investigating")
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE run_id=?", (run_id,))
        stopped.set()
        raise RuntimeError("late exception from old worker")

    monkeypatch.setattr("traceforge.engine.Engine.investigate", lose_lease_then_raise)
    await worker_loop(store, settings, stopped, 0)
    assert store.get_run(run["id"])["status"] == "investigating"
    assert not any(event["kind"] == "run.error" for event in store.events(run["id"]))
    assert store.claim_job("replacement")["run_id"] == run["id"]


def test_artifact_traversal_is_rejected(client, tmp_path):
    run = client.post("/api/demo").json()["run"]
    secret = tmp_path / "outside.txt"
    secret.write_text("outside", encoding="utf-8")
    client.app.state.store.update_run(run["id"], state={"verification": {"artifact_path": str(secret)}})
    assert client.get(f"/api/runs/{run['id']}/artifact").status_code == 404


def test_report_download_formats_and_read_only_snapshot(client):
    run = client.post("/api/demo").json()["run"]
    store = client.app.state.store
    before = store.get_run(run["id"])
    events_before = store.events(run["id"])
    url = f"/api/runs/{run['id']}/report"
    document = client.get(url + "?format=json")
    assert document.status_code == 200
    assert document.headers["cache-control"] == "no-store"
    assert document.headers["x-content-type-options"] == "nosniff"
    assert document.headers["content-disposition"].endswith('.json"')
    body = document.json()
    assert body["run"]["id"] == run["id"]
    assert body["run"]["mode"] == "replay"
    assert body["verification"]["verified"] is False
    assert len(body["content_sha256"]) == 64
    markdown = client.get(url)
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.headers["content-disposition"].endswith('.md"')
    assert "TraceForge" in markdown.text
    assert client.get(url + "?format=html").status_code == 422
    assert client.get("/api/runs/not-a-run/report").status_code == 404
    assert store.get_run(run["id"]) == before
    assert store.events(run["id"]) == events_before


def test_report_download_requires_token_and_removes_configured_secrets(tmp_path):
    api_key, token = "synthetic-provider-secret", "synthetic-access-token"
    app = create_app(Settings(data_dir=tmp_path, api_key=api_key, api_token=token), start_workers=False)
    store = app.state.store
    project = store.create_project({"name": "private", "root": str(tmp_path)})
    run = store.create_run({"project_id": project["id"], "title": f"Issue {api_key}",
                            "query": f"Symptom {token} {tmp_path}", "mode": "investigate"})
    store.update_run(run["id"], state={"agent_messages": [{"role": "assistant", "content": "private-continuation"}]})
    with TestClient(app) as client:
        url = f"/api/runs/{run['id']}/report"
        assert client.get(url).status_code == 401
        for format in ("json", "markdown"):
            response = client.get(f"{url}?format={format}", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert all(value not in response.text for value in (api_key, token, "private-continuation"))
            assert str(tmp_path).replace("\\", "\\\\") not in response.text
            assert "[REDACTED]" in response.text or "REDACTED" in response.text


def test_retrieval_evaluation_saved_report_and_controlled_errors(client):
    import json
    url = '/api/retrieval-evaluation'
    missing = client.get(url)
    assert missing.status_code == 404 and missing.json()['available'] is False
    path = client.app.state.settings.data_dir / 'retrieval-evaluation.json'
    report = {'kind': 'synthetic-retrieval-pilot', 'provider_calls': 0, 'available': False,
              'methods': {'bm25': {'mrr': 0.5}}}
    path.write_text(json.dumps(report), encoding='utf-8')
    loaded = client.get(url)
    assert loaded.status_code == 200
    assert loaded.json() == {**report, 'available': True}
    for invalid in ('{invalid', '[]', '{"kind": "unrelated"}'):
        path.write_text(invalid, encoding='utf-8')
        response = client.get(url)
        assert response.status_code == 500
        assert '报告不可读' in response.json()['detail']


def test_retrieval_report_is_authenticated(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token='local-test-token'), start_workers=False)
    with TestClient(app) as client:
        assert client.get('/api/retrieval-evaluation').status_code == 401
