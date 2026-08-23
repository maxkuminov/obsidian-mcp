## Why

Four unrelated surfaces state something the system does not do. Each one is
small; each one is a lie told to whoever reads it — an agent, an operator, or
a browser — and in three of the four the reader acts on it.

### 1. The truncation notices name a tool nobody is offered (#89)

`src/mcp_server/tools.py` tells the calling agent, twice, to use
`search_notes`: once in the outline-truncation summary (~line 472, "request one
directly, or narrow with `search_notes`") and once in the `read_note`
truncation notice (~line 601, "narrow the search first with `search_notes`").
There is no such tool. FastMCP takes a tool's name from the function in
`src/mcp_server/server.py`, and the registered name is `keyword_search`.

This is the *other half* of #78. That issue found the same wrong name in
`usage_logs.tool`, where it made `WHERE tool = 'keyword_search'` return
nothing; the fix corrected `_tracked`'s first argument and the panel, and left
the agent-facing copy alone. The consequence is worse here than in a query
predicate: the notice is emitted precisely when the note was too large to
return, i.e. at the moment the agent most needs a next step, and the next step
we hand it is a `-32602`/unknown-tool error. An agent that trusts the
instruction spends a turn discovering the tool does not exist; one that does
not trust it re-reads the note by `offset` and burns the context the cap
exists to protect.

`tests/test_read_response_cap.py:188` asserts the old string, so the test
currently pins the defect and must change with the copy.

Deliberately **not** touched: `src/control_panel/routes.py:335-337` and
`tests/test_issue_78_panel_labels.py`. Those retain `"search_notes"` as the
*historical* spelling of `usage_logs` rows written before #78, and dropping it
would un-attribute real history. Two spellings in the codebase is the correct
state: one is a tool name, the other is a value in old rows.

### 2. `delete_user` lets an admin delete themselves (#90)

PR #80 made the self-edit promise unconditional per #69: on the edit form an
admin cannot demote or deactivate their own account, the checkboxes render
`disabled`, and `edit_user_submit` refuses a hand-built POST that tries anyway.
`delete_user` — the *other* handler on the same page, reachable from two forms
in `user_edit.html` — still allows both, as long as one other active admin
exists. The last-admin guard is the only thing standing in the way, and it is
about the panel keeping *an* admin, not about this admin keeping their account.

So the promise holds on one form and not on the form directly beneath it. An
operator who has been told the role toggle is inert reasonably reads "Soft
delete: sets `is_active=false`. Data preserved" as a different, safer control.
It is not: it reaches the same `users.is_active` flag by a different route.

Permanent self-delete is strictly worse than self-deactivation, and it is one
click further down the same page. `users.id` cascades to `api_keys`,
`oauth_clients`, `oauth_tokens` and `notes_metadata`, so the actor destroys
their own credentials and their own vault index along with the account —
unrecoverable from the panel, because the account that could undo it is the one
that was deleted.

**Decision: #69's promise is about the account, not the form. `delete_user`
refuses both self-deactivation and permanent self-delete, unconditionally —
including when other active admins exist.** Another admin can still remove the
account; the actor cannot remove themselves.

The last-admin guard is left exactly as it is, and the two do not overlap. It
refuses only when the post-operation state would hold no active admin, which is
correct: one of two active admins deleting the *other* leaves an admin and is
permitted, and that is the removal the new refusal tells the operator to ask
for. What changes is that for an acting admin who is a `users` row the guard
becomes unreachable — a self-target is refused first, and any other target
leaves the actor, re-read as active and admin inside the same lock. Its one
remaining path is the single-user sentinel, which holds no `users` row and is
therefore never counted.

The refusal sits under the *existing* `_lock_admin_guard(session)` advisory
lock and *after* the existing `_actor_still_privileged(session, user)` re-check.
No second lock key: CLAUDE.md is explicit that two keys do not exclude each
other, and the whole point of `_ADMIN_GUARD_LOCK_KEY` is that a delete and an
edit can each remove the other's "remaining admin". Placing the self-check
after `_actor_still_privileged` also keeps the diagnostics in the right order —
an actor demoted while queued for the lock is told that, not told they cannot
delete themselves.

Single-user mode is untouched and has nothing to refuse: `require_admin_panel`
yields a `_SingleUserSentinel`, which is not a `User` and carries no `id`, so
there is no account for the target to *be*. The same `isinstance(user, User)`
test `edit_user_submit` already uses expresses that.

### 3. The users list prints a note count no tool will serve (#91)

`_active_user_ids()` filters `vault_path IS NOT NULL`, so an unassigned user's
`notes_metadata` / `note_embeddings` / `note_links` rows are frozen rather than
removed. Since #66 that is deliberate and correct: every tool is refused
meanwhile — the admission gate is in `_tracked`, not in individual tools — so
the rows are not a leak, and preserving them is what lets a reassignment resume
without re-embedding ~16.7k chunks. `mcp-request-routing` already pins that as
"the account's vault path is assigned again to the same directory → the
previously indexed rows SHALL still be present".

**The panel does not say any of that where the operator is looking.**
`users.html` renders `(unassigned)` in the Vault column and, three columns to
the right on the same row, a live-looking note count for the same account —
while `user_edit.html`'s own selector, one click away, reads "(unassigned —
every MCP tool refuses; index kept for reassignment)". Two pages describe one
account, and only one of them is true about what that account can serve.

That is the same over-reporting of liveness as #76 — a user shown "API Keys: 4"
when all four were revoked, with no surface on which to discover it — and the
same shape as the #64 blank space that read as success: a number that looks
like capacity, for a credential that cannot serve a single row of it. What the
operator does next is the expensive part. A count that reads as live invites
re-running a search that was never going to answer, or reaching for the Danger
zone to "fix" an index that is not the problem — the same misdiagnosis #78
found on the dashboard, where a healthy indexer looked stalled and the
suggested cure was a full re-embed. The actual fix is to assign a vault, and
the page never says so.

**Decision: the users list stops printing a note count for an account whose
tools are refused.** The count becomes an explicit not-served state naming the
reason, in the same register as the vault cell's `(unassigned)`. This is
template-only: the aggregate query is unchanged and the rows stay exactly where
#66 put them. Making the display true by *deleting* the rows is the one thing
this must not do — that is the full re-embed #66 exists to avoid, and the
display is what was wrong, not the data.

**Deferred: the other half of #91.** A reassignment to a *different* root
leaves the previous vault's index answering the metadata-only tools, and
nothing in the schema can tell that case apart from the reassignment #66
deliberately protects: `notes_metadata.file_path` is vault-relative, no column
records which root the rows came from, and the transition an operator actually
performs erases the evidence. Closing it needs a migration and a slice that
deletes a user's entire index on a string comparison, so it belongs with the
next wave's migration-carrying work — one `make test-schema` run and one
adversarial pass over both, rather than a schema gate dragged along by a
template change. The two halves were only ever adjacent because they came from
one issue. The full analysis is preserved in `DEFERRED-91a.md` beside this
proposal so the later drafter does not re-derive it; **#91 is therefore only
half closed by this change**, and the archive should be read that way.

### 4. `nosniff` on the OAuth scope error body (#92, item 3)

`_validate_scope` (`src/oauth/routes.py:90`) raises
`ValueError(f"Invalid scopes: {invalid}")` with the caller's own tokens in it,
and all three call sites (`/register`, `authorize_get`, `authorize_post`) echo
`str(exc)` into an `application/json` body. It is not XSS, and the issue does
not claim it is — it asks that `X-Content-Type-Options: nosniff` be confirmed
on those responses, so a browser cannot be talked into re-interpreting an
attacker-chosen scope string as some other content type.

**Investigated: it is already set, on every one of them, and no code change is
warranted.** `add_security_headers` in `src/main.py:243-250` is an
`@app.middleware("http")` on the application, registered before
`app.include_router(oauth_router)`, and it stamps `nosniff` (with HSTS,
`X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`) on every response
the router produces. Two details make that a real answer rather than an
assumption:

- The three scope errors are ordinary `JSONResponse` returns, not propagating
  exceptions. A `ValueError` escaping to Starlette's `ServerErrorMiddleware`
  *would* bypass the header, because that middleware sits outside the user
  middleware stack — but `_validate_scope` is wrapped in `try/except ValueError`
  at all three sites, so no scope error takes that path.
- Handled exceptions do not bypass it either: `ExceptionMiddleware` and the
  `RateLimitExceeded` handler run *inside* the stack, so their responses are
  stamped too.

So the correct outcome is a spec requirement pinning the behaviour and a
regression test that asserts it end-to-end on a real scope rejection — not a
patch. **The requirement is scoped to those three JSON error bodies, not to
"OAuth responses that reflect caller input".** The successful consent screen
reflects the client's registered name and the caller's own authorization
parameters and is HTML on purpose; `oauth-authorization-integrity` already
governs it by requiring that reflection be escaped ("Consent renders
client-supplied text as text"), and a media-type rule written broadly enough to
catch the scope errors would contradict it. `nosniff` itself is stamped on the
consent screen too, and the test pins that; the media type is what does not
generalise. Today exactly one test anywhere asserts this header
(`tests/test_transfer_routes.py:960`, and that one passes it explicitly in the
route), which is why "already correct" and "protected against regression" are
different states. Nothing stops someone reordering the middleware stack, or
moving the OAuth routes onto a sub-application with its own stack, and no test
would notice.

Items 1 and 2 of #92 (O_TMPFILE staging in `vault_fs.publish`, and the actor
label on transfer-route usage rows) belong to a later wave and are untouched
here.

## What Changes

- **The truncation copy names `keyword_search`** in both agent-facing strings,
  and the assertion in `tests/test_read_response_cap.py` moves with it. A new
  test asserts the property rather than the string, over the two producers of
  that guidance: every tool reference in their *rendered* output is a name the
  MCP server actually registers, and each producer yields at least one. The
  scope is two producers rather than the module because `list_files` already
  emits a bare `` `pattern` `` — a source-wide scan cannot tell that from a
  bare `` `keyword_search` ``, and filtering the candidates against the
  registry to fix that would hide precisely the unregistered names the test
  exists to catch.
- **`delete_user` refuses a self-targeted delete**, soft or permanent, under
  the existing advisory lock and after the existing actor re-check.
  `user_edit.html` states the refusal and disables both controls on a self-view,
  the way the role checkboxes already do — the handler is what makes the
  promise true, the markup is what stops the operator being surprised by it.
- **`users.html` renders a not-served state instead of a count** when the
  account has no vault assignment, naming the reason. Template-only; the
  aggregate query is unchanged and no index row is deleted to make the display
  true.
- **A regression test pins `nosniff` on the OAuth scope rejections** at
  `/oauth/register`, `/oauth/authorize` (GET) and `/oauth/authorize` (POST),
  and pins that those three are `application/json`. The header — but not the
  media type — is pinned on the successful consent screen too, which stays
  HTML. No source change.

## Capabilities

### Modified Capabilities
- `note-read`: the guidance emitted with a truncated read names only tools the
  caller can actually call.
- `mcp-request-routing`: the users list does not report a note count for an
  account whose tool calls the admission gate refuses.
- `oauth-authorization-integrity`: OAuth error bodies that echo caller input
  are not content-sniffable.

### Added Capabilities
- `panel-user-administration`: the control panel's user lifecycle handlers —
  the last-admin guard, the self-edit lock (#69/#80) and, now, the self-delete
  refusal. No existing capability covers the panel's user CRUD;
  `panel-password-hashing` is the precedent for a panel-scoped capability name.

## Impact

- `src/mcp_server/tools.py` — two strings (#89)
- `src/control_panel/users.py` — the self-delete refusal in `delete_user` (#90)
- `src/control_panel/templates/user_edit.html` — self-view delete copy (#90)
- `src/control_panel/templates/users.html` — the note-count cell (#91)
- `tests/` — `test_read_response_cap.py` updated; new
  `test_issue_89_tool_names_in_copy.py`,
  `test_issue_90_self_delete_refused.py`,
  `test_issue_91_users_list_not_served.py`,
  `test_issue_92_oauth_error_headers.py`

Nothing under `alembic/`, `src/models/db.py` or `src/services/` changes, so no
schema gate applies: `make test-schema` and `make db-check` belong to the wave
that picks up the deferred half of #91.
