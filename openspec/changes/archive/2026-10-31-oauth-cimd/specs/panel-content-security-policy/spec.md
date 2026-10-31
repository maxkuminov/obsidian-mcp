## MODIFIED Requirements

### Requirement: The consent page's form-action SHALL be 'self' https: and every other page's SHALL be 'self'
The `GET /authorize` response that renders the consent form SHALL carry `form-action 'self' https: http://localhost:* http://127.0.0.1:*`, and every other panel-surface response SHALL carry `form-action 'self'`. No request-derived value SHALL be interpolated into the policy; the only variable part of the header is the server-generated nonce. Where an authorization code may be delivered SHALL continue to be decided by the server's rules — a registered client's `redirect_uri` must be `https` with a non-empty host, a Client ID Metadata Document client's must match its validated document (loopback `http` port-agnostically, everything else exactly), and `POST /authorize` re-validates the submitted `redirect_uri` through the same matching function before redirecting.

The consent form posts to `/authorize`, which answers with a redirect to the client's redirect URI for both approve and deny, and browsers apply `form-action` to every redirect hop of a form-submission navigation; an HTTPS scheme source keeps multi-hop callbacks and non-canonically spelled hosts working, and the two loopback sources let a native client's `http://localhost:PORT` or `http://127.0.0.1:PORT` callback (RFC 8252) receive its code, while every other non-HTTPS form target stays refused. CSP host-source syntax cannot name an IPv6 literal, so a `[::1]` callback is not admitted by the policy.

#### Scenario: Approve and deny reach the registered redirect URI
- **WHEN** a user approves, or denies, on the consent page for a client whose registered callback redirects on to another HTTPS origin
- **THEN** the consent page's policy SHALL contain `form-action 'self' https: http://localhost:* http://127.0.0.1:*`
- **AND** the browser SHALL follow every hop without a policy violation

#### Scenario: A loopback callback receives its code
- **WHEN** a user approves for a CIMD client whose request names `http://localhost:3118/callback`
- **THEN** the browser SHALL follow the redirect to the loopback callback without a policy violation

#### Scenario: Non-HTTPS form targets stay refused on the consent page
- **WHEN** the consent page's policy is parsed
- **THEN** `form-action` SHALL consist of exactly `'self'`, `https:`, `http://localhost:*` and `http://127.0.0.1:*`

#### Scenario: Other pages keep form-action 'self'
- **WHEN** any panel, login or register page is rendered
- **THEN** its `form-action` SHALL be exactly `'self'`
