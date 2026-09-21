## ADDED Requirements

### Requirement: Machine-facing paths refuse plaintext HTTP instead of redirecting
The reverse-proxy configuration SHALL answer every plaintext HTTP request for a machine-facing path with a 4xx status carrying no `Location` header, and SHALL NOT forward such a request to the application. The machine-facing paths are `/mcp`, `/transfer`, `/health`, `/.well-known`, `/register`, `/token`, `/revoke`, **and the root path `/` when the request carries an `Authorization: Bearer` header**. The refusal MUST apply to every HTTP method, because a redirect that preserves the method is the case in which a client replays its credential without noticing.

The Bearer-qualified root path is not optional. Both reference deployments already route it: an HTTPS router matches `/` with a bearer header and the application rewrites it to `/mcp/` for clients that strip the prefix. A refusal that omitted it would leave the leak it exists to close open on a supported entry point.

A redirect is the wrong answer here for a reason particular to this product's consumer. An MCP client misconfigured with an `http://` server URL has already placed a long-lived bearer credential on the wire in cleartext; the client stack then follows the redirect, keeps the `Authorization` header across a direct http-to-https hop, and succeeds. Every request works, so nothing ever surfaces the mistake. A hard failure on the first request is the only outcome that reaches the operator.

#### Scenario: A GET to the MCP endpoint over plaintext
- **WHEN** a client issues `GET http://<host>/mcp`
- **THEN** the response SHALL carry a 4xx status
- **AND** it SHALL carry no `Location` header
- **AND** the application SHALL record no request for it

#### Scenario: A POST preserving its method and body
- **WHEN** a client issues `POST http://<host>/mcp` or `POST http://<host>/token` carrying a request body
- **THEN** the response SHALL carry a 4xx status and no `Location` header, so no redirect can replay the method, the body or the `Authorization` header

#### Scenario: Every machine-facing path behaves alike
- **WHEN** a plaintext request targets any of `/mcp`, `/transfer/upload`, `/health`, `/.well-known/oauth-authorization-server`, `/register`, `/token` or `/revoke`
- **THEN** each SHALL receive the same 4xx refusal with no `Location` header

#### Scenario: The Bearer-qualified root path is refused
- **WHEN** a client issues `POST http://<host>/` or `GET http://<host>/` carrying an `Authorization: Bearer …` header
- **THEN** the response SHALL carry a 4xx status and no `Location` header, so the request cannot be replayed into the HTTPS root MCP route

#### Scenario: The refusal never reaches the application
- **WHEN** a plaintext request for a machine-facing path is refused
- **THEN** the refusal SHALL be produced by the proxy itself
- **AND** no request SHALL be forwarded to the application over the internal network

#### Scenario: HTTPS is unaffected
- **WHEN** the same paths are requested over HTTPS
- **THEN** each SHALL behave exactly as it did before this change, including the existing 401 for an unauthenticated `/mcp` call

### Requirement: Browser-facing paths continue to redirect to HTTPS
Plaintext requests for the browser-facing paths `/admin` and `/authorize` SHALL continue to receive an HTTP redirect to the HTTPS origin, and so SHALL a request for the root path `/` that carries no `Authorization: Bearer` header. A person who typed a hostname leaks no long-lived credential by being redirected, and refusing them would break the panel for no security gain; the asymmetry between the two sets is the control, not an oversight. The root path is therefore split by the header rather than by the path: the credential, not the URL, is what makes a request machine-facing there.

#### Scenario: The panel still redirects
- **WHEN** a browser issues `GET http://<host>/admin/`
- **THEN** the response SHALL be a redirect carrying a `Location` header with an `https://` URL

#### Scenario: The consent endpoint still redirects
- **WHEN** a browser issues `GET http://<host>/authorize`
- **THEN** the response SHALL be a redirect carrying a `Location` header with an `https://` URL

#### Scenario: An ordinary browser request to the root still redirects
- **WHEN** a browser issues `GET http://<host>/` with no `Authorization` header
- **THEN** the response SHALL be a redirect carrying a `Location` header with an `https://` URL, unaffected by the Bearer-qualified refusal

### Requirement: The plaintext refusal MUST NOT intercept the ACME HTTP-01 challenge
The refusal rule SHALL exempt the path prefix `/.well-known/acme-challenge/`, even though it sits inside the refused `/.well-known` prefix. The certificate resolver that issues over HTTP-01 answers on the same plaintext entrypoint, and a refusal there would break certificate renewal. The exemption MUST be expressed in the rule itself rather than relying on the proxy's internal challenge router outranking ours, so that an upgrade to the proxy cannot silently remove it.

#### Scenario: The challenge path is not refused
- **WHEN** a plaintext request targets `http://<host>/.well-known/acme-challenge/<token>`
- **THEN** it SHALL NOT receive the plaintext refusal

#### Scenario: The rest of the well-known prefix is refused
- **WHEN** a plaintext request targets `http://<host>/.well-known/oauth-authorization-server`
- **THEN** it SHALL receive the 4xx refusal with no `Location` header

### Requirement: The refusal rules live in tracked repository files and carry no host-specific value
The plaintext-refusal rules SHALL be defined in this repository's tracked deployment files, and SHALL name the public hostname only through the `MCP_HOSTNAME` variable. No **deployment-specific** value — the real public hostname, an address or subnet of the deployed network, or a host filesystem path — may appear, because the repository is public and the deploy-directory copy of the compose file must stay identical to the tracked one. A control expressed only in the host's own proxy configuration is not reproducible from the tree and is therefore not a control this project can rely on.

The prohibition is on values that identify a deployment, not on literal addresses as such. Addresses reserved for documentation by RFC 5737 and RFC 3849 are explicitly permitted: the refusal middleware's own source range is one, and a rule that banned it would ban the control it is describing. Example values already present in the repository's reference configurations are likewise untouched by this requirement.

#### Scenario: No deployment-specific value is committed
- **WHEN** the tracked deployment files carrying the refusal rules are inspected
- **THEN** the hostname SHALL appear only as the `MCP_HOSTNAME` variable reference
- **AND** no real public hostname, deployed-network address or subnet, or host filesystem path SHALL appear

#### Scenario: A documentation-reserved address is permitted
- **WHEN** the refusal middleware declares its source range as an RFC 5737 documentation address
- **THEN** that literal SHALL be permitted, and the check SHALL NOT report it

#### Scenario: Pre-existing reference examples are untouched
- **WHEN** the reference configuration retains example addresses or paths it carried before this change
- **THEN** the check SHALL NOT require their removal

#### Scenario: The rule outranks the catch-all deterministically
- **WHEN** the refusal router is defined alongside a lower-priority catch-all redirect router on the same plaintext entrypoint
- **THEN** the refusal router SHALL declare its priority explicitly rather than depending on a priority derived from its rule's length

#### Scenario: The reference Caddy deployment refuses the same paths
- **WHEN** the published reference Caddy configuration serves a plaintext request for a machine-facing path, including the root path carrying a bearer credential
- **THEN** it SHALL refuse with a 4xx and no `Location` header, and SHALL redirect `/admin`, `/authorize` and an unauthenticated root request as before, so the published reference is not weaker than the deployment it documents

### Requirement: The plaintext refusal is reversible without touching persistent state
Removing the refusal SHALL require only reverting the tracked deployment files and redeploying, and SHALL leave no database row, migration or cached state behind. An edge control that cannot be backed out in one step is a control an operator will hesitate to apply.

#### Scenario: Rollback restores the previous behaviour
- **WHEN** the refusal rules are reverted and the deployment is recreated
- **THEN** plaintext requests for machine-facing paths SHALL again receive the catch-all redirect
- **AND** no database migration or stored state SHALL need to be undone
