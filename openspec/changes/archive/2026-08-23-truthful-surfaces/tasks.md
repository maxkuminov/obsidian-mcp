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

- [x] 1.1 In `_outline_text`'s `_summary` (~line 472), replace
      `` `search_notes` `` with `` `keyword_search` ``
- [x] 1.2 In the `read_note` truncation notice (~line 601), replace
      `` `search_notes` `` with `` `keyword_search` ``
- [x] 1.3 Update the assertion at `tests/test_read_response_cap.py:188` to the
      new name, keeping what it was testing (that a truncated whole-note read
      offers a narrowing tool at all)
- [x] 1.4 New `tests/test_issue_89_tool_names_in_copy.py`: pin the *property*,
      not the string, over the **two** producers of `read_note`'s truncation
      guidance and no wider — `_outline_text`'s `_summary` and the truncation
      notice built in `read_note_impl`.

      **Extraction rule**, this and no other: a backtick-delimited span whose
      content is exactly an identifier `[A-Za-z_][A-Za-z0-9_]*`, or such an
      identifier immediately followed by `(`; the name is that identifier. So
      `` `keyword_search` `` and `` `read_note(path="…", offset=N)` `` each
      yield a name, while `` `#7` ``, `` `## Title` `` and `` `section="#7"` ``
      yield nothing.

      **Render each producer so that its own search-guidance clause is the only
      one present.** Producer 1: `_outline_text(content, cap)` over a note with
      more headings than `cap` admits, with `cap` large enough that the summary
      is emitted intact — not the degenerate-cap path, which truncates the
      summary mid-string and can cut the name out of it. Producer 2: a truncated
      read of an oversized **headingless** note, so `_outline_text` returns None,
      producer 1's summary is not embedded in the notice, and the clause the
      notice appends is the only guidance in the output. Neither fixture's
      heading titles nor its body may contain the anchor word below.

      **Split each rendered output into clauses** — producer 1 on newlines (its
      summary is its own line), producer 2 on the blank-line separator (the
      notice's parts are joined with a blank line).

      **Locate the search-guidance clause by anchor, and assert exactly one
      clause matches.** Zero or several is a failure, not a skip. The anchor is
      the narrowing verb the guidance is built around — today "narrow", in both
      producers, matched case-insensitively. Rewording the copy past the anchor
      must break the test loudly; update the anchor deliberately rather than
      loosening it.

      Then assert (a) the **guidance clause's own** extracted set is non-empty;
      (b) that set **contains `keyword_search`** — membership, not equality, so
      a second legitimately added registered reference beside it still passes;
      (c) every name extracted from that clause is registered; and (d) every
      name extracted from the **whole** rendered output is registered — that is
      what covers the `` `read_note(…)` `` continuation reference. Run (d) once
      more over a *with-headings* truncated read, the shape production actually
      emits, in which producer 1's summary is embedded in producer 2's notice;
      no clause is isolated there.

      Registration is `mcp._tool_manager.list_tools()` — the same registry
      introspection `tests/test_issue_66_vault_unassignment_revokes_tools.py:124`
      already uses (read that file for the idiom; it is not owned by this group
      and is not edited), `list_tools()` being a plain synchronous call
      returning objects with `.name`.

      **Why the non-empty check is on the clause and not the whole output:** on
      the whole output it is vacuous. An ordinary truncated read also carries
      `` `read_note(…)` `` continuation references, so dropping the backticks
      from the guidance name still leaves a non-empty, fully registered set and
      the test passes over the defect. The clause is the smallest span that
      contains the guidance and nothing else.

      **Why membership, and why it was not the first idea.** (a), (c) and (d)
      encode one general property — "no agent-facing string names an
      unregistered tool" — and that property is too weak to express what is
      actually wanted here, which is that *this* guidance names *the search
      tool*. Three review rounds of this test passed vacuously on that gap:
      under (a) and (c) alone, rewriting the summary to end "or narrow with
      `` `delete_note` ``" satisfies every assertion while pointing the agent
      at a **destructive** tool, and adding a second registered reference makes
      the dropped-backticks mutation vacuous again. The registry check stays as
      the broad backstop; (b) is what pins the specific claim. This is an
      altitude correction, recorded rather than smoothed over — the final form
      of this test is not the form it was first written in.

      **Five mutations the test must fail on. Check it against all five
      before calling this task done:**

      1. **Unregistered name** — either producer's guidance name replaced with
         one no tool is registered under, e.g. reinstating `search_notes`. It
         is extracted from the clause, and `keyword_search` is no longer in
         that set, so **(b)** fails first; (c) would also catch it. Order the
         assertions (b) before (c): "does not name `keyword_search`" is the
         more actionable message, and it is the common case.

         **(c) and (d) are therefore unreachable by any single-name mutation,
         so prove they are not dead assertions separately:** plant
         `` `search_notes` `` *beside* a surviving `` `keyword_search` `` in
         the clause — (a) and (b) pass, **(c)** fails; plant it *outside* the
         clause, in the `read_note(…)` continuation line — every clause check
         passes, **(d)** fails.
      2. **Registered but wrong** — `` `keyword_search` `` replaced with
         another registered tool, e.g. `` `delete_note` ``. The anchor still
         matches exactly once and the clause's set is non-empty and fully
         registered, so (a), (c) and (d) all pass and the outline now points
         the agent at a destructive tool: **(b)** is the only assertion that
         catches it. This is the mutation the first three drafts passed.
      3. **Second reference added, then `keyword_search` unbackticked** — a
         second legitimately registered reference is added to the clause and
         `` `keyword_search` `` then loses its backticks. The survivor keeps
         the set non-empty and fully registered, so the other assertions go
         vacuous: again **(b)** is the only one that catches it.
      4. **Backticks dropped** — the backticks removed from around the guidance
         name with nothing else in the clause, so it yields no candidate:
         **(a)** fails.
      5. **Clause deleted** — the guidance clause removed outright, so no
         clause matches the anchor and the **exactly-one-clause** assertion
         fails.

      Do **not** scan `tools.py` source-wide: `list_files`'s truncation line
      emits a bare `` `pattern` ``, lexically identical to a bare
      `` `keyword_search` `` and not a tool. Do **not** filter the candidates
      against the registry before asserting — that is what makes an
      unregistered name disappear and the test pass over an empty set.

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

- [x] 2.1 In `delete_user`, after the actor re-check and after the existing
      `target` load (and its 404), and **before** the `remaining_admins` count,
      refuse when `isinstance(user, User) and user.id == target.id`, for both
      the soft and the `?permanent=true` paths, with an error naming that
      another admin must perform the removal. Refuse unconditionally — the
      existing last-admin guard is a separate, weaker check and stays exactly
      where it is, untouched. (Comparing `user.id` against the `user_id` route
      parameter before the load is equivalent and also acceptable; what is not
      acceptable is naming `target.id` before `target` exists)
- [x] 2.2 Extend the `delete_user` docstring to say the promise is about the
      account, not the form (#69/#80 cover the edit form; this covers the two
      delete forms on the same page), and why permanent self-delete is the
      worse of the two (the `users.id` cascade takes the actor's `api_keys`,
      `oauth_clients`, `oauth_tokens` and `notes_metadata`)
- [x] 2.3 In `user_edit.html`, on a self-view (`is_self`), disable both delete
      submits and state the refusal in the same register as the existing
      role-lock copy. The markup is the explanation; task 2.1 is the enforcement
- [x] 2.4 In `users.html`, when `u.vault_path` is empty, render an explicit
      not-served state in the Notes cell instead of `u.notes`, naming the reason
      (every MCP tool refuses; the index is kept for reassignment) in the same
      register as the vault cell's `(unassigned)`
- [x] 2.5 New `tests/test_issue_90_self_delete_refused.py`: soft self-delete
      refused with other admins present; permanent self-delete refused with
      other admins present; `is_active` and the row itself both unchanged
      after each; deleting *another* user still works, soft and permanent —
      including one admin deleting the **other** of two active admins, which
      the unchanged guard correctly permits because the actor remains active;
      the last-admin guard still fires on its one reachable non-self path, a
      `_SingleUserSentinel` actor (single-user mode, no `users` row, so it is
      never counted) deleting the sole active `User` admin; the same guard does
      **not** fire when the target is not an active admin, even on a table that
      holds no active admin at all — a sentinel actor deleting an active
      non-admin account succeeds, which is the false positive a broader
      "zero admins remain" reading would have introduced; a demoted actor
      still gets
      `_ACTOR_REVOKED_MSG` rather than the self-delete message, proving the
      ordering; and the self-view template offers no enabled delete control
- [x] 2.6 New `tests/test_issue_91_users_list_not_served.py`: the users list
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

- [x] 3.1 New `tests/test_issue_92_oauth_error_headers.py`: drive real requests
      through the application (not a hand-built `JSONResponse`) that make
      `_validate_scope` reject a caller-supplied scope at `/register`,
      `GET /authorize` and `POST /authorize`, and assert each
      response is `application/json`, carries
      `X-Content-Type-Options: nosniff`, and echoes the offending token only
      inside the JSON body
- [x] 3.2 In the same module, assert the header is present on a *successful*
      OAuth JSON response too, so a regression that stamps errors only is
      still caught; and assert it on the successful **HTML** consent screen,
      which stays `text/html` — the requirement is nosniff on every one of
      these, and JSON only on the three scope rejections. A test that asserts
      `application/json` on the consent screen is asserting the wrong thing

## 4. Gates (merged result, once)

No migration rides with this change and nothing under `alembic/` is touched, so
`make test-schema` and `make db-check` are not gates for it. They belong to the
wave that picks up the deferred half of #91.

- [x] 4.1 `pytest --ignore=tests/integration` green
- [x] 4.2 `openspec validate truthful-surfaces --strict`
- [x] 4.3 `make audit`
- [x] 4.4 Deploy
- [x] 4.5 In place of the `user-representative` browser pass (there is no
      browser UI on the MCP side): exercise `read_note` against a
      known-oversized note on the live server and confirm the truncation notice
      names `keyword_search`, then call `keyword_search` as instructed and
      confirm it resolves. Name the tools actually called in the report


## Deploy and live exercise (2026-08-23)

Deployed to the live server: image built, scanned and pushed, database backed
up to `backup_20260823_184308.sql.gz`, migrations run, container recreated and
reporting healthy. `docker exec obsidian-mcp alembic check` → "No new upgrade
operations detected." `pip-audit -r requirements.txt` → no known
vulnerabilities. This change carried no migration.

**4.5 — the tools actually called against the live server**, in place of the
`user-representative` browser pass (there is no browser UI):

- `list_notes` — selected a genuinely oversized note from the real vault.
- `read_note(path=…, limit=700)` — forced truncation and exercised **both**
  producers in one response. The outline summary rendered "Ordinals run
  #1–#22; request one directly, or narrow with `keyword_search`", and the
  truncation notice rendered "You can also narrow the search first with
  `keyword_search` instead of reading the whole note." Neither says
  `search_notes`.
- `keyword_search(query="statin CAC", limit=3)` — returned three ranked
  results, confirming the tool the guidance now names is one the caller can
  actually call. That is the property #89 is about: before this, an agent that
  followed the guidance got an unknown-tool error at the moment it most needed
  a working next step.

Not exercised live, deliberately: #90's self-delete refusal and #91's
users-list rendering would require mutating the operator's own account on the
production panel. Both are covered by 30 tests, including mutation checks that
fail when the refusal is removed or moved outside the lock.
