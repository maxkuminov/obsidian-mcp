## Why

The usage page is one undifferentiated stream: with multiple users, keys, and agents on the server there is no way to see who drives load, and no guardrail if a runaway agent loops on an expensive tool. Attribution columns already exist on every `usage_logs` row; nothing reads them selectively, and nothing enforces a ceiling.

## What Changes

- Usage page gains filters (user, API key, tool, window) and per-actor totals; the existing request log honors the filters.
- Optional per-key daily quota: nullable `daily_request_limit` on `api_keys` (migration **020**); enforced at the MCP layer — a key over its UTC-day count receives a structured tool error until the day rolls over. Key create/edit UI exposes it. OAuth-token traffic is exempt in v1 (panel OAuth is the operator).
- Quota state (today's count vs. limit) shown on the keys page.

## Capabilities

### New Capabilities

- `usage-quotas`: quota semantics and enforcement at the tool layer.
- `panel-usage-slicing`: filtered usage views, per-actor totals, quota administration UI.

### Modified Capabilities

(none — attribution write-side (`usage-attribution`) and key auth (`api-auth-hardening`) requirements stand; enforcement composes after auth)

## Impact

- `src/mcp_server/tools.py` (enforcement in `_tracked`), `src/models/db.py` + migration 020, `src/api/`/`src/control_panel/` (filters, key form, totals), `docs/architecture/usage-attribution.md`.
- Sequenced after `panel-light-mode`; schema gate mandatory. Adversarial Codex pass mandatory (touches the tool execution path). Issue: #162.
