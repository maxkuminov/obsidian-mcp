## ADDED Requirements

### Requirement: Every capability-token refusal SHALL be recorded server-side without weakening the uniform 404

Each refusal that the bearer-protected transfer endpoints answer with the uniform 404 SHALL emit one structured server-side record naming the reason the response deliberately withholds, the redacted SHA-256 tag of the presented token, the request route and method, and the trusted client address; and the response SHALL remain identical in status, headers and body across every refusal cause. The record SHALL carry the minting identity (`user_id`, `key_id` or `oauth_token_id`) only on the branches where a token row actually resolved, and SHALL never carry the token itself or any prefix of it. Refusal records SHALL be subject to the shared per-subject rate limit, so an enumeration burst cannot flood the log sink.

#### Scenario: Distinct reasons, one response

- **WHEN** `/transfer/upload/info` is requested with no header, an unknown token, an expired token, a completed upload token, a token minted by a since-revoked API key, and a token minted for a user whose vault root was since reassigned
- **THEN** every response SHALL be HTTP 404 with the same body, and six records SHALL be emitted carrying six distinct reason codes

#### Scenario: The tag correlates without disclosing

- **WHEN** the same unknown token is presented ten times
- **THEN** every record SHALL carry the same `token_tag` of the form `sha:` plus eight hexadecimal characters, and no record SHALL contain any substring of the token

#### Scenario: No identity is invented

- **WHEN** a refusal occurs before any token row resolved
- **THEN** the record SHALL carry the reason, the tag, the route and the client address, and SHALL carry no user, key or OAuth token identifier

#### Scenario: A publication refusal keeps its existing record

- **WHEN** a mount-boundary or unsupported-filesystem refusal occurs on the upload route
- **THEN** the existing 503 responses and their existing log lines SHALL be unchanged
