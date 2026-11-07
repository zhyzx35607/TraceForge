"""Real OS process restart: an approval survives and the source stays unchanged."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx

from traceforge.config import DEMO_ROOT, PACKAGE_ROOT


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def server(tmp_path, port):
    env = {key: value for key, value in os.environ.items() if not key.startswith("TRACEFORGE_")}
    env.update(TRACEFORGE_DATA_DIR=str(tmp_path / "state"), PYTHONIOENCODING="utf-8")
    with (tmp_path / "server.log").open("ab") as log:
        proc = subprocess.Popen([sys.executable, "-m", "traceforge", "serve", "--port", str(port)],
                                cwd=PACKAGE_ROOT, env=env, stdout=log, stderr=log,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False) as client:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    assert proc.poll() is None, (tmp_path / "server.log").read_text(errors="replace")
                    try:
                        if client.get("/api/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        time.sleep(0.1)
                else:
                    raise AssertionError("API process did not become ready")
                yield client, proc
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def poll(client, run_id, expected):
    deadline = time.monotonic() + 25
    run = None
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in expected:
            return run
        assert run["status"] != "failed", run
        time.sleep(0.1)
    raise AssertionError(json.dumps(run, ensure_ascii=False))


def test_approval_survives_real_server_process_restart(tmp_path):
    before = hashlib.sha256((DEMO_ROOT / "pricing.py").read_bytes()).hexdigest()
    port = free_port()
    with server(tmp_path, port) as (client, process):
        created = client.post("/api/demo").json()["run"]
        ready = poll(client, created["id"], {"awaiting_approval"})
        digest = ready["state"]["patch_digest"]
        events = client.get(f"/api/runs/{ready['id']}/events").json()
        assert all(e["kind"] != "verification.started" for e in events)
        first_pid = process.pid
    with server(tmp_path, port) as (client, process):
        assert process.pid != first_pid
        reopened = client.get(f"/api/runs/{ready['id']}").json()
        assert reopened["status"] == "awaiting_approval"
        assert reopened["state"]["patch_digest"] == digest
        response = client.post(f"/api/runs/{ready['id']}/approve", json={"decision": "approve", "patch_digest": digest})
        assert response.status_code == 200, response.text
        complete = poll(client, ready["id"], {"completed"})
        result = complete["state"]["verification"]
        assert result["baseline"]["tests"]["failed"] == 4
        assert result["patched"]["tests"]["passed"] == 8
        assert result["regression_fixed"]
        assert "agent_messages" not in complete["state"]
    assert hashlib.sha256((DEMO_ROOT / "pricing.py").read_bytes()).hexdigest() == before
