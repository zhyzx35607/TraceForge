# Implementation contract

## Product

TraceForge is a local-first engineering incident workbench. A user registers a repository, submits a symptom/log, reviews an evidence-backed diagnosis and proposed exact replacements, approves a snapshot-only patch, and gets baseline/post-patch test results. The original repository is never modified. A bounded LLM tool loop is available with OpenAI-compatible Chat Completions or Responses configuration. An explicit fixture replay mode enables an offline end-to-end demonstration without misrepresenting model calls.

## Ownership

- Root: `traceforge/config.py`, `models.py`, `store.py`, `api.py`, `__main__.py`, packaging, fixtures, integration tests and docs.
- Runtime worker: `traceforge/engine.py`, `provider.py`, `runner.py`, and corresponding tests.
- Retrieval worker: `traceforge/retrieval.py`, `connectors.py`, and corresponding tests.
- Frontend worker: all `web/` files only.

## Shared models (plain JSON compatible dicts at module boundaries)

Project: `{id, name, root, test_command: list[str], runner: "local"|"docker", trusted: bool, github_repo: str|null, created_at}`.
Run: `{id, project_id, title, query, mode:"replay"|"live"|"investigate", status, state:dict, created_at, updated_at, error:str|null}`.
Statuses: queued -> investigating -> awaiting_approval -> verifying -> completed; rejected/failed/cancelled are terminal. No repair found can complete with evidence and no patch. Recover investigating and verifying runs by resuming persisted phase; approval must never be bypassed.
Evidence: `{id, source, path, line_start, line_end, content, score, kind, sha256}`. IDs deterministic by source+content. Content is bounded and redacted.
Patch: `{path, before, after, reason, evidence_ids:list[str]}` exact string replacement, before must occur exactly once, no absolute/traversal/secret paths.
Diagnosis: `{summary, root_cause, evidence_ids:list[str], confidence:"low"|"medium"|"high", limitations:list[str], patches:list[Patch]}`.
Verification: `{baseline:{exit_code,stdout,stderr,duration_ms}, patched:{...}, regression_fixed:bool, artifact_path:str|null, diff:str, runner}`. Regression fixed ONLY if baseline nonzero and patched zero; both zero is not proof of a fix.
Additionally baseline/patched include `tests` (validated external observer report), `output_summary` (untrusted display only), and `observation_status`. A fix requires complete unittest/pytest observations, a real failed baseline, positive passed tests, preserved test identities and no newly skipped original test. Completion, audit events and verified memory commit atomically with `Store.complete_verification`.
Event: `{id:int,run_id,kind,payload,created_at}`. Payloads are bounded and redact secrets.

## Root-provided interfaces

`Store(db_path)` sync SQLite store. Methods:
- `get_project(project_id)->dict|None`, `list_projects()->list[dict]`, `create_project(data)->dict`.
- `create_run(data, idempotency_key=None)->dict`, `get_run(run_id)->dict|None`, `list_runs()->list[dict]`.
- `update_run(run_id, *, status=None, state=None, error=None)->dict` merges top-level state keys; status transition persisted with event.
- `add_event(run_id, kind, payload)->dict`, `events(run_id, after=0)->list[dict]`.
- `save_memory(project_id, run_id, summary, evidence_ids)->None`, `memories(project_id)->list[dict]`.

`Settings` dataclass fields: `data_dir:Path`, `allowed_roots:list[Path]`, `api_key:str`, `base_url:str`, `model:str`, `api_style:str` (chat/responses), `request_timeout:float`, `max_steps:int`, `max_context_chars:int`, `max_total_tokens:int`, `max_output_tokens:int`, `max_run_seconds:float`, `test_timeout:float`, `max_files:int`, `max_file_bytes:int`, `docker_image:str`, `api_token:str`.
Settings env vars prefixed `TRACEFORGE_`; defaults loopback/local read-only; model key only env, never API response.

## Retrieval worker interfaces

`RepositoryIndex(root:Path, *, max_files=1200, max_file_bytes=250000)`
- `.build()->dict` stats; incremental content hashes; skip dependencies/build/.git/secrets/binary files and symlinks.
- `.search(query:str, limit:int=8)->list[dict]`: BM25 + character n-gram TF-IDF cosine + RRF, explicitly NOT neural embeddings; parent-context bounded chunks; include citations/line numbers.
- `.read(path:str, start:int=1, end:int=120)->dict` one Evidence; containment and denylist checks enforced.
- `.files()->list[str]` indexed paths.
`collect_git_evidence(root:Path, query:str="", limit:int=8)->list[dict]`: read-only git log/diff/status; bounded output, subprocess timeout, no shell.
`collect_github_evidence(repo:str, limit:int=5)->list[dict]`: read-only gh issue list + recent failed workflow metadata; explicit project opt-in, no credentials printed.
`redact(text:str)->str`.

## Runtime worker interfaces

`Engine(store:Store, settings:Settings)`
- `async investigate(run_id:str)->None`: build index, collect specialists concurrently using asyncio.to_thread, persist events/evidence/checkpoints, use live agent or explicitly labelled replay/investigate mode. Live tools search/read/finish, strict structured final validation including cited IDs and patch safety. No invented observations. Budget/loop guards, transient retries only, actual usage recorded. Existing persisted diagnosis with awaiting_approval is respected.
- `async verify(run_id:str)->None`: requires persisted approval for exact SHA256 JSON patch digest. Snapshot repository filtered by same boundary rules; execute baseline tests then validated patch then post tests; save unified diff artifact; never change source; update memory only for genuinely verified regression fix. Checks cancellation between steps.
`run_verification(project, patches, settings, run_id)->dict` synchronous runner. Docker network none / read-only root / dropped caps / memory+CPU+PIDs limits and writable snapshot only; local runner requires trusted flag. No shell, command empty rejected. Docker unavailable returns actionable failure and no fallback. Limit stdout/stderr and timeouts. Reject changes to tests/lockfiles/.git/secrets from model patches (patches must fix implementation).
Replay fixture may only repair the packaged `examples/checkout-service` known fixture fingerprint, using its evidence and exact known bad line. Never use offline replay as general model substitute. Investigate mode runs real retrieval and emits evidence summary without claiming an LLM conclusion.

## REST API for frontend

- GET `/api/health`: `{status,version,provider:{configured,model,api_style},capabilities:{docker_available},allowed_roots}`.
- GET `/api/projects`: list Project. POST same body `{name,root,test_command,runner,trusted,github_repo?}`.
- POST `/api/demo`: returns `{project,run}` created from checkout fixture in replay mode and queues run.
- GET `/api/runs`: list Run. POST same `{project_id,title,query,mode}` with optional `Idempotency-Key`.
- GET `/api/runs/{id}`: Run including state.evidence, state.diagnosis, state.verification, state.usage.
- GET `/api/runs/{id}/events?after=0`: list Event.
- GET `/api/runs/{id}/stream`: SSE `id`, `event: trace`, `data: Event JSON`, Last-Event-ID supported; closes when terminal.
- POST `/api/runs/{id}/approve`: `{decision:"approve"|"reject",patch_digest:string}`; CAS status & digest; queues verification once; response Run. run.state.patch_digest provided by engine.
- POST `/api/runs/{id}/cancel`: response Run.
- GET `/api/runs/{id}/artifact`: downloads patch if exists.
- GET `/api/runs/{id}/report?format=markdown|json`: portable allowlisted snapshot; includes mode limitations, diagnosis, evidence metadata, patch hashes, approval and observed test counts. Reads run/project/latest 1000 event headers in one SQLite read transaction. No source bodies, test stdout/stderr, provider continuation or event payloads. Response is an authenticated attachment with `Cache-Control: no-store`; unknown formats return 422. `content_sha256` hashes canonical JSON excluding that field and is not a signature.
- GET `/api/metrics`: `{total_runs,completed_runs,verified_fixes,failed_runs,approval_pending,provider_calls,input_tokens,output_tokens}` derived real data.
- GET `/api/memories?project_id=...`: list memory.
- GET `/api/evaluation`: saved evaluation report if run, else `{available:false}`.
- GET `/api/benchmarks/latest`: saved isolated multi-fault benchmark, or 404 with an explicit empty state.
- GET `/api/retrieval-evaluation`: saved synthetic lexical retrieval pilot, or 404 with an explicit empty state; generated via `traceforge retrieval-evaluate`, never by a model call.

FastAPI on 127.0.0.1:8710. Vite dev on 5173 proxies /api to 8710. Built frontend served by backend when web/dist exists. No fabricated dashboard counters. Error banner renders backend detail. UI in simplified Chinese, restrained dark developer console with cyan/green accents. Timeline/evidence/diff/test comparison must function with live API. Browser credentials for optional API token use session-only storage and Authorization header. SSE can use fetch stream or event polling if token supplied.
