## 1. Setting and fingerprint

- [ ] 1.1 `src/config.py`: `chunk_min_chars: int = Field(CHUNK_MIN_CHARS_DEFAULT, ge=0, le=1000)` (the default constant is 200 and is shared with `index_state`). Add a model validator that refuses `chunk_min_chars * 2 > chunk_size * 4` and names both values. Comment the setting with the reset pointer.
- [ ] 1.2 `src/services/index_state.py`: `embedding_fingerprint()` adds `"chunk_min_chars"` only when the value is not `CHUNK_MIN_CHARS_DEFAULT`. `v` stays 1.
- [ ] 1.3 Tests:
  - At the default, the fingerprint string is byte-identical to the pre-change literal.
  - At 300, startup refuses and names `chunk_min_chars` and the reset.
  - Going from 300 back to 200 over a 300-built index refuses.
  - Validator bounds: -1, 1001, and 1025 with `CHUNK_SIZE=512` are refused; 0, 200 and 1000 are accepted.
  - The retrieval fingerprint carries the field only when it is not the default.

## 2. Chunker (`src/services/embeddings.py`)

- [ ] 2.1 `chunk_text_bounded(…, min_chars=0)`: add D1's lookback (`CHUNK_BOUNDARY_LOOKBACK_CHARS = 200`, `L = min(200, W // 10)`, line break first, then whitespace, then a hard cut), the forward start snap when overlap is above 0, and D3's tail rule. Keep the termination guard, the cap (one past the cap), and the unstripped single-window branch.
- [ ] 2.2 `chunk_note_bounded`:
  - D3 grouping over `_note_sections`, which stays unchanged. A group's text is the fence-cleaned contiguous raw span.
  - The backward tail at the end of the note.
  - One chunk for a note shorter than the minimum.
  - The headings-only fallback as one group.
  - One cap counter across groups.
- [ ] 2.3 D4 locator: keep a cleaned-to-raw offset map per group, give each chunk a raw span from its stripped text, and set `section_ordinal` to the deepest N with `raw span ⊆ _section_body_span(N)` (imported, not copied), or 0 when there is none. Set `heading_path` to N's path and build the prefix from it.
- [ ] 2.4 `CHUNKING_VERSION = 2`. Add `INPUT_COMPATIBLE_CHUNKING_VERSIONS = frozenset({1, 2})` and update the docstring with the set's rule (D6).
- [ ] 2.5 The reuse lookup accepts a note whose stamp is in the set (NULL never is). The fingerprint, non-NULL prefix, exact pair and under-lock recheck rules are unchanged. Check every other `== CHUNKING_VERSION` site, for example the full-hash backstop's `= :cv`, and leave it strict. Each such site must be justified in a code comment.
- [ ] 2.6 Unit tests (`tests/test_chunk_min_length.py`):
  - **Live fixture 1.** The note `## Email Triage\nSee entries in the Email Triage log\n### 2026-10-01\n<300 chars>` produces no chunk under 200 characters. The first chunk contains "in the Email Triage log" and the `### 2026-10-01` line, and its ordinal is `## Email Triage`'s.
  - **Live fixture 2.** A section of 2,048·k + 4 characters ending in "…themselves" leaves no chunk equal to "lves", and no chunk starts or ends inside a word when whitespace exists within the lookback.
  - Sibling merge: the locator is the parent, or 0.
  - Backward tail.
  - A whole note under the minimum yields one chunk.
  - Headings-only fallback.
  - `CHUNK_MIN_CHARS=0`: chunk texts are #355's except at window boundaries (the lookback still applies).
  - The cap counts across groups, and truncation is marked one past the cap.
  - A no-whitespace run is cut hard.
  - Overlap above 0 terminates and the start snaps.
  - LF, CRLF and lone CR.
  - Fences spanning a merge boundary.
- [ ] 2.7 Property test over generated notes (random heading depths, short and long sections, fences, frontmatter, the three terminators). For every chunk, `read_note(section="#N")` resolves and the chunk is a substring of that body with the whole-body fence spans removed. Also check:
  - No chunk is under the minimum except the single chunk of a whole-short note, or a whitespace-dominated window (L3).
  - Zero chunks exactly when `chunk_text_bounded(clean_for_embedding(body))` yields zero.
  - The concatenation of chunk texts, with whitespace normalised, covers the cleaned body minus only the heading lines of group-first sections.
- [ ] 2.8 Golden formatter test: fixed inputs to `embedding_input` and `build_embed_prefix` must match committed outputs. On a mismatch it fails with "formatter changed: reset INPUT_COMPATIBLE_CHUNKING_VERSIONS".
- [ ] 2.9 Update version literals in existing tests (`test_heading_chunks.py`, `test_heading_chunks_reuse.py`, `test_heading_chunks_pg.py`, `test_search_eval_pg.py`). Add reuse cases:
  - A v1-stamped note with an identical pair reuses.
  - A NULL-stamped note does not.
  - A stamp outside the set (3, and 0 via a patched set) does not.
  - Under the lock, a concurrent rewrite still fails the recheck.

## 3. Harness (`scripts/search_eval/`)

- [ ] 3.1 `db.chunk_lengths(session, owner)`: one read-only `SELECT` under `legacy_chunked_notes`' owner predicate returning `{chunks, under_min, p10, p50, p90}` (`length(btrim(chunk_text))`, `percentile_disc`). It returns null on an empty set or a pre-034 copy.
- [ ] 3.2 `runner.py` / `scoring.py`: record it beside `legacy_chunked_notes` for DB-backed semantic arms. A `runfile:` arm carries its staged value or null, and fixture arms gain nothing.
- [ ] 3.3 Tests: a two-tenant scoping case (the other tenant's rows change no byte), the fixture's golden projection unchanged, and null from an older runfile.

## 4. Docs (same PR)

- [ ] 4.1 README: a `CHUNK_MIN_CHARS` row, the reset list (beside `CHUNK_SIZE`, "non-default only"), and one line in the result-format section: previews start on whitespace, and a merged chunk can contain an inner heading.
- [ ] 4.2 `.env.example`: `CHUNK_MIN_CHARS=200` with the reset warning and the half-window bound.
- [ ] 4.3 DEPLOYMENT.md:
  - An upgrade note, "Chunking version 2 (#390)": no migration, a **second gradual re-embed of the whole vault** right after #355's, the marker and `/health` state reappear and drain, the D6 saving, and the rollback cost (L8).
  - The recipe's predicate literal becomes `2`.
  - The baseline-drain step and the before-distribution step (D8).
  - The under-min SQL.
- [ ] 4.4 `docs/architecture/indexing-and-embeddings.md`, "Heading-aware chunks (#355)": a subsection for the minimum, grouping, the locator rule, the lookback, the compatible set and the conditional fingerprint key. Replace the "byte-identical to `chunk_text_bounded(clean_for_embedding(body))`" sentence.
- [ ] 4.5 `docs/architecture/search.md`: a chunk-count and length note, and the windowing truth.
- [ ] 4.6 CLAUDE.md: one key-decision bullet covering the minimum and its forward merge, the locator as the deepest containing section, version 2 with `{1,2}` reuse, and the key that is non-default only.
- [ ] 4.7 Grep check: every new setting is in README and `.env.example`, and no doc still says `IS DISTINCT FROM 1`.

## 5. Gates

- [ ] 5.1 `pytest tests`, `make test-integration`, `make audit`, `make search-eval` (the fixture must be unchanged), and `openspec validate chunk-min-length --strict`. Skip `make test-schema` (no migration), but confirm with `alembic check` that it is still clean.
- [ ] 5.2 Real-data eval per D8 on a restored `BACKUP_FULL=1` copy:
  - Drain the baseline at v1.
  - Measure the before distribution.
  - Re-embed to v2 with budgets 0, and record the row count, provider calls and durations.
  - Run the candidate with `legacy_chunked_notes: 0`.
  - Attach `report.json` only if `attachable: true`. `CORPUS_LOSS` invalidates the run.
  - Release bar: owner decision 9.
- [ ] 5.3 `openspec-verifier` subagent (not the author). Iterate until there are zero blocking gaps.
- [ ] 5.4 Adversarial Codex (mandatory: chunking and search path), `gpt-daybreak-blue-latest`, read-only, on the committed tree. Framing: silently wrong search results and a broken live path are the expensive failures. Targets:
  - Containment and parity under merges.
  - The compatible-set reuse soundness.
  - The conditional fingerprint key at the default.
  - Termination of the lookback with overlap.
  - The cap.
  - The zero-chunk invariant.

  Two rounds by default. Triage per the workflow, and record declined findings as limitations.

## 6. Archive, merge, live check

- [ ] 6.1 `openspec archive chunk-min-length -y` and the doc commits as the last commits of the branch. Open one PR with `Closes #390`.
- [ ] 6.2 Merge, then watch the Flux rollout. The pod must start, not refuse.
- [ ] 6.3 Live check, in place of a browser pass (agent-facing):
  - `semantic_search` and `hybrid_search` show the `index_transition:` line right after deploy, and `/health` reads `indexer.rechunk: "in_progress"`.
  - Re-run the #390 paraphrase query once the transition drains. The line is gone, `rechunk` reads `complete`, the "in the Email Triage log" chunk no longer stands alone, and no preview starts mid-word.
  - Spot-check that `read_note(section="#N")` on a merged row's locator contains its preview.
  - Report which tools were called.
