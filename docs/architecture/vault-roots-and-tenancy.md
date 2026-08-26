# Vault roots, tenancy, and the admission gate

> Deep rationale extracted from `CLAUDE.md`. Read before touching `APIKeyMiddleware`, `_vault_root`, the owner predicates, or anything that publishes into a vault.

## The vault assignment is the admission gate for every tool

`_tracked` in `src/mcp_server/tools.py` resolves `_vault_root(current_user_id)`
**once, before the tool body runs**, and fails the call with a tool error when
it raises. That is the whole enforcement of "this user has no vault", and it
lives in the shared decorator on purpose.

Per-tool checks were the bug (#66). The tools that leaked — `semantic_search`,
`keyword_search`, `list_notes`, `get_recent` and every graph tool — are exactly
the ones with no reason to call `_vault_root`: they are served from
`notes_metadata` / `note_embeddings` filtered by `user_id` alone. Unassigning
`users.vault_path` stopped only the disk-touching tools, while the indexer's
`_active_user_ids()` (which filters `vault_path IS NOT NULL`) meant the user's
rows were never pruned either. An unchanged API key kept returning paths,
titles, tags, frontmatter and chunk excerpts indefinitely, while the panel had
told the operator "vault tools error".

- **Nothing is exempt.** Every `_tracked` tool reads or writes vault content or
  vault metadata — `get_vault_guide` returns the vault's own `CLAUDE.md`,
  `check_upload` reports a published vault path and digest. Keep the exemption
  list at zero; a new tool inherits the gate by being registered.
- **The index rows are preserved.** Deleting `notes_metadata` on the NULL
  transition was the weaker fix: it forces a full re-embed on reassignment and
  leaves the credential itself unaddressed.
- **`_vault_root` must stay a pure cache lookup.** What makes that correct is
  `APIKeyMiddleware` calling `warm_user_vault_cache(session, user_id)` on
  *every* authenticated MCP request. Do not add a DB query to the gate.
- **The single-user form of that warm is authoritative — it evicts**, and it
  returns the root it read. It used to be a silent no-op for a NULL
  `vault_path`, so a previously cached root survived; the panel's
  `clear_user_vault_cache` only clears the worker that served the POST.
- **`_vault_root` prefers the request's own snapshot over the shared dict, and
  that is the part that fails closed.** `_user_vault_cache` is process-global
  and the indexer's bulk warm is add-only, so a bulk `SELECT` issued *before*
  the admin cleared `vault_path` can land *after* the per-request warm evicted
  the entry and put the revoked root back — mid-request, with a write tool in
  flight. Eviction cannot order a query that was already running. So the
  middleware binds `current_vault_root = (user_id, Path | None)` (a ContextVar
  beside `current_user_id` in `src/auth/session.py`) and the gate reads that;
  no other task can write this request's context. **Do not "simplify" the gate
  back to the dict** — the bulk warm's add-only behaviour is safe only because
  the snapshot outranks it. The snapshot is keyed by user id (another user's
  snapshot falls through to the dict) and is never consulted for
  `user_id is None`.
- **A cold cache refuses too**, with the same message — it is not permission to
  serve stale rows — and the refusal is written to `usage_logs` with
  `params["error"] = "no_vault_assigned"` and no other new field.
- Single-user and sandbox mode are untouched: `current_user_id` is None there
  and `_vault_root(None)` answers from `settings.vault_path`.
- **In multi-user mode, `user_id is None` is a refusal, not the global vault.**
  An ownerless credential — `api_keys.user_id` / `oauth_tokens.user_id` NULL —
  is the *single-user* shape, and it survives a configuration cycle: a key
  minted while multi-user was off keeps its NULL, and the bootstrap backfill in
  `src/auth/routes.py` only claims NULL rows while `users` is empty, so
  flipping the flag after users exist never adopts it. Every layer then treated
  that key as single-user and handed it `settings.vault_path` — an ownerless
  *readwrite* key could edit the whole vault. `APIKeyMiddleware` now 401s such
  a credential (`reason=ownerless_credential`, same body as any other rejected
  key, on both the API-key and OAuth branches) and `_vault_root(None)` raises
  when `settings.multi_user_mode`. Two layers on purpose: the middleware is the
  gate, `_vault_root` is the one that cannot be bypassed by a future caller.
- **The panel's vault browser uses what the warm returned, not a re-read of the
  dict.** `vault_page` warmed the cache and then called `_vault_root(user.id)`,
  which reopens the same window: a stale bulk warm landing in between served an
  unassigned user's vault. It now takes the `Path | None` from
  `warm_user_vault_cache` directly and renders the `vault_error` empty state on
  None. Any new caller that warms-then-resolves has the same bug — use the
  return value.

## The read path's owner predicate is total

`apply_note_filters(user_id=None)` used to append **no** owner predicate while
every write path maps `None` to `user_id IS NULL`. `MULTI_USER_MODE` can be
turned off after users exist, so a database holding named users' rows read by
an ownerless credential handed over every tenant's paths, titles, tags,
frontmatter and chunk excerpts (#127). `None` is now a scoping value — `IS
NULL` — and every index-backed tool is swept to it: `keyword_search`,
`semantic_search`, `list_notes`, `get_recent`, **`get_tags`**, `get_backlinks`,
`get_links`, `get_neighborhood`, `find_related`, `find_orphans`. A single-user
deployment sees no change; every row there is NULL-owned.

- **`note_links` carries no `user_id`, so ownership rides the endpoint rows —
  and *where* it rides decides two different things.** In a JOIN's ON clause a
  cross-owner target simply fails to resolve; as a WHERE on the joined row it
  would discard every *dangling* link too, which is what `get_links` exists to
  report. `_owner_predicate_for(entity, uid)` exists so an alias can carry it.
- **An edge admitted into the neighborhood BFS or the orphan calculus changes
  what the answer *is*.** It occupies a slot against `limit`, it can bridge two
  owned notes through a row the caller cannot see, and on the target side it
  silently strips an owned note's orphan status — so both endpoints must be
  inside the owned set at *traversal* time, never at hydration time. An edge
  counts for `find_orphans` only when its source is owned and its target is
  either owned or genuinely dangling (dangling still means "not an orphan",
  unchanged, and unrelated to ownership).
- **`get_links` classifies by what the scoped join resolved**, not by the raw
  `note_links.target_note_id`, and omits a row that names a target outside the
  owned set — that row is not dangling, and printing it would print the other
  owner's path. Unreachable in normal operation (link resolution is per user),
  which is why it is refused rather than assumed away.
- The owner predicate counts as a filter for the exact fallback — see "Filtered
  vector search" in [search.md](search.md).

## Publication confirms the vault root, and the residual is declared

`APIKeyMiddleware` binds `current_vault_root` once, at admission, and that
snapshot is immutable by design — it is what makes #66's gate fail closed under
a concurrent bulk cache warm. The cost is that it is *stale by design* for the
whole of a request: an administrator can reassign, the panel can report it
complete, and a write already in flight still publishes into the former root.
So every mutating tool re-reads `users.vault_path` / `is_active` immediately
before **each** publication and refuses on change (#88). The answer is
deliberately not a lock: holding the credential and user rows `FOR UPDATE`
across `move_note`'s link rewrites would put arbitrary vault I/O inside a lock
every authenticated request contends for. The transfer routes keep their
stronger locked gate; `import_from_url` and `request_upload` are the two
allow-listed exemptions.

- **The residual is stated, not implied.** The window shrinks from a whole
  request to staging, the durability flush and one publishing call. A
  reassignment committing inside *that* still lands in the former root and the
  tool reports success — the same optimistic level as `edit_note(expected=…)`
  and the transfer fingerprint check. `move_note(rewrite_links=True)` has
  several such windows, one per publication, and can be refused part way
  through; "one window per tool call" would be false for it and must not be
  claimed.
- **There is no retainable confirmation.** `vault._confirm_vault_assignment`
  is private and the only entry point is `vault.confirmed_publication(user_id,
  publish)`, which awaits the read and calls a **synchronous** `publish` before
  returning control — so no caller-visible `await` can sit between the two.
  Coroutine, generator *and* async-generator callbacks are refused, and so is a
  returned coroutine/generator/awaitable (a callable object whose `__call__` is
  a generator is none of the first three). Nothing is `close()`d on the way
  out: that is arbitrary code of a stranger's choosing, and the lease below has
  already made the object inert.
- **The confirmation is leased for the callback's dynamic extent, and that is
  the part that bounds *when*.** `_leased` activates it, and a `finally`
  revokes it on every exit — normal return, exception, or a callback that
  stashed the object. `consume` refuses an unleased confirmation, and
  `confirmed_publication` refuses a callback that returned without consuming
  one. Single-consumption alone was **not** enough and must not be relied on
  again: it bounds how many times a confirmation is used and says nothing about
  when, so `lambda c: saved.append(c)` followed by a reassignment and a later
  `write_file_at(..., confirmation=saved[0])` was obeyed.
- **`RootConfirmation` is also single-consumption and target-bound.** The spent
  flag lives on the confirmation, not on a slot in the target, so one object
  cannot be spent by two publications however it is attached; and `consume`
  checks the acting user id and the canonical assignment against
  `MutableTarget.user_id` / `.assignment`. Every publish helper
  (`_atomic_write_at`, `move_file_no_clobber`, `soft_delete_target`,
  `unlink_at`) takes one or refuses with `UnconfirmedPublication` — a
  programming error, deliberately not a `RuntimeError`, because the tool bodies
  catch `RuntimeError` around their publishes and would render it as a failed
  write.
- **A rollback rides the confirmation it undoes, through a `MovePermit` that
  cannot be forged.** The forward `move_file_no_clobber` issues it — nobody
  else can, `__init__` requires the module-private `_PERMIT_ISSUE` token — and
  it is bound to that confirmation's *lease*, so it is inert the moment
  `confirmed_publication` returns, plus the immutable
  `(user_id, assignment, rel)` of each end and object identity. One use,
  reverse direction only. Two earlier shapes were wrong: stamping the one
  confirmation onto both endpoints made a reusable token of a single-use fact,
  and a public `MovePermit(destination, source)` constructor authorised a
  rename with no confirmation at all.
- **Both ends of a move must be one caller, one assignment, one root inode.**
  `rename_noreplace` removes the source entry as surely as it creates the
  destination one, yet only the destination's confirmation is consumed, so
  `_require_one_vault` compares `user_id`, `assignment` and `fstat` of each
  pinned `root_fd` (a pathname comparison is not enough — two assignments can
  spell the same string over different directories) before anything is spent,
  on the forward move and on the rollback. Unreachable from `move_note`, which
  opens both ends with one `uid`; checked at the primitive because the next
  caller may not.
- **Three distinct error markers, because they say different things.**
  `no_vault_assigned` (admission: this credential had no vault this call),
  `vault_assignment_changed` (an administrator moved it — `VaultAssignmentChanged`),
  and `vault_confirmation_unavailable` (the read *failed*;
  `VaultConfirmationUnavailable`, not a `RuntimeError`, so no tool body renders
  it as a bad write). An outage recorded under the reassignment marker puts an
  administrator's name on an infrastructure incident. Before a call's first
  publication an outage propagates and the call fails; after `move_note`'s move
  has stood it is caught, the remaining rewrites stop, and the partial outcome
  is reported through the existing `failed_rewrite_sources` idiom — naming it
  an outage, never a reassignment.
- **`delete_file` holds no `MutableTarget`**, so its confirmation is consumed
  against the `(uid, root)` its own `_vault_context` resolved, and the whole
  delete — trash probe included — runs inside the confirmed step.

## The index records the vault it was scanned under

`users.indexed_vault_assignment` / `indexed_vault_realpath` /
`indexed_vault_handle` (migration 016, all nullable, marker-owned, **no
backfill**) record the root a pass actually scanned, so a reassignment stops
`semantic_search`/`keyword_search`/`list_notes`/the graph tools answering from
a vault the caller no longer has (#91). `classify_provenance` is the one
function that computes the verdict, over six rows: **indeterminate** (root
unpinnable, or its realpath no longer names the pinned inode) → nothing, and
the pass fails; **re-derive** (no record, a half-set record, exactly one fact
differing, or a handle contradicting an otherwise-matching pair); **keep**
(both agree); **discard** (both differ). A handle can refuse a keep, never
establish one, and never establish a discard. Ambiguity never resolves toward
keeping — silently wrong search results are the failure this product ranks
highest — and never toward discarding, which costs a full re-embed.

- **Not backfilling is the load-bearing decision.** Deriving the assignment
  from `users.vault_path` would assert that an assigned user's index was built
  under what it carries *now*, which is exactly the reassignment lag the record
  exists to detect. NULL means "provenance unknown", the only true statement at
  migration time, and such a user is repaired by re-deriving rather than
  discarding — so introducing the record costs no vault-wide re-embed. It is
  also what makes the deploy order safe with no cross-container coordination:
  the previous code cannot write these columns.
- **The whole pass runs beneath one pinned root descriptor**, so the facts
  observed, the files discovered and the bytes read come from one inode.
  `indexed_vault_realpath` stores `os.fsencode(realpath).hex()` — a pathname is
  arbitrary non-NUL bytes, and a surrogate escape would fail to encode inside
  the one transaction that must not roll back.
- **A discard is bound to the assignment that produced it.** The verdict is
  computed in an earlier transaction against a cached root, so the discard
  transaction takes the `users` row `SELECT … FOR UPDATE`, re-reads it, and
  requires present/active/assigned/*equal to `facts.assignment`* before
  deleting anything; the stamp beside it must affect exactly one row or the
  whole transaction rolls back. Without that, an administrator correcting a
  reassignment back destroys a complete, valid index and records provenance for
  a root nobody is assigned to. The re-derive's tail stamp takes the same lock
  and the same re-read, and is *withheld* on disagreement rather than fatal —
  it destroys nothing.
- **The two take that lock differently, and it is lock ordering, not tuning.**
  The discard has its own transaction and locks the parent *before* any child
  write — the panel's own user-delete direction — so it may wait. The tail
  stamp runs at the end of the pass's transaction, already holding
  `notes_metadata` row locks, while a permanent user delete locks `users` first
  and then cascades onto exactly those rows: waiting there is a real deadlock
  cycle, and Postgres would abort one side — possibly the operator's delete. So
  the tail asks `FOR UPDATE NOWAIT` **inside `session.begin_nested()`** and
  treats `55P03` as a withheld stamp (a state that branch already knows). The
  savepoint is required, not tidy: a failed statement poisons its transaction,
  so without one the pass would lose every repair along with the stamp.
  `_is_lock_not_available` walks `.orig` *and* `__cause__` — the SQLSTATE lives
  on asyncpg's own error, two layers down, exactly as `_log_usage`'s FK
  recovery has to walk.
- **A re-derive that skipped anything records nothing.** Any per-file skip —
  including both link-extraction skips, the missing buffered body and the
  missing index row — withholds the stamp, because the record's whole claim is
  that every surviving row was written by that pass.
- **`embed_vault` is deliberately ungated on provenance, because it verifies.**
  Gating it composed with the completeness rule into indefinite staleness: one
  permanently unreadable file withholds the record forever and would then
  freeze every other note's vectors at content it no longer has, while
  `semantic_search` kept returning them. Running ungated is sound only because
  the pass refuses bytes that do not hash to the selected row's `content_hash`
  — an embedding is a pure function of content, so which directory supplied the
  bytes is not a fact the vector depends on. **Removing the verification means
  re-gating the pass in the same change.**
- **Verifying the bytes is not enough on its own.** The ORM re-read that
  follows can see a hash another pass has committed (H2) while the vectors were
  built from H1; stamping H2 marked the row embedded for content it does not
  have, and H2 == H2 then blocked every later repair. `embed_note` therefore
  takes `certified_hash`/`certified_path` and stamps them with a conditional,
  row-locking `UPDATE … WHERE id AND file_path AND content_hash = H1` **before**
  it replaces a vector and **after** the provider call — so no row lock is held
  across a network request, and a row that moved matches nothing.
  `StaleCertification` rolls the note back and leaves it unmarked.
- **The exclusion branch certifies through the same predicate.** It reads no
  file, but it deletes a note's vectors and marks the row embedded, which is
  the same claim — and a move is exactly what it cannot see, because relocating
  a note changes `file_path` and not `content_hash`. Stamping by `id` alone let
  a decision about `Private/A.md` delete the vectors of a row that had become
  `Public/A.md` and record it as embedded with none: included, hash-equal, and
  therefore never selected again — silently and permanently absent from
  `semantic_search`. `certify_embedded` is shared by both paths, stamps before
  the delete (the conditional UPDATE is what takes the row lock), and takes
  `note_id` plus an explicit `expire_on` because the exclusion branch certifies
  from a plain result row no session maps.
- **A path change clears `embedded_content_hash`, at every statement that
  changes `file_path`.** The predicate above closes only *move-before-certify*;
  the mirror ordering is invisible to it, because when the move lands after a
  correct certification the stamp is already there and already true of the
  content. It is no longer true of the *decision*: the stamp says the row's
  current content has been dealt with and nothing about **how**, and the
  exclusion branch decides how by matching `EMBEDDING_EXCLUDE_PATTERNS` against
  the path. Carried across a move it freezes the old answer for ever — the pass
  selects on `embedded_content_hash != content_hash`, which a preserved stamp
  makes false. Out of an excluded folder: included, zero vectors, never
  selected again, silently missing from `semantic_search`. Into one: still
  searchable while excluded. So `move_note`'s metadata UPDATE and the indexer's
  **id-preserving** move detection both `SET embedded_content_hash = NULL`
  (the prune-and-insert path is unaffected — its replacement row starts null).
  NULL means *re-evaluate next pass*, not *not embedded*. **Do not "improve"
  this by consulting the exclusion config at move time**: the config can change
  before the next pass, so that is the same frozen answer in a new place, and
  it would give the move path a dependency on embedding configuration it has no
  other reason to know.

