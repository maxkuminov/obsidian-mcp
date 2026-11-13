"""Sync-conflict copy names, for the opt-in index exclusion (#392).

File-sync tools resolve a conflicting edit by keeping both versions: one at
the note's path and one beside it under a marked name. With
`INDEX_EXCLUDE_CONFLICT_FILES=true` the indexer leaves such a copy out of the
index (it stays on disk, readable and writable by exact path). This module is
the single definition of "such a copy": a fixed, source-cited pattern set,
matched against a **basename** only.

The patterns are deliberately strict. Each needs the tool's literal marker,
immediately followed by a complete fixed-width timestamp and the closing
delimiter, immediately before `.md`. A miss leaves a copy indexed, which is the
behaviour without this setting; a false match would hide a real note. A title
that *exactly* matches a pattern is excluded by design, whoever wrote it
(design L1). Digits are `[0-9]`, never `\\d`, which would match non-ASCII
digits. Matching is case-sensitive, including the `.md` suffix the walk
requires.

Pure: no settings, no I/O. See
`openspec/changes/archive/2026-11-12-index-exclude-conflict-files/design.md` (D2).
"""

from __future__ import annotations

import re

CONFLICT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # P1 Syncthing. docs.syncthing.net/users/syncing.html, "Conflicting
    # Changes": `<filename>.sync-conflict-<date>-<time>-<modifiedBy>.<ext>`.
    # lib/model/folder_sendrecv.go `conflictName` formats the time as
    # `20060102-150405`; `modifiedBy` is `ShortID.String()`, seven base32
    # characters (A-Z, 2-7), empty for a zero ID (lib/protocol/deviceid.go).
    ("syncthing", re.compile(
        r".+\.sync-conflict-[0-9]{8}-[0-9]{6}-(?:[A-Z2-7]{7})?\.md"
    )),
    # P2 Nextcloud desktop (current). nextcloud/desktop src/common/utility.cpp
    # `makeConflictFileName`: inserts " (conflicted copy " + optional user
    # name (parentheses replaced by "_") + " " + `yyyy-MM-dd hhmmss` + ")"
    # before the extension.
    ("nextcloud", re.compile(
        r".+ \(conflicted copy (?:[^()/]* )?[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{6}\)\.md"
    )),
    # P3 Nextcloud / ownCloud desktop (legacy). Nextcloud desktop manual,
    # Appendix B "History and Architecture" (`message_conflict-20160101-
    # 153110.txt`); `Utility::isConflictFile` still recognises "_conflict-".
    ("nextcloud-legacy", re.compile(
        r".+_conflict-[0-9]{8}-[0-9]{6}\.md"
    )),
    # P4 Dropbox. help.dropbox.com "conflicted copy": the editor's username,
    # "conflicted copy" and the save date are added to the filename. The page
    # gives no literal example; `(<name>'s conflicted copy YYYY-MM-DD)` with
    # an optional ` (N)` counter is the client form. Apostrophe U+0027 or
    # U+2019 (right single quotation mark). The least documented pattern.
    ("dropbox", re.compile(
        r".+ \([^()/]+(?:'|’)s conflicted copy [0-9]{4}-[0-9]{2}-[0-9]{2}"
        r"(?: \([0-9]+\))?\)\.md"
    )),
    # P5 Obsidian Sync 1.9.7+, "Create conflict file".
    # obsidian.md/help/sync/troubleshoot: "original-note-name (Conflicted copy
    # device-name YYYYMMDDHHMM).md".
    ("obsidian-sync", re.compile(
        r".+ \(Conflicted copy .+ [0-9]{12}\)\.md"
    )),
    # P6 The form reported in #392 and found in the reference vault. Its
    # producer is unidentified (no primary documentation found); kept because
    # it is the form the issue reports.
    ("observed", re.compile(
        r".+ \(conflict [0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}\)\.md"
    )),
)


def is_conflict_copy(name: str) -> bool:
    """Whether the basename `name` is a sync-conflict copy by D2's patterns.

    Takes a basename. A caller holding a path passes its final component; a
    directory component is never matched.
    """
    return any(pattern.fullmatch(name) for _tool, pattern in CONFLICT_PATTERNS)
