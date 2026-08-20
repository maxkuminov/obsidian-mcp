## 1. Shared stream deadline (#75 groundwork)

- [x] 1.1 Add `transfer.upload_stream_deadline(row)` returning the absolute UTC
      `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)`, with a
      `_as_aware` normaliser for database timestamps
- [x] 1.2 Put the route and the tool in **one clock domain**: add
      `transfer.now_utc` and `_deadline_remaining`, have `_drain` measure the
      deadline through it, and make `routes._upload_deadline` return the shared
      absolute instant instead of a `time.monotonic()` value
- [x] 1.3 Delete the route module's local `_now` and its `time` import so
      nothing under `src/transfer/` defines a second source of "now"
- [x] 1.4 Tests: `test_the_route_and_check_upload_share_one_stream_deadline`
      (identical instant, not "within a second"),
      `test_a_realtime_clock_step_moves_both_surfaces_together` (forward and
      backward steps)

## 2. `check_upload` reports a claimed token honestly (#75)

- [x] 2.1 Hoist the `claimed` branch above the expiry test in
      `check_upload_impl`
- [x] 2.2 Inside the deadline: `uploading`, naming the deadline, with no claim
      about whether the transfer will complete
- [x] 2.3 Past it: `unknown`, stating the bytes may already be in the vault and
      directing the agent to `list_files` / `read_file` first, mirroring
      `import_from_url`'s post-publish wording
- [x] 2.4 Hoist `consumed` above the expiry test too and say explicitly that
      nothing was published, so "never used" is reachable only for `pending`
- [x] 2.5 Tests: `test_check_upload_never_says_never_used_for_a_claimed_token`,
      `test_check_upload_reads_the_stream_deadline_not_the_ttl`,
      `test_check_upload_reports_uploading_with_the_stream_deadline`,
      `test_a_consumed_link_says_nothing_was_published`,
      `test_an_expired_consumed_link_is_not_reported_as_unused`

## 3. `check_upload` re-checks liveness (#71)

- [x] 3.1 Extend the `async with async_session()` block to cover the branching
      decision
- [x] 3.2 For `pending`/`claimed` rows run `resolve_identity_ok(need_write=True)`
      and `resolve_root_ok`; skip both for `completed`
- [x] 3.3 Report a distinct `revoked` state for a dead `pending` link, naming
      whether it was the credential or the vault root
- [x] 3.4 For a dead `claimed` link, append the reason to the upload outcome
      rather than replacing it
- [x] 3.5 Tests: `test_check_upload_reports_a_downgraded_credential_instead_of_pending`,
      `test_check_upload_reports_a_reassigned_vault_root`,
      `test_the_liveness_check_asks_for_write_inside_the_open_session`,
      `test_a_completed_row_is_not_re_checked`,
      `test_a_dead_claimed_link_still_reports_the_upload_outcome_first`
- [x] 3.6 Test that nothing the new branches add reaches `usage_logs`
      (`test_check_upload_still_logs_only_the_handle_after_the_liveness_check`)

## 4. The credential's expiry clamps the link (#73)

- [x] 4.1 Add `MIN_MINT_TTL_SECONDS`, `CredentialNotUsable` /
      `CredentialTooShortLived`, `credential_expires_at`, `MintWindow` and
      `plan_mint_window` to `transfer.py`; let `_load_credential` take an
      `Identity` as well as a row
- [x] 4.2 `mint_token` takes **no** window parameter: it calls
      `plan_mint_window` itself, in its own transaction immediately before the
      INSERT, and returns `(token, row, window)` so the tools can report a clamp
- [x] 4.3 `plan_mint_window` also re-validates the credential with
      `_credential_ok` (the redemption predicate, `need_write` from the
      direction), so a revocation/downgrade landing between the tool's
      permission check and the INSERT mints nothing
- [x] 4.4 `request_upload` / `request_download` unpack the window and turn
      `CredentialNotUsable` into a re-authenticate tool error
- [x] 4.5 `_clamp_note` states in the tool result when the credential shortened
      the TTL
- [x] 4.6 Tests: `test_mint_clamps_the_link_to_the_credential_expiry`,
      `test_a_clamped_mint_says_so_and_why`, `test_an_unclamped_mint_stays_quiet`,
      `test_request_download_clamps_to_the_credential_too`,
      `test_a_credential_about_to_die_mints_nothing`,
      `test_a_credential_invalidated_before_the_insert_mints_nothing`,
      `test_mint_token_accepts_no_externally_computed_expiry`,
      `test_an_oauth_token_with_no_expiry_mints_nothing`,
      `test_a_call_with_no_credential_at_all_mints_nothing`
- [x] 4.7 Postgres integration: `test_info_reports_the_credential_clamped_expiry`
      (real mint, real route, clamped deadline on the wire),
      `test_a_nearly_expired_credential_mints_nothing`,
      `test_a_scope_downgrade_before_the_insert_mints_nothing`

## 5. The consent page states the mode (#72)

- [x] 5.1 Add the `Mode` row to `transfer_upload.html`, driven from
      `info.overwrite`
- [x] 5.2 Destructive button label, status copy and in-flight/result wording for
      an overwrite link; page stays self-contained and nonce-guarded
- [x] 5.3 Tests: `test_the_upload_page_states_the_mode_it_will_act_in` (mode row,
      `textContent` not `innerHTML`, destructive copy inside the `overwrite`
      branch), `test_info_reports_an_overwrite_token_as_such`, and `overwrite`
      asserted in `test_info_returns_the_bound_metadata`

## 6. Documentation and spec

- [x] 6.1 Spec deltas for the three modified `file-transfer` requirements
- [x] 6.2 Update the `check_upload`, `request_upload` and `request_download`
      tool docstrings in `server.py` — they carried the same falsehood the tool
      did, and said nothing about the credential clamp
- [x] 6.3 Update the "File transfer" section of `CLAUDE.md`
- [x] 6.4 `openspec validate transfer-status-honesty --strict` passes
- [x] 6.5 Full unit suite green, and `tests/integration/test_transfer_pg.py`
      green against a throwaway `pgvector/pgvector:pg16`
