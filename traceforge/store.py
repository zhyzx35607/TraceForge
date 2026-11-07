from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def patch_digest(patches: list[dict]) -> str:
    return hashlib.sha256(encode(patches).encode()).hexdigest()


def sanitized(value: Any) -> Any:
    from .retrieval import redact
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: sanitized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitized(item) for item in value]
    return value


class ConflictError(ValueError):
    pass


class LeaseLostError(RuntimeError):
    pass


_job_context: ContextVar[dict | None] = ContextVar("traceforge_job_context", default=None)


TERMINAL = {"completed", "failed", "cancelled", "rejected"}
TRANSITIONS = {
    "queued": {"investigating", "cancelled", "failed"},
    "investigating": {"awaiting_approval", "completed", "cancelled", "failed"},
    "awaiting_approval": {"verifying", "rejected", "cancelled", "failed"},
    "verifying": {"completed", "failed", "cancelled"},
}


class Store:
    """Connection-per-operation SQLite WAL store with transactional job leases.

    Transitions, state updates, and audit events commit together. A single machine
    deployment can run several worker loops; SQLite is not a distributed queue.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL, query TEXT NOT NULL, mode TEXT NOT NULL,
                    status TEXT NOT NULL, state TEXT NOT NULL DEFAULT '{}',
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE, request_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
                CREATE TABLE IF NOT EXISTS jobs (
                    run_id TEXT NOT NULL REFERENCES runs(id), phase TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', owner TEXT,
                    lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(run_id,phase)
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id), summary TEXT NOT NULL,
                    evidence_ids TEXT NOT NULL, created_at TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def fenced_job(self, run_id: str, phase: str, owner: str):
        """Fence worker writes; ContextVars also propagate through asyncio.to_thread."""
        token = _job_context.set({"run_id": run_id, "phase": phase, "owner": owner})
        try:
            yield
        finally:
            _job_context.reset(token)

    @staticmethod
    def _check_fence(db, run_id: str):
        job = _job_context.get()
        if not job or job["run_id"] != run_id:
            return
        row = db.execute("SELECT owner,status,lease_until FROM jobs WHERE run_id=? AND phase=?",
                         (run_id, job["phase"])).fetchone()
        if not row or row["owner"] != job["owner"] or row["status"] != "running" or row["lease_until"] <= time.time():
            raise LeaseLostError("任务租约已失效；旧 worker 不再允许写入状态或事件")

    def current_lease_valid(self, run_id: str) -> bool:
        try:
            with self.connect() as db:
                self._check_fence(db, run_id)
            return True
        except LeaseLostError:
            return False

    @staticmethod
    def _run(row) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["state"] = json.loads(result["state"])
        result.pop("request_hash", None)
        result.pop("idempotency_key", None)
        return result

    @staticmethod
    def _event(db, run_id: str, kind: str, payload: dict) -> dict:
        # Import lazily to keep initialization independent of adapter modules.
        payload = sanitized(payload)
        now = utcnow()
        cursor = db.execute("INSERT INTO events(run_id,kind,payload,created_at) VALUES(?,?,?,?)",
                            (run_id, kind, encode(payload), now))
        return {"id": cursor.lastrowid, "run_id": run_id, "kind": kind, "payload": payload, "created_at": now}

    def get_project(self, project_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM projects WHERE id=?", (project_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def list_projects(self) -> list[dict]:
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM projects ORDER BY created_at DESC")]

    def create_project(self, data: dict) -> dict:
        result = {**data, "id": uuid.uuid4().hex, "created_at": utcnow()}
        with self.transaction() as db:
            db.execute("INSERT INTO projects VALUES(?,?,?)", (result["id"], encode(result), result["created_at"]))
        return result

    def create_run(self, data: dict, idempotency_key: str | None = None) -> dict:
        from .retrieval import redact
        data = {**data, "query": redact(data["query"])}
        request_hash = hashlib.sha256(encode(data).encode()).hexdigest()
        with self.transaction() as db:
            if idempotency_key:
                previous = db.execute("SELECT * FROM runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise ConflictError("Idempotency-Key 已用于不同的请求")
                    return self._run(previous)
            run_id, now = uuid.uuid4().hex, utcnow()
            db.execute("""INSERT INTO runs(id,project_id,title,query,mode,status,state,created_at,updated_at,
                       idempotency_key,request_hash) VALUES(?,?,?,?,?,'queued','{}',?,?,?,?)""",
                       (run_id, data["project_id"], data["title"], data["query"], data["mode"], now, now,
                        idempotency_key, request_hash))
            self._event(db, run_id, "run.created", {"mode": data["mode"], "status": "queued"})
            db.execute("INSERT INTO jobs(run_id,phase) VALUES(?,'investigate')", (run_id,))
            return self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as db:
            return self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def report_snapshot(self, run_id: str) -> dict | None:
        """Read state and its event boundary from one WAL snapshot, without a write lock."""
        with self.connect() as db:
            db.execute("BEGIN")
            try:
                run = self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
                if run is None:
                    return None
                project = db.execute("SELECT data FROM projects WHERE id=?", (run["project_id"],)).fetchone()
                # A portable report never needs arbitrary event payloads or the
                # entire historical event table in memory. Keep the latest boundary.
                rows = db.execute("SELECT id,kind,created_at FROM events WHERE run_id=? "
                                  "ORDER BY id DESC LIMIT 1001", (run_id,)).fetchall()
                return {"run": run, "project": json.loads(project[0]) if project else {},
                        "events": [dict(row) for row in reversed(rows[:1000])],
                        "events_truncated": len(rows) > 1000}
            finally:
                db.rollback()

    def list_runs(self) -> list[dict]:
        with self.connect() as db:
            return [self._run(row) for row in db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 200")]

    def update_run(self, run_id: str, *, status: str | None = None, state: dict | None = None,
                   error: str | None = None) -> dict:
        from .retrieval import redact
        with self.transaction() as db:
            self._check_fence(db, run_id)
            run = self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if not run:
                raise KeyError(run_id)
            if run["status"] in TERMINAL:
                return run
            old_status = run["status"]
            if status and status != old_status and status not in TRANSITIONS.get(old_status, set()):
                raise ConflictError(f"非法状态转换：{old_status} -> {status}")
            merged = {**run["state"], **(state or {})}
            # Avoid writing secrets included in model/tool output.
            merged = sanitized(merged)
            now = utcnow()
            db.execute("UPDATE runs SET status=?,state=?,error=?,updated_at=? WHERE id=?",
                       (status or old_status, encode(merged), redact(error) if error else run["error"], now, run_id))
            if status and status != old_status:
                self._event(db, run_id, "run.status", {"from": old_status, "to": status})
            return self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def add_event(self, run_id: str, kind: str, payload: dict) -> dict:
        with self.transaction() as db:
            self._check_fence(db, run_id)
            return self._event(db, run_id, kind, payload)

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        with self.connect() as db:
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute(
                "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 1000", (run_id, after))]

    def decide(self, run_id: str, decision: str, digest: str) -> dict:
        with self.transaction() as db:
            run = self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if not run:
                raise KeyError(run_id)
            patches = run["state"].get("diagnosis", {}).get("patches", [])
            if run["status"] != "awaiting_approval":
                raise ConflictError("任务当前不在等待审批状态")
            if not patches or digest != run["state"].get("patch_digest") or digest != patch_digest(patches):
                raise ConflictError("补丁摘要不一致，请刷新后重新审阅")
            now = utcnow()
            state = {**run["state"], "approval": {"decision": decision, "patch_digest": digest, "approved_at": now}}
            status = "verifying" if decision == "approve" else "rejected"
            db.execute("UPDATE runs SET status=?,state=?,updated_at=? WHERE id=?", (status, encode(state), now, run_id))
            self._event(db, run_id, "approval.decided", {"decision": decision, "patch_digest": digest})
            self._event(db, run_id, "run.status", {"from": run["status"], "to": status})
            if decision == "approve":
                db.execute("INSERT OR IGNORE INTO jobs(run_id,phase) VALUES(?,'verify')", (run_id,))
            return self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def claim_job(self, owner: str, lease_seconds: float = 30) -> dict | None:
        now = time.time()
        with self.transaction() as db:
            # Terminal runs cannot execute orphaned pending work.
            db.execute("""UPDATE jobs SET status='done' WHERE status!='done' AND run_id IN
                       (SELECT id FROM runs WHERE status IN ('completed','failed','cancelled','rejected'))""")
            row = db.execute("""SELECT jobs.* FROM jobs JOIN runs ON runs.id=jobs.run_id
                              WHERE (jobs.status='pending' OR (jobs.status='running' AND jobs.lease_until<?))
                              AND ((jobs.phase='investigate' AND runs.status IN ('queued','investigating'))
                                   OR (jobs.phase='verify' AND runs.status='verifying'))
                              ORDER BY runs.created_at LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            db.execute("UPDATE jobs SET status='running',owner=?,lease_until=?,attempts=attempts+1 WHERE run_id=? AND phase=?",
                       (owner, now + lease_seconds, row["run_id"], row["phase"]))
            if result["attempts"]:
                self._event(db, row["run_id"], "run.recovered", {"phase": row["phase"], "attempt": result["attempts"] + 1})
            return {**result, "owner": owner, "attempts": result["attempts"] + 1}

    def renew_job(self, run_id: str, phase: str, owner: str, lease_seconds: float = 30) -> bool:
        with self.transaction() as db:
            now = time.time()
            return db.execute("""UPDATE jobs SET lease_until=? WHERE run_id=? AND phase=? AND owner=?
                              AND status='running' AND lease_until>?""",
                              (now + lease_seconds, run_id, phase, owner, now)).rowcount == 1

    def finish_job(self, run_id: str, phase: str, owner: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE jobs SET status='done',lease_until=0 WHERE run_id=? AND phase=? AND owner=? AND lease_until>?",
                       (run_id, phase, owner, time.time()))

    def release_owner(self, owner: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE owner=? AND status='running'", (owner,))

    def save_memory(self, project_id: str, run_id: str, summary: str, evidence_ids: list[str]) -> None:
        from .retrieval import redact
        with self.transaction() as db:
            self._check_fence(db, run_id)
            run = self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if not run or run["project_id"] != project_id or run["status"] != "completed" or not run["state"].get(
                    "verification", {}).get("regression_fixed"):
                raise ConflictError("只有已完成且通过修复验证的任务可以保存记忆")
            db.execute("INSERT OR IGNORE INTO memories VALUES(?,?,?,?,?,?)",
                       (uuid.uuid4().hex, project_id, run_id, redact(summary), encode(evidence_ids), utcnow()))

    def complete_verification(self, run_id: str, result: dict) -> dict:
        """Commit the result, completion event and verified memory as one unit."""
        with self.transaction() as db:
            self._check_fence(db, run_id)
            run = self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if not run:
                raise KeyError(run_id)
            if run["status"] in TERMINAL:
                return run
            if run["status"] != "verifying":
                raise ConflictError("任务尚未进入验证阶段")
            result = sanitized(result)
            state = {**run["state"], "verification": result, "phase": "completed"}
            now = utcnow()
            db.execute("UPDATE runs SET status='completed',state=?,updated_at=? WHERE id=?",
                       (encode(state), now, run_id))
            self._event(db, run_id, "verification.completed", {
                "regression_fixed": result["regression_fixed"], "baseline_exit": result["baseline"]["exit_code"],
                "patched_exit": result["patched"]["exit_code"]})
            self._event(db, run_id, "run.status", {"from": "verifying", "to": "completed"})
            if result["regression_fixed"]:
                diagnosis = state["diagnosis"]
                db.execute("INSERT OR IGNORE INTO memories VALUES(?,?,?,?,?,?)",
                           (uuid.uuid4().hex, run["project_id"], run_id, diagnosis["summary"],
                            encode(diagnosis["evidence_ids"]), now))
            return self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def memories(self, project_id: str) -> list[dict]:
        with self.connect() as db:
            return [{**dict(r), "evidence_ids": json.loads(r["evidence_ids"])} for r in db.execute(
                "SELECT * FROM memories WHERE project_id=? ORDER BY created_at DESC LIMIT 100", (project_id,))]

    def metrics(self) -> dict:
        with self.connect() as db:
            rows = list(db.execute("SELECT status,state FROM runs"))
        states = [json.loads(r["state"]) for r in rows]
        result = {"total_runs": len(rows), "completed_runs": sum(r["status"] == "completed" for r in rows),
                  "verified_fixes": sum(r["status"] == "completed" and bool(
                      s.get("verification", {}).get("regression_fixed")) for r, s in zip(rows, states, strict=True)),
                  "failed_runs": sum(r["status"] == "failed" for r in rows),
                  "approval_pending": sum(r["status"] == "awaiting_approval" for r in rows)}
        for key in ("provider_calls", "input_tokens", "output_tokens"):
            result[key] = sum(int(s.get("usage", {}).get(key, 0)) for s in states)
        return result
