## Why

A thorough security review identified 13 findings across critical, high, and medium severity. The most impactful are: panel POST forms lack CSRF protection, the REST API has no application-level auth, `ProxyHeadersMiddleware` trusts all hosts (bypassing rate limiting), OAuth hash comparisons leak timing info, and API keys are leaked in URL query parameters. These need to be fixed before the multi-user mode sees wider adoption.

## What Changes

- **CSRF protection**: Add signed CSRF tokens to all panel and auth POST forms using `itsdangerous` (already a dependency)
- **REST API auth**: Gate `/api/*` endpoints behind `require_user_panel` / `require_admin_panel` so they don't rely solely on Traefik
- **Proxy trust restriction**: Replace `trusted_hosts="*"` with the Docker network range
- **Timing-safe comparisons**: Use `secrets.compare_digest` for all hash comparisons in OAuth token validation
- **API key URL leak**: Move the newly-created key from the URL query parameter into a session flash
- **Rate limit on login**: Add slowapi rate limiting to `POST /admin/auth/login`
- **Vault browser traversal**: Validate the `folder` query parameter in the vault browser against path traversal
- **Session fixation**: Clear and re-populate the session on login to prevent fixation attacks
- **Secret key validation**: Reject `changeme` as `secret_key` even in single-user mode
- **OAuth client secret registration**: Tighten dynamic client registration (rate limit is already present but depends on `X-Forwarded-For` which is spoofable — fix rides on the proxy trust fix)
- **OpenAI key masking**: Show only last 4 characters instead of first 8 + last 4

## Capabilities

### New Capabilities
- `csrf-protection`: Signed CSRF tokens for all panel and auth POST forms
- `api-auth-hardening`: Application-level auth on REST API routes, timing-safe hash comparisons, login rate limiting, session fixation prevention, secret key validation
- `request-trust`: Restrict proxy header trust, validate vault browser folder param

### Modified Capabilities

## Impact

- `src/main.py` — proxy middleware config, secret key validation
- `src/config.py` — secret key validator change
- `src/api/routes.py` — add auth dependencies
- `src/control_panel/routes.py` — CSRF tokens in all forms, session flash for API keys, vault browser path validation, OpenAI key masking
- `src/control_panel/users.py` — CSRF tokens in user management forms
- `src/auth/routes.py` — CSRF tokens, login rate limiting, session fixation fix
- `src/oauth/routes.py` — timing-safe hash comparison
- `src/mcp_server/auth.py` — no changes needed (already uses hash comparison correctly for its purpose)
- All Jinja2 templates with `<form method="POST">` — add hidden CSRF input
- No new dependencies (itsdangerous and slowapi already present)
- No database schema changes
- No breaking API changes
