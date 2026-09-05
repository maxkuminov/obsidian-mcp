## ADDED Requirements

### Requirement: A non-finite frontmatter number never fails an index pass

The indexer SHALL store a non-finite YAML float from a note's frontmatter as the canonical YAML token — `.nan`, `.inf`, or `-.inf` — in the `notes_metadata.frontmatter` JSONB column, and a note carrying one SHALL NOT be able to abort, stall, or repeatedly retry an index pass.

`NaN`, `Infinity` and `-Infinity` are not valid JSON and PostgreSQL's `jsonb` parser rejects them, so a float that reaches the column unconverted raises inside the batch upsert. That batch has no per-note retreat: the pass's single transaction aborts, nothing commits, no note's `content_hash` advances, and every subsequent tick retries the same fatal batch — one note taking indexing down for the whole owner. The conversion SHALL therefore happen at the indexer's own JSON boundary — the sanitisation applied to a parsed frontmatter mapping before it is written — which is the same boundary that already stringifies dates and non-string keys.

The token SHALL be the canonical lowercase form whatever spelling the note used (`.NaN`, `.INF`, `+.inf` and the rest all load to the same float, and the parse preserves none of the spelling), and it SHALL be YAML's spelling rather than Python's `nan` / `inf`, so that the indexed value, the note's own frontmatter and every tool that displays it agree, and so `keyword_search(frontmatter=…)` matches the token a person would write.

The shared frontmatter representability boundary SHALL NOT be changed to remove or coerce non-finite floats: it drops only what nothing can render, both Python and YAML render these, and the parsed mapping is what `set_frontmatter` re-serialises — a coerced string there would rewrite the note's own bytes as a side effect of an unrelated key.

#### Scenario: A note with a non-finite frontmatter number indexes

- **WHEN** a note whose frontmatter contains `x: .nan` is discovered by an index pass
- **THEN** the pass SHALL complete, the note SHALL be upserted with `frontmatter` carrying `x` as the string `.nan`, and every other note in the same batch SHALL be committed

#### Scenario: One such note cannot wedge the index

- **WHEN** a vault contains a note with `a: .inf` and `b: -.inf` in its frontmatter, that note's body is edited between two index passes, and both passes run
- **THEN** both passes SHALL complete, the note's stored `content_hash` SHALL advance to the hash of the edited note, and neither pass SHALL raise on the JSONB write

#### Scenario: An alternate spelling is stored canonically

- **WHEN** a note's frontmatter contains `x: .NaN` and `y: +.inf`
- **THEN** the stored JSONB values SHALL be `.nan` and `.inf`

#### Scenario: The note's own bytes are never rewritten

- **WHEN** `set_frontmatter` sets an unrelated key on a note whose frontmatter contains `x: .nan`
- **THEN** the published block SHALL still contain `x: .nan` byte-identically, and no coerced string form SHALL appear in the note

### Requirement: One title normalization is shared by every consumer that shows a title

The coercion that turns a frontmatter `title` into a displayable string SHALL be a single shared rule applied identically by the indexer's `notes_metadata.title`, by the read path that serves `read_note`, and by the control panel's note viewer, and it SHALL render a non-finite number as the same canonical YAML token those consumers store and display everywhere else.

The indexer's JSONB sanitisation and its title coercion SHALL be separate functions over that shared rule rather than one function whose return value silently answers both questions: they answer different questions — "what may this value become in a JSONB document?" and "what is this note called?" — and today one function decides both, so a change made for the column silently re-keys titles. A note titled with a non-finite number SHALL therefore be called the same thing in search results, in a read, and in the panel.

#### Scenario: A non-finite title agrees across tools

- **WHEN** a note's frontmatter is `title: .nan` and the note is indexed
- **THEN** `notes_metadata.title`, the `title` field of `read_note`'s response, and the title the control panel shows SHALL all be `.nan`

#### Scenario: Ordinary titles are unaffected

- **WHEN** notes carry string, date, list and numeric titles
- **THEN** every one of those titles SHALL be byte-identical to what the current rule produces
