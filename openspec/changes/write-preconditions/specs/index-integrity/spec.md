## ADDED Requirements

### Requirement: A non-finite frontmatter number never fails an index pass

The indexer SHALL store a non-finite YAML float from a note's frontmatter as the canonical YAML token — `.nan`, `.inf`, or `-.inf` — in the `notes_metadata.frontmatter` JSONB column, and a note carrying one SHALL NOT be able to abort, stall, or repeatedly retry an index pass.

`NaN`, `Infinity` and `-Infinity` are not valid JSON and PostgreSQL's `jsonb` parser rejects them, so a float that reaches the column unconverted raises inside the batch upsert. That batch has no per-note retreat: the pass's single transaction aborts, nothing commits, no note's `content_hash` advances, and every subsequent tick retries the same fatal batch — one note taking indexing down for the whole owner. The conversion SHALL therefore happen at the indexer's own JSON boundary — the sanitisation applied to a parsed frontmatter mapping before it is written — which is the same boundary that already stringifies dates and non-string keys.

The token SHALL be the canonical lowercase form whatever spelling the note used (`.NaN`, `.INF`, `+.inf` and the rest all load to the same float, and the parse preserves none of the spelling), and it SHALL be YAML's spelling rather than Python's `nan` / `inf`, so that the indexed value, the note's own frontmatter and every tool that displays it agree, and so `keyword_search(frontmatter=…)` matches the token a person would write.

The coercion SHALL apply to mapping **keys** as well as values, since the sanitiser stringifies non-string keys on the same walk. When two keys collide after coercion, **the first key in document order SHALL win**, stated as a rule rather than left as an accident of iteration order — today's dict comprehension silently keeps the *last*. The index has no channel through which to report the loss and SHALL NOT fail the pass for it; a deterministic, documented winner is the available remedy.

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

#### Scenario: A non-finite mapping key is stored canonically, first key winning

- **WHEN** a note's frontmatter maps `.nan: 1` and, after it, `".nan": 2`
- **THEN** the stored JSONB object SHALL carry the key `.nan` with the value from the **first** of the two, and the pass SHALL complete

#### Scenario: The note's own bytes are never rewritten

- **WHEN** `set_frontmatter` sets an unrelated key on a note whose frontmatter contains `x: .nan`
- **THEN** the published block SHALL still contain `x: .nan` byte-identically, and no coerced string form SHALL appear in the note

### Requirement: One title normalization is shared by every consumer that shows a title

The coercion that turns a frontmatter `title` into a displayable string SHALL be a single shared rule applied identically by the indexer's `notes_metadata.title`, by the read path that serves `read_note`, and by the control panel's note viewer.

**That rule SHALL be the indexer's present `_note_title` behaviour** — the sanitised value, falling back to the filename stem when it is falsy, rendered with `str()` and bounded to 512 characters, where the sanitisation stringifies non-string mapping keys and non-JSON scalars *inside* a container before the outer rendering — **with exactly one exception: a non-finite number SHALL render as its canonical YAML token.** The indexer's is the rule to standardise on because it is already the value search results, listings and the panel's lists show, it is bounded to the column's width, and it is the one of the three that a titling incident has already hardened.

The indexer's JSONB sanitisation and its title coercion SHALL be separate functions over the shared token helper rather than one function whose return value silently answers both questions — "what may this value become in a JSONB document?" and "what is this note called?" — because today one function decides both, so a change made for the column silently re-keys titles.

Adopting it changes what the read path and the panel show in three cases besides the non-finite one, and those changes SHALL be stated with their expected outputs rather than discovered: a date inside a container renders as the stringified element (`['2026-08-25']`, not a Python `repr` of a date object); a non-string mapping key renders stringified (`{'1': 'a'}`); and a title longer than 512 characters is bounded to its first 512. A date at the top level, a list of strings, a numeric title, and every falsy title (`0`, `false`, an empty string, an empty list — all of which fall back to the filename stem) SHALL be unchanged.

#### Scenario: A non-finite title agrees across tools

- **WHEN** a note's frontmatter is `title: .nan` and the note is indexed
- **THEN** `notes_metadata.title`, the `title` field of `read_note`'s response, and the title the control panel shows SHALL all be `.nan`

#### Scenario: A date inside a container

- **WHEN** a note's frontmatter is `title: [2026-08-25]`
- **THEN** all three surfaces SHALL show `['2026-08-25']`

#### Scenario: A non-string mapping key in a title

- **WHEN** a note's frontmatter title is a mapping with the non-string key `1`
- **THEN** all three surfaces SHALL show the key stringified, as `{'1': 'a'}` for the value `a`

#### Scenario: A title longer than the column

- **WHEN** a note's frontmatter title is a 600-character string
- **THEN** all three surfaces SHALL show its first 512 characters

#### Scenario: Ordinary and falsy titles are unaffected

- **WHEN** notes carry a plain string title, a top-level date, a list of strings, a numeric title, and each falsy title (`0`, `false`, `""`, `[]`)
- **THEN** every one of those SHALL render exactly as the indexer renders it today, with each falsy title falling back to the filename stem
