## ADDED Requirements

### Requirement: A non-finite frontmatter number never fails an index pass

The indexer SHALL store a non-finite YAML float from a note's frontmatter as the YAML token the note's bytes use — `.nan`, `.inf`, or `-.inf` — in the `notes_metadata.frontmatter` JSONB column, and a note carrying one SHALL NOT be able to abort, stall, or repeatedly retry an index pass.

`NaN`, `Infinity` and `-Infinity` are not valid JSON and PostgreSQL's `jsonb` parser rejects them, so a float that reaches the column unconverted raises inside the batch upsert. That batch has no per-note retreat: the pass's single transaction aborts, nothing commits, no note's `content_hash` advances, and every subsequent tick retries the same fatal batch — one note taking indexing down for the whole owner. The conversion SHALL therefore happen at the indexer's own JSON boundary (the sanitisation applied to a parsed frontmatter mapping before it is written), which is the same boundary that already stringifies dates and non-string keys.

The conversion SHALL use YAML's spelling rather than Python's (`nan`, `inf`, `-inf`), so that the indexed value, the structured read view and the note's own bytes agree, and so `keyword_search(frontmatter=…)` matches the token a person would write.

The shared frontmatter representability boundary SHALL NOT be changed to remove or coerce non-finite floats: it drops only what nothing can render, both Python and YAML render these, and the parsed mapping is what `set_frontmatter` re-serialises — a coerced string there would rewrite the note's own bytes as a side effect of an unrelated key.

#### Scenario: A note with a non-finite frontmatter number indexes

- **WHEN** a note whose frontmatter contains `x: .nan` is discovered by an index pass
- **THEN** the pass SHALL complete, the note SHALL be upserted with `frontmatter` carrying `x` as the string `.nan`, and every other note in the same batch SHALL be committed

#### Scenario: One such note cannot wedge the pass

- **WHEN** a vault contains a note with `a: .inf` and `b: -.inf` in its frontmatter
- **THEN** consecutive index passes SHALL each complete, the note's `content_hash` SHALL advance normally, and no pass SHALL raise on the JSONB write

#### Scenario: The note's own bytes are never rewritten

- **WHEN** `set_frontmatter` sets an unrelated key on a note whose frontmatter contains `x: .nan`
- **THEN** the published block SHALL still contain `x: .nan` byte-identically, and no coerced string form SHALL appear in the note
