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

### Requirement: The frontmatter JSON view renders non-finite numbers as their YAML tokens and discloses the coercion

The `frontmatter` JSON view SHALL render a non-finite YAML float as the YAML token the note's own bytes use — `.nan`, `.inf`, or `-.inf` — and SHALL record that coercion in the server-controlled `metadata_omissions` list with its own reason code, naming the `frontmatter` field and pointing at `frontmatter_yaml` for the note's verbatim bytes.

`NaN`, `Infinity` and `-Infinity` are not JSON, and neither is Python's `nan` / `inf` spelling a form the note contains. Rendering the YAML token keeps the read view, the indexed value and the note's bytes in agreement. The view SHALL NOT be omitted for this reason alone: a non-finite number is renderable, unlike the shapes that force an omission, and dropping the whole view would hide an otherwise faithful mapping. `frontmatter_yaml` is unaffected and remains the authoritative representation.

The parsed frontmatter mapping SHALL keep the float itself; this coercion is a property of the JSON view only, so that no write path serialises the coerced string back into a note.

#### Scenario: A note with a non-finite frontmatter number reads successfully

- **WHEN** a note's frontmatter contains `x: .nan` and the note is read
- **THEN** the read SHALL succeed, `frontmatter` SHALL carry `x` as the string `.nan`, `frontmatter_yaml` SHALL carry the block verbatim, and `metadata_omissions` SHALL record the coercion

#### Scenario: Both renderings agree

- **WHEN** the same response is rendered as structured content and as the JSON text block
- **THEN** both SHALL carry the same coerced value, and neither SHALL emit the non-RFC tokens `NaN`, `Infinity` or `-Infinity`, and no protocol error SHALL be raised

#### Scenario: Positive and negative infinity are distinguishable

- **WHEN** a note's frontmatter contains `a: .inf` and `b: -.inf`
- **THEN** the view SHALL render them as `.inf` and `-.inf` respectively
