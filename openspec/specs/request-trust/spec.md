# request-trust Specification

## Purpose
TBD - created by archiving change security-hardening. Update Purpose after archive.
## Requirements
### Requirement: Restricted proxy header trust
The `ProxyHeadersMiddleware` SHALL trust only RFC 1918 private network ranges and localhost: `127.0.0.1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`. Requests from untrusted sources SHALL have their `X-Forwarded-For` and `X-Forwarded-Proto` headers ignored.

#### Scenario: Request from Docker network trusted
- **WHEN** a request arrives from IP `172.18.0.2` with `X-Forwarded-For: 203.0.113.1`
- **THEN** the middleware trusts the header and the application sees the client IP as `203.0.113.1`

#### Scenario: Request from public IP untrusted
- **WHEN** a request arrives from IP `203.0.113.50` with `X-Forwarded-For: 10.0.0.1`
- **THEN** the middleware ignores the header and the application sees the client IP as `203.0.113.50`

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

