## Why

The 2026-09-04 OWASP ASVS 5.0 assessment (Claude workflow plus Codex cross-family pass, every finding independently verified with measurements) left four high-severity findings open, all of the same shape: **one authenticated tenant can stall or kill the single-worker process for every other tenant with ordinary, in-cap input.** Production is multi-user, so each is a cross-tenant availability failure — one of the four expensive failures named in `CLAUDE.md`.

- **#180** — the wikilink, markdown-link, and `move_note` rewrite regexes are quadratic in line length (20 KB of `[[` = 18 s), and the frozen v0 fence cleaner is quadratic in the number of unclosed fences; all run synchronously on the event loop inside the indexer.
- **#203** — link extraction has no cap: one 10 MiB note of `[[a]] ` yields 1.75 M links, 6 s of regex on the loop and an 802 MiB peak; the indexer accumulates link rows across every changed note in a pass before inserting, so N such notes cost N × 802 MiB against a 2 GB container; `write_file` accepts a 25 MiB `.md` that the indexer then reads.
- **#204** — `list_files` passes `pattern` to `fnmatch` with no length cap; the compile is linear at ≈10 µs/char and runs on the loop (500 KB → 5.4 s stall; the 61 MiB body cap admits ~10 minutes).
- **#208** — `PUT /transfer/upload` re-checks out a DB connection after `claim_upload` commits and holds it across the semaphore wait and the entire body stream; the deadline is checked only after the semaphore is acquired, so a queued request's hold is unbounded. 15 slow uploads pin the whole pool (5 + 10) and every other request gets a 500 after 30 s.

## What Changes

- **Linear link grammar.** `[` and `]` are excluded from the wikilink target class and the markdown-link text class in all four link regexes (`_WIKILINK_RE`, `_MDLINK_RE`, `_WIKILINK_REWRITE_RE`, `_MDLINK_REWRITE_RE`), with possessive quantifiers, so a run of `[[` fails at each position in O(1). Obsidian forbids `[`/`]` in note names and link targets, so no valid link changes meaning; the one accepted difference (a stray `[` inside markdown link text) is recorded in a test. A time-bound regression test pins linearity for every pattern.
- **Linear v0 fence cleaner.** `_v0_clean` is reimplemented as a single-pass line scanner whose output is byte-identical to the frozen regex pair; a differential property test over generated fence inputs proves it. The regexes stay in the test as the oracle.
- **Extraction off the loop.** The indexer's link/tag extraction and `move_note`'s rewrite computation run via `asyncio.to_thread`, so even linear work on a 10 MiB note is not dead air for other tenants.
- **Link caps and per-note flushing.** `MAX_LINKS_PER_NOTE` (10,000) bounds extraction; the indexer inserts each note's rows inside the changed-path loop instead of accumulating a pass-wide list, and releases each buffered body once its derived rows are written. An over-cap note is a **declared degradation** (first N links kept, one ERROR line naming the note and the cap, pass stays complete), mirroring the keyword-index retreat — not a silent drop and not a certification-withholding skip.
- **`write_file` cap parity.** `write_file` refuses content over `MAX_NOTE_BYTES` for a `.md` destination, naming the limit, so no tool can land a markdown file the note tools would refuse.
- **`list_files` pattern cap.** `list_dir` refuses a `pattern` longer than `MAX_LIST_PATTERN_CHARS` (1,024) before touching `fnmatch`; the tool reports the limit.
- **Upload releases the pool.** The upload route commits and closes its session after claim + validation, streams with no connection held, and reopens short sessions for the release/consume handlers and the publish gate. The semaphore wait is bounded (`min(30 s, deadline remaining)` → 503 with the claim released), and the deadline is checked before acquiring. The engine sets `pool_timeout` explicitly and an `idle_in_transaction_session_timeout` so a future regression of this shape fails loudly instead of silently pinning.

No schema changes. No new dependencies.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `wikilink-graph`: link extraction is bounded (linear-time grammar, per-note cap as a declared degradation) and runs off the event loop.
- `index-integrity`: the link rebuild writes and releases per note; an over-cap note is a declared degradation, not a skip; the frozen v0 cleaner keeps byte-identical output under a linear implementation.
- `file-access`: `list_files` refuses over-long patterns; `write_file` applies `MAX_NOTE_BYTES` to `.md` destinations.
- `file-transfer`: the upload endpoint holds no database connection while waiting for a slot or streaming the body; the queue wait is bounded and deadline-aware.

## Impact

- `src/services/links.py`, `src/services/embeddings.py` (v0 cleaner), `src/mcp_server/tools.py` (rewrite regexes, `write_file`, `list_files` refusal), `src/services/indexer.py` (per-note flush, cap, `to_thread`), `src/services/vault.py` (`list_dir` cap), `src/transfer/routes.py` (session scope), `src/services/transfer.py` (bounded acquire), `src/database.py` (pool settings), `src/config.py` (two constants).
- Tests: new linearity benchmarks, v0 differential test, cap tests, a pool-occupancy test for the upload route.
- Docs: `docs/architecture/vault-tools.md` (grammar note, list cap), `docs/architecture/indexing-and-embeddings.md` (link cap as declared degradation, off-loop extraction), `docs/architecture/file-transfer.md` (session scope rule).
- Closes #180, #203, #204, #208.
