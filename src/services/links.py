"""Wikilink and markdown-link extraction + resolution.

Two-pass parser:
1. `extract_links(content)` strips fenced/inline code blocks, then runs three
   regexes for `[[wikilinks]]`, `![[embeds]]`, and `[label](path.md)` markdown
   links. Returns one `ExtractedLink` per match.
2. `resolve_target(target, source_path, vault_index)` maps the raw target
   string to a note ID using Obsidian-style filename-first resolution with
   same-folder preference.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedLink:
    target: str  # target string with alias and anchor stripped
    link_text: str  # full original text (e.g. "[[Foo|Bar]]")
    kind: str  # "link" | "embed" | "markdown"
    position: int  # byte offset in the (un-stripped) source


# Wikilink: optional `!` for embeds, then `[[Target(#Anchor)?(|Alias)?]]`.
# Target is "anything but ], |, #" so anchors and aliases peel cleanly.
_WIKILINK_RE = re.compile(
    r"(?P<embed>!)?\[\[(?P<target>[^\]\|#\n]+)"
    r"(?:#(?P<anchor>[^\]\|\n]*))?"
    r"(?:\|(?P<alias>[^\]\n]*))?\]\]"
)

# Markdown link: `[text](href.md)`, `[text](href.md#anchor)`, or the
# CommonMark angle-bracket form `[text](<href.md>)`. Href must end in `.md`
# (with optional `#anchor`) — we ignore non-note links here. The href class
# forbids only newlines (not all whitespace), so raw-space note names like
# `My Note.md` and `folder/My Note.md` are captured.
_MDLINK_RE = re.compile(
    r"\[(?P<text>[^\]\n]+)\]\("
    r"(?:<(?P<href_ab>[^>\n]+?\.md)(?:#[^>]*)?>"
    r"|(?P<href>[^)\n]+?\.md)(?:#[^)\n]*)?)\)"
)

# A line ends at LF, CRLF or a lone CR, the same universal-newline rule
# `read_file` applies before the read path ever parses a note and the same one
# the frontmatter partition and the ATX heading scan use. `(?m)^...$` knows
# only `\n`, and this masker feeds the heading scanner, so on a lone-CR note it
# diverged in BOTH directions: a `~~~`-fenced block was never masked, leaving a
# heading inside code selectable by `edit_note(section=…)` while `read_note`
# (which sees the same file normalised) hid it — a section write there replaces
# through the closing fence and deletes it — and inline code, whose `[^`\n]*`
# ran happily across `\r`, joined two lines and masked a REAL heading the read
# side could see. CRLF is matched ahead of the bare `\r` so a terminator is
# never split down the middle.
_LINE_BREAK = r"(?:\r\n|\n|\r)"
_AT_LINE_START = r"(?:\A|(?<=\n)|(?<=\r))"
# Fenced code blocks (``` or ~~~) — the whole block, terminators included.
#
# The closing fence keeps the original `\s*` (NOT a horizontal-only class):
# `\s` matches Unicode whitespace, and greedily running it up to the last
# position a line can end at is what the original `\s*$` did. Narrowing it
# would change which trailing blank lines fall inside the mask on LF and CRLF
# notes, and a heading's `#N` ordinal is exactly what shifts when masking
# changes — the compat break this rewrite must not introduce. Generalising `$`
# to "before any terminator, or end" reproduces it byte for byte on LF and
# CRLF while making it work at all on a lone-CR note.
_FENCE_RE = re.compile(
    rf"(?s){_AT_LINE_START}([`~]{{3,}})[^\r\n]*{_LINE_BREAK}"
    rf".*?{_AT_LINE_START}\1\s*(?={_LINE_BREAK}|\Z)"
)
# Inline code: backtick-delimited runs that don't span a line, in any dialect.
_INLINE_CODE_RE = re.compile(r"`[^`\r\n]*`")


def _mask_code(text: str) -> str:
    """Replace fenced/inline code with same-length whitespace.

    Preserves byte offsets so `position` values in `ExtractedLink` are valid
    against the original content (useful for snippet extraction later), and so
    `_scan_headings` can report positions into the unmasked text. Every
    substitution is exactly as long as what it replaces.
    """
    def _spaces(match: re.Match) -> str:
        return " " * (match.end() - match.start())

    text = _FENCE_RE.sub(_spaces, text)
    text = _INLINE_CODE_RE.sub(_spaces, text)
    return text


def extract_links(content: str) -> list[ExtractedLink]:
    """Extract every wikilink/embed/markdown-link from a note body."""
    masked = _mask_code(content)
    out: list[ExtractedLink] = []

    for m in _WIKILINK_RE.finditer(masked):
        target = m.group("target").strip()
        if not target:
            continue
        kind = "embed" if m.group("embed") else "link"
        out.append(ExtractedLink(
            target=target,
            link_text=m.group(0),
            kind=kind,
            position=m.start(),
        ))

    for m in _MDLINK_RE.finditer(masked):
        href = (m.group("href_ab") or m.group("href") or "").strip()
        if not href:
            continue
        # Decode percent-encoded characters (e.g. `%20` → space).
        try:
            decoded = urllib.parse.unquote(href)
        except Exception:
            decoded = href
        # Strip a trailing `.md` for resolver consistency — resolver tries
        # both with and without the extension.
        target = decoded[:-3] if decoded.endswith(".md") else decoded
        out.append(ExtractedLink(
            target=target,
            link_text=m.group(0),
            kind="markdown",
            position=m.start(),
        ))

    return out


# ────────────────────────────────────────────────────────────────────────────
# Resolution
# ────────────────────────────────────────────────────────────────────────────


def _normalize(target: str) -> str:
    """Strip alias/anchor fragments. Wikilinks already strip them at extraction
    time; markdown-link decoded paths might contain `#anchor` if they were
    embedded oddly. Be defensive."""
    target = target.strip()
    if "|" in target:
        target = target.split("|", 1)[0]
    if "#" in target:
        target = target.split("#", 1)[0]
    return target.strip()


def _source_dir(source_path: str) -> str:
    return os.path.dirname(source_path)


def resolve_target(
    target: str,
    source_path: str,
    vault_index: dict,
) -> int | None:
    """Resolve a raw link target to a `notes_metadata.id`.

    `vault_index` is expected to carry two sub-dicts:
      - `vault_index["paths"]`: dict[file_path, id]
      - `vault_index["stems"]`: dict[stem, list[(file_path, id)]]

    Resolution order (mirrors Obsidian defaults):
      1. Path-style: target contains `/` → try `<target>.md`, then `<target>`.
      2. Same-folder: `<source_dir>/<target>.md`.
      3. Bare-name unique: exactly one note in the vault has stem `<target>`.
      4. Bare-name ambiguous: pick the alphabetically first match.
      5. Fall through: return None (dangling).
    """
    name = _normalize(target)
    if not name:
        return None

    paths: dict[str, int] = vault_index.get("paths", {})
    stems: dict[str, list[tuple[str, int]]] = vault_index.get("stems", {})

    # Strip a trailing `.md` so the rest of the resolver treats `![[Foo.md]]`
    # the same as `![[Foo]]`. The path-style branch re-adds it as needed.
    has_md = name.endswith(".md")
    name_no_ext = name[:-3] if has_md else name

    # Path-style attempt — fires whenever the target contains a slash OR
    # already carries a `.md` extension. This catches `[[Folder/Foo]]` and
    # `[label](Folder/Foo.md)` (the .md was stripped by the extractor) alike.
    if "/" in name_no_ext or has_md:
        # Normalize `./` and `../` against the source's folder so markdown
        # links like `[label](./Foo.md)` resolve correctly.
        if name_no_ext.startswith("./") or name_no_ext.startswith("../"):
            base = _source_dir(source_path)
            normalized = (
                os.path.normpath(os.path.join(base, name_no_ext))
                if base else os.path.normpath(name_no_ext)
            )
            normalized = normalized.replace(os.sep, "/")
        else:
            normalized = name_no_ext
        candidate_md = f"{normalized}.md"
        if candidate_md in paths:
            return paths[candidate_md]
        if normalized in paths:
            return paths[normalized]

    # Same-folder bias.
    src_dir = _source_dir(source_path)
    if src_dir:
        local = f"{src_dir}/{name_no_ext}.md"
        if local in paths:
            return paths[local]

    # Bare-name lookup by stem.
    stem_key = os.path.basename(name_no_ext)
    candidates = stems.get(stem_key, [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]
    # Multiple — prefer same folder, else alphabetical.
    same_folder = [c for c in candidates if os.path.dirname(c[0]) == src_dir]
    if same_folder:
        same_folder.sort(key=lambda c: c[0])
        return same_folder[0][1]
    candidates_sorted = sorted(candidates, key=lambda c: c[0])
    return candidates_sorted[0][1]


def build_vault_index(rows) -> dict:
    """Build a `vault_index` dict from an iterable of `(file_path, id)` tuples."""
    paths: dict[str, int] = {}
    stems: dict[str, list[tuple[str, int]]] = {}
    for file_path, note_id in rows:
        paths[file_path] = note_id
        stem = os.path.splitext(os.path.basename(file_path))[0]
        stems.setdefault(stem, []).append((file_path, note_id))
    return {"paths": paths, "stems": stems}


def normalize_target(target: str) -> str:
    """Public wrapper for the alias/anchor-stripping helper."""
    return _normalize(target)


def mask_code(text: str) -> str:
    """Public wrapper around `_mask_code`.

    Replaces fenced and inline code blocks with whitespace of equal byte
    length. Useful for downstream scanners (heading parsers, link
    extractors) that must avoid false positives inside code.
    """
    return _mask_code(text)
