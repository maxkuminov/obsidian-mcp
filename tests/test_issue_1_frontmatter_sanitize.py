"""Regression test for GitHub issue #1.

YAML frontmatter can contain `date`/`datetime` objects (and other
non-JSON-serializable scalars). The indexer writes the frontmatter into a
JSONB column whose codec is `json.dumps`, which rejects dates. The sanitizer
must coerce every such value to a string regardless of where it appears:
as a top-level value, inside a list, inside a nested dict, inside a dict
nested in a list, and — crucially — as a mapping *key*.

Runs fully offline: imports the pure helper directly, no DB / network / model.
"""

import datetime
import json

from src.services.indexer import _sanitize_frontmatter


def _is_json_serializable(obj) -> bool:
    try:
        json.dumps(obj)
        return True
    except (TypeError, ValueError):
        return False


def test_date_as_top_level_value():
    fm = {"created": datetime.date(2024, 1, 2)}
    out = _sanitize_frontmatter(fm)
    assert _is_json_serializable(out)
    assert out["created"] == "2024-01-02"


def test_datetime_as_value():
    fm = {"ts": datetime.datetime(2024, 1, 2, 3, 4, 5)}
    out = _sanitize_frontmatter(fm)
    assert _is_json_serializable(out)
    assert isinstance(out["ts"], str)


def test_date_in_list():
    fm = {"dates": [datetime.date(2024, 1, 2), "plain"]}
    out = _sanitize_frontmatter(fm)
    assert _is_json_serializable(out)
    assert out["dates"] == ["2024-01-02", "plain"]


def test_date_in_nested_dict():
    fm = {"meta": {"due": datetime.date(2024, 1, 2)}}
    out = _sanitize_frontmatter(fm)
    assert _is_json_serializable(out)
    assert out["meta"]["due"] == "2024-01-02"


def test_dict_inside_list_is_recursed_not_stringified():
    fm = {"items": [{"due": datetime.date(2024, 1, 2)}]}
    out = _sanitize_frontmatter(fm)
    assert _is_json_serializable(out)
    # Must stay a dict (not stringified), with the inner date coerced.
    assert out["items"][0] == {"due": "2024-01-02"}


def test_list_inside_list_is_recursed():
    fm = {"matrix": [[datetime.date(2024, 1, 2)]]}
    out = _sanitize_frontmatter(fm)
    assert _is_json_serializable(out)
    assert out["matrix"] == [["2024-01-02"]]


def test_date_as_mapping_key():
    fm = {datetime.date(2024, 1, 2): "value"}
    out = _sanitize_frontmatter(fm)
    assert _is_json_serializable(out)
    assert out == {"2024-01-02": "value"}


def test_date_key_in_nested_dict():
    fm = {"schedule": {datetime.date(2024, 1, 2): "busy"}}
    out = _sanitize_frontmatter(fm)
    assert _is_json_serializable(out)
    assert out["schedule"] == {"2024-01-02": "busy"}


def test_plain_values_untouched():
    fm = {"title": "Hello", "n": 3, "f": 1.5, "b": True, "none": None}
    out = _sanitize_frontmatter(fm)
    assert out == fm
    assert _is_json_serializable(out)
