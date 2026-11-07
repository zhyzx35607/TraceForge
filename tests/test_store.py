from concurrent.futures import ThreadPoolExecutor

import pytest

from traceforge.store import ConflictError, LeaseLostError, Store, patch_digest


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state.db")


def make_run(store):
    project = store.create_project({"name": "test", "root": "/tmp/repo", "test_command": ["python", "-m", "unittest"],
                                    "trusted": True, "runner": "local"})
    return store.create_run({"project_id": project["id"], "title": "regression", "query": "broken total", "mode": "investigate"})


def ready(store):
    run = make_run(store)
    patches = [{"path": "source.py", "before": "x=1", "after": "x=2", "reason": "evidence", "evidence_ids": ["e1"]}]
    store.update_run(run["id"], status="investigating")
    store.update_run(run["id"], status="awaiting_approval", state={"diagnosis": {"patches": patches}, "patch_digest": patch_digest(patches)})
    return run["id"], patch_digest(patches)


def test_idempotency_is_bound_to_payload(store):
    run = make_run(store)
    body = {key: run[key] for key in ("project_id", "title", "query", "mode")}
    first = store.create_run(body, "unique-key")
    assert store.create_run(body, "unique-key")["id"] == first["id"]
    with pytest.raises(ConflictError):
        store.create_run({**body, "query": "another"}, "unique-key")


def test_approval_is_atomic_and_rejects_tampering(store):
    run_id, digest = ready(store)
    with pytest.raises(ConflictError):
        store.decide(run_id, "approve", "0" * 64)
    assert store.get_run(run_id)["status"] == "awaiting_approval"

    def approve():
        try:
            return store.decide(run_id, "approve", digest)["status"]
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve(), range(2)))
    assert sorted(results) == ["conflict", "verifying"]
    assert sum(e["kind"] == "approval.decided" for e in store.events(run_id)) == 1


def test_durable_lease_only_one_worker_and_recovers(store):
    run = make_run(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(store.claim_job, ["worker-a", "worker-b"]))
    job = next(x for x in results if x)
    assert sum(x is not None for x in results) == 1
    store.release_owner(job["owner"])
    reopened = Store(store.db_path)
    recovered = reopened.claim_job("worker-c")
    assert recovered["run_id"] == run["id"]
    assert recovered["attempts"] == 2
    assert any(e["kind"] == "run.recovered" for e in reopened.events(run["id"]))


def test_cancel_is_terminal_and_prevents_queue_execution(store):
    run = make_run(store)
    store.update_run(run["id"], status="cancelled")
    assert store.claim_job("late") is None
    assert store.update_run(run["id"], status="completed")["status"] == "cancelled"


def test_state_transition_and_event_commit_together(store):
    run = make_run(store)
    with pytest.raises(ConflictError):
        store.update_run(run["id"], status="completed", state={"bad": True})
    assert store.get_run(run["id"])["state"] == {}
    assert len(store.events(run["id"])) == 1
    store.update_run(run["id"], status="investigating", state={"checkpoint": "indexed"})
    reopened = Store(store.db_path)
    assert reopened.get_run(run["id"])["state"]["checkpoint"] == "indexed"
    assert reopened.events(run["id"])[-1]["payload"] == {"from": "queued", "to": "investigating"}


def test_memory_project_isolation_and_deduplication(store):
    a, b = make_run(store), make_run(store)
    store.update_run(a["id"], status="investigating")
    store.update_run(a["id"], status="completed", state={"verification": {"regression_fixed": True}})
    store.save_memory(a["project_id"], a["id"], "verified", ["e1"])
    store.save_memory(a["project_id"], a["id"], "duplicate", ["e1"])
    assert len(store.memories(a["project_id"])) == 1
    assert store.memories(b["project_id"]) == []


def test_event_cursor_is_monotonic(store):
    run = make_run(store)
    a = store.add_event(run["id"], "tool.started", {"name": "search"})
    b = store.add_event(run["id"], "tool.finished", {"name": "search"})
    assert [x["id"] for x in store.events(run["id"], a["id"])] == [b["id"]]


def test_expired_worker_cannot_write_after_lease_reassignment(store):
    run = make_run(store)
    job = store.claim_job("old-owner")
    with store.fenced_job(run["id"], job["phase"], "old-owner"):
        store.update_run(run["id"], status="investigating")
        store.release_owner("old-owner")
        replacement = store.claim_job("new-owner")
        assert replacement["run_id"] == run["id"]
        assert store.current_lease_valid(run["id"]) is False
        with pytest.raises(LeaseLostError):
            store.update_run(run["id"], status="failed", error="stale worker")
        with pytest.raises(LeaseLostError):
            store.add_event(run["id"], "stale.event", {})
        with pytest.raises(LeaseLostError):
            store.save_memory(run["project_id"], run["id"], "stale", [])
    assert store.get_run(run["id"])["status"] == "investigating"


def test_expired_worker_cannot_finish_before_reassignment(store):
    run = make_run(store)
    job = store.claim_job("old-owner")
    store.release_owner("old-owner")
    store.finish_job(run["id"], job["phase"], "old-owner")
    recovered = store.claim_job("replacement")
    assert recovered and recovered["run_id"] == run["id"]


def test_expired_heartbeat_cannot_resurrect_lease(store):
    run = make_run(store)
    job = store.claim_job("old-owner")
    store.release_owner("old-owner")
    assert store.renew_job(run["id"], job["phase"], "old-owner") is False
    assert store.claim_job("replacement")["run_id"] == run["id"]


def verification_ready(store):
    run_id, digest = ready(store)
    store.update_run(run_id, state={"diagnosis": {**store.get_run(run_id)["state"]["diagnosis"],
                                                  "summary": "fix", "evidence_ids": ["e1"]}})
    store.decide(run_id, "approve", digest)
    return run_id


RESULT = {"regression_fixed": True, "baseline": {"exit_code": 1}, "patched": {"exit_code": 0}}


def test_verification_memory_failure_rolls_back_completion(store):
    import sqlite3
    run_id = verification_ready(store)
    with store.connect() as db:
        db.execute("""CREATE TRIGGER fail_memory BEFORE INSERT ON memories BEGIN
                   SELECT RAISE(ABORT, 'simulated storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.complete_verification(run_id, RESULT)
    assert store.get_run(run_id)["status"] == "verifying"
    assert not any(event["kind"] == "verification.completed" for event in store.events(run_id))
    with store.connect() as db:
        db.execute("DROP TRIGGER fail_memory")
    assert store.complete_verification(run_id, RESULT)["status"] == "completed"
    store.complete_verification(run_id, RESULT)
    assert len(store.memories(store.get_run(run_id)["project_id"])) == 1
    assert sum(event["kind"] == "verification.completed" for event in store.events(run_id)) == 1


def test_cancel_racing_completion_never_leaves_partial_memory(store):
    run_id = verification_ready(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        completion = pool.submit(store.complete_verification, run_id, RESULT)
        cancellation = pool.submit(store.update_run, run_id, status="cancelled")
        completion.result()
        cancellation.result()
    run = store.get_run(run_id)
    memories = store.memories(run["project_id"])
    assert bool(memories) == (run["status"] == "completed")
    assert store.metrics()["verified_fixes"] == int(run["status"] == "completed")


def test_nonverified_memory_is_rejected(store):
    run = make_run(store)
    with pytest.raises(ConflictError):
        store.save_memory(run["project_id"], run["id"], "false success", [])
