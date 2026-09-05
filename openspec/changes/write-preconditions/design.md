## Context

The consumer of these tools is an agent, and the vault is the owner's single source of truth. The expensive failure is a destructive write, and the specific one open here is **lost update from stale client state**: an agent reads a note, spends a turn deciding, and writes back a result computed from bytes that are no longer on disk.

What exists today, precisely:

- `_atomic_write_at(target, …, expected=…)` re-reads the destination through the **same parent descriptor** immediately before the publishing rename and refuses with `File changed while editing: <name>` on a difference (`src/services/vault.py`). `edit_note_impl` and `set_frontmatter_impl` pass the bytes they read at the top of the same call. The window it closes is *this call's* read → *this call's* rename.
- `write_file_at` forwards `expected=` to it; **`write_bytes_at` and the single-shot `write_bytes` do not accept `expected` at all**, so the raw-byte path has no way to reach the comparison even if a tool wanted it.
- `write_file(overwrite=True)` passes no `expected=` and never reads the incumbent — an unconditional replace, documented as such in `vault-tools.md`. It also *creates* when the path is absent.
- `read_note` returns a structured `ReadNoteResult` (#149) built from `vault.read_file()`, which reads the note with `Path.read_text(encoding="utf-8")` — universal-newline translated, frontmatter partitioned. Nothing in the response identifies the bytes on disk.
- `notes_metadata.content_hash` is `sha256(text)` over that **translated** text (`indexer._content_hash`, fed by `read_note_at`, which deliberately reads in text mode so a CRLF note's hash does not churn). It is therefore *not* a hash of the file.
- `vault_fs.fingerprint()` already computes SHA-256 **over the raw bytes** of a file, through one `O_NOFOLLOW` descriptor whose inode it verifies, for the transfer publish gate.
- `_scrub_frontmatter` is the one representability boundary: it drops what *nothing* can render and explicitly declines to normalise what each consumer renders its own way — its docstring names "dates, non-string keys, non-finite floats" as out of scope.
- `indexer._sanitize_value` serves **two** consumers, not one: the JSONB `frontmatter` column *and* `_note_title`, which feeds `notes_metadata.title`. A change to it therefore changes what a note is called.

Constraints that shape the design:

- **Optional by default, forever.** Every deployed client calls these tools with today's arguments. A required precondition breaks all of them. (A deployment that *wants* strictness gets a setting — D12 — never a default.)
- **One read per call.** `read_note` must not re-read the file to serve a hash: #149's D3 rejected a second read for `frontmatter_yaml` precisely because two reads of one note can disagree, and a hash that describes different bytes than the response body is worse than no hash.
- **No new envelope around note-controlled bytes.** #149 ended the rendered-envelope class; nothing here may reintroduce a delimiter an agent has to split on.
- **No silent ignoring.** A supplied precondition that is not enforced is worse than none, because the caller believes it was.
- **Refusals must be machine-parseable.** Prose an agent has to pattern-match is not a contract. This change adopts the sibling `mcp-rate-limits` refusal shape rather than inventing a second one (D3).
- **The event loop is shared.** Hashing is one linear pass over bytes the tool already holds; nothing here may add an unbounded read.

## Goals / Non-Goals

**Goals**

- An agent can bind a write to the bytes it read, on every path that can destroy content, with one optional argument and one field it already receives.
- A stale precondition is a **typed** refusal that hands back everything needed to retry: the code, the path, the current hash, and the fact that nothing was written.
- The digest is defined once, computed from the same bytes everywhere, and impossible to confuse with the index's own hash.
- A non-finite frontmatter number can never abort an index pass, never desynchronise the two renderings of a read, never be rewritten into the note, and renders identically in every tool that shows it.

**Non-Goals**

- **Linearizability.** This is optimistic concurrency control. The residual windows are enumerated below and stay open; the kernel offers no compare-and-swap on a rename.
- **A version counter, an ETag registry, or lock tools.** State the server would have to keep, invalidate and recover; the file's own bytes are the version.
- **A section-scoped or region-scoped hash.** Unsound: `#N` ordinals are positional, so a hash over a section body says nothing about whether the selector still names that section. Whole-file only (D5).
- **Binding `move_note(rewrite_links=True)`'s backlink sources.** The caller never read them; a precondition it cannot compute is not an affordance (D8, L3).
- **`import_from_url(overwrite=True)` and `request_upload(overwrite=True)`.** The upload path already binds the incumbent's fingerprint at mint time; `import_from_url`'s bytes come from the network. Both recorded as residuals (L4).
- **Changing `notes_metadata.content_hash`.** It hashes translated text on purpose. Redefining it would mark every CRLF note changed and re-embed the vault.
- **Widening `_scrub_frontmatter`.** #154 is fixed at the JSON and title boundaries, not at the parse (D10).
- **Making `frontmatter_yaml` byte-exact.** It is LF-normalized today and stays so (L9).

## Decisions

### D1 — The digest, defined once, canonical form only

**`content_hash` = `"sha256:" + hex(SHA-256(the complete raw bytes of the file as stored on disk))`**, with the hex **lowercase**, 64 characters, 71 in total.

- **Over which bytes:** every byte of the file, in file order. No universal-newline translation, no frontmatter stripping, no windowing, no re-encoding, no normalisation of any kind. These are exactly the bytes `read_bytes_at(target, …)` returns and exactly what `_atomic_write_at(expected=…)` compares, and the same digest `vault_fs._hash_regular` computes for the transfer fingerprint.
- **Over which file:** the path as named, after `open_mutable` / `validate_visible_path` has resolved it — never through a symlinked leaf on a write path.
- **Not the body.** A note that carries frontmatter, or CRLF terminators, has a `content_hash` that a hash of `read_note`'s `content` field can never reproduce. Both docstring layers must say so: the field is a token to hand back, not a checksum of the text the caller received.
- **Not `notes_metadata.content_hash`.** Same algorithm, different input (translated text).
- **Exactly one accepted input form:** `sha256:<64 lowercase hex>`. A bare hex string, uppercase hex, whitespace, another algorithm prefix, or any other shape is `malformed_precondition` — a typed refusal naming the accepted form, never a mismatch, because "you sent the wrong kind of thing" and "the file changed" call for different actions. **Dual-accept was in the first draft and is withdrawn:** accepting bare hex would make the index column's own value — bare 64-hex, of *different* bytes — a syntactically valid precondition that fails as a mismatch on LF notes and, by coincidence of equal digests being impossible, always fails; a caller cannot tell that from a real conflict. Canonical-only turns that mistake into `malformed_precondition`, which says what actually happened. The proposal must not claim the prefix makes such a comparison "fail uniformly"; it makes it **refused as malformed**, which is the useful property.

### D2 — Where the check runs

Every guarded tool follows one order, and the order is the design:

1. `open_mutable(path)` — parent descriptor pinned, symlinked leaf refused.
2. `_leaf_state_error(...)` — missing / directory / symlink.
3. **the in-call read** of the incumbent bytes through that descriptor. `edit_note` and `set_frontmatter` already perform it; `write_file`, `move_note` and `delete_note` perform it **only when `expected_hash` is supplied**, so an unguarded call reads nothing it does not read today.
4. **the precondition compare** — `sha256(incumbent) == expected_hash`. This is the first thing after the read and runs **before** mode dispatch, before the size cap, before `dry_run`'s diff and before any no-op or defect determination: a diff, a "no changes" answer or a frontmatter-defect report computed against a base the caller does not hold is a wrong answer, not a cheap one.
5. composition, caps, confirmation.
6. `_atomic_write_at(expected=incumbent_bytes)` — the existing compare, re-reading through the same descriptor immediately before the rename.

Two exceptions to step 1–3 ordering, both because there are no incumbent bytes: `create_note` and `write_file(overwrite=False)` evaluate `expected_hash` **before any filesystem work at all** and return `no_incumbent` (D4). `write_file(overwrite=True, expected_hash=…)` on a **missing** path is the same case: it must not fall through to its create behaviour, because the caller asserted it was replacing something.

**Two windows, stated as a pair.** The precondition covers *the caller's read → this call's read*. `expected=` covers *this call's read → the rename*. Neither subsumes the other and neither is removed. What remains after both is the rename syscall itself, against an adversary who can already write the destination directory — the residual `vault-tools.md` has declared since #59.

### D3 — The refusal is typed, and it is the sibling contract

Prose is not a contract. This change reuses the caller-visible refusal shape defined by the sibling proposal `mcp-rate-limits` (its D5) rather than inventing a second one: `src/services/refusals.py`, importing nothing from the app, defining a `Refusal` with a closed `code` set and one renderer that appends a **final, line-initial, single-line** sentinel to the existing prose:

```
MCP-REFUSAL {"code":"stale_precondition","path":"Notes/a.md","current_hash":"sha256:<hex>","nothing_written":true}
```

`str` tools get prose + that line; a structured tool gets the identical complete text in its declared error field through `refusal_result`, so no output-schema validation can fail and both carry the same fields. Fields that do not apply are **absent**, never null.

This change contributes four codes to that closed set:

| `code` | Raised when | Carries |
| --- | --- | --- |
| `stale_precondition` | the supplied hash does not match the incumbent bytes | `path`, `current_hash`, `nothing_written: true` |
| `no_incumbent` | `expected_hash` on a path with no incumbent bytes to bind: `create_note`, `write_file(overwrite=False)`, or `write_file(overwrite=True)` on a missing path | `path`, `nothing_written: true` |
| `malformed_precondition` | `expected_hash` is not `sha256:<64 lowercase hex>` | `path`, `nothing_written: true` |
| `precondition_required` | `WRITE_PRECONDITION_REQUIRED` is on and a guarded write supplied none (D12) | `path`, `nothing_written: true`, and `current_hash` **only** where the tool had already read the incumbent |

`retry_after_seconds` is **absent for all four**, following the sibling's rule that a number is omitted wherever it would invite a loop that cannot end: no delay makes a stale hash match. The retry guidance lives in the prose half — re-read (`read_note`, or `read_file(hash_only=True)` for a raw file), recompute, resend.

The prose half carries **no note content** — no excerpt, no diff, no length. A digest of content the caller may already read discloses nothing; content in an error message is the forgery surface #149 closed. `path` is bounded by `MAX_PATH_CHARS` as every other path-bearing message is.

`File changed while editing: <name>` stays exactly as it is for the in-call window: two windows, two refusals, and a test asserts an agent can tell them apart.

**Dependency, stated.** `refusals.py` is created by whichever of the two changes lands first. If it does not exist when this one is implemented, **Slice A creates it with the identical contract** — same module path, same sentinel, same renderer, same absent-not-null rule — so the two changes converge on one file rather than two shapes. If it already exists, Slice A only adds the four codes.

### D4 — Where the precondition applies, and what each tool reports

| Tool / mode | `expected_hash` | Bound to | Success reports `content_hash` |
| --- | --- | --- | --- |
| `edit_note` full replace / `replace_frontmatter` / `append` / `find` / `section` | ✅ optional | whole file | ✅ the bytes published |
| `edit_note(dry_run=True)` | ✅ optional | whole file | ❌ nothing was published |
| `set_frontmatter` | ✅ optional | whole file | ✅ the bytes published (a no-op result reports none) |
| `create_note` | ⛔ `no_incumbent` | — | ✅ the bytes published |
| `write_file(overwrite=True)`, path present | ✅ optional (also gains the in-call `expected=` on such calls) | whole file | ✅ the bytes published |
| `write_file(overwrite=True)`, path absent | ⛔ `no_incumbent` when `expected_hash` is supplied | — | ✅ when unguarded and it creates |
| `write_file(overwrite=False)` | ⛔ `no_incumbent` | — | ✅ the bytes published |
| `move_note` | ✅ optional | the **source note's** bytes | ✅ the **destination's** hash — and for `rewrite_links=True` **only the moved note's**, never the rewritten sources' |
| `delete_note` (both `permanent` values) | ✅ optional | whole file | ❌ nothing exists to hash |
| `delete_file`, `list_files`, read tools | — | — | see `file-access` for where a read exposes it |
| `import_from_url(overwrite=True)` | ⛔ out of scope | — | — |
| `request_upload(overwrite=True)` | ⛔ out of scope (already fingerprint-bound at mint) | — | — |

"⛔" is load-bearing: the argument is **accepted by the signature** on every one of those tools and answered with a typed refusal. A signature that rejects it produces a protocol-level `TypeError`, not the contract this change promises — which is exactly why `create_note` takes the argument it can never honour.

`move_note` reporting the destination's hash matters because a `rewrite_links=True` move rewrites the moved note's own body: the bytes at `to_path` are not the bytes that were at `from_path`, and reporting the source's hash would hand the agent a token that binds nothing.

### D5 — The hash is always the whole file's, including for section reads and truncated reads

A section-body hash is unsound: `edit_note(section="#7")` resolves an **ordinal over the current document**, so a body-only hash would certify that the seventh section's text is unchanged while an insertion above it silently moved which section "#7" names. Binding heading identity *and* body bytes was the alternative (#205 suggests it) and is rejected: it is a second digest with its own definition, serialization and failure modes, for the mode whose blast radius is already the smallest.

The consequence is declared rather than hidden: **the whole-file hash makes the safest mode the most conflict-prone.** A section write to `## A` is refused when someone edited `## Z`. That is acceptable *because the argument is optional* — an agent appending to a log section omits it; an agent rewriting a section it reasoned about supplies it. Both docstring layers say exactly this.

`read_note(section=…)` and a truncated `read_note` both return the **whole file's** hash, so the round trip stays one read and one write. A truncated read still cannot be written back (the existing round-trip requirement), and the hash does not change that.

### D6 — `read_note` computes the hash from the same read that builds the response

`vault.read_file()` today does `Path.read_text(encoding="utf-8")`. It becomes: read **bytes** once; hash those bytes; derive today's text from the same bytes (UTF-8 strict decode, then `\r\n` → `\n`, then lone `\r` → `\n` — exactly what `read_text` produced). One read, one partition, one hash of provably the same bytes.

*Rejected:* hashing in a second pass over the path. #149's D3 rejected re-reading for `frontmatter_yaml` because two reads can disagree; a hash is a stronger reason to keep one read, since the whole point is that it identifies the bytes in the response.

The field is server-controlled, fixed at 71 characters, and is **never dropped under metadata-budget pressure**: dropping the precondition token silently disables the precondition on precisely the notes big enough to be worth guarding. It is accounted for as a fixed allocation alongside `path` in the worst-case arithmetic in `vault-tools.md`.

### D7 — The raw-file surface, without an envelope

`read_file`'s text branch returns the file's text **bare** — no header, no frame. Adding one would break every existing caller and rebuild the forgeable envelope #149 removed, so:

- the **base64** result carries `content_hash` in the labelled header it already emits ahead of its opaque body (the base64 alphabet contains neither `:` nor a newline, so the body cannot forge a header line);
- `read_file(path, hash_only=True)` returns **metadata only** — path, size, MIME, `content_hash` — and no file content at all;
- every `write_file` success line reports the resulting `content_hash`, so a write→write chain needs no read.

**Argument precedence in `read_file` is fixed and documented**, because two validations now compete: `encoding` is validated **first** (an invalid `encoding` is refused whatever `hash_only` says), then `hash_only` against `offset`/`limit` — supplying either with `hash_only=True` is a refusal, not a silently ignored window — then the existing `offset`/`limit` range checks. With `hash_only=True` a *valid* `encoding` has no effect, because the digest is over raw bytes in every case; the docstring says so rather than leaving it inferred.

`write_bytes_at` and the single-shot `write_bytes` gain `expected: bytes | None = None`, forwarded to `_atomic_write_at`, because today the raw-byte path cannot reach the comparison at all. That is a prerequisite of the guarded `write_file`, not an incidental tidy-up.

*Rejected:* `list_files(include_hashes=True)`. A 200-row listing of 10 MiB files is 2 GiB of synchronous reading on the shared event loop, and bounding it introduces a per-call byte budget and a "these rows were not hashed" reporting shape for an affordance `hash_only` already provides.

*Rejected:* a wrapper around text results behind an opt-in flag. Opt-in or not, it hands the agent a response it has to split content out of — the class #149 exists to end.

### D8 — `move_note` binds the source note only

`move_note` publishes with one `renameat2(RENAME_NOREPLACE)` and already pins and verifies the source inode, so the *move* is not the lost-update risk. Two things still are: `rewrite_links=True` rewrites the moved note's own body and every backlink source, and any move relocates a note whose content the caller may have reasoned about. `expected_hash` therefore binds **`from_path`'s bytes**, compared in the preflight before the rename and before any rewrite, and binds nothing else. Backlink sources are never bound (L3): the caller never read them, so there is no hash it could supply, and a precondition covering one of N files while implying all of them is worse than none.

### D9 — Every publishing write reports the content hash of what it published

Each guarded tool knows exactly the bytes it published, so its success message ends with the new `content_hash` (D4's last column says which, and `delete_note` reports none because nothing remains to hash). Server-authored prose, no forgery surface, and it removes the read from the common edit→edit loop. The docstrings must say the value describes *the bytes this call published*, not the bytes on disk now.

### D10 — #154: coerce at the JSON and title boundaries, never at the parse

**What happens today** (established by reading the code; the implementation slice pins each half with a test — nothing here was run against the server):

1. `_representability` returns `None` for every `float`, and its docstring names non-finite floats as deliberately out of scope. So `x: .nan` survives `_scrub_frontmatter` into every consumer, unrecorded in `lossy`.
2. `read_result._view_leaf` already handles it: `return value if math.isfinite(value) else _view_str(str(value), budget)`. The read path is therefore **already safe** — no `PydanticSerializationError`, no divergence between the text block and `structuredContent`. But the coercion is silent, uses Python's spelling (`"nan"`, `"inf"`, `"-inf"`) rather than the note's own, and is indistinguishable from a note whose value really is the string `"nan"`.
3. `indexer._sanitize_value` returns floats unchanged, so the float reaches `notes_metadata.frontmatter` (JSONB). The engine sets no `json_serializer`, so SQLAlchemy's default `json.dumps` applies, which with `allow_nan=True` emits the bare tokens `NaN` / `Infinity` / `-Infinity`. PostgreSQL's `jsonb` parser rejects all three. **Expected result: a `DataError` out of the 100-row batch upsert, the pass's single transaction aborts, nothing commits, `content_hash` never advances, and every subsequent tick retries the same fatal batch — #126's failure mode reached by a new route, taking indexing down for the whole owner, not just that note.** The batch has no per-note retreat (unlike the keyword-vector pass), which is what makes it total.
4. **The same function feeds titles.** `_note_title` calls `_sanitize_value(frontmatter.get("title"))` and then `str(value or stem)[:512]`, so `title: .nan` becomes the title `nan` in the index; `vault.read_file()` independently does `str(title)` for `read_note` and the panel, also `nan`. One function serving two different questions is why a fix to the JSONB half would silently re-key titles.
5. `set_frontmatter` re-serialises the block **from the parsed mapping** with `yaml.safe_dump`. PyYAML loads `.nan` to `float('nan')` and dumps it back as `.nan` (and `.inf` / `-.inf`), so today's round trip is already byte-identical, and `_same_frontmatter_value` already special-cases NaN through `float.hex()`. This is correct behaviour that must survive the fix.

**The rule.**

- **`_scrub_frontmatter` is not touched.** Its predicate is "nothing can render this"; Python renders a non-finite float and so does YAML. Coercing there would put the string `".nan"` into the mapping `set_frontmatter` serialises, so setting an unrelated key would rewrite `x: .nan` to `x: '.nan'` — a silent rewrite of a value the caller never named, the destructive-write class. Recording it in `lossy` instead would make `set_frontmatter` refuse outright on a note whose only "defect" is a NaN.
- **One shared token function, three boundaries.** A single helper renders a non-finite float as its **canonical YAML token** — `.nan`, `.inf`, `-.inf` — and is used by (a) the indexer's JSONB sanitisation, (b) the indexer's title coercion, and (c) `vault.read_file()`'s title coercion, which is what `read_note` and the control panel both display. The read view (`_view_leaf`) uses the same helper. **`_sanitize_value` is split** so the JSONB question and the title question are answered separately and visibly, instead of one function's return value silently deciding both.
- **Canonical output regardless of source spelling.** YAML 1.1 accepts `.nan`, `.NaN`, `.NAN`, `.inf`, `.Inf`, `.INF`, `+.inf` and their negatives, and the parse keeps none of that: by the time any consumer sees the value it is a Python float. The rendered token is therefore always the canonical lowercase form, whatever the note spelled, and that is stated rather than left to be discovered. `frontmatter_yaml` still carries the note's own spelling (LF-normalized — L9).
- **The index pass never fails for it**, and a note carrying one indexes, titles and searches like any other.
- **The read view discloses the coercion** — but *not* through `metadata_omissions`, which by its own requirement lists fields **dropped whole**. A retained-but-altered value is a different fact, so the result gains a sibling list, `metadata_coercions`, with the same server-controlled shape (field, reason code, what to read instead). The view is not omitted: a non-finite number is renderable, unlike the shapes that force an omission, and dropping a whole block's view over one value would hide an otherwise faithful mapping.
- **Nothing is written back to the note.** The only thing that rewrites a block is `set_frontmatter`, which serialises the mapping the parse produced — still a float — so `x: .nan` stays `x: .nan` byte for byte. A scenario pins exactly that.

*Rejected:* setting `json_serializer=partial(json.dumps, allow_nan=False)` on the engine. It converts an invalid-JSON write into a `ValueError` instead of a `DataError` — still a fatal pass — and spreads the boundary from one function into the engine configuration, which is the per-consumer screening the #149 boundary doctrine exists to avoid. Reconsider only as a tripwire once the coercion is in place (O5).

*Rejected:* dropping the key with a `lossy` reason. It deletes user data from the index and the read view for a value both YAML and Python carry fine, and drags `set_frontmatter` into refusing.

### D11 — Logging

`expected_hash` joins the `_tracked` parameter list of every tool that accepts it, `create_note` included. It is a digest, not a secret, and an operator reading `usage_logs` after a lost update needs to see which writes were guarded and which base each one claimed. A refused write logs exactly as any other in-band refusal does — **including the refusal itself**: the "nothing was written" guarantee is about the vault and the derived index, and the call's own `usage_logs` row is written as it is for every tool call (L10).

### D12 — `WRITE_PRECONDITION_REQUIRED`, default false

Optionality is what keeps deployed clients working, and it is also the whole limitation of the change: an agent that never sends `expected_hash` is exactly as exposed as it is today. A deployment that would rather refuse an unguarded write than take that risk gets a lever: `WRITE_PRECONDITION_REQUIRED` (a `Settings` boolean, **default false**), which when true makes every tool in D4's "✅ optional" rows refuse a call that supplies no `expected_hash`, with the typed `precondition_required` refusal and nothing written.

- It is a **deployment** decision, never a per-call one: an agent cannot turn it off, and a client that does not send hashes is broken by it deliberately and visibly rather than silently.
- The `no_incumbent` tools are **exempt**: `create_note` and `write_file(overwrite=False)` have nothing to bind, so requiring a hash there would make creation impossible.
- Where the tool has already read the incumbent (`edit_note`, `set_frontmatter`), the refusal carries `current_hash`, so a compliant agent recovers in one retry rather than two calls. Where it has not (`write_file`, `move_note`, `delete_note`), it does not, and the prose names the read that supplies one.
- Default false means the deploy is a no-op for every existing client; the setting is documented in `README`/`DEPLOYMENT` alongside the other write-safety knobs.

## Risks / Trade-offs

- [Whole-file hash makes section writes conflict-prone] → optional argument, documented trade-off, and the refusal names the current hash so a retry is one read away (D5).
- [An agent starts sending `expected_hash` everywhere and writes begin failing under normal single-user editing] → the failure is a refusal with nothing written, which is the correct direction; the docstring tells the agent when to bind and when not to.
- [`WRITE_PRECONDITION_REQUIRED` turned on breaks a client] → that is its purpose, it is off by default, and the refusal is typed and names the missing argument.
- [The `read_file` bytes-once change to `vault.read_file()` alters read semantics] → the explicit translation reproduces `read_text(encoding="utf-8")` exactly (LF, CRLF, lone CR); the existing frontmatter, section and round-trip suites are the regression gate, plus a byte-level test over all three terminator dialects.
- [Splitting `_sanitize_value` changes a title] → it changes exactly one class of title, `nan`/`inf` → `.nan`/`.inf`, in the same commit across the indexer, `read_note` and the panel, with a cross-tool test asserting all three agree. Any other title is byte-identical, asserted over a fixture corpus.
- [Adding `expected=` to `write_bytes_at`/`write_bytes` changes a shared primitive] → the parameter defaults to `None` and every existing call site keeps today's behaviour; a regression test covers the single-shot caller.
- [Hashing cost on every `read_note`] → one SHA-256 pass over bytes already in memory, single-digit milliseconds at `MAX_NOTE_BYTES`; no new read.
- [`write_file(expected_hash=…)` now reads the incumbent] → only when the argument is supplied, bounded by `MAX_FILE_READ_BYTES`, and refused (not silently skipped) when the incumbent exceeds it.
- [#154 coercion changes what `keyword_search(frontmatter=…)` matches for such notes] → today those notes are not indexed at all (the pass dies), so there is nothing to break.
- [The `.nan` diagnosis is reasoned from code, not observed] → each half is pinned by a test in slice D **before** anything changes: a `jsonb` round trip through the real column, and the `set_frontmatter` byte-identity case.

## Accepted limitations

| # | Limitation | Why it is accepted |
| --- | --- | --- |
| L1 | The precondition is optimistic, not linearizable. A writer landing between the compare and the rename is still overwritten (`expected=` narrows this to the rename syscall; a delete or a move has no `expected=` analogue, so its window is compare → `renameat2`). | No syscall offers compare-and-swap on a rename. The residual `vault-tools.md` has declared since #59, now narrowed rather than widened. |
| L2 | An agent that omits `expected_hash` gets today's behaviour, including today's silent lost update — unless the deployment sets `WRITE_PRECONDITION_REQUIRED`. | Mandating it by default breaks every deployed client. D12 is the lever for a deployment that prefers the break. |
| L3 | `move_note(rewrite_links=True)` binds only the moved note; the backlink sources it rewrites are unbound. | The caller never read them. Each source's own rewrite still goes through the in-call `expected=` compare, so a concurrent edit yields the existing partial-success report naming it. |
| L4 | `import_from_url(overwrite=True)` and `PUT /transfer/upload` keep their current behaviour. | The upload path already binds the incumbent fingerprint at mint time; `import_from_url`'s bytes come from the network. Tracked as a follow-up. |
| L5 | `read_file`'s text branch never carries the hash; a text file's hash costs a second call (`hash_only=True`). | The alternative is an envelope around note-controlled bytes — the forgery class #149 closed. |
| L6 | `notes_metadata.content_hash` and the wire `content_hash` remain two different digests of one note. | Redefining the column would mark every CRLF note changed and re-embed the vault. Canonical-only input (D1) makes handing one where the other belongs a `malformed_precondition` rather than a confusing mismatch. |
| L7 | The non-finite coercion is not reversible from the JSON view or the title: `x: .nan` and `x: ".nan"` both render as `.nan`, and `.NaN` renders as `.nan`. | `frontmatter_yaml` carries the block's own spelling; the view is documented as lossy and the coercion is disclosed in `metadata_coercions`. |
| L8 | A `tags: .nan` scalar still becomes the tag text `nan` via `extract_tags`' own `str()`. | Not a JSON or title boundary and it cannot fail; unifying every rendering is the normalisation `_scrub_frontmatter` deliberately refuses to do. Recorded rather than widened into this change. |
| L9 | `frontmatter_yaml` is authoritative **but LF-normalized**, not byte-exact — the residual #149 declared. A caller that needs the block's exact bytes uses `read_file` (base64), whose header also carries the `content_hash`. | Re-reading the note as bytes purely to serve one response field would give `read_note` a second partition of the same note, which #149's D3 exists to prevent. |
| L10 | "Nothing was written" excludes the call's own `usage_logs` row, which is written for a refusal exactly as for any other call. | That row is the audit trail; suppressing it would hide precisely the calls an operator investigating a lost update needs to see. The guarantee is about the vault and the derived index. |
| L11 | Backward compatibility is claimed for **mutation and conflict semantics only**. Success strings change: every publishing write now appends its `content_hash`, and `move_note`'s success line reports the destination's. | The result strings are prose that has never been a contract, and the new field is additive at the end; any client asserting on the whole string will see it. Stated so nobody reads "no client is broken" as covering exact output text. |

## Owner decisions

Each is a default chosen here; say the word and it flips.

- **O1 — Field name `content_hash`, value `sha256:<64 lowercase hex>`.** Chosen over an unprefixed hex string (indistinguishable from the index column) and over `file_hash`. The prefix leaves room for a future algorithm without a second field.
- **O2 — Canonical form only on input.** Bare hex, uppercase hex and any other shape are `malformed_precondition`. (Revised: the first draft accepted both forms; Codex's pre-code review established that tolerance mislabels the one mistake worth catching.)
- **O3 — `delete_note` accepts `expected_hash` in both modes**, not only `permanent=True`. A soft delete is recoverable only by an agent that knows to look in `.trash`; the check is one read and one comparison.
- **O4 — The raw-file hash surface is `read_file`'s base64 header + `hash_only=True` + the `write_file` success line.** `list_files(include_hashes=True)` rejected on event-loop cost (D7).
- **O5 — No engine-level `allow_nan=False`.** One coercion boundary, as the #149 doctrine prescribes.
- **O6 — The read view coerces and discloses in a new `metadata_coercions` list**, rather than omitting the view or overloading `metadata_omissions`, whose contract is "dropped whole".
- **O7 — `move_note` is in scope, bound to the source note only**, and reports the destination's hash.
- **O8 — `WRITE_PRECONDITION_REQUIRED` exists and defaults to false.** A per-deployment lever, not a per-call one, and never a default that breaks a client (D12).
- **O9 — One canonical spelling for non-finite numbers everywhere** (`.nan` / `.inf` / `-.inf`), including titles, at the cost of changing the title of any note that already has one.

## Migration Plan

None. No schema change, no new column, no data migration, no `make test-schema` (nothing carries a migration). `make db-check` after deploy as usual. One new setting, `WRITE_PRECONDITION_REQUIRED`, default false, so the deploy is a no-op for every existing client. Rollback is a code revert: `expected_hash` is optional everywhere, so a client that started sending it loses the guard, and `content_hash` fields disappear from read responses. The #154 half is pure code — but a tenant already wedged by a `.nan` note only recovers *after* the deploy, on the next pass, and any such note's indexed title changes from `nan` to `.nan` at the same time.

## Open Questions

- None blocking. Whether an agent ever needs bulk hashes (`list_files`) is left to observation.
