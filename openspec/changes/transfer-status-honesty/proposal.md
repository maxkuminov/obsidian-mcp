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
  moves into `transfer.upload_stream_deadline`, and both surfaces are put in
  **one clock domain**: the route hands `stream_to_vault` that absolute UTC
  instant instead of converting it to `time.monotonic()` at claim time, so a
  realtime clock step can no longer make the route and the status tool describe
  different instants. `transfer.now_utc()` is the single source of "now" for
  both. `import_from_url` keeps a monotonic fetch budget — it is private and
  nothing reports on it.
- `check_upload` re-checks `resolve_identity_ok(need_write=True)` and
  `resolve_root_ok` inside the still-open session for `pending`/`claimed` rows
  and reports a distinct dead state; `completed` rows are not re-checked.
- Mint clamps `expires_at` to `min(requested TTL, credential expiry)` in
  `transfer.plan_mint_window`, called by `mint_token` **itself**, in its own
  transaction, immediately before the INSERT. There is deliberately no
  parameter for the window — a caller-supplied deadline is a caller-supplied
  security boundary, and a stale one reinstates the bug — so `mint_token`
  returns the window it computed instead. It also re-validates the credential
  with `_credential_ok`, the redemption predicate, so a key revoked or
  downgraded between the tool's permission check and the INSERT mints nothing.
  Under 30 s of runway — or no credential at all — refuses the mint. The tool
  result says when and why the TTL was shortened.
- The stream deadline is **re-checked inside the locked publish gate**,
  immediately before `vault_fs.publish`. `_drain` bounds the body; the gate can
  wait unboundedly on `SELECT … FOR UPDATE` afterwards, so a body that finished
  just inside the deadline could publish (overwrite included) long after the
  capability expired. It raises the existing `Timeout`, so the route consumes
  the token per the state machine, and it is unambiguously pre-publication.
- **An ownerless identity is nobody in multi-user mode.** `user_id IS NULL`
  passed the ownership comparison (`None == None`) and the two vault-root checks
  then authorised the globally configured `VAULT_PATH` without consulting
  `MULTI_USER_MODE`, so a capability minted by an ownerless key before the
  operator flipped the switch stayed redeemable after it. All three predicates
  now fail closed; single-user mode is unchanged.
- The upload page gains a **Mode** row and destructive labelling for
  `overwrite=True`, driven from the `overwrite` field already in the info
  payload.

## Capabilities

### New Capabilities

**A note on what the MODIFIED `check_upload` requirement drops.** The old text
required the docstring to "tell the agent to mint a new token if `uploading`
persists". That is deliberately gone: it is the advice that produced the wrong
action. A claim that outlives its stream may be a `PostPublishFailure`, where
the file is already in the vault, so "mint a new one" is exactly what an agent
must *not* do before checking the path — and with an `overwrite=True` token it
would write over what already landed. The replacement requires the tool to name
the stream deadline and to answer definitively (`completed` or `unknown`)
afterwards, which is the same guidance made conditional on the truth.

### Modified Capabilities
- `file-transfer`: `check_upload`'s reported states and liveness re-check;
  token expiry clamped to the minting credential; the stream deadline re-checked
  inside the publish gate; ownerless identities refused in multi-user mode; the
  upload consent page must state the mode.

## Impact

- `src/services/transfer.py` — `now_utc`, `_deadline_remaining`,
  `MIN_MINT_TTL_SECONDS`, `CredentialNotUsable` /
  `CredentialTooShortLived`, `credential_expires_at`, `MintWindow`,
  `plan_mint_window`, `upload_stream_deadline`, `_refuse_if_past_deadline`,
  `_ownerless_in_multi_user` (consulted by `_credential_ok`, `resolve_root_ok`
  and `locked_rows_ok`); `mint_token` returns
  `(token, row, window)` and takes no window; `_drain` measures its deadline
  through `_deadline_remaining`; `_load_credential` accepts an `Identity` as
  well as a token row.
- `src/mcp_server/tools.py` — transfer region only: `_utc_stamp`, `_clamp_note`,
  the two mint tools, `check_upload_impl`.
- `src/transfer/routes.py` — `_upload_deadline` returns the shared absolute
  instant; the local `_now` and the `time` import are gone, so nothing here
  defines a second source of "now".
- `src/control_panel/templates/transfer_upload.html` — Mode row, destructive
  button and copy.
- `src/mcp_server/server.py` — `check_upload` / `request_upload` docstrings (the
  agent-facing contract stated the same falsehood the tool did).
- No database schema change, no new dependency, no config change.
