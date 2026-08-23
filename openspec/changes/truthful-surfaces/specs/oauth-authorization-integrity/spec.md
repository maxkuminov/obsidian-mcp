## ADDED Requirements

### Requirement: OAuth error responses that echo caller input SHALL NOT be content-sniffable
Every OAuth endpoint response that reflects a caller-supplied value into its body SHALL be served as `application/json` and SHALL carry `X-Content-Type-Options: nosniff`. This holds for the scope-validation rejections at client registration and at both halves of the consent flow, which echo the offending scope tokens back to the caller, and it MUST hold as a property of the application's response path rather than of any one handler remembering to set a header.

The header is set today, for these responses and all others, by the application-wide security-header middleware. This requirement exists because nothing pinned it: reordering the middleware stack, or moving the OAuth routes onto a sub-application with its own stack, would remove it silently. No source change is required to satisfy this requirement — a regression test is.

#### Scenario: Invalid scope at client registration

- **WHEN** a registration request supplies a scope containing an unrecognised token
- **THEN** the response SHALL be `application/json` carrying `X-Content-Type-Options: nosniff`
- **AND** the offending token SHALL appear only inside the JSON body

#### Scenario: Invalid scope on the consent request

- **WHEN** an authorization request supplies a scope containing an unrecognised token
- **THEN** the response SHALL be `application/json` carrying `X-Content-Type-Options: nosniff`

#### Scenario: Invalid scope on the consent submission

- **WHEN** a consent submission supplies a scope containing an unrecognised token
- **THEN** the response SHALL be `application/json` carrying `X-Content-Type-Options: nosniff`

#### Scenario: Successful responses carry it too

- **WHEN** an OAuth endpoint returns a successful JSON response
- **THEN** it SHALL carry the same header, so a regression that stamps only error paths is still detected
