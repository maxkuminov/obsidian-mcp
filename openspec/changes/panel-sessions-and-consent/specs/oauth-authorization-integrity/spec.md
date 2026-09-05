## MODIFIED Requirements

### Requirement: OAuth consent revalidates the browser session

Both OAuth authorization display and approval SHALL resolve the session identity through the server's single session-validation implementation, which requires a live, unrevoked, unexpired session row **and** an active user whose `session_version` matches the signed session. Missing, deleted, inactive, revoked, expired, or version-mismatched identities MUST NOT mint an authorization code and SHALL have their session cleared.

A cookie that has been logged out, or that belonged to a session revoked by a password change, an administrator reset, a deactivation or a delete, SHALL therefore be unable to display or approve a consent request — not merely unable to reach the panel.

#### Scenario: Password reset invalidates consent session
- **WHEN** a user's password reset increments `session_version` after the browser cookie was issued
- **THEN** an OAuth authorization GET or approval POST using that cookie SHALL require authentication again
- **AND** no authorization code SHALL be minted

#### Scenario: User is deactivated before approval
- **WHEN** an authenticated user is deactivated before submitting OAuth approval
- **THEN** approval SHALL be rejected as unauthenticated
- **AND** no authorization code SHALL be minted

#### Scenario: A logged-out cookie cannot approve a grant
- **WHEN** a consent form is opened, the user logs out in another tab, and the form is then submitted with the pre-logout cookie
- **THEN** approval SHALL be rejected as unauthenticated
- **AND** no authorization code SHALL be minted

#### Scenario: A revoked session cannot open the consent screen
- **WHEN** an authorization GET is made with a cookie whose session row has been revoked
- **THEN** the request SHALL be redirected to the login form rather than rendering the consent screen

## ADDED Requirements

### Requirement: The consent screen SHALL identify the client it is asking the user to trust

The authorization display SHALL show, alongside the application's self-declared name, the **host** of the redirect URI the authorization code would be delivered to, the client identifier the server assigned at registration, and the date the client registered.

The host SHALL be derived from the redirect URI's host component alone — never from its authority string — so that a URI embedding a familiar name in its userinfo (`https://claude.ai@evil.example/cb`) displays the host the code actually reaches. It SHALL be displayed lower-cased and in its ASCII form; a non-ASCII host SHALL be displayed in its punycode form and SHALL NOT be rendered decoded, because a decoded homograph host defeats the disclosure entirely.

All client-supplied values on the screen SHALL be rendered as text, and the host SHALL be presented in its own labelled element distinct from the application name, so that a name chosen to look like a host cannot be mistaken for one.

The approval handler already re-validates the submitted redirect URI against the client's registered list before minting anything, so the destination shown on display is the destination the code is delivered to.

#### Scenario: The redirect host is shown

- **WHEN** the consent screen is rendered for a client whose redirect URI is `https://example.test/cb`
- **THEN** the screen SHALL show `example.test` as the destination the authorization would be delivered to

#### Scenario: Userinfo cannot disguise the destination

- **WHEN** the consent screen is rendered for a redirect URI of the form `https://claude.ai@evil.example/cb`
- **THEN** the destination shown SHALL be `evil.example`

#### Scenario: A non-ASCII host is shown in punycode

- **WHEN** the consent screen is rendered for a client whose redirect host is not ASCII
- **THEN** the host SHALL be shown in its punycode form and SHALL NOT be shown decoded

#### Scenario: The client identifier and registration date are shown

- **WHEN** the consent screen is rendered
- **THEN** it SHALL show the server-assigned client identifier and the date the client registered

#### Scenario: A hostile name cannot impersonate the host field

- **WHEN** a client registers a name containing markup or a host-like string
- **THEN** it SHALL be rendered as text in the name element only
- **AND** the destination element SHALL still show the redirect URI's host

### Requirement: The consent screen SHALL state unconditionally that clients register themselves and are not verified

Every rendering of the authorization screen SHALL carry a notice that the application registered itself with this server, that registration is open, and that the displayed name was chosen by the application and is not verified here. This notice SHALL be shown regardless of whether the client's redirect host is recognised, because the server verifies no application's identity under any circumstances and the name is the first thing a user reads.

#### Scenario: The notice is present for an unrecognised client

- **WHEN** the consent screen is rendered for a client whose redirect host is not on the operator's list
- **THEN** the self-registration notice SHALL be present

#### Scenario: The notice is present for a recognised client

- **WHEN** the consent screen is rendered for a client whose redirect host is on the operator's list
- **THEN** the self-registration notice SHALL still be present

### Requirement: An operator-configurable allow-list of redirect hosts SHALL drive a known-client badge, and everything else SHALL carry a warning

The server SHALL accept an operator-configured list of known connector redirect hosts, defaulting to the connector hosts this deployment serves. When a client's redirect host is on that list the consent screen SHALL show a badge; when it is not, the screen SHALL show a warning that names the host, states that the authorization code will be sent there, and advises refusing unless the user began the flow from that application.

Matching SHALL be case-insensitive **equality** of the redirect URI's host against a configured entry. It SHALL NOT be a suffix, prefix, substring or wildcard match: `evilclaude.ai` and `claude.ai.evil.example` SHALL NOT match an entry of `claude.ai`. Configuration SHALL reject entries containing wildcard, path, userinfo or whitespace characters, so an operator who writes a pattern is told it is unsupported rather than silently given an entry that matches nothing.

An empty list SHALL mean that every client is unrecognised, so clearing the setting produces warnings rather than badges. The badge SHALL be worded as a statement about the **destination** being one the operator listed, never as a statement that the application, its name, or its intent has been verified.

#### Scenario: An allow-listed host shows the badge

- **WHEN** the consent screen is rendered for a client whose redirect host equals a configured entry
- **THEN** the known-client badge SHALL be shown
- **AND** the unverified warning SHALL NOT be shown

#### Scenario: A self-registered client without an allow-listed host shows the warning

- **WHEN** the consent screen is rendered for a client whose redirect host is not a configured entry
- **THEN** the warning SHALL be shown and SHALL name that host
- **AND** the known-client badge SHALL NOT be shown

#### Scenario: A look-alike host does not earn the badge

- **WHEN** the consent screen is rendered for a redirect host that contains a configured entry as a prefix, suffix or substring without equalling it
- **THEN** the warning SHALL be shown

#### Scenario: An empty list means nothing is recognised

- **WHEN** the operator configures an empty list of known redirect hosts
- **THEN** every consent screen SHALL show the warning

#### Scenario: A pattern entry is rejected at configuration time

- **WHEN** the operator configures an entry containing a wildcard, a path separator, an at-sign or whitespace
- **THEN** configuration SHALL fail with a message saying patterns are not supported

### Requirement: Client registration SHALL require a redirect URI with a resolvable ASCII host

Dynamic client registration SHALL reject a redirect URI whose host component is empty, and SHALL reject one whose host cannot be converted to its ASCII (IDNA A-label) form; a host that converts SHALL be stored in that converted form. The existing requirements — HTTPS scheme, no fragment — are unchanged and are not sufficient on their own.

The current check accepts any non-empty authority, so `https://@/cb` registers: its authority is `"@"` and truthy while its host is empty, and the consent screen would then have no destination to disclose at all — defeating the identification requirement above at registration time rather than at display time. Normalising to the A-label at registration also ensures a host cannot be stored in two forms that render alike and compare differently.

Because rows registered before this requirement may hold an empty or non-ASCII host, the consent screen SHALL degrade safely rather than assume: a redirect URI whose host cannot be determined SHALL be shown as such, SHALL take the unverified-warning branch, and SHALL NOT be eligible for the known-client badge under any configuration.

#### Scenario: A redirect URI with no host is refused at registration

- **WHEN** a client registers with a redirect URI of the form `https://@/cb`
- **THEN** registration SHALL be refused as an invalid redirect URI

#### Scenario: A non-ASCII host is normalised at registration

- **WHEN** a client registers with a redirect URI whose host is not ASCII and does convert
- **THEN** registration SHALL succeed and the stored redirect URI's host SHALL be its ASCII form

#### Scenario: A host that cannot be converted is refused

- **WHEN** a client registers with a redirect URI whose host cannot be converted to an ASCII form
- **THEN** registration SHALL be refused as an invalid redirect URI

#### Scenario: A pre-existing hostless redirect never earns the badge

- **WHEN** the consent screen is rendered for a client registered before this requirement whose redirect URI has no determinable host
- **THEN** the destination SHALL be shown as undetermined
- **AND** the unverified warning SHALL be shown and the known-client badge SHALL NOT be
