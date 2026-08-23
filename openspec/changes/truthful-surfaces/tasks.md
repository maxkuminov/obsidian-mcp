# Tasks

Three independent fixes. The groups are **disjoint by file** so they can be
implemented in parallel worktrees; each group's scope is listed before its
tasks, and no file appears in two groups. A group that finds itself needing a
file outside its scope must stop and report rather than reach for it — that is
a sign the split is wrong, not a licence to widen it.

Group 4 (gates) runs **once, on the merged result**, not per worktree.

The different-root half of #91 is **not in this change** — it is deferred to the
next migration-carrying wave and preserved in `DEFERRED-91a.md`. No task here
adds a column, a migration or a delete of `notes_metadata`; a group that thinks
it needs one has picked up the deferred half by mistake.

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
      not the string, over the **two** producers of `read_note`'s truncation
      guidance and no wider — `_outline_text`'s `_summary` and the truncation
      notice built in `read_note_impl`. Render each (a note with more headings
      than the cap admits for the first; a truncated whole-note read for the
      second), then extract *tool references* from the rendered text by this
      rule and no other: a backtick-delimited span whose content is exactly an
      identifier `[A-Za-z_][A-Za-z0-9_]*`, or such an identifier immediately
      followed by `(`; the name is that identifier. Assert (a) each producer
      yields a **non-empty** set, and (b) every extracted name appears in
      `mcp._tool_manager.list_tools()` — the same registry introspection
      `tests/test_issue_66_vault_unassignment_revokes_tools.py:124` already
      uses (read that file for the idiom; it is not owned by this group and is
      not edited), `list_tools()` being a plain synchronous call returning objects
      with `.name`. Do **not** scan `tools.py` source-wide: `list_files`'s
      truncation line emits a bare `` `pattern` ``, lexically identical to a
      bare `` `keyword_search` `` and not a tool. Do **not** filter the
      candidates against the registry before asserting — that is what makes an
      unregistered name disappear and the test pass over an empty set. Two
      failing inputs the test must have: reinstating `search_notes` in either
      producer (extracted, not registered), and dropping the backticks around
      the name (empty candidate set)

## 2. The panel's user surfaces (#90, #91)

Two unrelated defects on the same two pages, grouped by file boundary rather
than by cause. The self-delete refusal (#90) owns `users.py` and
`user_edit.html`; the not-served display (#91) is a cell in `users.html` fed by
the per-user aggregate that the *same* `users.py` builds. Splitting them would
put a template and the query behind it in two worktrees for no gain, and would
leave one group forbidden to touch a file it may legitimately need to read.

**Files owned:** `src/control_panel/users.py`,
`src/control_panel/templates/user_edit.html`,
`src/control_panel/templates/users.html`,
`tests/test_issue_90_self_delete_refused.py` (new),
`tests/test_issue_91_users_list_not_served.py` (new).

**Constraints:** the #90 refusal goes inside the *existing* critical section in
`delete_user` — after `_lock_admin_guard(session)`, after the
`_actor_still_privileged(session, user)` re-check, and after the target row is
loaded (it is loaded there today; leave that read where it is), but **before
the active-admin count and before any row is written**. That ordering is what
the spec's two ordering scenarios assert: a demoted actor gets
`_ACTOR_REVOKED_MSG`, and a self-target gets the self-delete message rather
than the last-admin one. Do **not** add a second advisory-lock key; CLAUDE.md
is explicit that two keys do not exclude each other. Do not commit between the
lock and the write. Leave `edit_user_submit` alone.

The #91 half is **template-only**. The aggregate query in `users.py` is not
changed, and no index row is deleted to make the display true — the rows are
kept for reassignment on purpose (#66), and deleting them costs the full
re-embed that #66 exists to avoid. Do not add age-based pruning: it is a
rejected alternative, not a stretch goal.

- [ ] 2.1 In `delete_user`, after the actor re-check and after the existing
      `target` load (and its 404), and **before** the `remaining_admins` count,
      refuse when `isinstance(user, User) and user.id == target.id`, for both
      the soft and the `?permanent=true` paths, with an error naming that
      another admin must perform the removal. Refuse unconditionally — the
      existing last-admin guard is a separate, weaker check and stays exactly
      where it is, untouched. (Comparing `user.id` against the `user_id` route
      parameter before the load is equivalent and also acceptable; what is not
      acceptable is naming `target.id` before `target` exists)
- [ ] 2.2 Extend the `delete_user` docstring to say the promise is about the
      account, not the form (#69/#80 cover the edit form; this covers the two
      delete forms on the same page), and why permanent self-delete is the
      worse of the two (the `users.id` cascade takes the actor's `api_keys`,
      `oauth_clients`, `oauth_tokens` and `notes_metadata`)
- [ ] 2.3 In `user_edit.html`, on a self-view (`is_self`), disable both delete
      submits and state the refusal in the same register as the existing
      role-lock copy. The markup is the explanation; task 2.1 is the enforcement
- [ ] 2.4 In `users.html`, when `u.vault_path` is empty, render an explicit
      not-served state in the Notes cell instead of `u.notes`, naming the reason
      (every MCP tool refuses; the index is kept for reassignment) in the same
      register as the vault cell's `(unassigned)`
- [ ] 2.5 New `tests/test_issue_90_self_delete_refused.py`: soft self-delete
      refused with other admins present; permanent self-delete refused with
      other admins present; `is_active` and the row itself both unchanged
      after each; deleting *another* user still works, soft and permanent —
      including one admin deleting the **other** of two active admins, which
      the unchanged guard correctly permits because the actor remains active;
      the last-admin guard still fires on its one reachable non-self path, a
      `_SingleUserSentinel` actor (single-user mode, no `users` row, so it is
      never counted) deleting the sole active `User` admin; a demoted actor
      still gets
      `_ACTOR_REVOKED_MSG` rather than the self-delete message, proving the
      ordering; and the self-view template offers no enabled delete control
- [ ] 2.6 New `tests/test_issue_91_users_list_not_served.py`: the users list
      renders the not-served state for an unassigned account and the real count
      for an assigned one; and the unassigned account's `notes_metadata`,
      `note_embeddings` and `note_links` rows are still present after the page
      renders, so the display change did not become a data change

## 3. `nosniff` on the OAuth scope error bodies (#92, item 3)

**Files owned:** `tests/test_issue_92_oauth_error_headers.py` (new).

**This group writes no source change.** The header is already set for every one
of these responses by `add_security_headers` in `src/main.py`; the gap is that
nothing pins it. If the implementing agent finds a response that genuinely
lacks the header, stop and report rather than patching — that contradicts the
proposal and the proposal is what needs correcting first.

- [ ] 3.1 New `tests/test_issue_92_oauth_error_headers.py`: drive real requests
      through the application (not a hand-built `JSONResponse`) that make
      `_validate_scope` reject a caller-supplied scope at `/oauth/register`,
      `GET /oauth/authorize` and `POST /oauth/authorize`, and assert each
      response is `application/json`, carries
      `X-Content-Type-Options: nosniff`, and echoes the offending token only
      inside the JSON body
- [ ] 3.2 In the same module, assert the header is present on a *successful*
      OAuth JSON response too, so a regression that stamps errors only is
      still caught; and assert it on the successful **HTML** consent screen,
      which stays `text/html` — the requirement is nosniff on every one of
      these, and JSON only on the three scope rejections. A test that asserts
      `application/json` on the consent screen is asserting the wrong thing

## 4. Gates (merged result, once)

No migration rides with this change and nothing under `alembic/` is touched, so
`make test-schema` and `make db-check` are not gates for it. They belong to the
wave that picks up the deferred half of #91.

- [ ] 4.1 `pytest --ignore=tests/integration` green
- [ ] 4.2 `openspec validate truthful-surfaces --strict`
- [ ] 4.3 `make audit`
- [ ] 4.4 Deploy
- [ ] 4.5 In place of the `user-representative` browser pass (there is no
      browser UI on the MCP side): exercise `read_note` against a
      known-oversized note on the live server and confirm the truncation notice
      names `keyword_search`, then call `keyword_search` as instructed and
      confirm it resolves. Name the tools actually called in the report
