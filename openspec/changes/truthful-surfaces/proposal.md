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

### 3. A reassigned vault leaves the previous vault's index serving (#91)

`_active_user_ids()` filters `vault_path IS NOT NULL`, so an unassigned user's
`notes_metadata` / `note_embeddings` / `note_links` rows are frozen. Since #66
that is deliberate and correct: every tool is refused meanwhile — the admission
gate is in `_tracked`, not in individual tools — so the rows are not a leak,
and preserving them is what lets a reassignment resume without re-embedding
~16.7k chunks. `mcp-request-routing` already pins that as "the account's vault
path is assigned again to the same directory → the previously indexed rows
SHALL still be present".

**A different directory is a different question, and nothing answers it.**
`notes_metadata.file_path` is vault-*relative*; no row, and no column anywhere,
records which root the index was built from. So after an admin repoints a user
at another vault, the metadata-only tools — `semantic_search`, `keyword_search`,
`list_notes`, `get_recent` and every graph tool, all served from the database
filtered by `user_id` alone — answer from the *previous* vault: its paths, its
titles, its tags, its frontmatter, its chunk excerpts. `read_note` on one of
those paths then either fails, or, worse, returns a genuinely different note
that happens to occupy the same relative path in the new root. CLAUDE.md names
"silently wrong search results" as one of the two expensive failures in this
product, because an agent acts on them without a human ever seeing the query.

The next index pass over the new root does eventually reconcile most of this by
relative path — `deleted_paths = existing − files` — but "eventually" is doing
real work in that sentence, and one case never reconciles at all: a note whose
relative path *and* content hash are identical in both roots is "no change", so
its `note_links` rows are never re-extracted while the old targets they point at
are deleted out from under them (`ON DELETE SET NULL`). The graph tools then
report a permanently under-resolved neighbourhood for that note.

**Decision (a): record the root the index was built from, and discard the index
when the assignment moves off it.** Migration 016 adds
`users.indexed_vault_path` (nullable, `String(1024)`). `index_vault(user_id)`
compares it with `users.vault_path` before scanning anything: equal — nothing
happens, which is exactly the #66 case and stays free; different and the
recorded root is non-NULL — delete the user's `notes_metadata` rows
(`note_embeddings` and `note_links` cascade) and stamp the new root, in one
transaction, before the new vault is read.

Two properties this buys that a comparison inside the panel handler could not:

- **It survives the unassignment.** The transition an operator actually
  performs is often `/old` → unassigned → `/new`, and `edit_user_submit` sees
  `old_vault = None` on the second step. Only a value that outlives
  `vault_path` distinguishes "reassigned to where the index came from" (keep,
  per #66) from "reassigned somewhere else" (discard). That is the entire
  reason the column exists rather than a comparison of two form values.
- **The indexer stays the only writer of index contents.** Adding a second
  place that deletes `notes_metadata` is how the two paths drift apart, which
  is the argument #64 made for resolving a grant family exactly one way. Every
  caller of `index_vault` — the startup pass, the periodic tick, and the
  panel's `_reindex_background` — inherits the reconciliation by calling it, in
  the same way every tool inherits the admission gate by being registered.

**Accepted residual, stated precisely.** Because the reconciliation runs in the
indexer, a reassignment is honoured at the *next* pass, not at the Save. The
window is bounded by `INDEX_INTERVAL_SECONDS` (default 5 minutes) plus the
duration of a pass already in flight, and during it the metadata-only tools
still answer from the previous root. Closing it entirely would mean purging
inside the panel's POST transaction — a second writer of the index, for a
5-minute improvement — or refusing every tool for the whole interval, which
breaks the disk-backed tools that are already correct against the new root. Not
worth either. This is the same optimistic level declared for
`edit_note(expected=…)` and the transfer fingerprint check, and the same
"takes effect at the next authenticated request" shape as an OAuth revocation.

**Decision (b): the users list stops printing a note count for an account whose
tools are refused.** `users.html` renders `(unassigned)` in the Vault column
and, three columns to the right on the same row, a live-looking note count for
the same account — while `user_edit.html`'s own selector says "(unassigned —
every MCP tool refuses; index kept for reassignment)". That is the same
over-reporting of liveness as #76 (a user shown "API Keys: 4" when all four
were revoked, with no surface on which to discover it) and the same shape as
the #64 blank space that read as success: a number that looks like capacity,
for a credential that cannot serve a single row of it. The count becomes an
explicit "not served" state that says the index is kept for reassignment.

**Explicitly rejected: age-based pruning.** It invents a retention policy
nobody asked for, it deletes exactly the rows #66 preserves on purpose, and the
cost of being wrong is the full re-embed that #66 exists to avoid. Reassignment
to a different root is a real event with a real trigger; "this index is old" is
not.

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
patch. Today exactly one test anywhere asserts this header
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
  test asserts the property rather than the string: no agent-facing guidance in
  `tools.py` names a tool that is not registered on the server.
- **`delete_user` refuses a self-targeted delete**, soft or permanent, under
  the existing advisory lock and after the existing actor re-check.
  `user_edit.html` states the refusal and disables both controls on a self-view,
  the way the role checkboxes already do — the handler is what makes the
  promise true, the markup is what stops the operator being surprised by it.
- **`users.indexed_vault_path`** (migration 016, nullable `String(1024)`)
  records the root the index describes. `index_vault` reconciles against it
  before scanning: same root, nothing; different non-NULL root, discard the
  user's index and stamp; NULL, stamp without discarding. 016 backfills
  `indexed_vault_path = vault_path` for assigned users — a fact the indexer's
  own scoping rule guarantees — and leaves it NULL for unassigned ones, which
  is the one-time hole named in the spec: an account already unassigned when
  016 runs gets one reassignment without reconciliation, because the previous
  root was never recorded anywhere and cannot be invented.
- **`users.html` renders "not served" instead of a count** when the account has
  no vault assignment, naming the reason. Template-only; the aggregate query is
  unchanged.
- **A regression test pins `nosniff` on the OAuth scope rejections** at
  `/oauth/register`, `/oauth/authorize` (GET) and `/oauth/authorize` (POST). No
  source change.

## Capabilities

### Modified Capabilities
- `note-read`: the guidance emitted with a truncated read names only tools the
  caller can actually call.
- `index-integrity`: the index records which vault root it was built from, and
  a reassignment away from that root discards it before the new root is read.
- `mcp-request-routing`: the users list does not report a note count for an
  account whose tool calls the admission gate refuses.
- `schema-integrity`: migration 016 owns `users.indexed_vault_path` and its
  backfill.
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
- `alembic/versions/016_indexed_vault_path.py` — new (#91)
- `src/models/db.py` — `User.indexed_vault_path` (#91)
- `src/services/indexer.py` — reconciliation at the head of `index_vault` (#91)
- `src/control_panel/templates/users.html` — the note-count cell (#91)
- `tests/` — `test_read_response_cap.py` updated; new
  `test_issue_89_tool_names_in_copy.py`,
  `test_issue_90_self_delete_refused.py`,
  `test_issue_91_vault_reassignment_reconcile.py`,
  `test_issue_92_oauth_error_headers.py`; new cases and a head-revision bump in
  `tests/integration/test_schema_check.py`

Carries a migration, so `make test-schema` is a required gate and `make
db-check` must report "No new upgrade operations detected" after deploy.
