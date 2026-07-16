## Why

A follow-up bug and security review found that authenticated MCP callers can request unbounded result sets, password resets do not invalidate existing signed browser sessions, and OAuth consent fails on the explicitly supported loopback HTTP configuration.

## What Changes

- Clamp MCP query result limits at public tool and service boundaries
- Bind signed browser sessions to a database-backed user session version
- Increment the session version whenever an administrator resets a password
- Make the OAuth consent-state cookie secure only when `BASE_URL` uses HTTPS
- Add regression tests for each behavior

## Capabilities

### New Capabilities

- `session-invalidation`: Password resets invalidate all previously issued browser sessions
- `query-resource-limits`: MCP search and listing calls have bounded result sizes

### Modified Capabilities

- OAuth loopback development supports the complete consent flow over HTTP

## Impact

- Adds `users.session_version` through Alembic revision 011
- Existing multi-user browser sessions are invalidated once during deployment because old cookies do not contain a session version
- No MCP method signatures or successful response formats change
