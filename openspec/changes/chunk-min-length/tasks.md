## 1. Constants (no setting)

- [x] 1.1 In `src/services/embeddings.py`, add the constants `CHUNK_MIN_CHARS = 200` and `CHUNK_BOUNDARY_LOOKBACK_CHARS = 200`, plus the helpers `effective_min_chars(W) = min(200, W // 2)` and `lookback(W) = min(200, W // 10)`. Docstring: changing either constant is a `CHUNKING_VERSION` bump. Do not touch `src/config.py` or `embedding_fingerprint()`.
- [x] 1.2 Tests:
  - At `CHUNK_SIZE` 1, 64 and 512, settings load and `embedding_fingerprint()` is byte-identical to the pre-change literal, so no startup refuses.
  - The effective minimum is 2 at `CHUNK_SIZE=1`, 128 at 64, and 200 at 512.

## 2. Chunker (`src/services/embeddings.py`)

- [x] 2.1 `chunk_text_bounded(…, min_chars=0)`:
  - Cut at the last line start, then the last word start, within the lookback; otherwise cut hard (D1).
  - Store the raw window, unstripped. Emit a window only if its stripped text is non-empty.
  - With overlap, snap forward only within `[s, cut]`.
  - Add `clen` (D1's one length, ASCII whitespace) beside its SQL twin.
  - Accept a cut only if it is admissible (`clen` of the preceding window ≥ m). Fall back to step 4 for windows that are mostly whitespace.
  - Apply D3's tail balancing: split at `S ≥ 2m + 1` (the admissible word start nearest the midpoint, otherwise the midpoint hard cut), or merge into one window at `S ≤ 2m`.
  - Keep the termination guard and the one-past-the-cap rule.
- [x] 2.2 `chunk_note_bounded`, using D3 grouping over the unchanged `_note_sections`:
  - Extend a group forward over short sections.
  - At the end, join a short group backward.
  - A sole short group stands alone when the whole note is short; otherwise the note becomes one whole-body group at ordinal 0.
  - Keep the headings-only fallback as one group.
  - Keep one cap counter across groups.
- [x] 2.3 D4 locator: build a cleaned-to-raw offset map per group, compute each chunk's raw span, and take the deepest N with `raw span ⊆ _section_body_span(N)` (imported), else 0. Take the heading path and the prefix from N.
- [x] 2.4 Set `CHUNKING_VERSION = 2` and add `EMBEDDED_TEXT_FORMATTERS = {1: identity, 2: collapse_long_runs}`, with the docstring rule (D6, D9). `embedding_input(prefix, chunk)` returns `prefix + "\n\n" + EMBEDDED_TEXT_FORMATTERS[CHUNKING_VERSION](chunk)`, and `embedded_input_digest` and `note_provider_inputs` use it unchanged.
- [x] 2.5 Make the reuse lookup accept a stamp that is a key of the table (NULL never is), with equal prefixes and `fmt[stamp](stored) == fmt[2](new)`. Every other `== CHUNKING_VERSION` site, such as the backstop's `= :cv`, stays strict, with a justifying comment.
- [x] 2.6 Unit tests in `tests/test_chunk_min_length.py`:
  - **Live fixture 1.** `## Email Triage\nSee entries in the Email Triage log\n### 2026-10-01\n<300 chars>`: no chunk is under the minimum. The first chunk contains the short line and the `###` line, at the ordinal of `## Email Triage`.
  - **Live fixture 2.** A section of 2,048·2 + 4 characters ending `…themselves`: no chunk is `lves`, and no cut falls inside a word that has whitespace in its lookback.
  - **Codex B2.** At W=400 with overlap 1, `A×400 + B×20 + " " + C×500`: every `B` is in a chunk.
  - **Codex M1.** Sixty empty-bodied headings, then a heading whose body is `x`: one whole-body chunk at ordinal 0, containing `x`.
  - **Codex M2.** A 300-character prefix and a group of `W + m − 1` characters of prose: every provider input is at most `302 + W`, the last two chunks both have `clen ≥ m`, and coverage is complete.
  - **Codex r2, W=400.** `A×161 + " " + B×239` yields exactly (raw 200, clen 200) and (raw 201, clen 201).
  - **Codex r2, default W.** `A×1800 + space×390 + B×10` yields exactly (raw 905, clen 905) and (raw 1,295, clen 906).
  - **Codex r3, high overlap.** At `CHUNK_SIZE=100`, overlap 99, `A×599` yields exactly `[0, 299)` and `[199, 599)`. Both have raw length and `clen` of at most 400, every provider input is at most `W + 302`, and coverage is complete.
  - **Whitespace that defeats the raw bound.** `A×100 + space×3000 + B×100` yields one chunk with `clen` 201, raw length over `W`, and the excess made of whitespace only.
  - Sibling merge, which takes the parent or 0.
  - A backward tail.
  - A whole-short note, which yields one chunk.
  - The headings-only fallback.
  - The cap across groups.
  - A no-whitespace run, which is cut hard.
  - Termination with overlap.
  - LF, CRLF and CR line endings.
  - A fence across a merge boundary.
  - `CHUNK_SIZE=1` and `64`.
- [x] 2.7 Property test over generated notes (random heading depths, short and long sections, empty sections, fences, frontmatter, three terminators, overlap 0 and above 0, small and default `CHUNK_SIZE`):
  - For every chunk, `read_note(section="#N")` resolves and the chunk is a substring of that body after the whole-body fence spans are removed.
  - Every non-heading cleaned character is in some chunk.
  - Every window except a whole-short note's only chunk has `clen ≥ m` and `clen ≤ W`.
  - The raw length is at most `W` whenever the generated text has no whitespace run longer than `W − 2m`. Otherwise any excess is whitespace only.
  - D8's `under_min` (`clen < m` in a note with more than one chunk) is 0.
  - A note has zero chunks exactly when `chunk_text_bounded(clean_for_embedding(body))` has zero.
- [x] 2.8 Golden formatter test: every `EMBEDDED_TEXT_FORMATTERS` entry, `embedding_input` and `build_embed_prefix` on fixed inputs match committed outputs. On a mismatch it fails with "frozen formatter changed: add an entry or clear the table".
- [x] 2.8a D9 tests:
  - For `A×100 + space×3000 + B×100` under prefix `P`, the provider input is `P + "\n\n" + A×100 + " " + B×100` (length `len(P) + 203`), and the stored `chunk_text` keeps the run.
  - Runs of exactly 8, and `\n\n`, are kept byte-for-byte.
  - A run of 9 tabs or CRs collapses to one space.
  - NBSP runs are not collapsed.
  - For every chunk of at most `W` raw in the 2.7 property test, the input is at most `W + 302`.
- [x] 2.9 Update the version literals in `test_heading_chunks.py`, `test_heading_chunks_reuse.py`, `test_heading_chunks_pg.py` and `test_search_eval_pg.py`. Reuse cases:
  - A v1 stamp with an identical pair and no run longer than 8 reuses; the same pair holding a 12-space run does not.
  - A NULL stamp does not reuse.
  - A stamp outside the set does not reuse.
  - The under-lock recheck still fails on a concurrent rewrite.
  - Certification stamps 2.

## 3. Observation

- [x] 3.1 `src/services/indexer.py`: sum `result.chunks_submitted` beside `total_chunks`, and extend the completion line to `…: N notes, X chunks (S submitted, R reused)`, counts only. Test with a mocked pass: one note fully reused and one fresh, and assert the line's numbers.
- [x] 3.2 `scripts/search_eval/db.chunk_lengths(session, owner, m, W)`: one read-only `SELECT` under `legacy_chunked_notes`' owner predicate. It returns `{chunks, p10, p50, p90, whole_short, under_min, raw_over_window}` per D8, or null for an empty set or a pre-034 copy. The SQL `clen` expression is a module-level constant paired with the chunker's Python `clen`.
- [x] 3.3 `runner.py` / `scoring.py`: record it for DB-backed semantic arms. A `runfile:` arm carries its staged value or null. Fixture arms gain nothing.
- [x] 3.4 Tests:
  - Two tenants: the other tenant's rows change no byte.
  - The fixture's golden projection is unchanged.
  - An older runfile yields null.
  - SQL `clen` equals Python `clen` on strings mixing tabs, CRs, NBSP (not whitespace in either) and runs.
  - On a seeded copy holding a whole-short note, an over-window whitespace chunk and normal chunks, `under_min` is 0, `whole_short` is 1 and `raw_over_window` is 1.

## 4. Docs (same PR)

- [x] 4.1 README result format: previews start on a word, and a merged chunk can contain an inner heading line. One sentence noting that the minimum is a constant tied to the chunking version (not a setting).
- [x] 4.2 DEPLOYMENT.md:
  - Upgrade note "Chunking version 2 (#390)": no migration or setting, a **second gradual re-embed of the whole vault** right after #355's, the marker and `/health` reappearing and draining, the `(S submitted, R reused)` line, and the rollback cost (L8).
  - Recipe: change the predicate literal to `2`, and add the baseline drain, the before distribution, the S/R sum, the `under_min` SQL and D8's CI95 release rule with its exact report fields.
- [x] 4.3 `docs/architecture/indexing-and-embeddings.md`, "Heading-aware chunks (#355)": add a subsection covering the minimum and the effective minimum, grouping, the tail rule, the window bound, the locator rule, lookback, coverage, raw windows, the compatible set and the log line. Replace the "byte-identical to `chunk_text_bounded(clean_for_embedding(body))`" sentence.
- [x] 4.4 `docs/architecture/search.md`: the windowing truth and the chunk-length note.
- [x] 4.5 CLAUDE.md: one key-decision bullet.
- [x] 4.6 Grep check: no doc still says `IS DISTINCT FROM 1`, and no doc names a `CHUNK_MIN_CHARS` setting.

## 5. Gates

- [x] 5.1 Run `pytest tests`, `make test-integration`, `make audit`, `make search-eval` (the fixture must be unchanged), `openspec validate chunk-min-length --strict`, and `alembic check` (must be clean; no migration).
- [ ] 5.2 Real-data eval per D8:
  - Drain the baseline at v1.
  - Record the before distribution.
  - Re-embed to v2 with budgets 0, and record rows, S/R and durations.
  - Run the candidate. It needs `legacy_chunked_notes: 0` and `under_min: 0`.
  - Apply the CI95 release rule over `deltas[<label>][overall|class:prose][recall@10|ndcg@10].ci95`. `INSUFFICIENT_DATA`, a null CI or `CORPUS_LOSS` means re-measure.
  - Attach `report.json` only if `attachable: true`.
- [ ] 5.3 Run the `openspec-verifier` subagent, not as the author, until there are zero blocking gaps.
- [ ] 5.4 Adversarial Codex review: mandatory, `gpt-daybreak-blue-latest`, read-only, on the committed tree.
  - Framing: silently wrong search results and a broken live path.
  - Targets: containment and parity under merges, coverage with overlap, the window bound, compatible-set reuse, the zero-chunk invariant, the cap, and the `under_min` predicate.
  - Two rounds by default, with triage per the workflow.

## 6. Archive, merge, live check

- [ ] 6.1 Run `openspec archive chunk-min-length -y`. The doc commits are the last commits on the branch. One PR, `Closes #390`.
- [ ] 6.2 Merge, then watch the Flux rollout. The pod must start.
- [ ] 6.3 Live check, in place of a browser pass:
  - Right after deploy, `semantic_search` and `hybrid_search` show `index_transition:`, and `/health` reads `rechunk: "in_progress"`.
  - The pod log shows `(S submitted, R reused)` lines.
  - After the drain, the line is gone, `rechunk` reads `complete`, the #390 paraphrase query no longer returns the standalone "in the Email Triage log" chunk, and no preview starts mid-word.
  - Spot-check `read_note(section="#N")` on a merged row's locator.
  - Report which tools were called.
