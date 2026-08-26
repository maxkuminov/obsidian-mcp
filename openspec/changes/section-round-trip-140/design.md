# Design: section read/write parity (#140)

## The defect, precisely

`_scan_headings` records `line_end` from the heading regex's match end. The
regex is

```
(?:\A|(?<=\n)|(?<=\r))(#{1,6})[^\S\r\n]+([^\r\n]+?)\s*(?=\r|\n|\Z)
```

and that final `\s*` is **not** restricted to horizontal whitespace. `\s`
includes `\n` and `\r`, and the lookahead only requires that the run *end* at a
line boundary — so the run happily crosses as many blank lines as it likes and
stops before the last terminator it can. `line_end`, documented as "byte pos at
end of heading text, before any trailing newline", is therefore nothing of the
sort whenever a heading is followed by whitespace-only lines.

`_section_body_span` then does the right thing with a wrong input: it steps over
one terminator from `line_end` and calls that `body_start`. Read and write split
at that point:

| | span | consequence |
| --- | --- | --- |
| `extract_section` (read) | `line_start … body_end` | sees everything |
| `replace_section` (write) | `body_start … body_end` | writes from after the swallowed run |

Everything the trailing `\s*` swallowed is **readable but unwritable**. That gap
is the bug; the two reported symptoms are two ways of filling it.

### Symptom 1 — a fenced block, via the masker

`_scan_headings` scans `mask_code(text)`, which replaces a fenced block with an
equal-length run of **spaces** — internal newlines included, since the fence
regex matches the whole block with `(?s)`. So on

```
# A
```<fence>
## Hidden
text
```<fence>
# B
```

the masked text the regex actually sees is `# A\n` + 24 spaces + `\n# B\nb\n`.
The trailing `\s*` runs from after `A` straight through the newline and the
entire masked block, stopping just before the `\n` that precedes `# B`.
`line_end` = 26, `body_start` = 27, `body_end` = 27 — an **empty** body span
pointing at the next heading. `extract_section` still returns the fence, because
it starts from `line_start`.

The masker is not at fault and must not change: it is offset-stable by
construction (a test in `test_issue_128_section_mode_frontmatter.py` pins that),
and every consumer depends on positions mapping back into unmasked text. Only
the heading regex's willingness to consume the mask is at fault.

### Symptom 2 — a blank line

`# A\n\nbody\n`: the run takes one `\n`, the lookahead is satisfied by the
second. `line_end` sits after the first newline, `body_start` after the second,
so `body` is `body\n` while read returns `# A\n\nbody\n`. Write back the part
after the first line (`\nbody\n`) and the blank line is emitted again on top of
the retained one. It compounds, because the *next* read returns the larger
prefix: measured growth is +1, +2, +4 blank lines over three round trips.

## The fix

Narrow the trailing run to horizontal whitespace:

```
(?:\A|(?<=\n)|(?<=\r))(#{1,6})[^\S\r\n]+([^\r\n]+?)[^\S\r\n]*(?=\r|\n|\Z)
```

This makes `line_end` mean what its docstring already claims, which restores the
invariant the two spans need: `body_start` is the first byte of the line after
the heading line, and `extract_section` returns `heading_line + terminator +
text[body_start:body_end]`. Read minus its first line **is** the write span, by
construction rather than by coincidence.

Note the asymmetry with the *separator* class earlier in the pattern, which is
already `[^\S\r\n]+` and was deliberately widened from `[ \t]` (see the comment
in `vault.py`, and `docs/architecture/vault-tools.md`) so an NBSP after the `#`
marker still separates. Narrowing the *trailing* run does not touch that: the
captured heading text is `([^\r\n]+?)` either way, and `.strip()` is applied to
it, so heading text, ordinals, depth and `line_start` are all bit-identical
before and after. **No selector changes meaning.** That is what makes this a
byte-level change to writes and not a re-addressing of anyone's notes.

## Why not the alternatives

**Skip the blank run on both sides** (read hides it too) would keep today's
write bytes exactly — the blank separator would always survive — at the cost of
`read_note(section=…)` no longer reproducing the note's bytes, plus a second
rule ("how many blank lines are separator, how many are body?") that a body
legitimately beginning with blank lines cannot express. Rejected: this codebase's
expensive failure is a *silently wrong read*, and a read that hides bytes to
protect a write is that.

**Compensate in `replace_section`** — re-emit a blank line when the replaced
body had one and the new content does not — was rejected as unspecifiable. It
guesses intent from content shape, which three audit rounds in #128 established
this tool must not do.

**Fix only the fence case** would leave the accumulation bug open and, worse,
leave the two spans still defined differently, so the next shape that lands in
the gap becomes the third bug in this family.

## The regex is necessary but not sufficient

Three things the first draft of this design got wrong, found by the pre-implementation
audit and verified against the current helpers.

### 1. `read_note` does not return raw section text

`read_note` builds every response as an envelope — `# <title>`, `**Path:**`,
optional `**Tags:**` and `**Frontmatter:**` — then `"\n---\n"` and the selected
content (`src/mcp_server/tools.py`, the `parts` list). So the response's first
line is the *title*, not the heading. "Strip the first line and write it back",
the obvious phrasing and the one the first draft used, makes an agent write

```
**Path:** `n.md`

---
# A
```

into the note. That is precisely the destructive-write class this repo exists to
avoid, and the spec would have instructed it.

The contract is therefore stated over the **selected-content portion**: the text
after the response's first `\n---\n`, minus its first line. Both docstrings must
carry that rule verbatim, and the end-to-end check must perform that extraction
against a real MCP response rather than against the pure helper.

### 2. `replace_section` inserts separators unconditionally

Independent of the regex, `replace_section` prepends a newline when the retained
prefix does not end in one, and appends one when a following heading exists and
the body does not end in a terminator. Both fire even when `new_body` is `""`.
Measured with the narrowed regex in place:

| note | round trip result | stable |
| --- | --- | --- |
| `# A\n# B\nb\n` | `# A\n\n# B\nb\n` | no |
| `# A` | `# A\n` | no |

So a section with no body — two consecutive headings, the commonest degenerate
shape in an outline-heavy note — still accumulates a blank line per round trip.
Both insertions become conditional on a non-empty body. The non-empty cases that
issue #5 pinned (`# Notes` at EOF, replaced with `- item`) are untouched.

### 3. The masker's fence grammar is narrower than CommonMark

`_FENCE_RE` requires a column-zero opener and a closer of *exactly* the same
length. CommonMark allows up to three spaces of indentation and a closer at
least as long as the opener. So `# A\n   ```\n# Hidden\n…` leaves `# Hidden`
visible to the scanner, and a section write there replaces only the opening
fence — orphaning the code and leaving the closer.

That is a genuine destructive-write hazard, and it is **not this change's**.
Measured before and after the narrowing, on both the indented-fence and
longer-closer shapes, the bytes written are identical. Widening the masker would
change which lines count as headings, which shifts every `#N` ordinal on an
affected note — a re-addressing break strictly larger than this one, and one
that must not ride along on it. It gets its own issue; this spec says "fenced
code block *as recognised by the shared masker*" rather than claiming coverage
the code does not have.

## The accepted cost

`edit_note(section=S, content=B)` now replaces the section's entire body, so
anything the caller does not resend is gone. This is the declared break, and it
is **destructive, not cosmetic** — the first draft of this design called it
cosmetic and was wrong:

- A blank separator that used to survive is lost unless `content` includes it.
- **A fenced code block directly under the heading is deleted.** On
  `# A\n```\nimportant\n```\nold\n`, `edit_note(section="A", content="new")`
  previously kept the block and replaced only `old`; it now yields `# A\nnew\n`.
  Leaving the block behind *was* the duplication bug, so replacing it is correct
  — but a caller that does not round-trip loses content, and the docstrings must
  say so rather than implying only whitespace is at stake.

What bounds it:

- No note is rewritten until someone writes to that section.
- The read→modify→write path, the one an agent actually takes, comes out
  strictly better: it now preserves what it previously duplicated.
- It is discoverable from the docstrings, which state the extraction rule, the
  whole-body replacement, and the LF qualification.

## Declared residual: newline dialect

`read_note` applies universal-newline translation before the caller ever sees a
section; `edit_note` reads and rewrites raw bytes. A CRLF note's section round
trip therefore rewrites *that section's* terminators as LF and leaves everything
else CRLF. Measured: `# A\r\nold\r\n# B\r\nkeep\r\n` →
`# A\r\nold\n# B\r\nkeep\r\n`. Content is preserved; bytes are not.

This is the section-mode instance of the whole-note residual #128 already
declared and accepted. It is not fixed here — normalising on write would rewrite
terminators the caller never touched — but the byte-identity guarantee is scoped
to LF-bodied notes and both docstrings say so.

## Verification shape

The reproductions belong in a dedicated `tests/test_issue_140_section_round_trip.py`
exercising the pure helpers (no DB, no vault), plus the differential property
that names the contract directly:

> for every heading ordinal in a note, `replace_section(text, "#N",
> extract_section(text, "#N") minus its first line)` returns `text` unchanged.

That property is the spec requirement in executable form, and it must hold over
a corpus that includes: blank lines after headings, fenced blocks directly under
headings (including ones containing `#` lines), inline code, EOF headings with
and without a trailing newline, headings with trailing spaces, CRLF notes, lone-CR
notes, and notes carrying a valid frontmatter block (where section mode operates
on the stripped body per #128).
