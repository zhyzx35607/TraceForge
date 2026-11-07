# Existing project audit — 2026-11-06/07

Read-only inspection covered the authenticated GitHub account `zhyzx35607`, local `D:/project`, and both interview transcripts (3,764 lines). No existing project was modified. Findings are snapshots, not production certifications.

| Existing asset | Verified evidence | Implication for this project |
| --- | --- | --- |
| xiaoxi-bot | GitHub main has 186 commits; runtime loop, tool gateway, verifier, budgets and CI exist. Local `bot/agent/execution.py` implements a shared deadline. | Borrow architectural lessons. Do not copy unrelated chatbot integrations. |
| report-workbench-agent | Real tool calls, bounded rounds, evidence gates; inspected CI reported 56 Python and 13 JS checks passing. | Another generic report tool would add little differentiation. |
| knowledge-navigator-agent | FTS/BM25 retrieval, optional embeddings, RRF, citation freshness; CI reported 71 Python and 4 JS checks. | Retrieval alone is already represented in the portfolio. |
| warehouse-modeling-agent / Wren | Real LangGraph workflow, approval, integration adapters and offline quality checks. NOTICE identifies extracted original source and newly added demo components. | Preserve contribution boundaries; complexity cannot be inferred from node count. |
| qingyan-eye-care | Business flows and service adapters exist; default analysis includes explicit demo output. | Application maturity and validated model quality are separate claims. |
| ai-agent-book | Third-party book experiments include log diagnosis and replay. | Diagnosis + test generation is not a novel idea by itself. |

The GitHub account exposed 20 accessible repositories during inspection, of which 3 were marked forks. The three public Agent workbench repositories were created on 2026-11-06 and had two commits each at inspection. Their CI results are useful implementation evidence; they are not evidence of weeks of real usage or model reliability. A non-fork repository is also not proof that every file was authored from scratch.

## Chosen gap

TraceForge adds a reproducible **investigation → evidence-backed patch → frozen approval → isolated baseline/post-patch verification → durable report** loop. It is independent new code, with no copied source from those projects. The intended first user is the maintainer of several local services who repeatedly investigates failed tests and regressions.

## Interview requirements converted to acceptance

1. Every claim should reference retrieved evidence and an exact source revision/hash.
2. Agent exploration is bounded by calls, elapsed time, context size and repetition detection.
3. Read permissions and patch restrictions are enforced in code, not only in prompts.
4. Approval is bound to the exact patch, and source changes invalidate verification.
5. State and audit events survive process restart; idempotency and leases prevent routine duplicate execution.
6. Verification runs original tests before and after a patch; modifying tests is prohibited.
7. Offline replay, deterministic verification evaluation and actual model runs have separate labels and denominators.
8. Metrics are computed from persisted runs. No invented adoption, throughput, cost savings or production accuracy.

## Remaining evidence to earn

Maintain a dated real-task dataset; collect failures as well as successes; compare models and retrieval ablations on the same cases; independently review root-cause citations; track token counts and actual provider invoices separately. A small lab run proves the execution path, not broad production readiness.
