## Why

GitHub #311 (triaged LOW, pre-existing). When a scope is in re-derive
(provenance unresolved: no record, a handle mismatch, or exactly one of
assignment and real path changed) and a discovered file **that still has a
row** cannot be read (EACCES, EIO), the pass correctly withholds the
provenance stamp: that row may be the previous root's row, and certifying it
is the false provenance the record exists to prevent (index-integrity, "A
re-derive that skipped any file is incomplete…").

But a re-derive has no memory of its own progress. Every re-deriving pass
disables content-hash change detection and the stat shortcut and forces
**every** discovered file into `to_upsert`, so every tick until that one file
is readable again re-upserts, re-vectorises (keyword vector) and re-links
every unchanged note in the scope, and throws away the knowledge that it did
exactly that on the previous tick. That is #308's write amplification — the
pattern that cost ~650 GB/day there — at a much rarer trigger. #308's
accounting makes it visible (`rederive_incomplete`, `/health` `degraded`
after `INDEXER_DEGRADED_AFTER_FAILURES` passes), but does not bound it.

The degraded report is right — a possibly foreign row is still being served —
the full-scope rewrite every tick is not.

## What Changes

- **Per-row re-derive progress, persisted and bound to what it certifies.**
  Migration **030** adds `notes_metadata.derived_under varchar(64) NULL`. A
  re-deriving pass that fully derives a row (upsert or id-preserving move,
  keyword vector and link extraction all written) sets it, in the same
  transaction, to a digest of **the root facts the pass observed** (assignment,
  real path, handle — exactly the triple the provenance stamp records) **and
  the row's own `file_path`, `content_hash` and `extraction_version`**. Every
  other write of a row's derived state sets it NULL. A row whose marker equals
  the digest the current pass expects for it is *derived under the current
  root*; anything else (NULL, another root's facts, a path, hash or extraction
  version that moved since) is not.
- **A repeated re-derive revisits only what is unresolved.** Under a
  re-derive, a row already derived under the current root goes through the
  ordinary incremental rules — hash comparison, and the stat shortcut where
  the pass is not a full-hash pass — instead of being forced. Only rows not
  derived under the current root (and new or changed files) are read, parsed
  and upserted. The issue's scenario drops from a full-scope rewrite per tick
  to rewriting nothing but what changed.
- **The stamp's gate becomes a checked invariant.** The provenance stamp is
  recorded only if no withholding skip occurred **and**, re-read in the pass's
  own transaction after its last write, every surviving row of the scope
  carries the marker expected for it. Before recording, that pass re-resolves
  every link row's target against the final row set, so link resolution is
  exactly what a single full re-derive would produce.
- **Withholding is narrowed to its own rationale.** A skip withholds the stamp
  only if it could leave a row **not derived under the current root** — a read,
  re-read, raw-body or parse skip on such a row, a directory the walk could not
  list with such a row at or beneath it, a C5 deferral of such a row, and (as
  now) every keyword-vector and link-rebuild skip. A skip on a row already
  derived under the current root cannot hide a foreign row and does not
  withhold.
- **The exclusion sweep is forgotten only when a re-derive changed a row**,
  not on every re-deriving tick.
- `rederive_incomplete` and `/health` are unchanged in meaning: a scope with
  an unresolved row stays incomplete and reaches `degraded` after the
  threshold, as it should. The incomplete-re-derive log line and
  `IndexPassResult` gain the count of rows still not derived under the current
  root.

No new setting.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `index-integrity`: the re-derive requirement (re-derive progress is per row
  and persisted; the completion re-resolution), the incomplete-re-derive
  requirement (withholding narrowed to rows not derived under the current
  root; the stamp gated on the row invariant), the stat-shortcut requirement
  (a re-derive no longer bypasses the shortcut for a row derived under the
  current root); one new requirement for the marker, its digest, its writers
  and migration 030.

## Impact

- `src/models/db.py`: `NoteMetadata.derived_under` + comment marker.
- `alembic/versions/030_note_derived_under.py`: nullable column, no default,
  no backfill, reconcile-or-refuse, marked downgrade.
- `src/services/indexer.py`: `_reconcile_provenance` (facts digest),
  `SnapshotRow` (marker in the C4 comparison), `_scan_vault` / `_needs_body`
  (per-row force), `_index_vault_pinned` (sweep forgetting),
  `_index_vault_attempt` (change detection, withholding, upsert / move SET
  NULL, tail marker write, completion re-resolution, invariant re-read before
  the stamp), `IndexPassResult.rederive_pending`.
- `tests/`: unit tests, `tests/integration/` real-Postgres test, schema gate
  head literal and 030 cases.
- Docs: `docs/architecture/indexing-and-embeddings.md`,
  `docs/architecture/schema-and-migrations.md`, CLAUDE.md's indexer decisions.
- Not touched: `move_note` and every other writer outside the pass (the digest
  binding invalidates the marker when they change path or content — see
  design D3), `/health` shape, usage logging, rate limits, OAuth, transfer,
  panel.
