# Tasks

Four independent fixes. The groups are **disjoint by file** so they can be
implemented in parallel worktrees; each group's scope is listed before its
tasks, and no file appears in two groups. A group that finds itself needing a
file outside its scope must stop and report rather than reach for it — that is
a sign the split is wrong, not a licence to widen it.

Group 5 (gates) runs **once, on the merged result**, not per worktree.

---

## 1. `keyword_search` is the tool's name (#89)

**Files owned:** `src/mcp_server/tools.py`,
`tests/test_read_response_cap.py`, `tests/test_issue_89_tool_names_in_copy.py`
(new).

**Do not touch:** `src/control_panel/routes.py` or
`tests/test_issue_78_panel_labels.py`. Both deliberately keep `"search_notes"`
as the historical spelling of pre-#78 `usage_logs` rows; changing either
un-attributes real history. `test_issue_78_panel_labels.py` also asserts
against the *source text* of `tools.py` — re-run it after editing.

- [ ] 1.1 In `_outline_text`'s `_summary` (~line 472), replace
      `` `search_notes` `` with `` `keyword_search` ``
- [ ] 1.2 In the `read_note` truncation notice (~line 601), replace
      `` `search_notes` `` with `` `keyword_search` ``
- [ ] 1.3 Update the assertion at `tests/test_read_response_cap.py:188` to the
      new name, keeping what it was testing (that a truncated whole-note read
      offers a narrowing tool at all)
- [ ] 1.4 New `tests/test_issue_89_tool_names_in_copy.py`: pin the *property*,
      not the string — collect every backticked identifier appearing in
      agent-facing strings in `src/mcp_server/tools.py` that looks like a tool
      reference, and assert each is a tool registered in
      `src/mcp_server/server.py`. A new notice naming a future non-tool must
      fail this test

## 2. An admin cannot delete their own account (#90)

**Files owned:** `src/control_panel/users.py`,
`src/control_panel/templates/user_edit.html`,
`tests/test_issue_90_self_delete_refused.py` (new).

**Constraints:** the refusal goes inside the *existing* critical section in
`delete_user` — after `_lock_admin_guard(session)` and after the
`_actor_still_privileged(session, user)` re-check, before the target is read
or any flag is written. Do **not** add a second advisory-lock key; CLAUDE.md is
explicit that two keys do not exclude each other. Do not commit between the
lock and the write. Leave `edit_user_submit` alone.

- [ ] 2.1 In `delete_user`, after the actor re-check, refuse when
      `isinstance(user, User) and user.id == target.id`, for both the soft and
      the `?permanent=true` paths, with an error naming that another admin must
      perform the removal. Refuse unconditionally — the existing last-admin
      guard is a separate, weaker check and stays where it is
- [ ] 2.2 Extend the `delete_user` docstring to say the promise is about the
      account, not the form (#69/#80 cover the edit form; this covers the two
      delete forms on the same page), and why permanent self-delete is the
      worse of the two (the `users.id` cascade takes the actor's `api_keys`,
      `oauth_clients`, `oauth_tokens` and `notes_metadata`)
- [ ] 2.3 In `user_edit.html`, on a self-view (`is_self`), disable both delete
      submits and state the refusal in the same register as the existing
      role-lock copy. The markup is the explanation; task 2.1 is the enforcement
- [ ] 2.4 New `tests/test_issue_90_self_delete_refused.py`: soft self-delete
      refused with other admins present; permanent self-delete refused with
      other admins present; `is_active` and the row itself both unchanged
      after each; deleting *another* user still works, soft and permanent; the
      last-admin guard still fires for a non-self target; single-user mode
      (`_SingleUserSentinel` actor) is unaffected; a demoted actor still gets
      `_ACTOR_REVOKED_MSG` rather than the self-delete message, proving the
      ordering; and the self-view template offers no enabled delete control

## 3. A reassigned vault does not serve the previous vault's index (#91)

**Files owned:** `alembic/versions/016_indexed_vault_path.py` (new),
`src/models/db.py`, `src/services/indexer.py`,
`src/control_panel/templates/users.html`,
`tests/test_issue_91_vault_reassignment_reconcile.py` (new),
`tests/integration/test_schema_check.py`.

**Constraints:** the reconciliation lives at the head of `index_vault` so every
caller inherits it (startup pass, periodic tick, and the panel's
`_reindex_background` in `routes.py` — which is *not* in this group's scope and
needs no edit). Do not add a second writer of `notes_metadata` contents. Do not
add age-based pruning: it is a rejected alternative, not a stretch goal. Part
(b) is template-only — the aggregate query in `users.py` belongs to group 2's
file and must not be edited.

- [ ] 3.1 `src/models/db.py`: add `User.indexed_vault_path`
      (`String(1024)`, nullable), documented as "the root the rows in
      `notes_metadata` were built from; stamped only by a completed
      reconciliation, never by the panel"
- [ ] 3.2 `alembic/versions/016_indexed_vault_path.py`: add the column;
      backfill `indexed_vault_path = vault_path WHERE vault_path IS NOT NULL`
      and leave the rest NULL; guard the backfill so a re-run cannot overwrite
      a stamp the indexer has since written; `downgrade()` drops the column
- [ ] 3.3 `src/services/indexer.py`: before `discover_markdown_files`, in one
      committed transaction — read `(vault_path, indexed_vault_path)` for the
      user; if they are equal, do nothing; if `indexed_vault_path` is NULL,
      stamp it and delete nothing; otherwise delete every `notes_metadata` row
      for that user (embeddings and links cascade) and stamp the new root. Log
      the discard with both roots and the row count. Skip the whole block when
      `user_id is None` — single-user mode has no `users` row
- [ ] 3.4 `src/control_panel/templates/users.html`: when `u.vault_path` is
      empty, render an explicit not-served state in the Notes cell instead of
      `u.notes`, naming the reason (every MCP tool refuses; the index is kept
      for reassignment) in the same register as the vault cell's `(unassigned)`
- [ ] 3.5 New `tests/test_issue_91_vault_reassignment_reconcile.py`:
      reassignment to a different root discards the rows before the new root is
      scanned; reassignment to the recorded root discards nothing and re-embeds
      nothing (the #66 property, exercised *through* an intervening
      unassignment); a NULL `indexed_vault_path` stamps without discarding; a
      second pass over an unchanged assignment is a no-op; single-user mode
      never reads or writes the column; and the users list renders the
      not-served state for an unassigned account and the real count for an
      assigned one
- [ ] 3.6 `tests/integration/test_schema_check.py`: bump the head-revision
      assertions to `016`; assert the column's presence, type and nullability;
      assert the backfill's grouping (assigned users stamped with their own
      `vault_path`, unassigned users left NULL); assert stamp-back idempotence
      (`alembic stamp 015` then `upgrade head` must not overwrite a stamp the
      indexer wrote); assert the downgrade; assert `alembic check` clean

## 4. `nosniff` on the OAuth scope error bodies (#92, item 3)

**Files owned:** `tests/test_issue_92_oauth_error_headers.py` (new).

**This group writes no source change.** The header is already set for every one
of these responses by `add_security_headers` in `src/main.py`; the gap is that
nothing pins it. If the implementing agent finds a response that genuinely
lacks the header, stop and report rather than patching — that contradicts the
proposal and the proposal is what needs correcting first.

- [ ] 4.1 New `tests/test_issue_92_oauth_error_headers.py`: drive real requests
      through the application (not a hand-built `JSONResponse`) that make
      `_validate_scope` reject a caller-supplied scope at `/oauth/register`,
      `GET /oauth/authorize` and `POST /oauth/authorize`, and assert each
      response is `application/json`, carries
      `X-Content-Type-Options: nosniff`, and echoes the offending token only
      inside the JSON body
- [ ] 4.2 In the same module, assert the header is present on a *successful*
      OAuth JSON response too, so a regression that stamps errors only is
      still caught

## 5. Gates (merged result, once)

- [ ] 5.1 `pytest --ignore=tests/integration` green
- [ ] 5.2 `make test-schema` green — required, this change carries a migration
- [ ] 5.3 `openspec validate truthful-surfaces --strict`
- [ ] 5.4 `make audit`
- [ ] 5.5 Deploy, then `make db-check` reports "No new upgrade operations
      detected"
- [ ] 5.6 In place of the `user-representative` browser pass (there is no
      browser UI on the MCP side): exercise `read_note` against a
      known-oversized note on the live server and confirm the truncation notice
      names `keyword_search`, then call `keyword_search` as instructed and
      confirm it resolves. Name the tools actually called in the report
