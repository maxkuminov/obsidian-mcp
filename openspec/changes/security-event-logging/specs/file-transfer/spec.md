## ADDED Requirements

### Requirement: Every capability-token refusal SHALL be recorded server-side without weakening the uniform 404

Each refusal that the bearer-protected transfer endpoints answer with the uniform 404 SHALL emit one structured server-side record naming the reason the response deliberately withholds, the request route and method, the trusted client address, and — where a token was presented — its redacted SHA-256 tag; and the response SHALL remain identical in status, headers and body across every refusal cause. The reason SHALL come from a typed refusal result carrying a reason code and, where one resolved, the token row: the predicates that decide the refusal SHALL be unchanged, and the reasons those predicates collapse SHALL be separated afterwards by a **read-only diagnosis** that takes no admission decision, is performed only once a suppression permit for the record has been acquired, and is never performed for a request that was not refused. A failure of the diagnosis itself SHALL NOT change the response: it SHALL be caught outside the admission decision, SHALL produce a best-effort record naming a diagnosis-failure reason, and the endpoint SHALL still return its uniform refusal. The refusal decided by the locked pre-publication gate SHALL carry a single generic reason, because that gate exposes no cause — an accepted limitation, meaning an operator cannot distinguish a revoked credential from a reassigned root within that window. That diagnosis SHALL apply a **total precedence** — no row for the token hash, then a direction mismatch, then the row's state, then expiry, then a lost claim race — so that a row matching more than one condition always yields the same reason. The suppression subject for these records SHALL be the trusted client address, which is known before the diagnosis runs. The record SHALL carry the minting identity (`user_id`, `key_id` or `oauth_token_id`) only on the branches where a token row actually resolved, SHALL carry no tag when no token was presented, and SHALL never carry the token itself or any prefix of it. Refusal records SHALL be subject to the shared per-subject rate limit, so an enumeration burst cannot flood the log sink — and because the diagnosis runs behind that limit, it cannot amplify the load either.

#### Scenario: Distinct reasons, one response

- **WHEN** `/transfer/upload/info` is requested with no `Authorization` header, an unknown token, an expired token, a completed upload token, a token minted by a since-revoked API key, and a token minted for a user whose vault root was since reassigned
- **THEN** every response SHALL be identical in status, headers and body, and six records SHALL be emitted carrying six distinct reason codes

#### Scenario: The set of accepted tokens is unchanged

- **WHEN** the full existing redemption suite runs with the refusal logging in place
- **THEN** every token that redeemed before SHALL still redeem, every token that was refused before SHALL still be refused, and the diagnosis SHALL never change an outcome

#### Scenario: The tag correlates without disclosing

- **WHEN** the same unknown token is presented repeatedly
- **THEN** every record SHALL carry the same `token_tag` of the form `sha:` plus eight hexadecimal characters, and no record SHALL contain any substring of the token twelve characters or longer

#### Scenario: No credential, no tag

- **WHEN** a bearer-protected transfer endpoint is requested with no `Authorization` header
- **THEN** the record SHALL carry the missing-credential reason and SHALL have no `token_tag` field

#### Scenario: No identity is invented

- **WHEN** a refusal occurs before any token row resolved
- **THEN** the record SHALL carry the reason, the route and the client address, and SHALL carry no user, key or OAuth token identifier

#### Scenario: A failed diagnosis still returns the uniform refusal

- **WHEN** the diagnosis read fails — a dead connection or an exhausted pool — on a request that was already refused
- **THEN** the response SHALL be byte-identical to any other refusal, and the record SHALL name a diagnosis-failure reason rather than being lost or raised

#### Scenario: Overlapping conditions resolve by the declared precedence

- **WHEN** a token is both expired and consumed, and separately a token is both expired and of the wrong direction
- **THEN** the first SHALL be reported as consumed and the second as a direction mismatch, and both SHALL still return the uniform response

#### Scenario: The accepted and the suppressed path both pay nothing

- **WHEN** a redemption succeeds, and separately when a refusal's source is already at its per-window allowance
- **THEN** no diagnosis read SHALL be issued in either case and no refusal record SHALL be emitted, while the refused request SHALL still return the uniform response

#### Scenario: A publication refusal keeps its existing record

- **WHEN** a mount-boundary or unsupported-filesystem refusal occurs on the upload route
- **THEN** the existing 503 responses and their existing log lines SHALL be unchanged
