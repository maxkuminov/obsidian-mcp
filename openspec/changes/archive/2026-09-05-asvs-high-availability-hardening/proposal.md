## Why

The 2026-09-04 OWASP ASVS 5.0 assessment (Claude workflow plus Codex cross-family pass, every finding independently verified with measurements) left four high-severity findings open, all of the same shape: **one authenticated tenant can stall or kill the single-worker process for every other tenant with ordinary, in-cap input.** Production is multi-user, so each is a cross-tenant availability failure — one of the four expensive failures named in `CLAUDE.md`.

- **#180** — the wikilink, markdown-link, and `move_note` rewrite regexes are quadratic in line length (20 KB of `[[` = 18 s), and the frozen v0 fence cleaner is quadratic in the number of unclosed fences; all run synchronously on the event loop inside the indexer.
- **#203** — link extraction has no cap: one 10 MiB note of `[[a]] ` yields 1.75 M links, 6 s of regex on the loop and an 802 MiB peak; the indexer accumulates link rows across every changed note in a pass before inserting, so N such notes cost N × 802 MiB against a 2 GB container; `write_file` accepts a 25 MiB `.md` that the indexer then reads.
- **#204** — `list_files` passes `pattern` to `fnmatch` with no length cap; the compile is linear at ≈10 µs/char and runs on the loop (500 KB → 5.4 s stall; the 61 MiB body cap admits ~10 minutes).
- **#208** — `PUT /transfer/upload` re-checks out a DB connection after `claim_upload` commits and holds it across the semaphore wait and the entire body stream; the deadline is checked only after the semaphore is acquired, so a queued request's hold is unbounded. 15 slow uploads pin the whole pool (5 + 10) and every other request gets a 500 after 30 s.

## What Changes

- **Linear link grammar.** Every unbounded character class in the four link regexes (`_WIKILINK_RE`, `_MDLINK_RE`, `_WIKILINK_REWRITE_RE`, `_MDLINK_REWRITE_RE`) is closed: wikilink target, anchor and alias classes exclude `[`/`]` (Obsidian's link syntax forbids them there), markdown link text excludes `[`, and markdown href classes are length-bounded to 2,048 characters (brackets are legal in filenames, so the href cannot exclude them). Possessive quantifiers throughout. The accepted differences are enumerated in a test; a ratio-based regression test pins linearity for every pattern and every pathological input. Because the grammar changed, `CURRENT_EXTRACTION_VERSION` is bumped so the existing versioned mechanism re-derives every note's links once, with no re-embedding.
- **Linear v0 fence cleaner.** `_v0_clean` is reimplemented as a single-pass line scanner whose output is byte-identical to the frozen regex pair; a differential property test over generated fence inputs proves it. The regexes stay in the test as the oracle.
- **Extraction off the loop.** The indexer's link/tag extraction and `move_note`'s rewrite computation run via `asyncio.to_thread`, so even linear work on a 10 MiB note is not dead air for other tenants.
- **Link cap and per-note flushing.** `MAX_LINKS_PER_NOTE` (10,000, first N in document order) bounds extraction; the indexer inserts each note's rows inside the changed-path loop instead of accumulating a pass-wide list, so peak link-row memory is one note's worth. An over-cap note is a **declared, durable degradation**: the first N links are kept, a new `notes_metadata.links_truncated` column (migration 022) is set and surfaced by `get_links` as `truncated: true`, and one ERROR line names the note and the cap; the pass stays complete. Mirrors the keyword-index retreat — not a silent drop and not a certification-withholding skip. The pass's body buffer is unchanged and recorded as a residual on #203.
- **`.md` cap parity on every transport ingress.** `write_file`, `PUT /transfer/upload` and `import_from_url` cap a `.md` destination at the smaller of `MAX_NOTE_BYTES` and `MAX_FILE_WRITE_BYTES`, naming the limit that applied, so no tool can land a markdown file the note tools would refuse.
- **`list_files` pattern cap.** `list_dir` refuses a `pattern` longer than `MAX_LIST_PATTERN_CHARS` (1,024) before touching `fnmatch`; the tool reports the limit.
- **Upload releases the pool.** The upload route commits and closes its session after claim + validation, streams with no connection held, and reopens short sessions for the release/consume handlers and the publish gate. The slot wait inside `stream_to_vault` is bounded (30 s) and sliced so the deadline stays on the wall clock; a wait that ends by deadline is the existing 408/`consumed`, a wait cut short by the cap is 503 with `Retry-After` and the claim released. The engine writes `pool_timeout` down explicitly. An idle-transaction timeout was considered and rejected — it would kill the indexer's steady-state pass.

One additive migration (022, `links_truncated`). No new dependencies.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `wikilink-graph`: link extraction is linear-time and bounded per note in document order; `get_links` reports truncation; extraction runs off the event loop.
- `index-integrity`: A.7a gains the capped-note carve-out; the link rebuild writes per note; truncation is recorded on the note; the grammar change is re-derived through the extraction version; the frozen v0 cleaner keeps byte-identical output under a linear implementation.
- `file-access`: `list_files` refuses over-long patterns before path validation; `write_file` applies the `.md` cap.
- `file-transfer`: the upload endpoint holds no database connection while waiting or streaming; the slot wait is bounded, deadline-aware, with 408-before-503 precedence; markdown transfers are capped; the pool timeout is explicit.

## Impact

- `src/services/links.py`, `src/services/embeddings.py` (v0 cleaner), `src/mcp_server/tools.py` (rewrite regexes, `write_file`, `list_files` refusal), `src/services/indexer.py` (per-note flush, cap, `to_thread`), `src/services/vault.py` (`list_dir` cap), `src/transfer/routes.py` (session scope), `src/services/transfer.py` (bounded acquire), `src/database.py` (pool settings), `src/config.py` (two constants).
- `alembic/versions/022_*` and `src/models/db.py` (`links_truncated`).
- Tests: ratio-based linearity benchmarks, v0 differential test, cap tests, a pool-occupancy integration test for the upload route.
- Docs: `docs/architecture/vault-tools.md` (grammar note, list cap), `docs/architecture/indexing-and-embeddings.md` (link cap as declared degradation, off-loop extraction), `docs/architecture/file-transfer.md` (session scope rule).
- Closes #180, #203, #204, #208.
