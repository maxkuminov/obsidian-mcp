## 1. Slice A — linear link grammar and v0 cleaner (`src/services/links.py`, `src/services/embeddings.py`, `src/mcp_server/tools.py` rewrite regexes)

- [ ] 1.1 Rewrite `_WIKILINK_RE` and `_MDLINK_RE` in `links.py` to exclude `[`/`]` from the target and text classes with possessive quantifiers; keep group names and semantics; update the grammar comment.
- [ ] 1.2 Apply the same change to `_WIKILINK_REWRITE_RE` and `_MDLINK_REWRITE_RE` in `tools.py` (they must stay grammar-identical to `links.py` — add a test that asserts the two pairs accept and reject the same corpus).
- [ ] 1.3 Add `MAX_LINKS_PER_NOTE = 10_000` to `src/config.py`; give `extract_links` a bounded variant (or a `max_links` parameter) that stops at the cap and reports truncation without changing the return type used by existing callers.
- [ ] 1.4 Reimplement `_v0_clean` in `embeddings.py` as a single-pass line scanner with byte-identical output; move the two v0 regexes into the test module as the oracle.
- [ ] 1.5 Tests: linearity benchmark for each of the four link regexes and the v0 cleaner (1 MiB of `[[`, `]]`, `[[a`, `[a](`; 160 KB of ```` ```x\n ````; assert < 0.5 s each); the accepted-difference test for `[a[b](x.md)`; a differential property test for v0 over generated fence inputs (unclosed openers, orphan closers, nested/adjacent mixed fences, trailing whitespace on closers, blank-line runs, CRLF, indented fences) plus every existing v0/v1 fixture.
- [ ] 1.6 Docs: note the grammar rule and the linearity guarantee in `docs/architecture/vault-tools.md` (link rewrite) and `docs/architecture/indexing-and-embeddings.md` (v0 cleaner: scanner with regex oracle).

## 2. Slice B — bounded indexing, `write_file` and `list_files` caps (`src/services/indexer.py`, `src/services/vault.py`, `src/mcp_server/tools.py` `write_file`/`list_files`)

- [ ] 2.1 In the changed-path link rebuild, insert each note's rows inside the loop (≤1000-row batches) instead of accumulating `new_rows` across notes; do the same in the one-shot backfill if it accumulates.
- [ ] 2.2 Release each buffered body once its links and tags are derived (confirm no later phase reads `bodies[path]`; if one does, derive in that phase or document why the body must persist).
- [ ] 2.3 Run `extract_links`/`extract_tags` in the indexer through `asyncio.to_thread`; run `move_note`'s rewrite computation through `asyncio.to_thread` as well.
- [ ] 2.4 Over-cap note: persist the first `MAX_LINKS_PER_NOTE` rows, emit one ERROR line (`path`, cap, count), do not append to `skips`; add the disposition to the `_format_skips`/certification docstring so a future reader knows why.
- [ ] 2.5 `write_file`: refuse decoded content over `MAX_NOTE_BYTES` when the destination ends in `.md` (case-insensitive), naming the limit; keep `MAX_FILE_WRITE_BYTES` for other extensions.
- [ ] 2.6 `MAX_LIST_PATTERN_CHARS = 1024` in `src/config.py`; `list_dir` raises a typed error for a longer pattern before `fnmatch`; `list_files_impl` reports it as an in-band refusal naming the limit.
- [ ] 2.7 Tests: memory/row-count test for N link-heavy changed notes (rows written, `new_rows` never exceeds one note's worth — instrument via a monkeypatched `insert` counter or peak-length assertion); over-cap note test (exactly the cap persisted, ERROR logged, not in `skips`, re-derive certifies); `write_file` `.md` cap and `.pdf` unaffected; `list_files` 1,024 accepted / 1,025 refused without reading the directory.
- [ ] 2.8 Docs: `docs/architecture/indexing-and-embeddings.md` — link cap as a declared degradation (mirror the keyword-retreat paragraph), per-note flush, off-loop extraction; `docs/architecture/vault-tools.md` — `write_file` cap parity and the `list_files` pattern cap.

## 3. Slice C — upload session scope and engine hardening (`src/transfer/routes.py`, `src/services/transfer.py`, `src/database.py`)

- [ ] 3.1 Restructure `upload()`: phase-1 session performs claim + `resolve_identity_ok` + `resolve_root_ok` + `_path_ok`, commits, and exits before any wait or body read; the claimed row is used detached (`expire_on_commit=False`).
- [ ] 3.2 Give each `release_claim`/`consume` call site and the publish gate its own short `async_session()`; the gate already opens one — remove the outer session dependency.
- [ ] 3.3 Before acquiring the semaphore check the deadline (overrun → existing 408/`consumed` path); wrap the acquire in `asyncio.wait_for(min(30.0, remaining))`; on timeout return 503 with `Retry-After` and release the claim to `pending`. Keep the acquire inside `stream_to_vault` if that is where it lives, but make the deadline check and the bounded wait precede it.
- [ ] 3.4 `src/database.py`: `pool_timeout=30` explicit; `server_settings["idle_in_transaction_session_timeout"] = "120000"`.
- [ ] 3.5 Tests (existing transfer harness): during a stalled stream and with a second upload queued, the engine's `pool.checkedout()` attributable to upload requests is 0 and an unrelated session acquires a connection immediately; queue-wait timeout → 503 + `Retry-After` + token `pending` + no staged bytes; deadline-overrun-before-wait → 408 + `consumed`; the full existing upload suite still passes.
- [ ] 3.6 Docs: `docs/architecture/file-transfer.md` — "no session across a wait or a stream" rule with the `download_file` precedent, the bounded queue wait, and why the timeout releases rather than consumes.

## 4. Integration

- [ ] 4.1 Merge slices; run the full suite, `make audit`, and `make db-check` (no migration, must still be clean).
- [ ] 4.2 `openspec validate --strict`; `openspec-verifier` pass; adversarial review of the merged diff (Codex only if no other Codex thread is running, otherwise a Claude adversarial verifier — say which in the report).
- [ ] 4.3 `make deploy`; post-deploy end-to-end exercise against the live server via the MCP tools: `list_files` with a 2,000-char pattern (refused), `create_note` with a 1 MiB `[[a]] ` body then `get_links` (bounded, ERROR line present, other tools responsive during the pass), `write_file` of an 11 MiB `.md` (refused), and one upload while a second is queued (health and an unrelated tool call succeed throughout). Delete the test notes afterwards.
- [ ] 4.4 Archive the change, PR with `Closes #180`, `Closes #203`, `Closes #204`, `Closes #208`; update the vault report's issue rows.
