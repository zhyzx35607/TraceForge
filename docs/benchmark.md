# TraceForge Benchmark

`traceforge.benchmark.run_benchmark` runs a fixed catalog of independent engineering incident cases. Every run copies a case's public workspace into a fresh temporary directory. The agent can see only that copy, which contains the source, `PROBLEM.md`, and `test_public.py`. Hidden tests and the oracle patch never enter the agent workspace. Hidden tests are added to a separate verification copy after investigation; in oracle mode, a benchmark adapter supplies the known patch before verification.

The catalog currently has ten cases:

- repair cases: money and discount calculation across `basket.py` and `pricing.py`, timezone date boundaries, cursor pagination ties, cache invalidation, tenant scoped idempotency, milliseconds to seconds conversion, and discount input validation with an untrusted log injection;
- abstention cases: a correct banker's rounding implementation, an upstream timeout with insufficient evidence, and a permission denial that is required by policy and includes an injection attempt.

Each manifest records the expected decision, relevant source paths, whether the case exercises a safety boundary, and the public command. `oracle.json` contains exact replacements for repair cases and an empty patch list for abstention cases. It is never included in the workspace indexed by the agent.

Use the harness from Python:

```python
from traceforge.benchmark import run_benchmark
from traceforge.config import Settings

report = await run_benchmark(Settings.from_env(), mode="oracle")
```

The equivalent CLI is `python -m traceforge benchmark --mode oracle`. Use `--mode retrieval` for retrieval-only coverage, or `--mode live --case unit_conversion` for one configured provider case. Repeat `--case` to select more cases. Live uses Docker and does not fall back if the provider or Docker fails.

`mode` is one of `oracle`, `retrieval`, or `live`.

- `oracle` runs the normal Engine state machine and approval/verification flow with a benchmark-only oracle adapter. It executes real public and hidden tests, but its correctness is reported under `oracle_verifier_correctness`; it is excluded from model functional accuracy and records zero provider calls.
- `retrieval` builds the real index and runs `investigate` mode. It reports only whether all manifest relevant paths appeared in the initial top-eight retrieval. It does not apply patches or count a repair.
- `live` requires configured `TRACEFORGE_API_KEY` and `TRACEFORGE_MODEL`, uses the configured provider and Docker runner, and records provider usage returned by the provider. Requests use the configured service and may incur its charges. It never switches provider or model automatically and never falls back from Docker to local execution.

The per-case retrieval coverage is a weak smoke metric: each public workspace has only 4–6 files and includes the query's `PROBLEM.md`. Even a query-independent alphabetical top-eight baseline covers the required files. Do not present 10/10 coverage as ranking quality. The separate `traceforge retrieval-evaluate` command combines public workspaces, removes problem text from the index and compares four methods at smaller cutoffs; see [retrieval ablation](retrieval-evaluation.md).

For every selected case the report records status, latency, provider usage, evidence coverage, patch count, public and hidden baseline/post exit codes, whether the original workspace remained unchanged, and any bounded error. The report uses separate denominators for repair correctness, abstention, and safety. A failed or timed-out selected case remains in its applicable denominator. A cancelled run stores the completed prefix and writes a partial report.

Safety also requires the hidden contract to pass. A patch to an allowed implementation file can still bypass audit behavior indirectly: a regression test verifies that passing only the public example cannot earn a safety pass. Public and hidden repair checks use complete external observer reports, not stdout summaries.

The latest report is atomically written to `settings.data_dir/benchmark/latest.json`. A complete session also stores its immutable report at `settings.data_dir/benchmark/runs/<session_id>/report.json`, with per-case records alongside it. Reports contain labels such as `VERIFIER ORACLE — ... not a model success rate` so offline verifier performance cannot be presented as LLM quality.

Run the benchmark tests with:

```text
.venv\\Scripts\\python.exe -m pytest -q tests/test_benchmark.py
```
