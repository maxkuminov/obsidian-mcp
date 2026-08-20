## ADDED Requirements

### Requirement: A transfer handle is scoped to the minting principal

The identity-scoped lookup behind `check_upload` SHALL resolve a `public_id` only for the **principal** that minted it, where the principal of an API key is that `api_keys` row and the principal of an OAuth access token is its **grant family** (`oauth_tokens.grant_id`), and SHALL additionally require the calling `user_id` to equal the row's. A handle SHALL therefore remain visible to its minting agent across any number of refresh rotations within one grant, and SHALL remain invisible to every other principal — a different API key, a different client, or a second `/authorize` approval by the same user for the same client. When the presenting credential row cannot be resolved, the lookup SHALL find nothing. The family comparison SHALL include the credentials' `client_id` as well as their `grant_id`, so that two clients can never share a principal. Grant separation is exact for every grant issued after migration 014; for rows whose `grant_id` came from 014's backfill it is approximate in one direction, because that backfill groups by `(client_id, user_id)` — two pre-014 consents by the same user for the same client share one family, and a token of either MAY therefore read the other's handle status. That is an accepted limitation, not a permitted widening: it grants read-only status on a handle that authorises nothing, and no other pair of principals SHALL be conflated.

Redemption SHALL NOT be widened in the same way: the `/transfer/*` routes and the publish gate SHALL keep re-validating the exact credential row recorded on the token. This is safe because a capability's expiry is already clamped to that credential's own expiry, so the minting credential outlives every link it minted unless it is revoked — and a revocation SHALL kill the link.

#### Scenario: A rotated access token still owns its handles

- **WHEN** an upload link is minted, completes, and the minting access token is then replaced by one or more refresh rotations within the same grant
- **THEN** the identity-scoped lookup SHALL return that row for any live token of the grant, with its recorded `path`, `size`, `sha256` and `mime`

#### Scenario: A second consent is a different principal

- **WHEN** the same user approves the same client a second time, producing a second grant family, and a token of that family presents the first family's handle
- **THEN** the lookup SHALL find nothing

#### Scenario: Two clients never share a principal

- **WHEN** a token registered to a different `client_id` presents a handle whose minting token carries the same `grant_id`
- **THEN** the lookup SHALL find nothing, and a token of the minting client's own family SHALL still resolve it

#### Scenario: Another user, another key

- **WHEN** a handle minted by one principal is presented by another user's credential, by the same grant under a different `user_id`, or by a different API key of the same user
- **THEN** the lookup SHALL find nothing in every case

#### Scenario: The two credential kinds do not see each other

- **WHEN** an API-key-minted handle is presented by an OAuth token, or an OAuth-minted handle is presented by an API key
- **THEN** the lookup SHALL find nothing in both directions

#### Scenario: The presenting credential row is gone

- **WHEN** the `oauth_tokens` row of the presenting access token has been deleted while the transfer row survives
- **THEN** the lookup SHALL find nothing

#### Scenario: Redemption stays bound to the minting credential

- **WHEN** an upload link's minting access token is revoked while a sibling token of the same grant family is still live
- **THEN** redemption SHALL be refused with the uniform 404, and the identity-scoped lookup SHALL still return the row so the authenticated tool can report it as no longer redeemable rather than as never minted

## MODIFIED Requirements

### Requirement: `check_upload` tool

`check_upload(upload_id)` SHALL report the outcome of an upload link honestly, never asserting more about the vault than the token row proves, and SHALL return one of: `completed` (with `path`, `size`, `sha256`, `mime`, `completed_at`); `uploading` for a claimed token still inside its stream deadline, naming that deadline; `unknown` for a claimed token past it, stating that the bytes may already be in the vault because a publish can succeed and still fail to record its completion, and directing the caller to `list_files` / `read_file` the bound path before re-minting; `revoked` for a `pending` token whose minting credential no longer satisfies the redemption predicates or whose vault root has changed; `expired`; or `pending`. The stream deadline SHALL be `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)` as an absolute UTC instant, computed by the same helper the upload route enforces it with **and measured against the same clock**: the route SHALL enforce that instant directly rather than converting it to a monotonic value, so that a realtime clock step cannot make the route and this tool describe different instants. A claimed or consumed token SHALL NEVER be reported as unused, whatever its expiry — "never used" SHALL be reachable only for a `pending` row — and a consumed token SHALL additionally state that nothing was published, since the deadline and idle-timeout paths abort before `publish`. For `pending` and `claimed` rows the tool SHALL re-check `resolve_identity_ok(need_write=True)` and `resolve_root_ok` against the database inside the session that read the row, because redemption decides usability from a strictly larger predicate than the row's state; `completed` rows SHALL NOT be re-checked. It SHALL report `not found` for an `upload_id` minted outside the calling **principal** — a different API key, a different OAuth grant family, or, in multi-user mode, a different user — as the handle-scoping requirement defines it, and SHALL NOT report `not found` merely because the presenting OAuth access token is a later rotation of the one that minted the handle. Precise status is permitted here and nowhere else: this side is authenticated and identity-scoped, unlike the public routes' uniform 404. The argument SHALL be validated against the exact shape `upload_id`s are minted with (22 characters of the URL-safe base64 alphabet) **before** it is written to `usage_logs`: an off-shape value SHALL be logged as a fixed `<invalid>` marker, SHALL NOT reach the database lookup, and SHALL return `not found`. No branch SHALL write a token, a credential, or any other secret into `usage_logs`.

#### Scenario: A misused argument never reaches the log

- **WHEN** `check_upload` is called with a whole `…/transfer/upload#<token>` URL, or with the token itself, in place of the handle
- **THEN** the tool SHALL return `not found` and the `usage_logs` row SHALL record `upload_id` as `<invalid>`, containing no part of the supplied value

#### Scenario: Status transitions

- **WHEN** `check_upload` is called before an upload, after a completed upload, and after expiry of an unused token
- **THEN** it SHALL return `pending`, then `completed` with the file's `sha256`, then `expired`

#### Scenario: Cross-principal lookup

- **WHEN** a credential outside the minting principal — another API key, or an access token from another grant family — calls `check_upload` with that handle
- **THEN** the tool SHALL return `not found`

#### Scenario: The agent's own handle after a refresh rotation

- **WHEN** the OAuth access token that minted an upload link has been replaced by a routine refresh, and the agent calls `check_upload` with that `upload_id` presenting the new access token
- **THEN** the tool SHALL answer for the row — `completed` with its `sha256` for an upload that landed, or the state's ordinary answer otherwise — and SHALL NOT report `not found`

#### Scenario: A claimed token past its stream deadline

- **WHEN** a token was claimed, the publish stranded it in `claimed` (a `PostPublishFailure`), and `check_upload` is called after `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)` has passed
- **THEN** the tool SHALL report `unknown`, SHALL NOT say the link was never used, and SHALL direct the caller to inspect the bound path with `list_files` or `read_file` before minting another link

#### Scenario: A claimed token still in flight

- **WHEN** `check_upload` is called on a token claimed a moment ago, inside its stream deadline
- **THEN** the tool SHALL report `uploading`, name the deadline as a timestamp, and SHALL NOT assert that the transfer will not complete

#### Scenario: A realtime clock step

- **WHEN** the system clock steps forward or backward past a claimed token's stream deadline
- **THEN** `check_upload`'s classification and the upload route's enforcement SHALL move together, both measured against the same clock and the same absolute instant

#### Scenario: The credential lost write access after the mint

- **WHEN** the OAuth token that minted a still-`pending` upload link has its scope downgraded to read, or the user's vault root is reassigned
- **THEN** `check_upload` SHALL report that the link is no longer redeemable and name the reason, rather than reporting `pending`

#### Scenario: A consumed link past its TTL

- **WHEN** `check_upload` is called on a token whose stream was cut short (state `consumed`) after its TTL has passed
- **THEN** the tool SHALL say the upload was cut short and that nothing was published, and SHALL NOT say the link was never used

#### Scenario: A completed row needs no liveness re-check

- **WHEN** `check_upload` is called on a `completed` row whose credential has since been revoked
- **THEN** the tool SHALL report `completed` with the recorded `size`, `sha256`, `mime` and `completed_at`, and SHALL NOT run the liveness predicates
