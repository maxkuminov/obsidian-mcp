## ADDED Requirements

### Requirement: The OAuth scope rejections SHALL be JSON and not content-sniffable
The three responses that reject a caller-supplied `scope` — at client registration, at the consent request, and at the consent submission — SHALL be served as `application/json` and SHALL carry `X-Content-Type-Options: nosniff`, and this MUST hold as a property of the application's response path rather than of any one handler remembering to set the header. Each of the three echoes the offending scope tokens out of the request and into its body, so a browser that could be talked into re-interpreting the body as some other content type would be re-interpreting a string the caller chose.

The scope is those three JSON error bodies, and deliberately no wider. Reflecting caller-supplied input is *not* what makes a response subject to this requirement: the successful consent screen reflects the client's registered name and the caller's own authorization parameters, and it is HTML on purpose — "Consent renders client-supplied text as text" in this capability is what governs it, by requiring the reflection be escaped rather than by requiring a media type. Nothing here may be read as requiring an OAuth response that reflects caller input to be JSON.

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

#### Scenario: Successful JSON responses carry it too

- **WHEN** an OAuth endpoint returns a successful `application/json` response
- **THEN** it SHALL carry the same header, so a regression that stamps only error paths is still detected

#### Scenario: The consent screen stays HTML and still carries the header

- **WHEN** a valid authorization request renders the consent screen, reflecting the client's registered name and the caller's authorization parameters into an HTML body
- **THEN** the response SHALL carry `X-Content-Type-Options: nosniff`
- **AND** this requirement SHALL NOT require that response to be `application/json`
