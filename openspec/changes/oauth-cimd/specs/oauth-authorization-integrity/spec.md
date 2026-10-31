## MODIFIED Requirements

### Requirement: The consent screen SHALL identify the client it is asking the user to trust

The authorization display SHALL show, for a registered client, alongside the application's self-declared name, the **host** of the redirect URI the authorization code would be delivered to, the client identifier the server assigned at registration, and the date the client registered. For a Client ID Metadata Document client it SHALL instead show the **host of the client identifier URL** as the application, the full client identifier URL, the redirect host, and the date this user first approved the client (or that this is the first approval), and SHALL NOT render the document's self-declared `client_name` on the consent screen.

The host SHALL be derived from the redirect URI's host component alone — never from its authority string — so that a URI embedding a familiar name in its userinfo (`https://claude.ai@evil.example/cb`) displays the host the code actually reaches. It SHALL be displayed lower-cased and in its ASCII form; a non-ASCII host SHALL be displayed in its punycode form and SHALL NOT be rendered decoded, because a decoded homograph host defeats the disclosure entirely. The client identifier URL's host SHALL be derived and displayed by the same rules.

All client-supplied values on the screen SHALL be rendered as text, and the host SHALL be presented in its own labelled element distinct from the application name, so that a name chosen to look like a host cannot be mistaken for one.

When every redirect URI the client can use is a loopback address, the screen SHALL additionally warn that the code is delivered to a program on this computer and that any local program can claim that address.

The approval handler already re-validates the submitted redirect URI against the client's registered list (or, for a CIMD client, the validated document through the same matching function) before minting anything, so the destination shown on display is the destination the code is delivered to.

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

- **WHEN** the consent screen is rendered for a registered client
- **THEN** it SHALL show the server-assigned client identifier and the date the client registered

#### Scenario: A hostile name cannot impersonate the host field

- **WHEN** a client registers a name containing markup or a host-like string
- **THEN** it SHALL be rendered as text in the name element only
- **AND** the destination element SHALL still show the redirect URI's host

#### Scenario: A CIMD client is identified by its document host

- **WHEN** the consent screen is rendered for `client_id=https://claude.ai/oauth/claude-code-client-metadata` whose document names itself `Totally Not Claude`
- **THEN** the application element SHALL show `claude.ai` and the client identifier element the full URL
- **AND** the text `Totally Not Claude` SHALL NOT appear on the page

#### Scenario: Loopback-only destinations carry the extra warning

- **WHEN** the consent screen is rendered for a CIMD client whose document lists only `http://localhost/callback` and `http://127.0.0.1/callback`
- **THEN** the loopback warning SHALL be present

### Requirement: The consent screen SHALL state unconditionally that clients register themselves and are not verified

Every rendering of the authorization screen SHALL carry a notice about what the server has and has not verified. For a registered client it SHALL say that the application registered itself with this server, that registration is open, and that the displayed name was chosen by the application and is not verified here. For a Client ID Metadata Document client it SHALL say that the application identified itself with a document published at the displayed host, that this server fetched that document, and that it does not verify who operates that host. The notice SHALL be shown regardless of whether the client's redirect host is recognised, because the server verifies no application's identity under any circumstances.

#### Scenario: The notice is present for an unrecognised client

- **WHEN** the consent screen is rendered for a client whose redirect host is not on the operator's list
- **THEN** the self-registration notice SHALL be present

#### Scenario: The notice is present for a recognised client

- **WHEN** the consent screen is rendered for a client whose redirect host is on the operator's list
- **THEN** the self-registration notice SHALL still be present

#### Scenario: The CIMD notice is present

- **WHEN** the consent screen is rendered for a CIMD client, recognised destination or not
- **THEN** the notice SHALL name the document host and state that its operator is not verified
