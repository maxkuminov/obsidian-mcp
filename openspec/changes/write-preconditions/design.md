## Context

The consumer of these tools is an agent, and the vault is the owner's single source of truth. The expensive failure is a destructive write, and the specific one open here is **lost update from stale client state**: an agent reads a note, spends a turn deciding, and writes back a result computed from bytes that are no longer on disk.

What exists today, precisely:

- `_atomic_write_at(target, …, expected=…)` re-reads the destination through the **same parent descriptor** immediately before the publishing rename and refuses with `File changed while editing: <name>` on a difference (`src/services/vault.py`). `edit_note_impl` and `set_frontmatter_impl` pass the bytes they read at the top of the same call. The window it closes is *this call's* read → *this call's* rename.
- `write_file(overwrite=True)` passes no `expected=` at all and never reads the incumbent — an unconditional replace, documented as such in `vault-tools.md`.
- `read_note` returns a structured `ReadNoteResult` (#149) built from `vault.read_file()`, which reads the note with `Path.read_text(encoding="utf-8")` — universal-newline translated, frontmatter partitioned. Nothing in the response identifies the bytes on disk.
- `notes_metadata.content_hash` is `sha256(text)` over that **translated** text (`indexer._content_hash`, fed by `read_note_at`, which deliberately reads in text mode so a CRLF note's hash does not churn). It is therefore *not* a hash of the file.
- `vault_fs.fingerprint()` already computes SHA-256 **over the raw bytes** of a file, through one `O_NOFOLLOW` descriptor whose inode it verifies, for the transfer publish gate.
- `_scrub_frontmatter` is the one representability boundary: it drops what *nothing* can render and explicitly declines to normalise what each consumer renders its own way — its docstring names "dates, non-string keys, non-finite floats" as out of scope.

Constraints that shape the design:

- **Optional forever.** Every deployed client calls these tools with today's arguments. A required precondition breaks all of them, and an agent that cannot supply one must still be able to write.
- **One read per call.** `read_note` must not re-read the file to serve a hash: #149's D3 rejected a second read for `frontmatter_yaml` precisely because two reads of one note can disagree, and a hash that describes different bytes than the response body is worse than no hash.
- **No new envelope around note-controlled bytes.** #149 ended the rendered-envelope class; nothing here may reintroduce a delimiter an agent has to split on.
- **No silent ignoring.** A supplied precondition that is not enforced is worse than none, because the caller believes it was.
- **The event loop is shared.** Hashing is one linear pass over bytes the tool already holds; nothing here may add an unbounded read.

## Goals / Non-Goals

**Goals**

- An agent can bind a write to the bytes it read, on every path that can destroy content, with one optional argument and one field it already receives.
- A stale precondition is a refusal that hands back everything needed to retry: the current hash, the path, and the statement that nothing was written.
- The digest is defined once, computed from the same bytes everywhere, and impossible to confuse with the index's own hash.
- A non-finite frontmatter number can never abort an index pass, never desynchronise the two renderings of a read, and never be rewritten into the note.

**Non-Goals**

- **Linearizability.** This is optimistic concurrency control. The residual windows are enumerated below and stay open; the kernel offers no compare-and-swap on a rename.
- **A version counter, an ETag registry, or lock tools.** State the server would have to keep, invalidate and recover; the file's own bytes are the version.
- **A section-scoped or region-scoped hash.** Unsound: `#N` ordinals are positional, so a hash over a section body says nothing about whether the selector still names that section. Whole-file only (D5).
- **Binding `move_note(rewrite_links=True)`'s backlink sources.** The caller never read them; a precondition it cannot compute is not an affordance (D8, accepted limitation L3).
- **`import_from_url(overwrite=True)` and `request_upload(overwrite=True)`.** The upload path already binds the incumbent's fingerprint at mint time; `import_from_url` fetches its content from the network rather than from a caller's read. Both are recorded as residuals (L4), not widened into this change.
- **Changing `notes_metadata.content_hash`.** It hashes translated text on purpose. Redefining it would mark every CRLF note changed and re-embed the vault.
- **Widening `_scrub_frontmatter`.** #154 is fixed at the JSON boundaries, not at the parse (D10).

## Decisions

### D1 — The digest, defined once

**`content_hash` = `"sha256:" + hex(SHA-256(the complete raw bytes of the file as stored on disk))`**, lowercase hex, 64 characters after the prefix, 71 characters total.

- **Over which bytes:** every byte of the file, in file order. No universal-newline translation, no frontmatter stripping, no windowing, no re-encoding, no normalisation of any kind. These are exactly the bytes `read_bytes_at(target, …)` returns and exactly what `_atomic_write_at(expected=…)` compares, and the same digest `vault_fs._hash_regular` computes for the transfer fingerprint.
- **Over which file:** the path as named, after `open_mutable`/`validate_visible_path` has resolved it — never through a symlinked leaf on a write path.
- **Not the body.** A note that carries frontmatter, or CRLF terminators, has a `content_hash` that a hash of `read_note`'s `content` field can never reproduce. Both docstring layers must say so: the field is a token to hand back, not a checksum of the text the caller received.
- **Not `notes_metadata.content_hash`.** Same algorithm, different input (translated text). The `sha256:` prefix is what makes an accidental cross-comparison fail *always* instead of failing only on CRLF notes — a consistent mismatch is debuggable, an intermittent one is not.
- **Accepted input forms:** `sha256:<64 hex>` (canonical, what every read emits) or a bare `<64 hex>`, hex case-insensitive. Anything else — wrong length, non-hex, an unknown algorithm prefix — is a refusal naming the expected form, never a mismatch, because "malformed" and "stale" call for different actions from the caller.

### D2 — Where the check runs

Every guarded tool follows one order, and the order is the design:

1. `open_mutable(path)` — parent descriptor pinned, symlinked leaf refused.
2. `_leaf_state_error(...)` — missing / directory / symlink.
3. **the in-call read** of the incumbent bytes through that descriptor. `edit_note` and `set_frontmatter` already perform it; `write_file`, `move_note` and `delete_note` perform it **only when `expected_hash` is supplied**, so an unguarded call reads nothing it does not read today.
4. **the precondition compare** — `sha256(incumbent) == expected_hash`. This is the first thing after the read and runs **before** mode dispatch, before the size cap, before `dry_run`'s diff and before any no-op determination: a diff or a "No changes" answer computed against a base the caller does not have is a misleading answer, not a cheap one.
5. composition, caps, confirmation.
6. `_atomic_write_at(expected=incumbent_bytes)` — the existing compare, re-reading through the same descriptor immediately before the rename.

**Two windows, stated as a pair.** The precondition covers *the caller's read → this call's read*. `expected=` covers *this call's read → the rename*. Neither subsumes the other and neither is removed. What remains after both is the rename syscall itself, against an adversary who can already write the destination directory — the residual `vault-tools.md` has declared since #59.

### D3 — The refusal shape

Every guarded tool but `read_note` returns `str`, so the refusal is an in-band error string; `refusal_result=` in `_tracked` maps *admission* refusals onto a typed shape and exists today only for `read_note`, the one tool with an output schema. The rule for this change is therefore: **a stale precondition is delivered in whatever shape that tool's successful result already takes** — a string for the write tools, and, if a write tool ever gains an output model, the typed result via the same `refusal_result=` mechanism rather than a bare string (which would fail FastMCP's output validation and reach the agent as a protocol error).

The string is server-authored, with a stable leading phrase so a client can classify it without parsing prose:

```
<tool>: precondition failed for <path>. expected_hash names bytes this note no
longer has; its current content_hash is sha256:<hex>. Nothing was written.
Re-read the note and recompute your edit from the current bytes.
```

Rules on that message:

- It carries **the current hash**, so the caller knows what it is racing and can detect a second change between its retry read and its retry write.
- It carries **no note content** — no excerpt, no diff, no length. A digest of content the caller is already authorised to read discloses nothing; content in an error message is the forgery surface #149 closed.
- It is distinct from `File changed while editing: <name>`, which stays exactly as it is for the in-call window. Two windows, two messages; a test asserts an agent can tell them apart.
- `path` is bounded by `MAX_PATH_CHARS` as every other path-bearing message is.

### D4 — Where the precondition applies

| Tool / mode | `expected_hash` | Bound to | Notes |
| --- | --- | --- | --- |
| `edit_note` full replacement | ✅ optional | whole file | the highest-blast-radius note path |
| `edit_note(replace_frontmatter=True)` | ✅ optional | whole file | whole-file overwrite |
| `edit_note(append=True)` | ✅ optional | whole file | appends lose nothing, but the caller may still want to bind |
| `edit_note(find=…)` | ✅ optional | whole file | zero/non-unique matches already refuse; this binds the base |
| `edit_note(section=…)` | ✅ optional | whole file | never the section body — D5 |
| `edit_note(dry_run=True)` | ✅ optional | whole file | checked before the diff; a diff against another base is a wrong answer |
| `set_frontmatter` | ✅ optional | whole file | checked before the defect/lossy/no-op determinations |
| `write_file(overwrite=True)` | ✅ optional | whole file | **also gains the in-call `expected=`** on such calls |
| `write_file(overwrite=False)` | ⛔ refused | — | no-clobber; `linkat` is already linearizable, and there are no incumbent bytes to bind |
| `create_note` | ⛔ refused | — | same |
| `move_note` | ✅ optional | the **source note's** bytes | checked in the preflight, before the rename and before any rewrite; never binds backlink sources (L3) |
| `delete_note(permanent=False)` | ✅ optional | whole file | soft delete is recoverable, but only by an agent that knows to look in `.trash` |
| `delete_note(permanent=True)` | ✅ optional | whole file | irreversible; the issue's own recommendation |
| `delete_file`, `list_files`, every read tool | — | — | not a content-destroying overwrite of bytes the caller read |
| `import_from_url(overwrite=True)` | ⛔ out of scope | — | content comes from the network, not from a caller's read (L4) |
| `request_upload(overwrite=True)` | ⛔ out of scope | — | already binds the incumbent fingerprint at mint time |

"⛔ refused" is the load-bearing entry: supplying `expected_hash` where it cannot be enforced returns an error naming the reason. Ignoring it would leave the caller believing it was guarded.

### D5 — The hash is always the whole file's, including for section reads and truncated reads

A section-body hash is unsound: `edit_note(section="#7")` resolves an **ordinal over the current document**, so a body-only hash would certify that the seventh section's text is unchanged while an insertion above it silently moved which section "#7" names. Binding heading identity *and* body bytes was the alternative (#205 suggests it) and is rejected: it is a second digest with its own definition, its own serialization, and its own failure modes, for a mode whose blast radius is already the smallest.

The consequence is declared rather than hidden: **the whole-file hash makes the safest mode the most conflict-prone.** A section write to `## A` is refused when someone edited `## Z`. That is acceptable *because the argument is optional* — an agent appending to a log section omits it; an agent rewriting a section it reasoned about supplies it. Both docstring layers say exactly this.

`read_note(section=…)` and a truncated `read_note` both return the **whole file's** hash, so the round trip stays one read and one write. A truncated read still cannot be written back (the existing round-trip requirement), and the hash does not change that.

### D6 — `read_note` computes the hash from the same read that builds the response

`vault.read_file()` today does `Path.read_text(encoding="utf-8")`. It becomes: read **bytes** once; hash those bytes; derive the text from the same bytes by decoding UTF-8 strict and applying universal-newline translation explicitly (`\r\n` → `\n`, then a lone `\r` → `\n`) — which is exactly what `read_text` produced. One read, one partition, one hash of provably the same bytes.

*Rejected:* hashing in a second pass over the path. #149's D3 rejected re-reading for `frontmatter_yaml` because two reads can disagree; a hash is a stronger reason to keep one read, since the whole point is that it identifies the bytes in the response.

The field is server-controlled, fixed at 71 characters, and is **never dropped under metadata-budget pressure**: dropping the precondition token silently disables the precondition (the caller can only omit `expected_hash`, i.e. write unguarded) on precisely the notes big enough to be worth guarding. Add it to the fixed allocation beside `path` in the worst-case response arithmetic in `vault-tools.md`.

### D7 — The raw-file surface, without an envelope

`read_file`'s text branch returns the file's text **bare** — no header, no frame. Adding one would break every existing caller and rebuild the forgeable envelope #149 removed, so:

- The **base64** branch already emits a server-controlled header (`encoding`, `mime`, `bytes`, `path`) ahead of an opaque body. It gains `content_hash:`. Safe by construction: the base64 alphabet contains neither `:` nor a newline, so the body cannot forge a header line.
- `read_file(path, hash_only=True)` returns **metadata only** — path, size, mime, `content_hash` — and no content at all. Nothing is wrapped around note-controlled bytes, no existing response shape changes, and a text file's hash costs no tokens. `encoding` has no effect (the digest is over raw bytes, always); `offset`/`limit` combined with `hash_only` are a refusal rather than a silent no-effect.
- Every `write_file` success line reports the resulting `content_hash`, so a write→write chain needs no read at all.

*Rejected:* `list_files(include_hashes=True)`. A 200-row listing of 10 MiB files is 2 GiB of synchronous reading on the shared event loop, and bounding it introduces a per-call byte budget and a "these rows were not hashed" reporting shape for an affordance `hash_only` already provides at one file per call. Recorded as a follow-up if an agent ever needs bulk hashes.

*Rejected:* a wrapper around text results behind an opt-in flag. Opt-in or not, it hands the agent a response it has to split content out of, which is the class #149 exists to end.

### D8 — `move_note` binds the source note only

`move_note` publishes with one `renameat2(RENAME_NOREPLACE)` and already pins and verifies the source inode, so the *move* is not the lost-update risk. Two things still are: `rewrite_links=True` rewrites the moved note's own body and every backlink source, and any move relocates a note whose content the caller may have reasoned about. `expected_hash` therefore binds **`from_path`'s bytes**, compared in the preflight before the rename and before any rewrite, and binds nothing else. Backlink sources are never bound (L3): the caller never read them, so there is no hash it could supply, and a precondition covering one of N files while implying all of them is worse than none.

### D9 — Every write tool reports the resulting `content_hash`

Each guarded tool knows exactly the bytes it published, so its success message ends with the new `content_hash`. Server-authored prose, no forgery surface, and it removes the read from the common edit→edit loop. The docstrings must say the value describes *the bytes this call published*, not the bytes on disk now.

### D10 — #154: coerce at the JSON boundaries, never at the parse

**What happens today** (established by reading the code; the implementation slice pins each half with a test — nothing here was run against the server):

1. `_representability` returns `None` for every `float`, and its docstring names non-finite floats as deliberately out of scope. So `x: .nan` survives `_scrub_frontmatter` into every consumer, unrecorded in `lossy`.
2. `read_result._view_leaf` already handles it: `return value if math.isfinite(value) else _view_str(str(value), budget)`. The read path is therefore **already safe** — no `PydanticSerializationError`, no divergence between the text block and `structuredContent`. But the coercion is silent, uses Python's spelling (`"nan"`, `"inf"`, `"-inf"`) rather than the note's own (`.nan`, `.inf`, `-.inf`), and is indistinguishable from a note whose value really is the string `"nan"`.
3. `indexer._sanitize_value` returns floats unchanged (`isinstance(v, (str, int, float, bool, NoneType))`), so the float reaches `notes_metadata.frontmatter` (JSONB). The engine sets no `json_serializer`, so SQLAlchemy's default `json.dumps` applies, which with `allow_nan=True` emits the bare tokens `NaN` / `Infinity` / `-Infinity`. PostgreSQL's `jsonb` input parser rejects all three. **Expected result: a `DataError` out of the 100-row batch upsert, the pass's single transaction aborts, nothing commits, `content_hash` never advances, and every subsequent tick retries the same fatal batch — #126's failure mode reached by a new route, taking indexing down for the whole tenant, not just that note.** The batch has no per-note retreat (unlike the keyword-vector pass), which is what makes it total.
4. `set_frontmatter` re-serialises the block **from the parsed mapping** with `yaml.safe_dump`. PyYAML loads `.nan` to `float('nan')` and dumps `float('nan')` back as `.nan` (and `.inf` / `-.inf`), so today's round trip is already byte-identical, and `_same_frontmatter_value` already special-cases NaN through `float.hex()`. This is correct behaviour that must survive the fix.

**The rule.**

- **`_scrub_frontmatter` is not touched.** Its predicate is "nothing can render this"; Python renders a non-finite float and so does YAML. Coercing there would put the string `".nan"` into the mapping `set_frontmatter` serialises, so setting an unrelated key would rewrite `x: .nan` to `x: '.nan'` — a silent rewrite of a value the caller never named, which is the destructive-write class. Recording it in `lossy` instead would make `set_frontmatter` refuse outright on a note whose only "defect" is a NaN, an availability break for the same non-defect.
- **Each JSON boundary coerces, to the value's YAML token**: `.nan`, `.inf`, `-.inf`. Two boundaries, and only two — `indexer._sanitize_value` (JSONB) and `read_result._view_leaf` (the read view). YAML's spelling rather than Python's so that the indexed value, the read view and the note's bytes all agree, and so `keyword_search(frontmatter=…)` matches the token a person would type.
- **The index pass never fails for it**, and a note carrying one indexes and searches like any other.
- **The read view discloses the coercion** in the server-controlled `metadata_omissions` list, with its own reason code, naming `frontmatter` and pointing at `frontmatter_yaml` for the note's own bytes. The view is *not* omitted: dates and over-wide integers are already coerced in place without omission, and dropping a whole block's view over one NaN hides the rest of a perfectly good mapping.
- **Nothing is written back to the note.** The only thing that rewrites a block is `set_frontmatter`, which serialises the mapping the parse produced — still a float — so `x: .nan` stays `x: .nan` byte for byte. A scenario pins exactly that.

*Rejected:* setting `json_serializer=partial(json.dumps, allow_nan=False)` on the engine. It converts an invalid-JSON write into a `ValueError` instead of a `DataError` — still a fatal pass, just with a different traceback — and it spreads the boundary from one function to the engine configuration, which is the per-consumer screening the #149 boundary doctrine exists to avoid. Worth reconsidering only as a tripwire once the coercion is in place (O5).

*Rejected:* dropping the key with a `lossy` reason. It deletes user data from the index and from the read view for a value both YAML and Python carry fine, and it drags `set_frontmatter` into refusing.

### D11 — Logging

`expected_hash` joins the `_tracked` parameter list of every tool that accepts it. It is a digest, not a secret, and an operator reading `usage_logs` after a lost update needs to see which writes were guarded and which base each one claimed. A refused write logs exactly as any other in-band refusal does.

## Risks / Trade-offs

- [Whole-file hash makes section writes conflict-prone] → optional argument, documented trade-off, and the refusal names the current hash so a retry is one read away (D5).
- [An agent starts sending `expected_hash` everywhere and writes begin failing under normal single-user editing] → the failure is a refusal with nothing written, which is the correct direction; the docstring tells the agent when to bind and when not to.
- [The `read_file` bytes-once change to `vault.read_file()` alters read semantics] → the explicit translation reproduces `read_text(encoding="utf-8")` exactly (LF, CRLF, lone CR); the existing frontmatter, section and round-trip suites are the regression gate, plus a byte-level test over all three terminator dialects.
- [Hashing cost on every `read_note`] → one SHA-256 pass over bytes already in memory, single-digit milliseconds at `MAX_NOTE_BYTES`; no new read, no new allocation of consequence.
- [`write_file(expected_hash=…)` now reads the incumbent] → only when the argument is supplied, bounded by `MAX_FILE_READ_BYTES`, and refused (not silently skipped) when the incumbent exceeds it.
- [#154 coercion changes what `keyword_search(frontmatter=…)` matches for such notes] → today those notes are not indexed at all (the pass dies), so there is nothing to break; stated in the spec.
- [The `.nan` diagnosis is reasoned from code, not observed] → each half is pinned by a test in slice D before anything ships: a `jsonb` round trip through the real column, and the `set_frontmatter` byte-identity case.

## Accepted limitations

| # | Limitation | Why it is accepted |
| --- | --- | --- |
| L1 | The precondition is optimistic, not linearizable. A writer landing between the compare and the rename is still overwritten (`expected=` narrows this to the rename syscall; a delete or a move has no `expected=` analogue at all, so its window is compare → `renameat2`). | No syscall offers compare-and-swap on a rename. The same residual `vault-tools.md` has declared since #59, now narrowed rather than widened. |
| L2 | An agent that omits `expected_hash` gets exactly today's behaviour, including today's silent lost update. | Mandating it breaks every deployed client. The affordance plus the docstring is the whole remedy available without a compatibility break. |
| L3 | `move_note(rewrite_links=True)` binds only the moved note; the backlink sources it rewrites are unbound. | The caller never read them and has no hash to supply. Each source's own rewrite still goes through the in-call `expected=` compare, so a concurrent edit to a source yields the existing partial-success report naming it. |
| L4 | `import_from_url(overwrite=True)` and `PUT /transfer/upload` keep their current behaviour. | The upload path already binds the incumbent fingerprint at mint time; `import_from_url`'s bytes come from the network, so a caller-read precondition has no meaning there. Tracked as a follow-up. |
| L5 | `read_file`'s text branch never carries the hash; a text file's hash costs a second call (`hash_only=True`). | The alternative is an envelope around note-controlled bytes, which is the forgery class #149 closed. |
| L6 | `notes_metadata.content_hash` and the wire `content_hash` remain two different digests of one note. | Redefining the column would mark every CRLF note changed and re-embed the vault. The `sha256:` prefix makes confusing them fail consistently. |
| L7 | The non-finite coercion is not reversible from the JSON view: `x: .nan` and `x: ".nan"` both render as the string `.nan` in `frontmatter` and in JSONB. | `frontmatter_yaml` carries the note's bytes verbatim and is the authoritative representation (#149); the view is documented as lossy, and the coercion is disclosed in `metadata_omissions`. |
| L8 | A `tags: .nan` scalar still becomes the tag text `nan` via `extract_tags`' `str()`, and the panel's note viewer renders `nan`. | Neither is a JSON boundary and neither can fail; unifying every rendering is the normalisation `_scrub_frontmatter` deliberately refuses to do. |

## Owner decisions

Each is a default chosen here; say the word and it flips.

- **O1 — Field name `content_hash`, value prefixed `sha256:`.** Chosen over an unprefixed hex string (indistinguishable from the DB column) and over `file_hash` (accurate but unfamiliar). The prefix also leaves room for a future algorithm without a second field.
- **O2 — `expected_hash` accepts both `sha256:<hex>` and bare `<hex>`, case-insensitive.** Tolerant input, canonical output. Malformed input is refused as malformed, never treated as a mismatch.
- **O3 — `delete_note` accepts `expected_hash` in both modes**, not only `permanent=True`. A soft delete is recoverable only by an agent that knows to look in `.trash`; the check is one read and one comparison.
- **O4 — The raw-file hash surface is `read_file`'s base64 header + `hash_only=True` + the `write_file` success line.** `list_files(include_hashes=True)` was rejected on event-loop cost (D7).
- **O5 — No engine-level `allow_nan=False`.** One coercion boundary, as the #149 doctrine prescribes. Add the tripwire later if a second non-finite route ever appears.
- **O6 — The read view coerces and discloses rather than omitting.** Consistent with how dates and over-wide integers are already handled; omitting would hide an otherwise fine block over one value.
- **O7 — `move_note` is in scope, bound to the source note only.** The alternative was to defer it entirely; it is the one destructive tool where a caller *has* read the file it names.

## Migration Plan

None. No schema change, no new column, no data migration, no `make test-schema` (nothing carries a migration). `make db-check` after deploy as usual. Rollback is a code revert: `expected_hash` is optional everywhere, so a client that started sending it simply loses the guard, and `content_hash` fields disappear from read responses. The #154 half is likewise pure code — but note that any tenant already wedged by a `.nan` note only recovers *after* the deploy, on the next pass.

## Open Questions

- None blocking. Whether an agent ever needs bulk hashes (`list_files`) is left to observation.
