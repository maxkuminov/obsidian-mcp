- [x] 1. Independent Codex proposal review before implementation.
- [x] 2. Retain source witness through the synchronous commit callback; update architecture.
- [x] 3. Deterministic before/after capture tests and descriptor cleanup on success/failure.
- [x] 4. Non-author defensive implementation review and focused tests.
- [x] 5. Merged full tests, integration, OpenSpec validation, deployment/live move check and archive.

Independent implementation PASS: 185 anchored-note/precondition tests; merged offline suite 4,644 passed, 556 skipped.

Completion evidence (2026-09-06): supervisor reports merged offline tests 4,644 passed; integration 553 passed; schema 182 passed; strict validation 33 passed. Deployed image ed68cfb is healthy at migration 024 with no schema drift. Independent live MCP exercise passed 314 assertions with cleanup confirmed, including a guarded move whose destination hash bound a subsequent edit and a stale move refused without mutation. Auth live checks also passed with cleanup confirmed.
