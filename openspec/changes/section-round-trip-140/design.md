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

## The accepted cost

`edit_note(section=S, content=B)` where `B` does not itself begin with a newline
now loses a blank separator that used to survive. This is the declared break.
Three things bound it:

- It is cosmetic — no content is lost, and no note is rewritten until someone
  writes to that section.
- The read→modify→write path, which is the one an agent actually takes, comes
  out *better*: it now preserves the separator, which it previously duplicated.
- It is discoverable from the docstrings, which state that `content` is the
  body exactly as `read_note(section=…)` returns it below the heading line.

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
