## ADDED Requirements

### Requirement: read_note returns the note's content hash

`read_note` SHALL return a `content_hash` field carrying the digest defined by the `vault-write` capability's "The write-precondition digest is defined in exactly one place" requirement — the whole file's raw bytes — on every successful read.

The value SHALL be the **whole file's** hash in every case: for a section read, for a windowed or truncated read, and for a read whose `limit` lowered the content cap. It describes the file, never the returned selection, so that the token a caller hands to `edit_note(expected_hash=…)` means the same thing whatever it read.

The hash SHALL be computed from the **same read** that produced the response. `read_note` SHALL NOT re-open or re-read the note to serve this field: two reads of one note can disagree, and a hash describing bytes other than the ones in the response is worse than no hash at all.

The field is server-controlled and of fixed length. It SHALL NOT be dropped under metadata-budget pressure and SHALL NOT participate in the metadata drop order, because dropping it silently disables the caller's only precondition on exactly the notes large enough to be worth guarding; it SHALL instead be accounted for as a fixed allocation alongside `path` in the response's worst-case arithmetic.

A response that carries an `error` need not carry a hash.

#### Scenario: A whole-note read carries the file's hash

- **WHEN** `read_note(path)` returns a complete, unwindowed read
- **THEN** the response SHALL carry `content_hash` equal to the digest of the file's raw bytes

#### Scenario: A section read carries the whole file's hash

- **WHEN** `read_note(path, section="Tasks")` returns a section body
- **THEN** `content_hash` SHALL be the whole file's, identical to the value a whole-note read of the same unchanged file returns

#### Scenario: A truncated read carries the whole file's hash

- **WHEN** `read_note(path, limit=…)` returns a truncated first window with `truncated` true
- **THEN** `content_hash` SHALL be the whole file's, identical to the value the untruncated read of the same unchanged file returns

#### Scenario: The hash survives budget pressure

- **WHEN** a note's metadata exceeds the metadata budget and fields are dropped in the established priority order
- **THEN** `content_hash` SHALL still be present, and its omission SHALL never appear in `metadata_omissions`

#### Scenario: The hash matches the file, not the body

- **WHEN** the note carries a frontmatter block, or uses CRLF terminators, or both
- **THEN** `content_hash` SHALL still equal the digest of the file's raw bytes, and feeding it to `edit_note(expected_hash=…)` on the unchanged file SHALL succeed

### Requirement: The frontmatter JSON view renders non-finite numbers as canonical YAML tokens and discloses the coercion

The `frontmatter` JSON view SHALL render a non-finite YAML float as the canonical YAML token — `.nan`, `.inf`, or `-.inf` — and SHALL record that coercion in the server-controlled `metadata_coercions` list, naming the `frontmatter` field, a stable reason code, and where to read the value as the note spells it.

`NaN`, `Infinity` and `-Infinity` are not JSON, and Python's `nan` / `inf` spelling is not a form any note contains. Rendering the canonical YAML token keeps the read view, the indexed value and the note's own frontmatter in agreement.

**The token is canonical regardless of how the note spelled it.** YAML accepts `.nan`, `.NaN`, `.NAN`, `.inf`, `.Inf`, `.INF`, `+.inf` and their negatives, and the parse preserves none of that — by the time any consumer sees the value it is a float. The view therefore always emits the lowercase canonical form, and that lossiness SHALL be stated rather than left to be discovered; `frontmatter_yaml` carries the note's own spelling, LF-normalized.

The view SHALL NOT be omitted for this reason alone: a non-finite number is renderable, unlike the shapes that force an omission, and dropping a whole block's view over one value would hide an otherwise faithful mapping. The coercion SHALL NOT be reported through `metadata_omissions`, whose contract is that an entry names a field dropped whole.

The parsed frontmatter mapping SHALL keep the float itself; this coercion is a property of the rendered view only, so that no write path can serialise the coerced string back into a note.

#### Scenario: A note with a non-finite frontmatter number reads successfully

- **WHEN** a note's frontmatter contains `x: .nan` and the note is read
- **THEN** the read SHALL succeed, `frontmatter` SHALL carry `x` as the string `.nan`, `frontmatter_yaml` SHALL carry the block, and `metadata_coercions` SHALL record the coercion
- **AND** `metadata_omissions` SHALL NOT gain an entry for it, because nothing was dropped

#### Scenario: Both renderings agree

- **WHEN** the same response is rendered as structured content and as the JSON text block
- **THEN** both SHALL carry the same coerced value, neither SHALL emit the non-RFC tokens `NaN`, `Infinity` or `-Infinity`, and no protocol error SHALL be raised

#### Scenario: Positive and negative infinity are distinguishable

- **WHEN** a note's frontmatter contains `a: .inf` and `b: -.inf`
- **THEN** the view SHALL render them as `.inf` and `-.inf` respectively

#### Scenario: An alternate spelling renders canonically

- **WHEN** a note's frontmatter contains `x: .NaN`, `y: .INF` and `z: +.inf`
- **THEN** the view SHALL render `.nan`, `.inf` and `.inf`, and `frontmatter_yaml` SHALL still carry the note's own spellings

## MODIFIED Requirements

### Requirement: read_note responses are structurally framed

`read_note` SHALL return a structured result — discrete fields for metadata, note content, truncation state, and errors — declared via the tool's MCP output schema and delivered as structured content alongside a JSON-serialized text rendering. No note-controlled value (title, path, tags, frontmatter keys or values, or note text) SHALL be able to alter which field any other value appears in: there SHALL be no delimiter-based envelope whose frame note content could forge. The unstructured text rendering and the structured content SHALL be built from the same already-JSON-safe values, so the two never diverge and recovery of any field from either form is reversible. Fields that are inapplicable to a given response SHALL be absent from both renderings, not `null`.

Every note-controlled field SHALL have an explicit budget: `content` is governed by the existing response-size cap; the outline by its existing independent budget; `path` is always exact — bounded by an admission-time path-length limit (1,024 characters, matching the index's `file_path` column) so the exact value is a fixed allocation, never elided or marked, and path-bearing error messages inherit the same bound; and the remaining metadata fields (`title`, `tags`, `frontmatter_yaml` and its JSON view, `heading`) share a metadata budget also bounded by `MAX_READ_RESPONSE_CHARS`. When the aggregate exceeds that budget, fields SHALL be dropped in a deterministic priority order — the lossy `frontmatter` JSON view first, then `frontmatter_yaml`, then `tags`, then `heading`, then `title` — until the remainder fits; a dropped field is **omitted whole: never truncated in place, never cut short, and never replaced by an in-band textual marker** (a shortened or marked value inside a note-controlled field is indistinguishable from note content — the forgery class this framing exists to end). Every omission SHALL be reported in a separate server-controlled `metadata_omissions` field naming the field, the reason, and how to read the full value (the raw note). Error and notice strings interpolate only bounded values. The worst-case serialized response — the sum of these budgets plus the fixed allocations, doubled for the structured-plus-text duplication and multiplied by JSON string escaping's worst-case six-characters-per-character expansion of note-controlled text — SHALL be stated in the architecture documentation.

**A retained-but-altered value is not an omission, and SHALL NOT be reported as one.** `metadata_omissions` entries name fields the response dropped whole; a value the server rendered in a form the note does not literally contain is a different fact about a field that is still there. The result SHALL therefore carry a second server-controlled list, `metadata_coercions`, of the same shape — the field, a stable reason code, and where to read the value as the note spells it — and each list SHALL be used only for its own case. Both lists are server-authored: no note-controlled value appears in either except through the same bounds the rest of the response applies.

`frontmatter_yaml` is the **authoritative but LF-normalized** representation of the block: it is content-lossless as text, and its terminators are those of the read path rather than the file's, so a caller that needs the block's exact bytes SHALL be directed to `read_file` (whose base64 result also carries the file's `content_hash`) rather than to `frontmatter_yaml`. Nothing in this capability may promise byte-exactness for it.

When more than one failure applies to a call, precedence SHALL be: path resolution (missing note) first, then parameter validation (`offset`, `limit`), then section resolution; exactly one `error` is reported and content-bearing fields are absent.

#### Scenario: A multiline YAML title cannot forge the frame

- **WHEN** a note's frontmatter title is a block scalar containing a line that is exactly `---` (issue #149 reproduction 1) and the note is read with `section="#1"`
- **THEN** the forged line SHALL appear only inside the `title` field's JSON-escaped value, and the `content` field SHALL carry exactly the section body

#### Scenario: A quoted frontmatter key cannot forge the frame

- **WHEN** a note's frontmatter contains a key whose decoded value embeds `\n---\n` (issue #149 reproduction 2)
- **THEN** the key SHALL appear only as JSON-escaped text inside the frontmatter fields, and the `content` field SHALL carry exactly the selected content

#### Scenario: Distinct paths stay distinguishable

- **WHEN** two note paths differ only by a character that a lossy rendering would collapse (e.g. a newline versus a space)
- **THEN** the `path` field SHALL distinguish them exactly

#### Scenario: Any valid frontmatter serializes, and the raw block is authoritative

- **WHEN** a note carries a valid frontmatter block
- **THEN** the response SHALL carry `frontmatter_yaml` — the block's text as the read path sees it, universal-newline-normalized to LF (the declared terminator residual, with `read_file` named as the byte-exact route) with fence lines excluded — as the authoritative representation, and a best-effort JSON view in `frontmatter`; under metadata-budget pressure the block is omitted whole (reported in `metadata_omissions`), never truncated, so the field is content-lossless whenever present
- **AND** the JSON view SHALL be constructed defensively: leaves with no native JSON form (dates, timestamps) become strings; construction is depth- and size-bounded so recursive aliases (`x: &X [*X]`) cannot raise or loop; a parser-accepted value the server cannot serialize — a lone-surrogate escape (`"\uD800"`) — SHALL cost at most the affected fields (view/title/tags omitted via `metadata_omissions`, `frontmatter_yaml` unaffected), and SHALL NOT raise, produce a protocol error, or desynchronize the structured and text renderings; a block the YAML parser cannot even construct (an integer beyond Python's digit limit, a composer-recursion overflow) SHALL be classified as the existing yaml-error defect — the note stays fully readable with the block's bytes in `content`, and structured frontmatter mutation refuses by name — because omitting only the view would hand `set_frontmatter` an empty mapping to merge over a block it never parsed, which is a destructive write; when construction fails, or two YAML keys would collide onto one JSON key (`1:` and `"1":`), the JSON view SHALL be omitted and reported in `metadata_omissions`, with `frontmatter_yaml` still present — unless budget pressure independently drops it, in which case `metadata_omissions` reports both omissions with their distinct reasons
- **AND** the documentation SHALL direct callers that mutate frontmatter to `set_frontmatter` (or the raw block), never to a round trip through the lossy JSON view

#### Scenario: Errors are in-band fields

- **WHEN** `read_note` is invoked on a missing path, with an invalid `offset` or `limit`, or with a selector matching no heading
- **THEN** the result's `error` field SHALL carry the message today's contract requires (identifying the path, the offending value, or the available headings), content-bearing fields SHALL be absent, and the tool SHALL NOT raise

#### Scenario: A coercion is reported apart from an omission

- **WHEN** one response both drops a field whole under budget pressure and renders another field's value in a canonicalized form
- **THEN** the drop SHALL appear in `metadata_omissions` and the canonicalization in `metadata_coercions`, each naming its own field and reason, and neither list SHALL carry the other's entry
