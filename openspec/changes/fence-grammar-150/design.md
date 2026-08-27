## Context

`mask_code` (`src/services/links.py`) feeds `_scan_headings` (section addressing, outlines), `extract_links`, `extract_tags`, and `move_note` link rewriting; `clean_for_embedding` (`src/services/embeddings.py`) separately strips fences before embedding with its own LF-only, column-zero, exact-closer regexes. Neither implements CommonMark's fence rules (0–3 space indentation, closer ≥ opener, unterminated-runs-to-container-end). The gap is a reproduced silent destructive write (issue #150).

Constraints inherited from #140/#146: universal terminators (LF, CRLF as a unit, lone CR); masking is same-length substitution so positions survive; headings are column-zero only; read and write share one resolver.

## Goals / Non-Goals

**Goals:**
- One shared fence recognizer, grammar pinned in the `code-masking` spec, feeding both `mask_code` and `clean_for_embedding`.
- Close the reproduced destructive write; refuse the one shape flat scanning cannot decide.
- Re-derive stale links/tags/embeddings without corrupting note identity.

**Non-Goals:**
- Container-aware parsing (lists, blockquotes); 4-space indented code blocks; CommonMark inline-code pairing; widening heading recognition; any change to #140's body-span contract.

## Decisions

**1. Line scanner exposing fence spans; regex retired.** A single `(?s)` regex cannot express "closer at least as long as the opener" or the indented-unterminated exclusion. The recognizer scans lines (universal-terminator split), returns `(start, end, unmatched_indented_openers)` spans; `mask_code` masks the spans (same code-point length, positions preserved), `clean_for_embedding` deletes them, section writes consult the unmatched-opener list. Each CommonMark clause is auditable in isolation — a widened regex is exactly what hid this bug class.

**2. Mask span excludes the closing line's terminator** (the current masker's convention, kept deliberately): the surviving terminator is what lets an immediately following heading match `_AT_LINE_START`. Internal terminators are masked to spaces as today. (Codex spec-audit finding 1.)

**3a. The unmatched-opener refusal covers every automatic mutation, not just section writes.** `move_note(rewrite_links=True)` mutates link text in sources it selects itself; a link inside an actual list-contained unterminated fence must not be silently rewritten. The rewrite preflight (which already refuses moves whose link graph disagrees with the vault) additionally inspects every selected source — the moved note's own body included — and refuses the whole move, naming each offending source and opener position, when any reports an unmatched indented opener. `rewrite_links=False` is unaffected. (Codex r2 finding 2.) Docstrings for `edit_note` and `move_note` (server.py registrations and tools.py implementations) disclose the refusal and stop advertising unqualified read/write parity. (r2 finding 3.)

**3. Unterminated openers split by indentation.** Column-zero: the document is the CommonMark container, so mask to end of note. Indented (1–3 spaces): the opener may be a list-item child whose block ends at the item's end — unknowable flat — so it is **not a fence**, and `edit_note(section=…)` on a note containing one is **refused by name** (position included). Reads and the outline keep working under the not-a-fence interpretation, mirroring the defective-frontmatter doctrine: the guarantee is the refusal, not the round trip. (Codex finding 2.) Rejected alternatives: container-aware parsing (unbounded scope, new divergence surface); masking to EOF (lets one stray line inside a list swallow every later real section — a *new* destructive class); refusing reads too (reads are non-destructive; refusing them walls off content).

**4. Backtick info strings may not contain backticks; closer suffix is U+0020/U+0009 only** (CommonMark; NBSP does not close). Tilde info strings unrestricted.

**5. Frontmatter is opaque to fence scanning — and the partition runs at most once.** Without opacity, a fence-shaped YAML scalar (now indent-matchable) would swallow the body for the raw-text consumers (`extract_tags`, `move_note` rewriting). But several consumers hand the recognizer an already-stripped body (section resolution over the stripped body, index-time extraction, embedding cleanup); auto-detecting frontmatter there would eat a mapping-shaped *body prefix* as a phantom second block and hide an unmatched opener from the refusal. So the recognizer API takes explicit context — full-note (discover and skip the valid line-1 block) vs. body (never re-partition) — and each consumer declares which it passes. (Codex r1 finding 5, r2 finding 1.)

**6. `clean_for_embedding` consumes the shared recognizer** and keeps its remove-don't-mask behavior and inline-code preservation. Its private regexes are deleted. (Codex finding 3.)

**7. Length/position invariant is stated in code points, not bytes.** Python `str` offsets are what every consumer stores and reports; "byte" phrasing was inherited prose. Non-ASCII tests pin it. (Codex finding 7.)

**8. Remediation: `extraction_version` column, not `content_hash` games.** `content_hash` is NOT NULL and is the indexer's move-detection key; nulling it fails and a sentinel breaks rename identity (cascade-deletes embeddings). New column `notes_metadata.extraction_version SMALLINT NOT NULL DEFAULT 0` (server default so the migration is metadata-only); code constant `CURRENT_EXTRACTION_VERSION = 1`. Index pass re-derives a note when its hash changed **or** its marker is stale: re-extract links/tags; recompute the **cleaned-for-embedding output** under the current grammar and under the frozen cleaner of the version stamped on the row (a small per-version registry of cleaning functions: v0 = the legacy sequential regex cleaner copied verbatim; version N = that release's cleaner; an entry is removable once no row is stamped with it) — if the outputs differ, clear `embedded_content_hash` so the embed pass rebuilds that note; stamp the marker. Cleaned-output comparison, not span comparison: the v0 cleaner's sequential substitution makes span equality neither necessary nor sufficient (adversarial-review finding). The version-to-version comparison is what makes rollback work: after a revert-and-bump, v1-stamped rows compare the frozen v1 cleaner against the restored legacy cleaner and invalidate exactly the affected notes. Two transition-window controls close the gap between deploy and pass completion: `move_note(rewrite_links=True)` refuses (owner-scoped) while any stale marker remains — `note_links` rows from the old grammar cannot be trusted as the rewrite-source inventory — and the ID-preserving external-move branch re-derives links and tags and stamps in its own transaction rather than deferring to a second pass. `extract_tags` and the preflights take the already-partitioned body (BODY context) so the frontmatter partition truly runs at most once per note. Ollama (production provider) makes the re-embed compute-only, and the scoping keeps it to affected notes. (Codex finding 4.)

**9. No blanket refusal mode for "grammar-ambiguous" notes.** The grammar is deterministic except the unmatched-indented-opener case, which gets the targeted refusal in decision 3; a broader refusal would permanently wall off legitimate notes over a one-time transition.

## Risks / Trade-offs

- [Ordinal shift surprises an agent holding a pre-deploy outline] → outlines are per-response; shifted ordinals occur only where the old ordinal named a heading inside code, where a write was already destructive. Accepted, declared.
- [Flat extent of a *matched* indented fence diverges from container semantics (e.g. closer outside the list)] → documented divergence; the refusal covers only the unmatched case. Real-vault cost is bounded: a matched pair masks something CommonMark might not, which under-counts headings — never extends a write past a visible heading... it can hide a real heading between opener and closer; that heading was between two fence lines the author wrote as a pair, accepted.
- [Scanner diverges from spec on an untested shape] → the code-masking scenario list is the test matrix; exact masked spans pinned; adversarial Codex pass mandatory (section-addressing surface).
- [extraction_version pass is heavy] → same work as the existing first-deploy backfill, minus embedding for unaffected notes.
- [Legacy-regex comparator drifts] → it is frozen (copied verbatim from the pre-change tree), used only in the remediation branch, and deleted once the fleet is stamped.

## Migration Plan

1. `make test-schema` (change carries a migration), then `make deploy` (build → backup → migrate → recreate); `make db-check` clean after.
2. First index pass re-derives all notes (stale markers); dashboard/logs confirm completion; spot-check `note_links` and a known indented-fence note's outline via live MCP tools.
3. **Rollback is roll-forward, and the plan says so.** A bare redeploy of the previous image does NOT restore derived state: old code ignores `extraction_version` and skips unchanged notes by `content_hash`, so links/tags/embeddings stay derived under the new grammar indefinitely. The rollback procedure is: revert the grammar commits on a branch, bump `CURRENT_EXTRACTION_VERSION` to 2 in that build (the versioned re-derivation mechanism and the frozen per-version recognizer registry are kept — the registry is what lets the pass compare each row's stamped grammar against the restored one), and deploy — the same owner-scoped re-derivation pass then rebuilds every note's links, tags, and (span-diff-scoped, direction-aware) embeddings under the legacy grammar without touching `content_hash`. This is recorded in `docs/architecture/indexing-and-embeddings.md` as the rollback recipe. (Codex r2 finding 5.)
