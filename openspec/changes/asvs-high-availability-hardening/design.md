## Context

Single uvicorn worker, one asyncio loop shared by every tenant's MCP tool calls, OAuth, the panel, and the indexer task (`src/main.py`). A CPU-bound or pool-holding operation triggered by one tenant is therefore felt by all. The four findings are the four ways a tenant can do that today with legal input; each was measured on the production host (see the assessment report in the vault and issues #180, #203, #204, #208). Constraints that shape the fix:

- The frozen v0 cleaner (`_v0_clean`) must produce **byte-identical** output forever — `extraction_version` comparisons and the documented rollback recipe depend on it (`index-integrity`, "A masker grammar change forces re-derivation…").
- The link rebuild certifies "every link row was written by this pass" and any *skip* withholds a re-derive's certification (`index-integrity`, A.7a). A cap must not become a permanent self-inflicted uncertified state for a tenant with one large MOC note.
- `_vault_root` and the `_tracked` admission gate must stay cheap; nothing here may add a DB query to them.
- The upload route's claim/validate/publish sequence is a linearizability argument (`file-transfer`); reordering must keep "claim first, before any body byte" and the locked publish gate intact.

## Goals / Non-Goals

**Goals:**
- No input under the documented caps can hold the event loop for more than a small constant per note, or hold a pool connection while waiting on I/O it does not need.
- Every bound is a *declared* one: refused with a message naming the limit, or degraded with an ERROR log line naming the note, never silent.
- Byte-identical v0 cleaner output, proven by test, not by inspection.

**Non-Goals:**
- Per-tenant fairness or round-robin in the indexer (#202) and a general `/mcp` rate limiter (#188/#194) — separate changes.
- Indexer-side handling of `.md` files over `MAX_NOTE_BYTES` placed by Obsidian or sync (not tenant-reachable via a tool once `write_file` is capped; memory is bounded by the link cap regardless of note size).
- Encryption or authentication of internal hops.

## Decisions

**D1 — Make the link grammar linear by excluding `[` and `]` from the target/text classes, plus possessive quantifiers.**
The quadratic case is a run of `[[`: at each start the target class `[^\]\|#\n]+` consumes every following `[` then fails to find `]]`, so the scan is O(n) per position. Obsidian forbids `[` and `]` in file names and link targets, so `[^\[\]\|#\n]++` matches exactly the same valid links and fails immediately on the pathological input. The markdown-link text class `[^\]\n]+` already excluded `]`, so nested balanced brackets were never supported; excluding `[` only changes `[a[b](x.md)`, which is malformed. *Alternative rejected:* a hand-written scanner — more code, same result, and the regexes are shared with the rewrite path where a grammar drift would be a destructive-write bug. *Alternative rejected:* `asyncio.to_thread` alone — moves the burn off the loop but one tenant still consumes the worker's CPU for minutes (Codex challenge on #180).

**D2 — Reimplement `_v0_clean` as a line scanner and prove equivalence differentially.**
The regex `^```[^\n]*\n.*?\n```\s*$` (MULTILINE|DOTALL) is quadratic in the number of unclosed fence openers because each `.*?` walks to end-of-input. The scanner reproduces the regex's exact semantics — leftmost opener, nearest closer line that is exactly the fence followed by `\s*` to a line end (which may swallow following blank lines), backtick pass then tilde pass, sequential — and a hypothesis-style generator (random mixes of openers, closers, trailing whitespace, blank lines, indented fences, CRLF) asserts `scanner(x) == regex(x)` over thousands of inputs. The regex constants are retained **only** in the test module as the oracle. *Alternative rejected:* leaving v0 as-is because "no v0-stamped rows remain" — the code path is reachable and the rollback recipe re-enables it.

**D3 — Run extraction and rewrite computation via `asyncio.to_thread`.**
`extract_links`, `extract_tags` and the rewrite computation are pure functions of a string. In the indexer's changed-path loop and backfill, and in `move_note`'s rewrite planning, call them through `to_thread` so the loop stays responsive while a 10 MiB note is parsed. Thread-pool exhaustion is not a concern at one indexer task plus rare rewrites. *Alternative rejected:* a process pool — overkill for sub-second work once D1 holds.

**D4 — `MAX_LINKS_PER_NOTE = 10_000`, per-note flush, over-cap is a declared degradation.**
Extraction stops at the cap and reports truncation. The indexer inserts each note's rows immediately (still in ≤1000-row batches) inside the loop and drops the buffered body once its rows and tags are derived, so peak memory is one note's worth, not the pass's. An over-cap note gets one ERROR line naming the path, the cap and the count, and the pass stays complete. *Why not a skip:* a skip withholds re-derive certification (A.7a), which for a tenant with one generated MOC would leave provenance unrepaired indefinitely — a self-DoS on the index-integrity machinery, worse than a bounded, logged truncation. This is the same disposition `index-integrity` already takes for the keyword-index retreat ("declared degradation"). ERROR rather than WARNING so it reaches the ops-health ring buffer, which captures ERROR and above.

**D5 — `write_file` applies `MAX_NOTE_BYTES` to `.md` destinations.**
`vault-write` states every note-writing path has a tool-level cap; `write_file` was the exception because it is byte transport. The indexer treats any `.md` as a note, so the cap follows the extension (case-insensitive), not the tool. Non-markdown files keep `MAX_FILE_WRITE_BYTES`.

**D6 — `MAX_LIST_PATTERN_CHARS = 1024`, enforced in `list_dir`.**
`fnmatch.translate` + `re.compile` is linear at ≈10 µs/char (Python 3.12 emits atomic groups, so there is no backtracking to cap); 1,024 chars compiles in ~5 ms. The check lives in `list_dir` because it is the shared service entry, and `list_files_impl` reports the limit as an in-band refusal. A wildcard-count cap is redundant; `to_thread` for the walk is defense-in-depth only and is not part of this change.

**D7 — Upload: session per phase, bounded and deadline-aware queue wait.**
Phase 1 (claim + identity/root/path validation) commits and exits the session before any wait. `expire_on_commit=False` keeps the claimed row's attributes readable on the detached instance, so no re-read is needed. The stream runs with no session. The release/consume handlers and the publish gate each open their own short session — the gate already did. Before acquiring the semaphore the route checks the deadline (already overrun → same 408/`consumed` semantics as a body overrun); the acquire is wrapped in `asyncio.wait_for(min(30 s, deadline remaining))`, and a timeout returns 503 with `Retry-After` and the claim **released to `pending`** (nothing was read, so a retry is safe and the token is not burned by our queue). `import_from_url` already holds no session across its fetch and is unchanged. *Alternative rejected:* a reserved second pool for non-transfer traffic — unnecessary once the session is released, and a second pool to keep in sync.

**D8 — Engine hardening.** `pool_timeout=30` becomes explicit, and `server_settings` gains `idle_in_transaction_session_timeout` (120 s). This does not fix #208 — it makes any future "hold a connection idle across I/O" regression fail with a visible error instead of silently pinning. 120 s is far above any legitimate idle gap (the publish gate holds locks for milliseconds; `rebuild_tsvectors` is executing, not idle, between statements).

## Risks / Trade-offs

- [D1 grammar change silently alters an existing valid link] → Obsidian's own character rules make `[`/`]` impossible in targets; a regression test enumerates the accepted difference and runs the full existing link/rewrite suites unchanged.
- [D2 scanner differs from the regex on an input the generator never produced] → generator covers openers without closers, closers without openers, nested, adjacent mixed backtick/tilde, trailing whitespace on closers, blank-line runs after closers, CRLF, and indented fences; plus every fixture the existing v0/v1 tests already use.
- [D4 truncation makes `get_backlinks` incomplete for a giant MOC] → declared in the ERROR line and the docs; 10,000 outgoing links per note is far beyond any real note in either production vault (largest measured: low thousands).
- [D5 breaks a client that uploads a large `.md` via `write_file`] → it would already be unindexable through every other tool; the refusal names the limit.
- [D7 detaches the claimed row; a later handler needs a fresh attribute] → handlers re-select by id inside their own short session where they need current state (`release_claim`, `consume` already take the row and re-issue conditional UPDATEs).
- [D7 503 on queue timeout releases a claim that a concurrent `check_upload` observes as `pending` again] → that is the documented retryable state; nothing was streamed.
- [D8 `idle_in_transaction_session_timeout` terminates a legitimately slow transaction] → 120 s idle (not execution) is generous; `make reindex`/`rebuild_tsvectors` issue statements continuously. Set per-connection via `server_settings`, so it applies only to this app's role.

## Migration Plan

No migration. Deploy with `make deploy`; the deploy gate runs the full test suite in CI first. Rollback is a plain revert — no persisted format changes. After deploy: exercise `list_files` with an over-long pattern (expect refusal), `create_note` with a 1 MiB `[[a]] ` body (expect ERROR line, bounded rows, other tools responsive), and one upload while a second is queued (expect pool occupancy unchanged).

## Open Questions

- None blocking. `MAX_LINKS_PER_NOTE` and `MAX_LIST_PATTERN_CHARS` are constants, not settings; promote to `Settings` only if an operator asks.
