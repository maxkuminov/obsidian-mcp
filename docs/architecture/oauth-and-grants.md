# OAuth: grant families, consent, scope

> Deep rationale extracted from `CLAUDE.md`. Read before touching `src/oauth/`, the consent page, or anything that mints, rotates or revokes a token.

## OAuth grant families

A `/authorize` approval is **one grant**; the token endpoint mints an
access/refresh pair from it and every rotation mints another pair.
`oauth_tokens.grant_id` (migration 014, NOT NULL, indexed) is what ties them
together, and it is the *only* way a family is ever resolved — the decision in
#64 was explicit that a second "find the family" path is how the bug comes
back. `src/oauth/grants.py` owns the primitives.

Why it exists: without it the panel could only offer per-row controls, and both
were near no-ops. Revoking the access row left the refresh token to mint a
fresh, identically-scoped pair on the client's next 401 retry (access tokens
live one hour). Downgrading the access row silently reverted, because
`_handle_refresh` copies the **refresh** token's scope. The revoked row then
vanished from the page, so the operator saw a blank space that read as success.

- **Invariant: one `grant_id` ⇒ one `(client_id, user_id)`.** Established at
  every write site and by 014's group key. Family writes therefore do **not**
  re-filter by `user_id` — under a broken invariant that predicate would turn a
  complete revocation into a partial one, which is the failure the whole thing
  exists to remove. `_assert_oauth_token_owner` still guards the token the
  operator names, and because a family cannot span users that covers it.
- **`lock_grant` is correctness, not tuning.** Under READ COMMITTED an
  `UPDATE … WHERE grant_id = :g` takes its snapshot at statement start, so rows
  a concurrent `_handle_refresh` *inserts* afterwards are invisible to it: the
  panel reports "revoked" and the client keeps the pair it just rotated into.
  Row locks cannot close that — the rows do not exist yet. Both sides take
  `pg_advisory_xact_lock` on the grant **before** touching any family row, so
  the order is total and nothing deadlocks; `_handle_refresh` does one unlocked
  `grant_id` lookup first because it cannot lock what it has not identified.
- **Revocation kills in-flight access tokens; rotation does not.** Letting the
  replaced access token run to its expiry is right for rotation and wrong for
  revocation — an hour of surviving write access after the operator clicked
  Revoke is exactly the defect.
- **A replayed refresh token revokes the family (#182).** Rotation is only
  half of ASVS V10.4.5. After a rotation exactly one party can hold the
  current refresh token, so a *second* presentation of one that was already
  rotated away means two parties hold the same credential — and the
  production clients register `token_endpoint_auth_method: none`, where
  possession is the entire credential. Without reuse detection the thief who
  redeems first keeps an identically-scoped, silently-renewing pair for the
  30-day sliding window while the legitimate client sees `invalid_grant` and
  quietly re-authorizes; the theft signal RFC 6819 §5.2.1.1 defines is thrown
  away. `_handle_refresh` therefore calls `revoke_grant_family` at exactly the
  point where the unfiltered lookup resolved a `grant_id` but the
  `revoked == False` select returned nothing — that combination *is* "the row
  exists and is revoked", i.e. a replay.
  - **The lock is what makes it correct, not an optimization.** The grant lock
    is already held from the unfiltered lookup onwards, so nothing can rotate
    a new pair into the family between that select and this UPDATE. "Every
    live token" cannot be stale. `revoke_grant_family` re-takes the same
    transaction-scoped advisory lock, which is re-entrant.
  - **The response stays constant.** The reuse path returns the byte-identical
    `invalid_grant` body and status the not-found path returns — the caller
    must not learn whether it named a live family, whether anything died, or
    that detection exists at all. That is also why the revocation is wrapped:
    a failure to write must not surface as a 500 on this path only.
  - **Boundaries.** An *expired* refresh token that was never rotated is not
    reuse — its row is still `revoked == False`, so the handler reaches the
    expiry check and revokes nothing; a token dying of old age must not kill
    the family's live access token. A token hash that matches no row revokes
    nothing (there is no family to name). A family that is already fully
    revoked is a harmless no-op: `revoke_grant_family` flips zero rows,
    nothing is committed.
  - **One WARNING, on the path that killed something.** `event =
    "oauth.refresh_reuse_detected"` with `client_id`, `grant_id`, `user_id`
    and the number of rows revoked — never any token or hash material. It is
    emitted only when live tokens were actually revoked: the plain not-found
    path is not reuse, and a second replay against an already-dead family has
    nothing new to report, so neither may drown the real alarm.
- **Revocation takes effect at the next authenticated request; a request
  already in flight completes.** `APIKeyMiddleware` resolves the token once, at
  the start of the request, so a tool call authenticated microseconds before a
  revoke or downgrade commits still runs with the permission it was granted.
  Closing that would mean holding the grant lock across tool execution —
  arbitrary vault I/O, embedding calls, network fetches — trading a bounded,
  sub-second staleness for unbounded lock contention on every request. Accepted
  and documented, at the same optimistic level as `edit_note(expected=…)` and
  the transfer fingerprint check.
- **`/revoke` (RFC 7009) is family-scoped too**, which §2.1 explicitly permits.
  Anything narrower reproduces the near no-op for any client presenting its
  access token. It **authenticates the client** per its registered method and
  requires `client_id` to be **present and exactly equal** to the token's —
  without that, any holder of any token value ended a 30-day grant. Absence is
  not a match: a public client has no secret to check, so "omit `client_id`"
  would be a universal bypass proving nothing, and unlike `/token` there is no
  PKCE verifier binding the request to the initiating client. §2.2 governs the
  other direction: a foreign, unnamed or unknown token is answered 200 with
  nothing done, so the endpoint is not an oracle for who owns a token value.
  The only real error is naming the right client and failing to authenticate.
- **No token is minted without an owner, and the mint paths serialize with the
  multi-user bootstrap.** `register_submit` claims ownerless rows with
  `UPDATE ... WHERE user_id IS NULL`, whose snapshot is taken at statement
  start, so a mint committing afterwards left tokens belonging to nobody. Both
  token handlers take the *same* advisory key the bootstrap already held
  (`USER_BOOTSTRAP_LOCK_KEY` — a wire constant: changing it un-serializes the
  window during a rolling deploy) and, under `multi_user_mode`, refuse to mint
  a NULL-owner token at all. Lock order is bootstrap-then-grant on the only
  path that takes both; the panel takes the grant lock and never the bootstrap
  key, so there is no cycle.
- **The first-authorizer claim is `UPDATE ... WHERE user_id IS NULL
  RETURNING`**, not an ORM assignment on a row read from this transaction's
  snapshot — two users consenting to the same unbound client both saw NULL and
  the second write silently re-bound it. `_handle_refresh` and
  `src/mcp_server/auth.py` additionally refuse a grant whose owner is not the
  client's, so a legacy or race-created cross-user grant cannot rotate or
  authenticate.
- **The registered scope caps every path.** `src/oauth/scope.py` holds the one
  definition (`clamp_scope`, `client_can_write`, `token_has_write`); the OAuth
  routes, `src/mcp_server/auth.py` and the panel all use it. The panel refuses
  `readwrite` for a client not registered for it *and* clamps what it writes,
  and `_handle_refresh` re-clamps on every rotation — otherwise a scope raised
  above the registration survives forever (#67).
- **`authorize_post` refuses a client bound to a different user** (#68). Fixed
  at the source rather than by unioning the panel listing, which would hand the
  other user the owner's cascading Delete. Single-user mode cannot trigger it.
- **The panel lists revoked and expired rows, dimmed**, with one "Revoke
  access" control and one scope select *per grant*. Status also reads the
  owner's `User.is_active` ("Owner inactive") **and `has_vault_scope`** ("No
  vault scope"), both of which `APIKeyMiddleware` already enforces and the page
  used to badge green (#76). A no-vault-scope grant counts as dead: offering a
  Revoke and a scope select for a credential the middleware 401s is the same
  over-reporting of liveness, and that select could only ever write a scope the
  client is not registered for.
- **Live rows are queried unbounded; only history is capped.** One `LIMIT` over
  all of a client's tokens applied *before* grants were identified let a chatty
  grant's rotations push another grant's live refresh token off the page —
  a working credential with no control to revoke it. Losing the tail of the
  history costs a row nobody can act on; losing a live row costs a revocation.
  When the history query hits its cap the page says so rather than printing a
  total it did not count.
- **A grant's permission is `any(token_has_write(...))` over its live rows.**
  014's backfill can legitimately merge two pre-014 sessions of one client and
  user, one `read` and one `readwrite`; reading the newest row alone showed
  "read" while an older live access token still held write. Such a family is
  marked "mixed", and saving the select writes one clamped scope across all of
  it — which is what makes it uniform again. `offline_access` is read the same
  way, so a write cannot strip the marker from a sibling that carried it.
- **014 verifies a pre-existing `grant_id` column rather than patching it.**
  The backfill is a partition only because the migration created the column;
  on a column somebody else added, `WHERE grant_id IS NULL` becomes a patch
  that hands a NULL row beside a stamped sibling a *fresh* id — splitting one
  grant in two, so revoking either leaves the other alive. It therefore refuses
  a wrong type, any NULL row, any id spanning more than one
  `(client_id, user_id)`, and an index of its name that is not *exactly* its
  index. That last check reads `pg_index` — table, `indisvalid`,
  `indpred IS NULL`, `indexprs IS NULL`, and exactly one key column equal to
  `grant_id`'s attnum. "Which column is it on?" is not enough: a partial index
  covers a subset of rows, an expression index cannot serve an equality lookup,
  a multi-column index leads with the wrong key, and an INVALID leftover from a
  failed `CREATE INDEX CONCURRENTLY` serves nothing — `CREATE INDEX IF NOT
  EXISTS` keeps all of them, and autogenerate compares index *names*, so the
  check would look installed while staying dirty forever.
- **`cleanup_expired_tokens` retains on `expires_at`, never `created_at`.** Its
  revoked branch used to have no age condition at all, so the indexer deleted
  every revoked token within five minutes — the same blank space the listing
  exists to prevent, just delayed. Revocation time is not stored, but a token
  can only be revoked while it exists (`R <= expires_at`), so a 7-day window
  measured from `expires_at` *guarantees* a revoked row stays visible for at
  least 7 days after revocation. `created_at` inverts that: a refresh token
  minted 30 days ago and revoked a minute ago would be purged at once. Once
  age-gated the revoked branch is a strict subset of the expiry branch, which
  is why the predicate is a single comparison rather than an `or_`.

## The consent page preselect

- **OAuth consent preselects "Read only" unconditionally — never bind `checked` to the requested scope.** On an HTML form the checked radio *is* the submitted value, so the preselect is the default grant, not a display detail. `/register` is unauthenticated and a DCR client that omits `scope` is registered `read readwrite offline_access`, so a requested-scope preselect lets one unchanged **Approve** click hand vault-wide write to any self-registered client (#63; #62 introduced exactly that and it was reverted). What #62 was right about is kept as *prose*: `authorize_get` passes `requested_write` (from the validated scope alone, ungated by the registered scope — display only) and `write_unavailable`, and the request box names the level asked for and says when the client cannot hold it. `_clamp_scope` in `authorize_post` is the enforcement boundary and is unrelated to any of this. The markup default is not enough on its own: Firefox restores a control's dynamic checked state across page loads in preference to it, so the form **and** every `name="scope"` radio carry `autocomplete="off"` — without them a user who once picked write gets that radio re-checked on a repeat visit to the same `/authorize` URL (clients reuse `state`/PKCE) and one Approve re-grants write after a revocation. The `.scope-option:has(:checked)` highlight lives in standalone rule blocks: a selector list is dropped wholesale when any part fails to parse, so grouping it would take the native-radio fallback down with it.
