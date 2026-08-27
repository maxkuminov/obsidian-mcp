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

**3. Unterminated openers split by indentation.** Column-zero: the document is the CommonMark container, so mask to end of note. Indented (1–3 spaces): the opener may be a list-item child whose block ends at the item's end — unknowable flat — so it is **not a fence**, and `edit_note(section=…)` on a note containing one is **refused by name** (position included). Reads and the outline keep working under the not-a-fence interpretation, mirroring the defective-frontmatter doctrine: the guarantee is the refusal, not the round trip. (Codex finding 2.) Rejected alternatives: container-aware parsing (unbounded scope, new divergence surface); masking to EOF (lets one stray line inside a list swallow every later real section — a *new* destructive class); refusing reads too (reads are non-destructive; refusing them walls off content).

**4. Backtick info strings may not contain backticks; closer suffix is U+0020/U+0009 only** (CommonMark; NBSP does not close). Tilde info strings unrestricted.

**5. Frontmatter is opaque to fence scanning.** The recognizer skips a valid line-1 frontmatter block (shared partition helper) before scanning; defective/absent frontmatter → raw scan, as today. Without this, a fence-shaped YAML scalar (now indent-matchable) would swallow the body for the raw-text consumers (`extract_tags`, `move_note` rewriting). (Codex finding 5.)

**6. `clean_for_embedding` consumes the shared recognizer** and keeps its remove-don't-mask behavior and inline-code preservation. Its private regexes are deleted. (Codex finding 3.)

**7. Length/position invariant is stated in code points, not bytes.** Python `str` offsets are what every consumer stores and reports; "byte" phrasing was inherited prose. Non-ASCII tests pin it. (Codex finding 7.)

**8. Remediation: `extraction_version` column, not `content_hash` games.** `content_hash` is NOT NULL and is the indexer's move-detection key; nulling it fails and a sentinel breaks rename identity (cascade-deletes embeddings). New column `notes_metadata.extraction_version SMALLINT NOT NULL DEFAULT 0` (server default so the migration is metadata-only); code constant `CURRENT_EXTRACTION_VERSION = 1`. Index pass re-derives a note when its hash changed **or** its marker is stale: re-extract links/tags; recompute recognised fence spans under the new grammar and under a frozen copy of the legacy regexes (kept only for this comparison, removable next release) — if they differ, clear `embedded_content_hash` so the embed pass rebuilds that note; stamp the marker. Ollama (production provider) makes the re-embed compute-only, and the scoping keeps it to affected notes. (Codex finding 4.)

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
3. Rollback: redeploy previous image. Old code ignores the new column (additive); re-derivation under the old grammar re-runs the same way if ever needed by bumping nothing — rows already stamped are simply stale relative to a *future* version bump.
