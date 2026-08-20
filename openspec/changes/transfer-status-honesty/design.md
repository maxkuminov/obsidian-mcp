## Context

The transfer design puts *precise* status on exactly one surface —
`check_upload`, which is authenticated and identity-scoped — and the uniform
404 everywhere else. That split is preserved here: nothing in this change adds
detail to a public route, and nothing removes the shape validation that keeps a
pasted token out of `usage_logs`.

## Decisions

### D1. Claimed is answered before expiry, and the answer is ambiguity

`claimed` is reachable past its TTL for one reason: `PostPublishFailure`, which
by construction fires only once `state["published"]` is true. So a claimed row
whose stream is over means *either* the request died without consuming the
token *or* the bytes are at the path and only the bookkeeping failed. The token
row cannot tell those apart, so the tool must not.

Ordering: `completed` → `claimed` → `expired` → `consumed` → dead → `pending`.
Hoisting `claimed` above the expiry test is the whole fix; patching the claimed
branch alone would leave it unreachable for the case that matters, because past
the TTL the expiry branch already returned.

The branch keys on `state == "claimed"` rather than on `claimed_at is not None`.
Same behaviour for every row the state machine can actually produce
(`claim_upload` writes both fields, `release_claim` clears both), and it makes
"never used" unreachable for a claimed row even if a future path leaves
`claimed_at` null — `upload_stream_deadline` treats a null as "the stream
started now", which lands in `uploading` until the TTL and in `unknown` after.

`consumed` keeps saying the link is spent, and now says explicitly that nothing
was published: the deadline and idle-timeout paths raise `Timeout` from inside
the stream, always before `publish`.

### D2. One stream deadline, in the service

`routes._upload_deadline` and `check_upload` need the same instant for opposite
purposes — one abandons the stream at it, the other decides whether "in flight"
is still a defensible thing to say. A second copy of
`min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)` would eventually
disagree, and the disagreement would be invisible: the tool would keep saying
`uploading` about a stream the route had already killed. The arithmetic lives in
`transfer.upload_stream_deadline`; the route converts it to monotonic.

### D3. The liveness re-check runs inside the session, for two states only

`lookup_by_public_id` deliberately applies no state filter. The re-check has to
happen while that session is open, which is why the `async with` block now
covers the branching decision rather than closing at the lookup.

`pending` and `claimed` only. A `completed` row records something that already
happened; re-checking it would let a later revocation turn a true report of a
landed file into a "revoked", which is a *new* lie in the opposite direction.

For a `claimed` row the dead state is appended to the upload outcome rather than
replacing it — revocation does not un-publish bytes, so the ambiguity still
leads and the revocation is a second sentence.

### D4. Clamp at mint, not at report

`min(requested TTL, credential expiry)` is written into
`transfer_tokens.expires_at`. Everything downstream — the tool result,
`/transfer/*/info`, both pages — reads that column, so clamping once makes every
surface honest without touching any of them. Reporting the two deadlines
separately would leave three places to get it right and a `expires_at` column
that still means something nobody enforces.

`mint_token` computes the window itself when the caller does not pass one, so
adding a mint path cannot silently skip the clamp. The tools pass it explicitly
because they need to know whether it fired, to say so.

`oauth_tokens.expires_at` is NOT NULL in the model and `_credential_ok` treats a
null as unusable, so `credential_expires_at` maps a null OAuth expiry to the
epoch — "already dead" — rather than to `None`, which means "immortal" and is
correct only for an API key.

### D4b. The deadline is re-checked inside the locked gate

`_drain` bounds the *body*. The publish gate runs after it and can wait for an
unbounded time on `SELECT … FOR UPDATE` — behind another publisher, a migration,
or a long transaction. So a body that finished a second inside the deadline
could publish, overwrite included, long after the capability's advertised
expiry, at a moment when `check_upload` was already answering `unknown` for the
same token. `_refuse_if_past_deadline` runs as the last thing before
`vault_fs.publish`, inside the locks, which is the only point where "still in
time" and "about to write" are the same instant.

**Accepted limitation: the deadline is honoured to within the publish latency,
not to the syscall.** The check runs before `vault_fs.publish`'s own
authoritative `open_parent` walk and, on an overwrite, the incumbent's
fingerprint re-hash (bounded by `MAX_FILE_WRITE_BYTES`), so the bytes can land a
few milliseconds after the instant we checked. A literal no-write-after-deadline
guarantee would need a pre-mutation callback threaded inside `vault_fs.publish`,
and that coupling was judged not worth it: the write is the consented,
fingerprint-verified one, so a publish late by publish latency is not a
destructive write on anything unintended.

It raises the existing `Timeout`, deliberately. The route maps that to
`consume`, and consuming is what the state machine says about a request that
ran past its deadline: a retry must mint afresh rather than replay a link whose
window is gone. It is also unambiguously pre-publication — `state["published"]`
is still false — so the `PostPublishFailure`-is-the-only-post-publish-exception
contract is untouched. It takes the deadline from the parameter rather than
re-deriving it from `row`, so `import_from_url`'s monotonic budget gets the same
treatment without needing a row shape it does not have.

### D4c. An ownerless identity is nobody once multi-user mode is on

`user_id IS NULL` means "single-user vault" on a credential and on a token. The
ownership comparison in `_credential_ok` is `cred.user_id == row.user_id`, and
`None == None` passes; `resolve_root_ok` and `locked_rows_ok` then authorised
`settings.vault_path` outright. So a capability minted by an ownerless key
*before* an operator enabled multi-user mode stayed redeemable afterwards —
replacing a file in whatever vault that setting names — even once the MCP
middleware had started rejecting the same key.

`_ownerless_in_multi_user` is consulted in all three places rather than only in
`_credential_ok`: the two root checks are the other half of a defensive pair, so
neither is the only thing standing between a stale capability and the global
root. Single-user mode is unchanged, byte for byte.

### D5. Refuse under 30 seconds

An already-dead or two-second link is worse than an error: the error tells the
agent to re-authenticate, the link tells it to hand a human a URL that will
404. 30 s is the threshold — comfortably below the 60 s floor `expires_in`
already clamps to, so it only ever fires because the *credential* is nearly
spent, never because a caller asked for something short.

A request with no credential row at all (neither `key_id` nor
`oauth_token_id`, or a row that has since been deleted) is refused for the same
reason: `resolve_identity_ok` returns False for exactly that case, so any link
minted would be born unredeemable.

### D6. The consent page states the mode

`overwrite` was already in the info payload; this is a display change plus a
spec amendment so the two stop disagreeing. The page stays self-contained and
nonce-guarded — no new asset, no server-side rendering of the path, everything
still through `textContent`.

## Risks / Trade-offs

- **`check_upload` now issues up to two extra queries** per call for
  `pending`/`claimed` rows. It is an interactive, low-frequency tool; the
  redemption route already pays the same cost on every PUT.
- **`unknown` is less actionable than a definite answer**, by design. The
  alternative is a definite answer that is sometimes false about whether a file
  is in the vault, which is the failure this change exists to remove.
- **The clamp can shorten links that used to "work"** — only in the sense that
  they used to *display* a longer life than redemption would honour. No link
  that was redeemable becomes unredeemable.
- **`mint_token` now reads the credential row**, so any test double for its
  session must answer that lookup.
- **Accepted limitation: the mint-time credential validation is an unlocked
  `SELECT`.** A revoke, downgrade or reassignment that commits between
  `plan_mint_window` reading the credential and the row's INSERT produces a
  capability that every redemption rejects and that `check_upload` reports as
  `revoked`. That is fail-closed and matches the optimistic level declared
  everywhere else in this design — locking the credential across the mint would
  hold a row lock across a filesystem probe and a fingerprint hash for no
  security gain, since the *publish* gate is where the locked re-check that
  matters already happens.
