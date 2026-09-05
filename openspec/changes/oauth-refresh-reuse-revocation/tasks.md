## 1. Reuse detection in the token endpoint

- [x] 1.1 `src/oauth/routes.py` `_handle_refresh`: in the `if not old_token` branch — reached only when the unfiltered lookup resolved a `grant_id` from this hash and the `revoked == False` locking select found nothing, i.e. the row exists and is revoked — call `revoke_grant_family(session, grant_id)` and commit before returning. No change to any other branch.
- [x] 1.2 Keep the response byte-identical to the not-found refusal (`{"error": "invalid_grant"}`, HTTP 400): guard the revocation so a write failure rolls back and still returns that response rather than the outer handler's 500.
- [x] 1.3 Commit only when live tokens were actually revoked; an already-fully-revoked family rolls back a no-op. Confirm no new lock is taken: the grant lock is held from before the select, and `revoke_grant_family`'s `lock_grant` is the same re-entrant transaction-scoped key.
- [x] 1.4 Add a module logger and emit one WARNING `oauth.refresh_reuse_detected` (`event`, `client_id`, `grant_id`, `user_id`, `revoked_tokens`) on that path only — never a token value or hash. A revocation that raises logs `oauth.refresh_reuse_revocation_failed` instead.

## 2. Tests

- [x] 2.1 `tests/test_issue_182_refresh_reuse.py` (new, `_oauth_grant_fakes` pattern): rotate once, replay the original refresh token → `invalid_grant`, nothing minted, every row in the family revoked; the rotated-in refresh token is then refused and no live token remains.
- [x] 2.2 Non-triggers in the same module: an unknown token revokes nothing and commits nothing; an expired-but-never-rotated token revokes nothing; an already-revoked family stays revoked with nothing committed; an ordinary rotation still succeeds and still leaves the old access token live.
- [x] 2.3 Response and record: the replay's body is byte-identical to the unknown-token body; exactly one WARNING with the expected fields, carrying no token value or hash; a second replay adds no further record; the revoking replay took the family lock.
- [x] 2.4 `tests/integration/test_oauth_grants_pg.py`: pg-backed replay case (real UPDATE and commit; `live_count == 0` after the replay, and the rotated-in token refused) plus an unknown-token case that leaves the family at 2. Skips without `PGVECTOR_TEST_ADMIN_URL`.
- [x] 2.5 Confirm the new module fails against the pre-fix handler (5 of 11 cases) and passes after; full unit suite green.

## 3. Docs and spec

- [x] 3.1 `docs/architecture/oauth-and-grants.md`: the rotation section gains the reuse rule — what fires it, why the held lock makes it race-safe, why the response stays constant, the three non-triggers, and the logging rule.
- [x] 3.2 `openspec/changes/oauth-refresh-reuse-revocation/` proposal + spec delta; `openspec validate --strict` and `openspec validate --specs --strict` both clean.
