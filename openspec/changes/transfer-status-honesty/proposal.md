## Why

Four findings from the divergence sweep, all one shape: a transfer surface
tells someone something the rest of the system does not agree with. The
consumer is an autonomous agent that relays these strings to a human and acts
on them without anybody reading the database.

- **#75** — `check_upload` answers a claimed-and-stranded token with *"the link
  was never used"*. That token state is reached by exactly one path,
  `PostPublishFailure`, which happens **after** the bytes are in the vault. The
  expiry branch fired before the claimed branch, and with a ten-minute TTL the
  false answer is the one an agent is most likely to see. `import_from_url`
  already handles the identical outcome honestly; `check_upload` said the
  opposite.
- **#71** — `check_upload` decides `pending` from the token row alone, while
  `PUT /transfer/upload` additionally requires `resolve_identity_ok(need_write)`
  and `resolve_root_ok`. After an OAuth scope downgrade or a vault
  reassignment, the agent asserts the link is live and the human's upload
  returns a bare 404.
- **#73** — every surface shows `transfer_tokens.expires_at`, but redemption
  also requires the *minting credential* to be unexpired. On the OAuth path (the
  Claude.ai connector) an access token lives one hour, so a link minted late in
  that hour dies well inside the window the agent quoted.
- **#72** — the upload consent page shows Destination / Maximum size / Link
  expires and drops `overwrite`, which has been on the wire since the routes'
  first commit. The one session-less write path in the app never marks a
  destructive upload as destructive to the person authorising it.

## What Changes

- `check_upload` answers a claimed token **before** testing expiry, and never
  as "never used": inside the stream deadline it is `uploading` (naming the
  deadline), past it `unknown`, with the same "the file may already be there,
  check it" advice `import_from_url` gives.
- The stream deadline `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)`
  moves into `transfer.upload_stream_deadline`, which the route's
  `_upload_deadline` now calls, so the status tool cannot drift from the route
  that enforces it.
- `check_upload` re-checks `resolve_identity_ok(need_write=True)` and
  `resolve_root_ok` inside the still-open session for `pending`/`claimed` rows
  and reports a distinct dead state; `completed` rows are not re-checked.
- Mint clamps `expires_at` to `min(requested TTL, credential expiry)` in
  `transfer.plan_mint_window`, called from `mint_token` itself so no mint path
  can forget it. Under 30 s of runway — or no credential at all — refuses the
  mint. The tool result says when and why the TTL was shortened.
- The upload page gains a **Mode** row and destructive labelling for
  `overwrite=True`, driven from the `overwrite` field already in the info
  payload.

## Capabilities

### New Capabilities

### Modified Capabilities
- `file-transfer`: `check_upload`'s reported states and liveness re-check;
  token expiry clamped to the minting credential; the upload consent page must
  state the mode.

## Impact

- `src/services/transfer.py` — `MIN_MINT_TTL_SECONDS`, `CredentialTooShortLived`,
  `credential_expires_at`, `MintWindow`, `plan_mint_window`,
  `upload_stream_deadline`; `mint_token` takes an optional `window`;
  `_load_credential` accepts an `Identity` as well as a token row.
- `src/mcp_server/tools.py` — transfer region only: `_utc_stamp`, `_clamp_note`,
  the two mint tools, `check_upload_impl`.
- `src/transfer/routes.py` — `_upload_deadline` delegates to the shared helper.
- `src/control_panel/templates/transfer_upload.html` — Mode row, destructive
  button and copy.
- `src/mcp_server/server.py` — `check_upload` / `request_upload` docstrings (the
  agent-facing contract stated the same falsehood the tool did).
- No database schema change, no new dependency, no config change.
