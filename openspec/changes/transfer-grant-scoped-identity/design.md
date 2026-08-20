# Design

## D1. What a transfer handle belongs to

A capability token is redeemed by whoever holds it, so it is bound to
everything it may do at mint time. A **handle** (`public_id`) is not a
capability: it authorises nothing, and `check_upload` is the only read scoped
by identity rather than by a bearer token. That scoping is the access control,
so the only question is what "the same identity" means over time.

- An **API key** is a principal. It does not rotate. Nothing stands behind it.
- An **OAuth access token** is one hour of a principal. The thing that persists
  across rotations — and the thing the operator actually consented to and can
  revoke — is the grant. Since migration 014 that is `oauth_tokens.grant_id`,
  NOT NULL, one value per `/authorize` approval, inherited by every rotation,
  and belonging to exactly one `(client_id, user_id)`.

So: key → the key row; OAuth → the family. That is the whole change.

## D2. Why no `transfer_tokens.grant_id` column

The alternative was to freeze the minting grant onto the transfer row
(migration 015, backfilled from the joined `oauth_tokens.grant_id`, indexed)
and compare columns. It was rejected because every argument for it is empty
here:

- **Not a hot path.** `check_upload` is one call per upload the agent follows
  up on. The uniform-404 routes do not use this function at all — they look a
  token up by hash — so nothing on the public path gets slower.
- **Nothing to freeze.** `grant_id` is assigned at insert and never updated;
  rotation *copies* it. The joined value and a frozen copy can never differ.
- **The row cannot outlive the join.** `transfer_tokens.oauth_token_id` is
  `ON DELETE CASCADE`, and `cleanup_expired_tokens` keeps an `oauth_tokens`
  row until 7 days past its expiry while transfer rows are pruned 1 day past
  theirs — which the mint clamp already bounds by the credential's expiry. The
  minting row is therefore present for the whole time the transfer row is.
- **A migration is not free.** It is a live-database change plus a new case in
  the schema gate, in exchange for none of the above.

The predicate is instead one correlated `EXISTS` over `oauth_tokens` aliased
twice (`minting_token`, `presenting_token`) joined on `grant_id`, inside the
single statement the lookup already issued. If the presenting token's row is
gone the `EXISTS` is false and the answer is "not found" — the fail-closed
direction.

## D3. Redemption stays bound to the minting credential row

The natural-looking completion of this change is to widen `resolve_identity_ok`
and the publish gate the same way, so that a pending upload minted under access
token A stays redeemable once A expires and its family sibling B is live. It is
**not** done, and the fail-closed default costs nothing here — which is the part
worth recording, because "fail closed" is only free when it cannot make another
surface lie.

`plan_mint_window` clamps every capability's expiry to
`min(requested TTL, minting credential's expiry)`. So `transfer_tokens.expires_at
<= oauth_tokens.expires_at` for the minting row, always. Consequences:

- While the link is live, the minting token is live too — rotation revokes only
  the *refresh* token, so the replaced access token runs to its own expiry
  (`src/oauth/routes.py`). The narrow binding refuses nothing that the link's
  own TTL would not have refused a moment later.
- When the minting token is revoked, the family is revoked with it (#64's
  revocation is family-scoped), so widening would not have kept the link alive
  anyway — and it should not.
- `check_upload` therefore cannot be pushed back into lying: the row it now
  finds after a rotation is reported `expired` by its own `expires_at` in
  exactly the cases where the minting credential is dead.

Widening redemption, by contrast, would bind an already-issued capability to
credentials that did not exist when it was issued. That is strictly more than
the operator agreed to at mint time, for no case that is currently wrong.

## D4. What must still be refused

- **A different grant.** Another client, or a second `/authorize` approval by
  the same user for the same client. Two consents are two things the operator
  revokes independently; one must not read the other's handles. This is the
  existing "Cross-identity lookup" scenario, re-stated in terms that survive a
  rotation. Exact for every grant issued after migration 014; see D4a for the
  pre-014 backfill's one approximation.
- **A different user.** The `user_id` comparison stays. The family invariant
  (one `grant_id` ⇒ one `(client_id, user_id)`) already implies it for the
  OAuth path, but it is the *only* check on the API-key path — a key reassigned
  to another user must not carry its handles across — and defence in depth
  costs one predicate.
- **The other credential kind.** An OAuth caller never matches a key-minted row
  (`key_id IS NULL` is required) and a key caller never matches an OAuth-minted
  one (`oauth_token_id IS NULL`).

## D4a. Accepted limitation: pre-014 families are approximate

Migration 014 backfills `grant_id` one value per distinct `(client_id,
user_id)`, which #64 accepted as approximate — nothing in the pre-014 schema
recorded which consent a row descended from, so there was nothing better to
group by. Two consents made by the same user **for the same client** before 014
therefore share one backfilled family, and this change makes a token from
either able to read `check_upload` status — path, size, sha256, mime — for a
handle minted by the other.

Accepted, not fixed. It is the same user and the same client software, the
exposure is read-only status on a handle that authorises nothing, and it is
bounded: every grant issued after 014 is exact, and pre-014 families can only
shrink as their tokens age out (`cleanup_expired_tokens` deletes 7 days past
expiry). Fixing it would mean inventing a consent boundary the database never
recorded.

The one thing that *is* defended is the cross-**client** case, which the
backfill cannot produce and the invariant forbids: the `EXISTS` compares
`client_id` as well as `grant_id`, so a family that somehow spanned two clients
still cannot leak between them.

## D5. Testing

The behavioural proof needs real rows and a real correlated subquery, so it
lives in `tests/integration/test_transfer_pg.py` — the module that is already
the mandatory Postgres gate for transfer security properties. `_rotate` models
`_handle_refresh` (new row, same `grant_id`, old access token left to its own
expiry) and `_second_consent` models a second approval.

The always-on suite cannot execute the predicate, so
`tests/test_issue_74_transfer_grant_lookup.py` pins its *shape*: it captures
the statement `lookup_by_public_id` actually issues and compiles it, asserting
that the OAuth branch never constrains `transfer_tokens.oauth_token_id` to the
presenting id and that the API-key branch never mentions `oauth_tokens` at all.
Both files were checked against the pre-change service to confirm they fail
there.
