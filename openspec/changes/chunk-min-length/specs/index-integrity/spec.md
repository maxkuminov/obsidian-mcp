## MODIFIED Requirements

### Requirement: The indexer SHALL chunk a note at its ATX headings using the section-addressing scanner, so a chunk's ordinal is the one `read_note` resolves
`embed_note` SHALL split the body it embeds into sections using the same heading scanner that `read_note(section=…)` and `edit_note(section=…)` use to resolve selectors. It SHALL scan the same frontmatter-stripped body, so a line inside fenced or inline code is never a boundary.

**Sections and lengths.**
- Section *k* (k ≥ 1) SHALL be the text of the *k*-th heading in document order, from the line after the heading line up to the next heading of any depth or the end of the body.
- Section 0 SHALL be the text before the first heading, or the whole body when there is none.
- Each section SHALL be cleaned by removing the fence spans the shared recognizer finds over the **whole** body.
- Let `W = CHUNK_SIZE × 4`. The effective minimum SHALL be `m = min(CHUNK_MIN_CHARS, W // 2)`. `CHUNK_MIN_CHARS` is the module constant 200. It is not a setting and not a fingerprint field, so every previously valid `CHUNK_SIZE` remains valid. A change to the constant is a change of chunking format and SHALL bump `CHUNKING_VERSION`.
- A section is *non-empty* when its cleaned text, stripped, is not empty. It is *short* when it is non-empty and its `clen` is below `m`.

**Grouping.** Non-empty sections SHALL be grouped in document order.
- A group opens at a non-empty section. While its text is short and another non-empty section follows, it SHALL extend to that section, and it closes once it reaches `m`.
- A group's text SHALL be the contiguous raw span from its first section's own text to its last section's end, cleaned with the whole-body fence spans. It therefore includes the heading lines of absorbed sections and of empty sections between them.
- At the end of the body, a group that is still short SHALL join the last closed group, if one exists.
- If no closed group exists, the short group SHALL stand alone only when the note's whole cleaned body, heading lines included, is also short. Otherwise the note SHALL be chunked as one group of its whole cleaned body at ordinal 0.
- Heading lines SHALL NOT be part of the text of a group that holds one section.

**One length.** Every length in this requirement SHALL be the *collapsed length* `clen(t)`. That is the code-point length of `t` after every maximal run of the whitespace characters U+0009–U+000D and U+0020 is replaced by one space and leading and trailing spaces are removed. This applies to the minimum, section shortness, cut admissibility, tail balancing and the evaluation harness's `under_min`. The stored `chunk_text` SHALL be the raw window, unstripped, so previews are unaffected. The provider input is defined by the prefix requirement. A window SHALL be stored only when its `clen` is non-zero.

**Windowing.** Each group's text SHALL be windowed with `L = min(200, W // 10)`. A *word start* is a non-whitespace character at the start of the text or after a whitespace character. A *line start* is a word start whose preceding whitespace contains a line break. A cut `c` for a window starting at `a` is *admissible* when `clen(text[a:c]) ≥ m`.
- **Cut.** A window starting at `a` whose limit `a + W` falls before the end of the text SHALL be cut at the first admissible choice of these, in order: the last line start in `(a + W − L, a + W]`; the last word start in that range; `a + W`. Only when `clen(text[a:a+W]) < m`, it SHALL be cut at the first word start after `a + W` that is admissible, or else at the first admissible position. The next window SHALL start at the cut.
- **Overlap.** With overlap above zero, the next start SHALL be `max(cut − overlap × 4, a + 1)`, moved forward to the first word start at or before the cut. It SHALL NOT be moved beyond the cut.
- **Tail balancing.** When the text after a window's cut has `clen < m`, the last two windows SHALL be re-decided over the span from that window's start to the end, with `S` the span's `clen`.
  - When `S ≥ 2m + 1`, the span SHALL be cut into two windows, each with `clen ≥ m`. The cut SHALL be the word start nearest the span's collapsed midpoint (the earlier on a tie) whose two sides both have `clen ≥ m` and raw length at most `W`. If there is none, the cut SHALL be the smallest position at which the left side's `clen` equals `⌊S/2⌋`.
  - When `S ≤ 2m`, the span SHALL be one window.
- **Bounds.** Every chunk SHALL have `clen ≤ W`. Its raw length SHALL be at most `W`, except where no choice above both meets the minimum and stays within `W` raw. In that case the excess SHALL be whitespace only.
- **Minimum.** Every chunk SHALL have `clen ≥ m`, except the only chunk of a note whose whole cleaned body has `clen < m`.
- **Coverage.** Every non-heading cleaned character of a section SHALL be inside at least one chunk.

**Locator.** Each chunk's `section_ordinal` SHALL be the deepest heading ordinal N whose `read_note(section="#N")` body span contains the chunk's raw span, or 0 when none does.
- Its `heading_path` SHALL be that section's enclosing headings, outermost first, each component trimmed and cut at 200 characters with a marked cut. The path is empty for 0.
- Its prefix SHALL be built from that path.
- A chunk drawn from one section's own text SHALL therefore record that section's ordinal.
- Every chunk SHALL be a substring of its locator section's body with the whole-body fence spans removed.

**No sections.** If no section is non-empty while the whole-body chunker would yield a chunk for the cleaned body, the note SHALL be chunked as one group of the whole cleaned body at ordinal 0. A note SHALL therefore yield zero chunks exactly when the pre-#355 chunker yields zero.

**Cap and version.** The per-note chunk cap SHALL count across all groups in document order, and the truncation marker SHALL keep its meaning. These rules are chunking format version 2.

#### Scenario: A heading inside a code fence is not a boundary
- **WHEN** a note contains a fenced block whose content includes a line `# Not a heading`, inside a section headed `## Real` whose text is at least `m`
- **THEN** no section boundary SHALL fall at that line, and every chunk of that section SHALL record the ordinal of `## Real`

#### Scenario: Ordinal parity with section addressing
- **WHEN** any note, with LF, CRLF or lone-CR terminators, with or without a valid or defective frontmatter block, and with or without merged groups, is chunked, and a chunk records ordinal N ≥ 1
- **THEN** `read_note(path, section="#N")` SHALL resolve, and the chunk's text SHALL be a substring of that section's body with the whole-body fence spans removed

#### Scenario: Text before the first heading
- **WHEN** a note has at least `m` characters of prose above its first heading
- **THEN** that prose SHALL be chunked at ordinal 0 with an empty heading path

#### Scenario: A short lead merges into its first subsection under its own locator
- **WHEN** a note holds `## Email Triage`, then the single line `See entries in the Email Triage log`, then `### 2026-10-01` followed by 300 characters of text, at the default `CHUNK_SIZE`
- **THEN** no chunk SHALL have `clen` below 200
- **AND** the first chunk SHALL contain the short line and the `### 2026-10-01` heading line, and SHALL record the ordinal and path of `## Email Triage`

#### Scenario: A short section merges into a following sibling under their common parent
- **WHEN** `# Project` contains `## A` with 40 characters of text, followed by `## B` with 600 characters, at the default `CHUNK_SIZE`
- **THEN** the chunk holding `## A`'s text SHALL record `# Project`'s ordinal and SHALL be contained in that section's body

#### Scenario: A short last section merges backward
- **WHEN** a note's final section holds 30 characters and an earlier section holds 600, at the default `CHUNK_SIZE`
- **THEN** the 30 characters SHALL be the end of the previous group's last chunk, and no chunk SHALL consist of them alone

#### Scenario: An outline with one tiny body
- **WHEN** a note holds sixty headings with empty bodies, followed by a heading whose body is `x`, and its whole cleaned body is longer than `m`
- **THEN** it SHALL be chunked as one group of the whole cleaned body at ordinal 0, and no chunk SHALL consist of `x` alone

#### Scenario: A whole note shorter than the minimum keeps one chunk
- **WHEN** a note's whole cleaned body is shorter than `m` and not empty
- **THEN** exactly one chunk SHALL be stored for it, and the note SHALL NOT be certified as empty

#### Scenario: No four-character tail and no mid-word cut
- **WHEN** a section's cleaned text is 2,048 × 2 + 4 characters of prose ending in `themselves`, at the default `CHUNK_SIZE`
- **THEN** no chunk SHALL equal `lves`, every chunk SHALL have `clen` of at least 200, and no cut SHALL fall inside a word when an admissible word start exists in its lookback

#### Scenario: A hard cut with overlap skips nothing
- **WHEN** `CHUNK_SIZE` is 100 (W = 400), the overlap is 1, and a section's cleaned text is 400 `A`, 20 `B`, one space and 500 `C`
- **THEN** every `B` SHALL be inside a stored chunk, and chunking SHALL terminate

#### Scenario: A merged tail never makes a chunk longer than a window
- **WHEN** a group's text is `W + m − 1` characters and its chunks are embedded under a 300-character prefix
- **THEN** no chunk text SHALL exceed `W` characters, every provider input SHALL be at most `W + 302` characters, and both of the last two chunks SHALL have `clen` of at least `m`

#### Scenario: Tail balancing never strands a prose window (Codex r2, W = 400)
- **WHEN** `CHUNK_SIZE` is 100 (W = 400, m = 200) and a group's text is 161 `A`, one space and 239 `B`
- **THEN** exactly two chunks SHALL be stored: 161 `A`, a space and 38 `B` (raw 200, `clen` 200), then 201 `B` (raw 201, `clen` 201)

#### Scenario: A whitespace-heavy tail at the default window (Codex r2)
- **WHEN** at the default `CHUNK_SIZE` a group's text is 1,800 `A`, 390 spaces and 10 `B`
- **THEN** exactly two chunks SHALL be stored: 905 `A` (raw 905, `clen` 905), then 895 `A`, the 390 spaces and 10 `B` (raw 1,295, `clen` 906)

#### Scenario: Whitespace that defeats the raw bound yields to the minimum
- **WHEN** at the default `CHUNK_SIZE` a group's text is 100 `A`, 3,000 spaces and 100 `B`
- **THEN** no chunk SHALL have `clen` below 200, every chunk SHALL have `clen` of at most `W`, and any chunk longer than `W` raw SHALL exceed it by whitespace only

#### Scenario: A run without whitespace is cut hard
- **WHEN** a section holds a 5,000-character run with no whitespace
- **THEN** it SHALL be cut at the full window width, and chunking SHALL terminate

#### Scenario: Every previously valid chunk size still starts
- **WHEN** a deployment with `CHUNK_SIZE=64`, or `CHUNK_SIZE=1`, starts the version-2 build
- **THEN** startup SHALL proceed, and the effective minimum SHALL be 128, or 2, respectively

#### Scenario: A parent with only subsections
- **WHEN** a `## Parent` heading is followed directly by `### Child` with at least `m` characters of body text
- **THEN** no group SHALL open at `## Parent`, and the child's chunks SHALL record the path `["Parent", "Child"]`

#### Scenario: A long section
- **WHEN** a single section's cleaned text spans three windows and its remainder after the second cut has `clen` of at least `m`
- **THEN** three chunks SHALL be stored, with the same ordinal and heading path and consecutive chunk indexes

#### Scenario: A note of headings only
- **WHEN** a note consists only of heading lines
- **THEN** it SHALL be chunked as one group of the whole cleaned body at ordinal 0, and SHALL NOT be certified as empty

#### Scenario: The cap counts across groups
- **WHEN** a note's groups together yield more than `MAX_CHUNKS_PER_NOTE` chunks
- **THEN** the first `MAX_CHUNKS_PER_NOTE` in document order SHALL be stored and the note SHALL be marked truncated

### Requirement: The text sent to the provider for a chunk SHALL be a bounded title-and-heading prefix followed by the chunk, and the stored chunk text SHALL stay un-prefixed
For every chunk, the input `embed_note` sends to the provider SHALL be `embed_prefix + "\n\n" + embedded_text(chunk_text)`, built by one function. `embedded_text` SHALL replace every maximal run of more than 8 whitespace characters with one space, using the same six ASCII whitespace characters as the collapsed length (U+0009–U+000D, U+0020). Runs of 8 or fewer characters, and every other character, SHALL be kept. `embedded_text` is the version-2 entry of the frozen formatter table.

`embed_prefix` SHALL be built as follows:
- The note's title (the shared title rule), followed by ` > ` and each heading-path component.
- Each component SHALL be whitespace-collapsed and cut at 80 characters with a marked cut.
- Then a line `tags: ` followed by the note's tags, comma-separated, in stored order, only when the whole prefix including it is at most 300 characters.
- When the breadcrumb alone exceeds 300 characters, the outermost headings after the title SHALL be replaced by one `…` component until it fits. The title and the innermost heading SHALL always be kept.

The row SHALL store `embed_prefix` and the raw, un-prefixed `chunk_text`, which is never collapsed. Previews SHALL be drawn from `chunk_text` only. `embedded_input_digest` SHALL be computed over the provider inputs as sent.

**The bound this gives.** A chunk of raw length at most `W` has embedded text of at most `W`, so its provider input stays within #355's maximum of `W + 302`. The one exception is a chunk that exceeds `W` raw by whitespace (the chunking requirement's bound). Its embedded text is at most `clen + 7r`, where `r` is the number of whitespace runs of 2 to 8 characters it holds. Because `clen ≤ W`, its input exceeds #355's maximum by at most `7r` characters.

#### Scenario: Prefix shape
- **WHEN** a chunk of section `## Plan` under `# Project` in a note titled `Alpha` tagged `work` is embedded
- **THEN** the provider input SHALL be `Alpha > Project > Plan\ntags: work\n\n` followed by the chunk's embedded text, and the stored `chunk_text` SHALL NOT contain the prefix

#### Scenario: A whitespace-heavy chunk is sent collapsed
- **WHEN** a stored chunk is 100 `A`, 3,000 spaces and 100 `B` under prefix `P`
- **THEN** the provider input SHALL be `P + "\n\n" + 100 A + " " + 100 B`, of length `len(P) + 203`
- **AND** the stored `chunk_text` SHALL still hold the 3,000 spaces

#### Scenario: Ordinary whitespace is kept
- **WHEN** a chunk contains paragraph breaks, an indented list of up to 8 spaces and single spaces only
- **THEN** its embedded text SHALL equal its `chunk_text`

#### Scenario: Tags that do not fit are omitted entirely
- **WHEN** adding the tags line would make the prefix longer than 300 characters
- **THEN** the prefix SHALL contain no tags line

#### Scenario: A deep, long breadcrumb is cut from the outside
- **WHEN** a six-level heading path's breadcrumb exceeds 300 characters
- **THEN** the prefix SHALL keep the title and the innermost heading, SHALL replace outer headings with `…`, and SHALL be at most 300 characters

### Requirement: An unchanged chunk SHALL reuse its stored vector only when that vector provably belongs to the current generation
`embed_note` SHALL reuse a stored vector of the note for a chunk whose **provider input** is byte-identical to the input the stored vector was embedded from. That is, the prefixes are equal and the embedded texts are equal, under the rules below. Only the remaining chunks SHALL be sent to the provider. Reuse SHALL happen only if all of the following hold. Otherwise every chunk SHALL be embedded as before.
- **Lookup transaction.** The lookup of stored `(id, chunk_text, embed_prefix, embedding)` rows, of the note's `embedded_chunking_version` and of the stored embedding fingerprint runs in a read-only transaction that is committed before any provider call.
- **Fingerprint.** The stored embedding fingerprint is present and equal to the current one. An absent fingerprint SHALL disable reuse.
- **Chunking version.** The note's `embedded_chunking_version` is a key of the frozen per-version formatter table (`EMBEDDED_TEXT_FORMATTERS`, which is `{1: identity, 2: collapse runs of more than 8 whitespace characters}`). A NULL stamp is never a key. If the stamp is not a key, reuse SHALL be disabled for the whole note.
- **Same provider input.** The stored row's `embed_prefix` equals the new chunk's prefix. The stored row's embedded text, meaning its `chunk_text` passed through the formatter of the note's **stamped** version, equals the new chunk's embedded text under the current version's formatter. A version-1 row therefore reuses only when its text holds no whitespace run longer than 8 characters. A frozen formatter SHALL NOT be edited, and a golden test of every entry SHALL fail until a changed entry is reviewed. A version that changes the prefix builder SHALL clear the table to its own entry.
- **Prefix present.** A row whose `embed_prefix` is NULL SHALL NOT be reused.
- **Rows survive.** Under the generation lock, after the provider call and before certification, every reused row still exists for this note with the same `chunk_text` and `embed_prefix`. Otherwise the attempt SHALL certify nothing, write nothing and report `GENERATION_MISMATCH`.

A row's `heading_path` and `section_ordinal` SHALL NOT be part of the reuse key. Every re-inserted row SHALL carry the locator of the chunk it now stores. Certification SHALL stamp `CHUNKING_VERSION`, whatever version the reused rows were stored under.

The cardinality check SHALL apply to the chunks actually sent. The note SHALL be certified only when every requested chunk has a vector, whether reused or fresh. After certification, all of the note's rows SHALL be deleted and re-inserted in document order.

A note whose chunks are all reusable SHALL make no provider call. It SHALL NOT be counted as a provider attempt, even when the under-lock check fails and it reports `GENERATION_MISMATCH`, and it SHALL debit no chunk budget.

#### Scenario: An append re-embeds only the new tail
- **WHEN** a note's content is extended so that every earlier chunk's text and prefix are unchanged, and its chunking version is a key of the formatter table
- **THEN** only the new chunks SHALL be sent to the provider and the note SHALL be certified with every chunk present

#### Scenario: No fingerprint, no reuse
- **WHEN** the stored embedding fingerprint is absent or differs from the current one
- **THEN** no stored vector SHALL be reused

#### Scenario: A legacy row is never reused
- **WHEN** a note's stored rows predate migration 034 (NULL `embed_prefix`), or its `embedded_chunking_version` is NULL or not a key of `EMBEDDED_TEXT_FORMATTERS`
- **THEN** every chunk SHALL be sent to the provider

#### Scenario: A version-1 note re-chunked under version 2 reuses its unchanged chunks
- **WHEN** a note stamped version 1 is re-selected under version 2, and one of its single-window sections of at least `CHUNK_MIN_CHARS` yields the same `(embed_prefix, chunk_text)` pair as its stored row
- **THEN** that chunk's stored vector SHALL be reused, only the changed chunks SHALL be sent to the provider, and the note SHALL be stamped version 2

#### Scenario: A version-1 row holding a long whitespace run is not reused
- **WHEN** a note stamped version 1 is re-selected under version 2, and a stored chunk equal to a new chunk contains a run of 12 spaces
- **THEN** that chunk SHALL be sent to the provider, because its version-1 input kept the run and its version-2 input collapses it

#### Scenario: A changed prefix defeats reuse
- **WHEN** a note's tags or title change and its chunk texts do not
- **THEN** no stored vector SHALL be reused

#### Scenario: An inserted heading shifts ordinals without re-embedding
- **WHEN** a heading is inserted above existing sections so that their ordinals shift and their prefixes and texts do not change
- **THEN** their vectors SHALL be reused and the re-inserted rows SHALL carry the new ordinals

#### Scenario: A reset during the provider call defeats reuse
- **WHEN** a reset deletes the note's stored vectors while the provider call for the remaining chunks is in flight
- **THEN** the attempt SHALL certify nothing and write nothing, and SHALL not deadlock with the reset

#### Scenario: Nothing to send
- **WHEN** every chunk of a changed note has a stored row with the same prefix and text, under a chunking version that is a key of the formatter table
- **THEN** no provider call SHALL be made, the note SHALL be certified, and the pass's attempt count and chunk budget SHALL be unchanged

#### Scenario: An all-reuse note loses a reset race without an attempt
- **WHEN** every chunk of a note is reusable and a reset deletes the stored rows between the lookup and the under-lock check
- **THEN** the outcome SHALL be `GENERATION_MISMATCH`, nothing SHALL be written, and the pass's attempt count SHALL be unchanged
