## Why

Two findings from the 2026-09-04 ASVS 5.0 assessment and #149's round-4 pass, both on the note-content path, both about a write or an index pass that goes wrong quietly.

- **#205 (ASVS V2.3.4) — there is no caller-visible write precondition.** The `expected=` compare-and-swap in `_atomic_write_at` is server-internal: `edit_note` reads the file inside the call and compares those bytes immediately before the rename, so it covers the tool's own read→publish interval and *structurally cannot* see a change that landed between the agent's `read_note` and its `edit_note`. An agent that reads a note, thinks, and writes back therefore overwrites a concurrent edit — the second key on the same root, or the owner typing in Obsidian on the read-write mount — with no error and no record. The blast radius is the whole note under full replacement and `replace_frontmatter=True`, the addressed region under `append`/`find`/`section`, and the whole file under `write_file(overwrite=True)`, which has no conflict detection **at all**, not even in-call. `read_note` surfaces no hash, so even a caller who wanted to guard could not. #212 already corrected the `edit_note` docstring that read as if the in-call compare covered the caller's own round trip; the affordance itself is still missing, and lost-update is the failure class `CLAUDE.md` ranks first.
- **#154 — non-finite YAML floats in frontmatter are Python-representable but not JSON.** `_scrub_frontmatter`'s predicate deliberately admits them (its docstring names non-finite floats alongside dates as "renderable, each consumer's own business"), so `x: .nan` reaches every consumer. `read_note`'s JSON view already coerces it — silently, and to Python's spelling `"nan"` rather than the note's own `.nan`. The indexer does not coerce at all: the float goes into the `notes_metadata.frontmatter` JSONB column through SQLAlchemy's default `json.dumps`, which emits the bare tokens `NaN` / `Infinity` / `-Infinity` that PostgreSQL's `jsonb` parser rejects. The expected consequence is the failure shape #126 already cost this project: the batch upsert raises, the pass's single transaction aborts, nothing commits, `content_hash` never advances, and every subsequent tick retries the same fatal batch — indexing dead for that whole tenant because of one note.

## What Changes

- **One digest, defined once.** `content_hash` is `"sha256:" + hex(SHA-256(complete raw bytes of the file on disk))` — no newline translation, no frontmatter stripping, no re-encoding: exactly the bytes `expected=` compares and exactly the digest `vault_fs.fingerprint` already computes for the transfer publish gate. It is deliberately **not** `notes_metadata.content_hash`, which hashes the universal-newline-*translated* text and therefore differs for every CRLF note; the `sha256:` prefix exists so the two can never be compared by accident.
- **Reads hand the digest back.** `read_note` gains a `content_hash` field in its structured result — always the **whole file's**, for a section read and a truncated read alike — computed from the same single read that produces the response, never a second one. `read_file` reports it in the header it already emits for base64 results, and gains `hash_only=True`, a metadata-only response (path, size, mime, hash, no content) so a text file's hash costs no tokens.
- **Writes accept an optional precondition.** `expected_hash` is added to `edit_note` (all four modes, `dry_run` included), `set_frontmatter`, `write_file(overwrite=True)`, `move_note` (binding the source note's own bytes) and `delete_note`. **Never required** — omitted, every tool behaves exactly as it does today. Supplied and matching, the write proceeds. Supplied and stale, the call is an in-band refusal that names the **current** hash, writes nothing, and touches no database row. A precondition that cannot be enforced — on a no-clobber creation, or on a file too large to read — is a refusal, never silently ignored.
- **The existing in-call compare stays.** It closes a different window (this call's read → this call's rename) and the two are documented as a pair: the precondition covers the caller's read → this call's read, `expected=` covers the rest. `write_file(overwrite=True)` gains the in-call `expected=` too, but only on the calls that supply `expected_hash` — without one it stays the unconditional replace it is documented to be.
- **Every write tool reports the resulting `content_hash`.** A write→write chain is then guarded without a re-read.
- **Non-finite frontmatter numbers are coerced at the JSON boundaries, not at the parse.** `indexer._sanitize_value` and `read_result._view_leaf` render them as their **YAML** tokens (`.nan`, `.inf`, `-.inf`), so the indexed value, the read view and the note's own bytes agree; the read view discloses the coercion in `metadata_omissions`. The parsed mapping keeps the float, so `set_frontmatter` still round-trips `x: .nan` byte-identically — PyYAML loads and dumps it unchanged. An index pass never fails for a non-finite number.

No migration. No new dependencies. No change to `notes_metadata.content_hash`'s definition (changing it would invalidate change detection for every CRLF note and re-embed the vault).

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `vault-write`: defines the write-precondition digest once; every overwrite path accepts an optional `expected_hash` checked immediately after the in-call read; a stale precondition is a refusal naming the current hash with nothing written; the existing in-call `expected=` compare is restated as the second, distinct window; write tools report the resulting hash.
- `note-read`: `read_note` returns `content_hash` over the whole file in every mode, from the same read that builds the response, never dropped under budget pressure; the frontmatter JSON view renders non-finite numbers as YAML tokens and reports the coercion.
- `file-access`: `read_file` carries the hash in the base64 header and gains `hash_only`; `write_file(overwrite=True)` honours `expected_hash` and reports the resulting hash; `write_file` without one is unchanged.
- `index-integrity`: a non-finite frontmatter number is stored as its YAML token and can never abort an index pass.

## Impact

- `src/services/vault.py` — the digest helper, and `read_file()` reading bytes once so the hash and the text come from the same read.
- `src/mcp_server/read_result.py` — the `content_hash` field, `_view_leaf`'s YAML-token rendering, the new omission reason.
- `src/mcp_server/tools.py` — the shared precondition helper and its use in `edit_note_impl`, `set_frontmatter_impl`, `write_file_impl`, `move_note_impl`, `delete_note_impl`; `read_note_impl`, `read_file_impl`, `_base64_payload`; the `_tracked` parameter lists (`expected_hash` is logged).
- `src/mcp_server/server.py` — signatures and docstrings for the five write tools and the two read tools, kept symmetric.
- `src/services/indexer.py` — `_sanitize_value`.
- Tests: precondition matrix per tool and per mode, CRLF and frontmatter-bearing notes (where a body hash would not match), the check/publish interleaving, `.nan` through the indexer and the read path, `set_frontmatter` byte-identity.
- Docs: `docs/architecture/vault-tools.md` (the digest, the two windows, the per-tool table, the residuals), `docs/architecture/indexing-and-embeddings.md` (the non-finite boundary rule).
- Closes #205
- Closes #154
