## ADDED Requirements

### Requirement: Key-based CIMD client authentication SHALL be recorded with closed reasons and MUST NOT log assertion or key material
Every refusal caused by `private_key_jwt` document rules, JWKS resolution or assertion verification SHALL be recorded on the endpoint's existing refusal event with a closed reason, and on no new refusal event:
- at `/authorize`, `oauth_authorize_refused` with `client_metadata.` followed by `jwks_uri_invalid`, `jwks_uri_cross_origin`, `jwks_inline_refused`, `signing_alg_unsupported`, `auth_method_downgrade`, `jwks_fetch_failed`, `jwks_fetch_timeout`, `jwks_http_status`, `jwks_too_large`, `jwks_content_encoded`, `jwks_ssrf_refused`, `jwks_not_json`, `jwks_invalid`, `jwks_private_key` or `jwks_no_usable_key`;
- at `/token`, `oauth_token_refused` with `invalid_client.<sub>` on a live credential and `invalid_grant.spent_code_<sub>` on a spent code;
- at `/revoke`, `oauth_revoke_refused` with `reason` `<sub>`.

The assertion subs are `assertion_missing`, `secret_presented`, `assertion_unexpected`, `assertion_type_invalid`, `assertion_malformed`, `assertion_header_invalid`, `assertion_alg_invalid`, `assertion_kid_unknown`, `assertion_key_ambiguous`, `assertion_signature_invalid`, `assertion_claims_invalid`, `assertion_audience_invalid`, `assertion_expired`, `assertion_lifetime_exceeded`, `assertion_not_yet_valid`, `assertion_replayed` and `assertion_replay_cache_full`.

The server SHALL also emit one `oauth_client_jwks_refetched` record per actual JWKS refetch attempt, for either trigger, and none for a cooldown skip or a cache hit. It SHALL be INFO, or WARNING when the outcome is `failed`. Its declared fields are exactly `client_id` (the resolved row), `cimd_host`, `client_ip`, `trigger` (`kid_miss` or `stale`), `outcome` (`updated`, `kid_still_absent` or `failed`), `reason` (a `jwks_*` sub when failed) and `route`.

No record SHALL carry an assertion or any part of one, a `jti`, a `kid`, a key or JWKS member, an unverified `iss`, a `jwks_uri`, or an `https://` client identifier.

#### Scenario: An assertion failure names its check and nothing else
- **WHEN** a ChatGPT refresh is refused because the assertion's `aud` is wrong
- **THEN** one `oauth_token_refused` record SHALL carry `reason` `invalid_client.assertion_audience_invalid`, the row's `client_id` and `cimd_host` `chatgpt.com`
- **AND** the record SHALL NOT contain the assertion, its `jti`, its `kid` or any URL

#### Scenario: A refetch is recorded once
- **WHEN** a kid-miss or stale refetch replaces a row's snapshot
- **THEN** exactly one `oauth_client_jwks_refetched` record SHALL be emitted with `outcome` `updated`, and the request's own outcome event SHALL be emitted as usual

#### Scenario: A failed stale refetch is recorded and the request proceeds
- **WHEN** a stale refetch fails and the request then succeeds on the stored snapshot
- **THEN** exactly one `oauth_client_jwks_refetched` record SHALL be emitted with `trigger` `stale` and `outcome` `failed`, and the request's success event SHALL be emitted as usual

#### Scenario: Catalogue entries
- **WHEN** the event catalogue and the documented event table are compared
- **THEN** `oauth_client_jwks_refetched` SHALL be declared in both with the same fields, and the documented reason lists of `oauth_authorize_refused`, `oauth_token_refused` and `oauth_revoke_refused` SHALL include the added values
