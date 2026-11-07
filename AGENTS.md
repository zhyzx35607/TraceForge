# TraceForge

Build a verifiable engineering incident agent. Keep claims tied to executed tests and stored evidence.

- Python backend lives in `traceforge/`, React frontend in `web/`.
- Never persist credentials, local user data, or private repository content in source control.
- Source repositories are read only. Patches apply to isolated snapshots after approval.
- Local test execution is only for explicitly trusted repositories. Docker is the default for other projects; do not silently fall back.
- Offline fixture replay is labelled as replay, never as a successful LLM run.
- Evidence citations must exist in the run's retrieved evidence. Invalid citations fail validation.
- Every durable state transition and approval has a persisted audit event.
- Run `python -m pytest` and `npm run build` in `web/` before delivery.
- Multiple agents are working concurrently. Respect file ownership in `docs/implementation-contract.md` and do not revert others' edits.
