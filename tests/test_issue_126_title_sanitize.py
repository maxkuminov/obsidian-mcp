"""Regression test for GitHub issue #126.

A frontmatter `title` that YAML parses into a date (`title: 2026-08-25`), a
list, or a string longer than the column's VARCHAR(512) used to be assigned
to the row unsanitized. The batch INSERT then raised, aborting the whole
pass transaction — and because nothing committed, the content hash never
advanced and every 5-minute tick retried the same fatal batch forever while
search kept answering from the last good rows.

Runs fully offline: imports the pure helper directly, no DB / network.
"""

import datetime

from src.services.indexer import _note_title


def test_plain_string_title_passes_through():
    assert _note_title({"title": "My Note"}, "file.md") == "My Note"


def test_missing_title_falls_back_to_stem():
    assert _note_title({}, "Daily Plan.md") == "Daily Plan"


def test_empty_title_falls_back_to_stem():
    assert _note_title({"title": ""}, "note.md") == "note"


def test_date_title_is_stringified():
    # YAML parses `title: 2026-08-25` into datetime.date.
    assert _note_title({"title": datetime.date(2026, 8, 25)}, "n.md") == "2026-08-25"


def test_datetime_title_is_stringified():
    out = _note_title({"title": datetime.datetime(2026, 8, 25, 1, 2, 3)}, "n.md")
    assert isinstance(out, str) and out.startswith("2026-08-25")


def test_list_title_is_stringified():
    out = _note_title({"title": ["a", "b"]}, "n.md")
    assert isinstance(out, str) and len(out) <= 512


def test_dict_title_is_stringified():
    out = _note_title({"title": {"weird": True}}, "n.md")
    assert isinstance(out, str) and len(out) <= 512


def test_overlong_title_is_bounded_to_512():
    out = _note_title({"title": "x" * 2000}, "n.md")
    assert out == "x" * 512


def test_int_title_is_stringified():
    assert _note_title({"title": 42}, "n.md") == "42"
