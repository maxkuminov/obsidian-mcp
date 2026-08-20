## Why

`check_upload` calls the agent's own upload link somebody else's.

`lookup_by_public_id` scopes an OAuth-minted transfer to the exact
`oauth_tokens.id` row that minted it, because `transfer_tokens` records that id
and nothing else about the OAuth side. But an access token lives one hour, and
`_handle_refresh` mints a **brand-new row** for the same user, the same client
and the same consent. After any rotation the lookup matches nothing, and the
tool answers with the string reserved for a genuinely foreign handle: "not
found: no upload link with id … was minted by this identity" (#74).

The completed case needs no assumption about refresh timing. An agent mints an
upload link at 14:50; the human uploads at 14:55 and it completes; the access
token expires at 15:50 and the client refreshes; at 15:55 the agent calls
`check_upload` to get the sha256 — which `request_upload`'s own result text
instructs it to do. The row is present (transfer rows are pruned only a day
past expiry) and reads `completed` with path, size, sha256 and mime. The tool
disowns it.

The implementation matched the spec. **The spec's notion of OAuth identity is
what is too narrow**: it defines a transfer's identity as the credential row,
and for OAuth that row is one hour of a principal, not the principal.

## What Changes

- **`lookup_by_public_id` scopes to the principal, not the credential row.** An
  API key is its own principal and is unchanged. An OAuth access token's
  principal is its **grant family** — `oauth_tokens.grant_id`, NOT NULL since
  migration 014 (#64) — so the lookup matches a transfer row whose *minting*
  token is a sibling of the *presenting* token in that family. `user_id` is
  still compared on top.
- **No new column and no migration.** The predicate is one correlated `EXISTS`
  joining `oauth_tokens` to itself on `grant_id`. See design.md for why the
  frozen-column alternative buys nothing here.
- **Nothing else widens.** A different grant — another client, or a second
  `/authorize` approval by the same user — is still `not found`, and the family
  comparison includes `client_id` as well as `grant_id` so two clients can
  never share a principal. Redemption on the `/transfer/*` routes stays bound
  to the exact minting credential row; design.md records why that cannot make
  the tool lie again.
- **One accepted limitation, recorded rather than fixed** (design.md D4a):
  014's backfill groups pre-existing rows by `(client_id, user_id)`, so two
  consents made by the same user for the same client *before* 014 share one
  family and can read each other's handle status. Same user, same client,
  read-only status; every grant issued after 014 is exact.
- **`_credential_ok` uses `src.oauth.scope.token_has_write`** instead of its
  private `"readwrite" not in (cred.scope or "").split()` copy. Same
  behaviour, one fewer place for the definition of "write" to drift (#67).
- Test fixtures in `tests/integration/test_transfer_pg.py` seed `grant_id`,
  which migration 014 made NOT NULL after that module was last touched.

## Capabilities

### Modified Capabilities
- `file-transfer`: handle scoping is defined against the minting principal
  (grant family for OAuth, the key itself for an API key) rather than the
  credential row, and the redemption/lookup asymmetry is stated.

## Impact

- `src/services/transfer.py` — `lookup_by_public_id`, new `_same` /
  `_minted_by_principal` helpers, `_credential_ok`'s write test
- `src/mcp_server/tools.py` — comment only, in `check_upload_impl`
- `tests/test_issue_74_transfer_grant_lookup.py` — new
- `tests/integration/test_transfer_pg.py` — `grant_id` in the fixtures, plus a
  handle-scoping section (rotation, multi-hop rotation, second consent, other
  user, other key, vanished presenting token, direction, redemption binding)
- `CLAUDE.md` — "The handle belongs to the principal, not to the credential row"

No migration, no schema change, no config. `Identity` is unchanged, so nothing
outside the transfer service needed to learn about grants.

Rebased onto `origin/main` after the wave-1 archive, so the MODIFIED
`check_upload` block is diffed against the **archived** requirement text: the
only differences are the principal wording, the renamed cross-principal
scenario, and the added rotation scenario.
