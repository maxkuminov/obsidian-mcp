# Tasks

## 1. Scope the lookup to the principal

- [x] 1.1 Add `_same` and `_minted_by_principal` to `src/services/transfer.py`;
      the OAuth branch is a correlated `EXISTS` joining `oauth_tokens` to
      itself on `grant_id` (minting token ↔ presenting token), the other
      branch keeps the exact `key_id` / both-null comparison.
- [x] 1.2 Rewrite `lookup_by_public_id` to use them, keeping `public_id`,
      `direction` and `user_id` unchanged and applying no state filter.
- [x] 1.3 Document the widening *and* the two things that did not widen
      (a different grant; redemption) in the function docstring.
- [x] 1.4 Update the comment in `check_upload_impl` that describes what the
      lookup matches on. No other tools change; `Identity` is unchanged.

## 2. One definition of "write"

- [x] 2.1 Replace `_credential_ok`'s private
      `"readwrite" not in (cred.scope or "").split()` with
      `src.oauth.scope.token_has_write`.

## 3. Tests

- [x] 3.1 `tests/integration/test_transfer_pg.py`: seed `grant_id` in
      `_seed_identity` and `_ownerless_credentials` (migration 014 made it NOT
      NULL after this module was last touched), and add `_rotate` /
      `_second_consent` helpers.
- [x] 3.2 Add the handle-scoping section: rotation finds the completed row,
      three rotations still find it, a second consent does not, another user
      does not, a second API key does not, the two credential kinds do not see
      each other, a vanished presenting token is not found, `direction` still
      scopes, and redemption stays bound to the minting row.
- [x] 3.3 `tests/test_issue_74_transfer_grant_lookup.py`: pin the compiled
      predicate for all three identity shapes plus the shared-helper swap.
- [x] 3.4 Confirm the new tests fail against the pre-change service (stash the
      service file and re-run) — 2 unit + 4 integration failures.
- [x] 3.5 Full suite green: `pytest --ignore=tests/integration`, and
      `tests/integration/test_transfer_pg.py` against a throwaway
      `pgvector/pgvector:pg16`.

## 4. Documentation

- [x] 4.1 CLAUDE.md: new "The handle belongs to the principal, not to the
      credential row" subsection under **File transfer**, and the two places
      that said the lookup scopes to the credential row.
- [x] 4.2 Spec deltas + `openspec validate --strict`.
