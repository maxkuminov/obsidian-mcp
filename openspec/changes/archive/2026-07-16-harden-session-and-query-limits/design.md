## Context

Starlette stores session state in signed client-side cookies, so changing a password does not inherently revoke an already issued cookie. MCP tool arguments are authenticated but still untrusted and can request result sizes large enough to exhaust database, application, or client resources.

## Decisions

### Version signed sessions

Add a non-null integer `session_version` to each user. Login and bootstrap copy it into the signed session. Authentication requires an exact match with the current database value and clears mismatched cookies. Password reset increments the database value.

This keeps the existing stateless cookie design while providing global per-user revocation. Existing cookies without a version fail closed after deployment.

### Clamp limits twice

Clamp ordinary query/list tools to 500 results and semantic search to 50. Apply limits at the MCP implementation boundary and again in reusable search services so future internal callers cannot bypass the resource guard.

### Match OAuth cookie security to transport

Use the same `BASE_URL` HTTPS check already used for the main session cookie. Public deployments remain Secure-only; loopback HTTP development remains functional.

## Risks / Trade-offs

- Password reset signs out all devices, including the device initiating an administrative self-reset. This is intentional.
- A deployment of the migration invalidates existing cookies that lack `session_version`.
- Result requests above the cap return the capped number rather than an error, preserving current tool ergonomics.
