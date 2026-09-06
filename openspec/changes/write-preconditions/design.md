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

### D2 — Where the check runs, and in what order

Every guarded tool follows one order, and the order is the design:

1. **Syntax.** If `expected_hash` is present and is not `sha256:<64 lowercase hex>` → `malformed_precondition`, **at the tool's entry: before path resolution, before the leaf check, before any read**, in every tool and every mode. It is a pure function of the argument, so it is a *separate* helper from the comparison — the two are only adjacent in the ladder, not in the code — and it therefore wins over not-found, over a symlinked leaf and over the size cap. A caller told "not found" for a call whose argument was never valid fixes the wrong thing.
2. **Applicability.** A tool or mode that can never bind incumbent bytes — `create_note`, `write_file(overwrite=False)` — answers a supplied hash with `no_incumbent`, still before any filesystem work.
3. `open_mutable(path)` — parent descriptor pinned, symlinked leaf refused; then `_leaf_state_error(...)`. An **absent** target on an overwrite path with a hash supplied is `no_incumbent` (`write_file(overwrite=True)` must not fall through to creating, because the caller asserted it was replacing something); on the note tools absence stays today's "not found".
4. **Availability.** The incumbent exists but is larger than the cap the tool may read → `precondition_unavailable`, naming the cap. This is also the answer under required mode when no hash was supplied, because supplying one was impossible: telling such a caller `precondition_required` sends it to fetch a hash it can never obtain. **When no hash was supplied and required mode is off, an over-cap file is not a refusal at all** — the call proceeds exactly as today and reports no result hash. This capability must not fail a call that succeeds today merely because a file is too large to hash.
5. **Required mode.** No hash supplied on an enforceable call while `WRITE_PRECONDITION_REQUIRED` is on → `precondition_required` (D12).
6. **the in-call read** of the incumbent bytes through the pinned descriptor, **bounded** by that tool's own content cap — `MAX_NOTE_BYTES` for a note tool, `MAX_FILE_READ_BYTES` for a raw-file tool — with the size taken from an `fstat` on the descriptor the tool already holds and the read then performed through that same descriptor, so the bytes measured are the bytes hashed and no second pathname is resolved. `edit_note` and `set_frontmatter` already perform it; `write_file`, `move_note`, `delete_note` and `delete_file` perform it **only when a hash is supplied or required mode demands one**, so an unguarded call reads nothing it does not read today.
7. **the comparison** — `sha256(incumbent) == expected_hash`; a difference is `stale_precondition`. This runs **before** mode dispatch, before the size cap, before `dry_run`'s diff and before any no-op or defect determination: a diff, a "no changes" answer or a frontmatter-defect report computed against a base the caller does not hold is a wrong answer, not a cheap one.
8. composition, caps, confirmation.
9. `_atomic_write_at(expected=incumbent_bytes)` — the existing compare, re-reading through the same descriptor immediately before the rename; a difference is `concurrent_write` (D3).

The steps are a strict precedence, not a set of independent checks, and the combinations are what make that observable: a malformed hash on `create_note` is `malformed_precondition`, not `no_incumbent`; a malformed hash on a no-clobber write, on a missing path, and on an over-cap file are all `malformed_precondition` too. Each of those four is a scenario.

**Two windows, stated as a pair.** The precondition covers *the caller's read → this call's read*. `expected=` covers *this call's read → the rename*. Neither subsumes the other and neither is removed. What remains after both is the rename syscall itself, against an adversary who can already write the destination directory — the residual `vault-tools.md` has declared since #59.

### D3 — The refusal is typed, and it is the sibling contract

Prose is not a contract. This change reuses the caller-visible refusal shape defined by the sibling proposal `mcp-rate-limits` (its D5) rather than inventing a second one: `src/services/refusals.py`, importing nothing from the app, defining a `Refusal` with a closed `code` set and one renderer that appends a **final, line-initial, single-line** sentinel to the existing prose:

```
MCP-REFUSAL {"code":"stale_precondition","path":"Notes/a.md","current_hash":"sha256:<hex>","nothing_written":true}
```

`str` tools get prose + that line; a structured tool gets the identical complete text in its declared error field through `refusal_result`, so no output-schema validation can fail and both carry the same fields. Fields that do not apply are **absent**, never null.

This change contributes six codes to that closed set:

| `code` | Raised when | Carries |
| --- | --- | --- |
| `stale_precondition` | the supplied hash does not match the incumbent bytes | `path`, `current_hash`, `nothing_written: true` |
| `concurrent_write` | the **in-call** comparison observes a change between this call's read and its publication | `path`, `nothing_written: true` |
| `no_incumbent` | `expected_hash` on a path with no incumbent bytes to bind: `create_note`, `write_file(overwrite=False)`, or `write_file(overwrite=True)` on a missing path | `path`, `nothing_written: true` |
| `malformed_precondition` | `expected_hash` is not `sha256:<64 lowercase hex>` | `path`, `nothing_written: true` |
| `precondition_unavailable` | the incumbent exists but is larger than the cap this tool may read, so no comparison is possible | `path`, `cap_name`, `cap_bytes`, `nothing_written: true` |
| `precondition_required` | `WRITE_PRECONDITION_REQUIRED` is on and an enforceable write supplied none (D12) | `path`, `nothing_written: true`, and `current_hash` **only** where the tool had already read the incumbent |

**`concurrent_write` is the other window, and it is typed too.** The in-call comparison already refuses with `File changed while editing: <name>`; that prose stays exactly as it is — every existing `in`/`startswith` assertion still holds, the same additive rule the sibling applies to the three pre-body refusals — and the sentinel is appended to it. The two windows are then distinguishable **by code** rather than by prose shape, which is the whole point of typing them: `stale_precondition` means "the file moved before you called and you can see the new hash", `concurrent_write` means "the file moved during my call, so re-read and retry". Implementation note: `_atomic_write_at` raises, and the tools already catch that exception to turn it into an in-band string, so the rendering happens where the catch is. `move_note`'s per-source rewrite failures keep their existing partial-success report — that is a different statement (some sources were rewritten) and is not this refusal.

`precondition_unavailable` exists because "I could not check" and "I checked and it differs" are different facts, and answering the first with the second would send a caller into a retry loop against a file whose hash it will never be able to produce. It carries the cap's **name and value** so the caller (or the operator reading the log) knows which knob applies.

**Each code's prose states the action that resolves *it*, and the actions differ** — one generic "re-read and retry" would be wrong for three of the six. `stale_precondition`: re-read and recompute, and the returned `current_hash` is resendable if nothing else changes in between. `concurrent_write`: the file changed during the call, so no pre-call hash can be valid — re-read and retry. `precondition_required`: this deployment requires a precondition; resend with `expected_hash`, using the `current_hash` here when one is present. `no_incumbent`: there is nothing to guard at this path — **call again without `expected_hash`**. `malformed_precondition`: the canonical form is `sha256:<64 lowercase hex>`, and here is the read that produces one. `precondition_unavailable`: this file is over `<cap name>` (`<value>`), which only an operator can raise; to inspect its bytes, use the transfer download route.

`retry_after_seconds` is **absent for all six**, following the sibling's rule that a number is omitted wherever it would invite a loop that cannot end: no delay makes a stale hash match, and no delay shrinks a file below a cap. The retry guidance lives in the prose half — re-read (`read_note`, or `read_file(hash_only=True)` for a raw file), recompute, resend.

The prose half carries **no note content** — no excerpt, no diff, no length. A digest of content the caller may already read discloses nothing; content in an error message is the forgery surface #149 closed. `path` is bounded by `MAX_PATH_CHARS` as every other path-bearing message is.

`File changed while editing: <name>` stays exactly as it is for the in-call window: two windows, two refusals, and a test asserts an agent can tell them apart.

**Dependency, stated.** `refusals.py` is created by whichever of the two changes lands first. If it does not exist when this one is implemented, **Slice A creates it with the identical contract** — same module path, same sentinel, same renderer, same absent-not-null rule — so the two changes converge on one file rather than two shapes. If it already exists, Slice A only adds the six codes.

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
| `move_note` | ✅ optional | the **source note's** bytes | ✅ the hash of the bytes actually published at the destination — see the matrix below |
| `delete_note` (both `permanent` values) | ✅ optional | whole file | ❌ nothing exists to hash |
| `delete_file` (both `permanent` values) | ✅ optional | whole file | ❌ nothing exists to hash |
| `list_files`, read tools | — | — | see `file-access` for where a read exposes it |
| `import_from_url(overwrite=True)` | ⛔ out of scope | — | — |
| `request_upload(overwrite=True)` | ⛔ out of scope (already fingerprint-bound at mint) | — | — |

"⛔" is load-bearing: the argument is **accepted by the signature** on every one of those tools and answered with a typed refusal. A signature that rejects it produces a protocol-level `TypeError`, not the contract this change promises — which is exactly why `create_note` takes the argument it can never honour.

**`delete_file` is in scope, in both modes.** It was excluded in the first draft, on the reasoning that the raw-byte tools are transport; that is wrong against this change's own claim to cover every path that can destroy content. A `delete_file` on a file that changed since the caller read it destroys bytes the caller never saw — `permanent=True` irreversibly, `permanent=False` into a `.trash` name only an agent that knows to look will find. It takes `expected_hash`, runs the same comparison through the same helper, and obeys required mode; the check happens **before** the trash rename or the unlink. Its path guard is `vault_fs`'s beneath-root walk rather than `open_mutable`, so the incumbent read happens through that same anchored lookup and not through a re-resolved pathname.

**`move_note`'s reported hash is the hash of the bytes actually published**, which needs a matrix rather than a sentence, because a `rewrite_links=True` move publishes twice — the `renameat2`, and then the moved note's own body rewrite — and the second can fail after the first has committed:

| Case | Reported `content_hash` |
| --- | --- |
| plain move (`rewrite_links=False`) | the moved bytes, unchanged from the source |
| `rewrite_links=True`, the moved note's own rewrite **published** | the post-rewrite bytes at `to_path` |
| `rewrite_links=True`, the moved note's own rewrite **failed without observing a change** (I/O error, cap, a refusal computed before it read) | the bytes the **rename** published — the unrewritten bytes now at `to_path` — never the intended rewritten bytes, which are not on disk |
| `rewrite_links=True`, the moved note's own rewrite **lost the in-call conflict** (`concurrent_write`: an editor changed `to_path` between the rename and the rewrite's publication) | **no hash at all.** The destination holds that writer's bytes, which this call never read; the result says the move completed, the rewrite did not, and the destination must be re-read before it is written to |
| `rewrite_links=True`, a **backlink source's** rewrite failed | unaffected: the moved note's hash, per the two rows above |

Reporting an intended-but-unpublished hash would hand the agent a token that binds nothing and would make the next guarded write fail as `stale_precondition` against bytes that were never written — and the `concurrent_write` row is the sharper case: there the *rename's* hash is equally wrong, because someone else's bytes are at the destination. Omitting is the only honest answer, and the result has to say so rather than leaving the field silently absent.

**After a rename, nothing may claim that nothing was written.** A post-rename failure is a partial success, never a whole-call refusal, and `nothing_written: true` must not appear on it in any form: the move happened, and a caller told otherwise goes looking for a note that has already relocated. The failure modes are tested separately — moved-note rewrite failure without a conflict, moved-note rewrite losing the conflict, and a backlink source failing — because they are different code paths behind the same user-visible "partial success" wrapper.

**A hash is reported only when the bytes can be bounded.** Where a tool would have to read a file back to report its hash and that file is over the tool's cap, the result omits the hash and says why rather than failing a call that already succeeded.

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

The `hash_only` conflict is decided **by value, not by "was it passed"**: `offset != 0 or limit is not None`. MCP arguments arrive as a dict with defaults applied, so "the caller supplied it" is not reliably knowable, and a caller that sends `offset=0` explicitly means the same thing as one that omits it. Testing values keeps the rule stable and testable.

**The metadata header is structurally encoded (#149's rule, applied to a place that predates it).** `_base64_payload` interpolates the path raw into `path: {path}`, and a vault path may contain a newline, a carriage return or a colon — the framing requirement itself insists `a\nb.md` and `a b.md` must stay distinguishable — so a filename can inject a `content_hash:` line into a server-controlled header the caller is about to trust. Adding a hash to that header without fixing it would be shipping a forgeable envelope. The path is therefore rendered as a **JSON string** (quoted, escaped) in both the base64 header and the `hash_only` result: reversible, already the house answer to this exact class, and nothing new for an agent to learn. It is applied **uniformly**, to every path — a conditional escape would leave the reader unable to tell which mode a given response is in — so the header line for an ordinary path gains quotes. That is a declared output change (L12), and it is the smaller price.

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
- **One shared token function, four boundaries.** A single helper renders a non-finite float as its **canonical YAML token** — `.nan`, `.inf`, `-.inf` — and is used by (a) the indexer's JSONB sanitisation, (b) the indexer's title coercion, (c) `vault.read_file()`'s title coercion, which is what `read_note` and the control panel both display, and (d) the read view (`_view_leaf`). **`_sanitize_value` is split** so the JSONB question and the title question are answered separately and visibly, instead of one function's return value silently deciding both.
- **Mapping keys go through it too.** A YAML mapping may be *keyed* by a non-finite number (`.nan: 1`), and both key paths stringify today — `_view_key`'s `str(key)` and the JSONB sanitiser's `k if isinstance(k, str) else str(k)` — producing `nan` in a place the note spells `.nan`. Both take the helper. Collisions then need a rule, because coercion can make two distinct YAML keys land on one string (`.nan: 1` beside `".nan": 2`):
  - **JSONB sanitiser: the first key wins**, stated rather than inherited. Today it is a dict comprehension, so the *last* key silently wins — an accident of iteration order, not a decision. The index has no channel to report a loss and must never fail the pass, so a deterministic, documented rule is the whole available remedy.
  - **Read view: the existing collision path stands** — the view is omitted whole with a duplicate-key omission, as the framing requirement already mandates, and the reason code says the collision arose *from the coercion* so a caller can tell it from a native `1:` / `"1":` collision. **This deviates from the reviewer's suggested "first key wins plus a coercion entry"**, and deliberately: first-wins in the *view* silently drops a key the note contains, which is exactly what #149's omit-whole-view rule exists to prevent, and a partial view is indistinguishable from a complete one. The two boundaries differ because one can report a loss and the other cannot.
- **Canonical output regardless of source spelling.** YAML 1.1 accepts `.nan`, `.NaN`, `.NAN`, `.inf`, `.Inf`, `.INF`, `+.inf` and their negatives, and the parse keeps none of that: by the time any consumer sees the value it is a Python float. The rendered token is therefore always the canonical lowercase form, whatever the note spelled, and that is stated rather than left to be discovered. `frontmatter_yaml` still carries the note's own spelling (LF-normalized — L9).
- **The index pass never fails for it**, and a note carrying one indexes, titles and searches like any other.
- **The read view discloses the coercion** — but *not* through `metadata_omissions`, which by its own requirement lists fields **dropped whole**. A retained-but-altered value is a different fact, so the result gains a sibling list, `metadata_coercions`, with the same server-controlled shape (field, reason code, what to read instead). The reason code is the literal **`non_finite_float`**, named here so the spec, the implementation and the tests use one string. The view is not omitted: a non-finite number is renderable, unlike the shapes that force an omission, and dropping a whole block's view over one value would hide an otherwise faithful mapping.
- **Nothing is written back to the note.** The only thing that rewrites a block is `set_frontmatter`, which serialises the mapping the parse produced — still a float — so `x: .nan` stays `x: .nan` byte for byte. A scenario pins exactly that.

### D10b — One title rule: the indexer's, plus the non-finite exception

"The three surfaces agree" is not a rule until one of them is named as the rule. **The canonical behaviour is the indexer's `_note_title` as it stands today** — `str(sanitised_value or filename_stem)[:512]`, where the sanitisation stringifies non-string mapping keys and non-JSON scalars *inside* a container before the outer `str()` — **with exactly one exception: a non-finite float renders as its canonical YAML token.** `read_note` and the control panel adopt that rule; the indexer keeps it.

The indexer's is the right one to standardise on because it is already the value `keyword_search` results, `list_notes`, `get_recent` and the panel's lists show, it is bounded to the column width, and it is the only one of the three that has survived a titling incident (#126). The alternative — teaching the indexer `read_note`'s rule — would re-key `notes_metadata.title` for every note with a non-scalar title and change search output, for no gain.

**The non-NaN differences this creates are behaviour changes, and they are listed rather than discovered.** All of them are on the `read_note` / panel side; the indexed title is unchanged except for the non-finite case.

| Frontmatter `title:` | `read_note` today | Indexer today | All three, after |
| --- | --- | --- | --- |
| `Plain string` | `Plain string` | `Plain string` | `Plain string` |
| `2026-08-25` (a date) | `2026-08-25` | `2026-08-25` | `2026-08-25` |
| `[2026-08-25]` (date in a list) | `[datetime.date(2026, 8, 25)]` | `['2026-08-25']` | `['2026-08-25']` |
| `{1: a}` (non-string key) | `{1: 'a'}` | `{'1': 'a'}` | `{'1': 'a'}` |
| `[a, b]` | `['a', 'b']` | `['a', 'b']` | `['a', 'b']` |
| a 600-character string | all 600 characters | first 512 | **first 512** |
| `5` | `5` | `5` | `5` |
| `0`, `false`, `""`, `[]` (falsy) | the filename stem | the filename stem | the filename stem |
| `.nan` / `.inf` / `-.inf` | `nan` / `inf` / `-inf` | `nan` / `inf` / `-inf` | **`.nan` / `.inf` / `-.inf`** |

Three of those rows are real changes to what `read_note` returns — the nested date, the non-string key, and the 512-character bound — and each is a case where `read_note` was showing a Python `repr` or an unbounded value where every other surface showed something shorter and more honest. The truncation in particular means a note whose title is a whole paragraph now reports the same 512 characters everywhere; the `title` field's metadata budget is unaffected by this and still applies on top.

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
| L11 | Backward compatibility is claimed for **mutation and conflict semantics only**. Success strings change: every publishing write now appends its `content_hash`, and `move_note`'s success line reports the destination's. Refusal prose is unchanged; the typed sentinel is appended to it. | The result strings are prose that has never been a contract, and the new field is additive at the end; any client asserting on the whole string will see it. Stated so nobody reads "no client is broken" as covering exact output text. |
| L12 | `read_file`'s base64 header renders the path as a **quoted JSON string** for every path, not only for the ones that would break the header, so that header's output changes for ordinary paths too. | A conditional escape leaves the reader unable to tell which form it is looking at, which is how a forgeable envelope survives review. The body is unaffected. |
| L13 | Adopting the indexer's title rule changes what `read_note` and the panel show for three non-NaN cases (a date inside a container, a non-string mapping key, and a title over 512 characters). | Listed in D10b with expected outputs and pinned by tests. Each is a case where `read_note` was returning a Python `repr` or an unbounded string where every other surface already showed the shorter, more honest form. |
| L15 | A `rewrite_links=True` move whose own rewrite loses the in-call conflict reports **no** hash, so the agent must re-read the destination before it can guard its next write there. | The destination holds a third writer's bytes that this call never read; any hash the server could name would be one it cannot stand behind. |
| L16 | A file over the tool's read cap cannot be guarded at all, and a successful call on one reports no `content_hash`. | Hashing is a linear read, and the caps exist to bound exactly that. An unguarded call on such a file is unaffected — it behaves as it does today — so nothing that works now stops working. |
| L14 | A coercion collision in the **read view** costs the whole `frontmatter` view (omitted with a duplicate-key reason), not just the colliding key. | #149's rule: a partial view is indistinguishable from a complete one. The JSONB side, which cannot report a loss, takes first-key-wins instead. |

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
- **O9 — One canonical spelling for non-finite numbers everywhere** (`.nan` / `.inf` / `-.inf`), including titles and mapping keys, at the cost of changing the title of any note that already has one.
- **O10 — `delete_file` is in scope, both modes** (revised: excluded in the first draft, which contradicted this change's own claim to cover every content-destroying path).
- **O11 — The canonical title rule is the indexer's**, not `read_note`'s, plus the non-finite exception; the three resulting `read_note` behaviour changes are accepted and listed (D10b, L13).
- **O12 — The read view keeps omit-whole-view on a coercion-induced key collision** rather than first-key-wins, deviating from the round-2 suggestion for the reason in D10 and L14. The JSONB side takes first-key-wins, where no loss can be reported.
- **O13 — The base64 header's path is JSON-quoted uniformly**, changing that line for every path (L12).

## Migration Plan

None. No schema change, no new column, no data migration, no `make test-schema` (nothing carries a migration). `make db-check` after deploy as usual. One new setting, `WRITE_PRECONDITION_REQUIRED`, default false, so the deploy is a no-op for every existing client. Rollback is a code revert: `expected_hash` is optional everywhere, so a client that started sending it loses the guard, and `content_hash` fields disappear from read responses. The #154 half is pure code — but a tenant already wedged by a `.nan` note only recovers *after* the deploy, on the next pass, and any such note's indexed title changes from `nan` to `.nan` at the same time.

## Open Questions

- None blocking. Whether an agent ever needs bulk hashes (`list_files`) is left to observation.
