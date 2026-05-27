## Context

A security audit identified 13 findings across critical/high/medium severity. The codebase already has solid fundamentals (path traversal prevention, atomic writes, bcrypt, PKCE, container hardening) but has gaps in CSRF, API auth layering, proxy trust, and several smaller issues. All fixes use existing dependencies — no new packages required.

## Goals / Non-Goals

**Goals:**
- Fix all 13 identified security findings
- Maintain backward compatibility (no breaking API changes, no DB migrations)
- Keep single-user mode working without degradation
- Use existing dependencies (itsdangerous, slowapi, secrets) for all fixes

**Non-Goals:**
- Adding a WAF or request-signing layer
- Rearchitecting the OAuth flow (it's already solid with PKCE + S256)
- Adding Content-Security-Policy headers (separate effort)
- Audit logging / alerting on suspicious activity
- Rate limiting on MCP tool endpoints (rate limiting on /token and /register already exists)

## Decisions

### 1. CSRF: Signed double-submit tokens via itsdangerous

**Decision**: Use `itsdangerous.URLSafeTimedSerializer` to generate per-session CSRF tokens, validated on every POST. The token is embedded in each form as a hidden input and verified server-side.

**Why not Starlette's CsrfMiddleware?** Starlette doesn't ship one. Third-party middleware (starlette-csrf) adds a dependency. itsdangerous is already present and the re-embed confirmation flow already uses this exact pattern — we generalize it.

**Approach**: A helper module `src/csrf.py` with `generate_csrf_token(session)` and `validate_csrf_token(session, token)`. The token is an HMAC of a random nonce using `secret_key` + a per-session salt. Templates get a `csrf_token` context variable via `_panel_context()`. Max age: 1 hour (generous enough for slow form filling).

### 2. REST API auth: Reuse existing panel dependencies

**Decision**: Add `require_user_panel` (or `require_admin_panel` for dangerous routes) as dependencies on the `/api` router, same pattern as panel routes.

**Why not a separate API auth scheme?** The REST API is currently used only by the panel's JavaScript (htmx). Adding the same session-based auth the panel uses is the simplest path. API-key-based REST auth would be a separate future capability.

### 3. Proxy trust: Restrict to RFC 1918 ranges

**Decision**: Change `trusted_hosts="*"` to `trusted_hosts=["127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]`. This covers Docker's default bridge/overlay networks and localhost.

**Alternative considered**: Making it configurable via env var. Decided against — the private ranges cover all reasonable Docker/Podman/K8s setups, and a misconfigured trust list is worse than a conservative default.

### 4. Session management on login

**Decision**: Clear `request.session` before populating it on successful login. Starlette's `SessionMiddleware` is cookie-based (signed payload, no server-side store), so clearing and re-populating in the same request effectively rotates the session.

### 5. API key flash: Session-based instead of URL

**Decision**: Store the new key in `request.session["flash_new_key"]` and read it once in the template (then clear). This keeps the key out of URLs, logs, and Referer headers.

### 6. Vault browser folder validation

**Decision**: Run the `folder` query parameter through the same `validate_path` function used by MCP tools. If it fails traversal validation, treat it as root (`""`).

## Risks / Trade-offs

- **CSRF tokens require session middleware** → In single-user mode, `SessionMiddleware` is not mounted. CSRF protection will only be active in multi-user mode. In single-user mode, Traefik's `chain-oauth@file` is the CSRF defense (SSO cookies are SameSite). This is acceptable: single-user mode already delegates all panel auth to Traefik.
  - *Mitigation*: Document this explicitly. If single-user mode ever drops Traefik, the CSRF gap reopens.

- **REST API auth breaks non-session callers** → Any script calling `/api/keys` without a session cookie will get 302'd. Currently this is only used by the panel itself, so no real breakage.
  - *Mitigation*: The REST API routes already aren't documented as a public API.

- **Secret key rejection in single-user mode** → Existing deployments with `SECRET_KEY=changeme` will fail to start after this change. The `.env.example` already uses `changeme`, so anyone who copied it verbatim is affected.
  - *Mitigation*: The error message tells the user exactly what to set. `secrets.token_hex(32)` is suggested in the error.

- **Proxy trust restriction** → If someone runs behind a non-RFC-1918 proxy, `X-Forwarded-For` will be ignored and rate limiting will see the proxy IP instead of the client IP.
  - *Mitigation*: This is the secure default. Users with exotic setups can override in code.
