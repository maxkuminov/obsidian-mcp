# request-trust Specification

## Purpose
TBD - created by archiving change security-hardening. Update Purpose after archive.
## Requirements
### Requirement: Restricted proxy header trust
The `ProxyHeadersMiddleware` SHALL trust `X-Forwarded-For` and `X-Forwarded-Proto` only from peers listed in a single configuration setting, `TRUSTED_PROXY_IPS`, whose default is exactly the behaviour in force before this change: `127.0.0.1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`. Requests from untrusted sources SHALL have those headers ignored. There SHALL be exactly one such control in the deployment: the uvicorn-level forwarded-address allow-list MUST be disabled, so that no second list can diverge from this one.

The setting exists because the hard-coded list spans a Docker network shared with other containers, several of which run user-supplied code, and every slowapi limiter keys on the address this middleware resolves. Narrowing the list is the operator's decision and must not require a code change. Two controls that happen to agree is not one control: uvicorn's own allow-list defaults to enabled and reads an environment variable, so an operator can break the agreement without editing either file.

Every entry SHALL be validated at startup as an IP address or CIDR network, and a malformed entry SHALL refuse startup naming the offending entry rather than being silently dropped — a trust list that quietly loses a range is worse than one that fails loudly.

Validation alone is insufficient: an entry SHALL be stored in its **canonical** network form, and that canonical value SHALL be what reaches the middleware and the startup log. A CIDR carrying host bits, such as `192.168.0.10/24`, is accepted by a permissive parse but rejected by the middleware's own stricter one, which then keeps the string as a literal that matches no peer. The failure is silent and inverts the intent: every proxied request retains the proxy's address, so all callers collapse into one limiter bucket. Canonicalising at the boundary is what prevents a setting that appears valid from disabling the control it configures.

The effective canonical list SHALL be logged once at startup, because a trust boundary that cannot be read from the logs cannot be audited. An empty setting means no peer is trusted, which is the correct configuration for a directly exposed deployment.

#### Scenario: Request from Docker network trusted
- **WHEN** a request arrives from IP `172.18.0.2` with `X-Forwarded-For: 203.0.113.1` under the default setting
- **THEN** the middleware SHALL trust the header and the application SHALL see the client IP as `203.0.113.1`

#### Scenario: Request from public IP untrusted
- **WHEN** a request arrives from IP `203.0.113.50` with `X-Forwarded-For: 10.0.0.1`
- **THEN** the middleware SHALL ignore the header and the application SHALL see the client IP as `203.0.113.50`

#### Scenario: The default trusts a private-network proxy peer
- **WHEN** the application starts with no `TRUSTED_PROXY_IPS` value set and a request arrives from `192.168.0.10` carrying `X-Forwarded-For: 203.0.113.7`
- **THEN** the header SHALL be trusted and the application SHALL see `203.0.113.7`, so a deploy that changes no environment value changes no client-IP resolution

#### Scenario: A narrowed list excludes a former peer
- **WHEN** `TRUSTED_PROXY_IPS` is set to a single proxy address and a request arrives from a different address on the same subnet carrying `X-Forwarded-For`
- **THEN** the header SHALL be ignored and the application SHALL see the peer's own address

#### Scenario: A CIDR with host bits is canonicalised, not passed through
- **WHEN** `TRUSTED_PROXY_IPS` is set to `192.168.0.10/24`
- **THEN** the stored and logged value SHALL be `192.168.0.0/24`
- **AND** a request arriving from `192.168.0.10` carrying `X-Forwarded-For` SHALL be trusted by the installed middleware, proving the canonical form reached it rather than an unmatched literal

#### Scenario: A bare address stays bare
- **WHEN** `TRUSTED_PROXY_IPS` contains a bare address such as `127.0.0.1`
- **THEN** it SHALL be stored and logged unchanged, not rewritten into a prefixed form

#### Scenario: A malformed entry refuses startup
- **WHEN** `TRUSTED_PROXY_IPS` contains a value that is neither an IP address nor a CIDR network
- **THEN** startup SHALL fail with an error naming that entry, and no request SHALL be served

#### Scenario: The list is accepted in either operator spelling
- **WHEN** `TRUSTED_PROXY_IPS` is supplied as a comma-separated string or as a JSON array
- **THEN** both SHALL parse to the same trust list

#### Scenario: Exactly one control exists
- **WHEN** the deployed process's startup command is inspected
- **THEN** it SHALL disable uvicorn's own forwarded-address handling, leaving the application middleware as the only layer that interprets `X-Forwarded-*`

#### Scenario: An empty setting trusts nobody
- **WHEN** `TRUSTED_PROXY_IPS` is set to an empty or explicitly disabled value and a request arrives carrying `X-Forwarded-For`
- **THEN** the header SHALL be ignored regardless of the peer's address

#### Scenario: The effective list is observable
- **WHEN** the application starts
- **THEN** it SHALL log the effective trust list once, so an operator can confirm what the running process trusts

### Requirement: Vault browser path traversal prevention
The vault browser (`GET /admin/vault`) SHALL validate the `folder` query parameter against directory traversal. If the resolved path escapes the vault root, the system MUST fall back to displaying the vault root instead of the requested folder.

#### Scenario: Normal folder navigation
- **WHEN** a user navigates to `/admin/vault?folder=Cards`
- **THEN** the vault browser displays the contents of the `Cards` folder

#### Scenario: Traversal attempt blocked
- **WHEN** a user navigates to `/admin/vault?folder=../../etc`
- **THEN** the vault browser displays the vault root (not `/etc`)

#### Scenario: Dot-dot in middle of path blocked
- **WHEN** a user navigates to `/admin/vault?folder=Cards/../../../etc`
- **THEN** the vault browser displays the vault root

