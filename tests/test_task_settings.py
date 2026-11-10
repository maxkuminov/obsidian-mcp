"""Task settings: defaults, merge, validation and the boot refusal (#428, D14).

Pure: no database. The database half — the NULL-scope row, owner scoping,
the upsert and the cascade on user deletion — is
`tests/integration/test_task_settings_pg.py`.
"""
import zoneinfo

import pytest
from pydantic import ValidationError

from src.config import (
    TASK_CONTEXT_MAX_LINES,
    TASK_REFRESH_LOCK_WAIT_MS,
    TASK_SIMILAR_SCAN_MAX,
    Settings,
)
from src.models.db import TaskSettings
from src.services import task_settings
from src.services.task_settings import (
    BOOLEAN_FIELDS,
    TASK_SETTINGS_MAX_ENTRIES,
    EffectiveTaskSettings,
    normalise_folder,
    normalise_tag,
    validate,
)

SECRET = "task-settings-test-secret-only-0123456789abcdef"


def make_settings(**kw):
    return Settings(_env_file=None, secret_key=SECRET, **kw)


# --------------------------------------------------------------------------
# Settings: defaults and the boot refusal
# --------------------------------------------------------------------------


def test_environment_defaults_are_the_plugins_own():
    s = make_settings()
    assert s.tasks_default_timezone == "UTC"
    assert s.tasks_set_done_date is True
    assert s.tasks_set_cancelled_date is True
    assert s.tasks_set_created_date is False
    assert s.tasks_next_recurrence_below is False


def test_the_constants():
    assert TASK_REFRESH_LOCK_WAIT_MS == 2000
    assert TASK_SIMILAR_SCAN_MAX == 2000
    assert TASK_CONTEXT_MAX_LINES == 8


@pytest.mark.parametrize("name", ["Mars/Olympus", "", "  ", "../etc/passwd", "utc/x"])
def test_an_invalid_default_timezone_refuses_startup_naming_the_variable(name):
    with pytest.raises(ValidationError) as err:
        make_settings(tasks_default_timezone=name)
    assert "TASKS_DEFAULT_TIMEZONE" in str(err.value)


def test_the_refusal_comes_from_the_environment_too(tmp_path):
    env = tmp_path / "tasks.env"
    env.write_text(f"SECRET_KEY={SECRET}\nTASKS_DEFAULT_TIMEZONE=Mars/Olympus\n")
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=env)
    assert "TASKS_DEFAULT_TIMEZONE" in str(err.value)


def test_a_valid_default_timezone_is_kept_stripped(tmp_path):
    env = tmp_path / "tasks.env"
    env.write_text(f"SECRET_KEY={SECRET}\nTASKS_DEFAULT_TIMEZONE= Europe/Berlin \n")
    assert Settings(_env_file=env).tasks_default_timezone == "Europe/Berlin"


# --------------------------------------------------------------------------
# effective: the merge
# --------------------------------------------------------------------------


def test_no_row_means_every_value_is_the_default():
    eff = task_settings.defaults()
    assert eff == EffectiveTaskSettings(
        timezone="UTC",
        timezone_fallback=False,
        configured_timezone=None,
        excluded_folders=(),
        excluded_note_tags=(),
        set_done_date=True,
        set_cancelled_date=True,
        set_created_date=False,
        next_recurrence_below=False,
    )
    assert eff.tzinfo == zoneinfo.ZoneInfo("UTC")


def test_effective_settings_are_frozen():
    eff = task_settings.defaults()
    with pytest.raises(Exception):
        eff.timezone = "Europe/Berlin"  # type: ignore[misc]


def test_a_row_overrides_only_the_fields_it_sets():
    row = TaskSettings(
        timezone="Europe/Berlin",
        excluded_folders=["Archive/"],
        excluded_note_tags=["ticket"],
        set_done_date=False,
        set_cancelled_date=None,
        set_created_date=True,
        next_recurrence_below=None,
    )
    eff = task_settings._merge(row)
    assert eff.timezone == "Europe/Berlin" and not eff.timezone_fallback
    assert eff.configured_timezone == "Europe/Berlin"
    assert eff.excluded_folders == ("Archive/",)
    assert eff.excluded_note_tags == ("ticket",)
    assert eff.set_done_date is False
    assert eff.set_cancelled_date is True  # NULL: the env default
    assert eff.set_created_date is True
    assert eff.next_recurrence_below is False  # NULL: the env default


def test_a_null_timezone_uses_the_environment_default(monkeypatch):
    monkeypatch.setattr(task_settings.settings, "tasks_default_timezone", "Asia/Tokyo")
    eff = task_settings._merge(TaskSettings(timezone=None))
    assert eff.timezone == "Asia/Tokyo"
    assert eff.configured_timezone is None
    assert not eff.timezone_fallback


def test_the_env_defaults_drive_null_flags(monkeypatch):
    for field in BOOLEAN_FIELDS:
        monkeypatch.setattr(task_settings.settings, f"tasks_{field}", True)
    eff = task_settings.defaults()
    assert all(getattr(eff, field) is True for field in BOOLEAN_FIELDS)


def test_an_unloadable_stored_zone_falls_back_to_utc_and_says_so():
    eff = task_settings._merge(TaskSettings(timezone="Mars/Olympus"))
    assert eff.timezone == "UTC"
    assert eff.timezone_fallback is True
    assert eff.configured_timezone == "Mars/Olympus"


# --------------------------------------------------------------------------
# validate: the save rules
# --------------------------------------------------------------------------


def _ok(form):
    values, errors = validate(form)
    assert errors == [], errors
    return values


def _refused(form):
    values, errors = validate(form)
    assert values is None and errors
    return errors


def test_an_empty_form_stores_no_timezone_no_exclusions_and_leaves_the_flags():
    values = _ok({})
    assert values.timezone is None
    assert values.excluded_folders == [] and values.excluded_note_tags == []
    assert values.unchanged == frozenset(BOOLEAN_FIELDS)


@pytest.mark.parametrize("value", ["Europe/Berlin", " America/New_York ", "UTC"])
def test_a_valid_timezone_is_stored(value):
    assert _ok({"timezone": value}).timezone == value.strip()


def test_a_blank_timezone_means_the_server_default():
    assert _ok({"timezone": "   "}).timezone is None


@pytest.mark.parametrize("value", ["Mars/Olympus", "europe/berlin", "../etc", "x" * 65])
def test_an_invalid_timezone_is_refused(value):
    errors = _refused({"timezone": value})
    assert any("Timezone" in e for e in errors)


@pytest.mark.parametrize(
    "value",
    ["../other", "/Archive", "Projects/../other", "a//b", "./Archive", "Arch\x00ive",
     "Arch\x7five", "Arch\x9five", "/", "..", "."],
)
def test_an_invalid_folder_is_refused(value):
    errors = _refused({"excluded_folders": value})
    assert any("Excluded folder" in e for e in errors)


@pytest.mark.parametrize(
    "value,stored",
    [
        ("Archive", "Archive/"),
        ("Archive/", "Archive/"),
        ("Archive///", "Archive/"),
        ("Projects/Old Stuff", "Projects/Old Stuff/"),
        (".obsidian", ".obsidian/"),
    ],
)
def test_a_folder_is_normalised_to_one_trailing_slash(value, stored):
    assert normalise_folder(value) == (stored, None)


def test_folders_one_per_line_deduplicated_blank_lines_skipped():
    values = _ok({"excluded_folders": "Archive\r\n\n  Templates/ \nArchive/\n"})
    assert values.excluded_folders == ["Archive/", "Templates/"]


def test_a_folder_list_is_accepted_too():
    assert _ok({"excluded_folders": ["Archive", "Templates"]}).excluded_folders == [
        "Archive/", "Templates/",
    ]


def test_a_folder_over_the_path_cap_is_refused():
    assert normalise_folder("a" * 1023)[0] == "a" * 1023 + "/"
    assert normalise_folder("a" * 1024)[1] is not None


@pytest.mark.parametrize(
    "value,stored",
    [
        ("#Ticket", "ticket"),
        ("ticket", "ticket"),
        ("Waiting-For/Chase", "waiting-for/chase"),
        ("a_b-c/d1", "a_b-c/d1"),
    ],
)
def test_a_tag_is_normalised(value, stored):
    assert normalise_tag(value) == (stored, None)


@pytest.mark.parametrize("value", ["1abc", "##x", "a/", "#", "has space", "ü", "a" * 129])
def test_an_invalid_tag_is_refused(value):
    errors = _refused({"excluded_note_tags": value})
    assert any("Excluded note tag" in e for e in errors)


def test_tags_are_deduplicated_after_normalisation():
    assert _ok({"excluded_note_tags": "#Ticket\nticket\nTICKET"}).excluded_note_tags == [
        "ticket"
    ]


@pytest.mark.parametrize("field", ["excluded_folders", "excluded_note_tags"])
def test_at_most_32_entries(field):
    items = [f"t{i}" for i in range(TASK_SETTINGS_MAX_ENTRIES)]
    assert len(getattr(_ok({field: "\n".join(items)}), field)) == 32
    errors = _refused({field: "\n".join(items + ["t99"])})
    assert any("At most 32" in e for e in errors)


@pytest.mark.parametrize(
    "value,stored",
    [("on", True), ("true", True), ("1", True), (True, True),
     ("off", False), ("false", False), ("0", False), (False, False),
     ("default", None), ("Default", None)],
)
def test_checkbox_values(value, stored):
    assert _ok({"set_done_date": value}).set_done_date is stored


def test_an_absent_flag_is_left_unchanged_not_switched_off():
    """Regression: an absent key used to store False, so a caller posting only
    `timezone` silently turned the done and cancelled dates off."""
    values = _ok({"timezone": "Europe/Berlin", "set_done_date": "on"})
    assert values.unchanged == frozenset(
        {"set_cancelled_date", "set_created_date", "next_recurrence_below"}
    )
    assert values.set_done_date is True


def test_the_panel_cards_three_options_are_all_explicit():
    values = _ok({"set_done_date": "default", "set_cancelled_date": "on",
                  "set_created_date": "off", "next_recurrence_below": "default"})
    assert values.unchanged == frozenset()
    assert (values.set_done_date, values.set_cancelled_date, values.set_created_date,
            values.next_recurrence_below) == (None, True, False, None)


@pytest.mark.parametrize("sep", ["\x1c", "\x1d", "\x1e", "\x85", " ", " ", "\x0b", "\x0c"])
def test_a_separator_character_refuses_the_entry_instead_of_splitting_it(sep):
    """Regression: `splitlines()` used to turn `Arch<sep>ive` into two
    valid-looking entries `Arch/` and `ive/`."""
    for raw in (f"Arch{sep}ive", f"Archive{sep}", f"{sep}Archive"):
        errors = _refused({"excluded_folders": raw})
        assert len(errors) == 1 and "control character" in errors[0], (raw, errors)
    for raw in (f"tic{sep}ket", f"ticket{sep}", f"{sep}ticket"):
        errors = _refused({"excluded_note_tags": raw})
        assert len(errors) == 1 and "Excluded note tag" in errors[0], (raw, errors)


def test_lines_break_only_at_newlines_of_any_convention():
    values = _ok({"excluded_folders": "A\r\nB\rC\nD"})
    assert values.excluded_folders == ["A/", "B/", "C/", "D/"]


@pytest.mark.parametrize("name", ["localtime", "Factory", "posixrules", "posix/Europe/Berlin",
                                  "right/UTC"])
def test_host_only_pseudo_zones_are_refused_everywhere(name):
    assert any("Timezone" in e for e in _refused({"timezone": name}))
    with pytest.raises(ValidationError) as err:
        make_settings(tasks_default_timezone=name)
    assert "TASKS_DEFAULT_TIMEZONE" in str(err.value)


def test_an_unrecognised_checkbox_value_is_refused():
    errors = _refused({"set_created_date": "maybe"})
    assert any("set_created_date" in e for e in errors)


def test_every_error_is_reported_at_once():
    errors = _refused({
        "timezone": "Mars/Olympus",
        "excluded_folders": "../other\n/abs",
        "excluded_note_tags": "1x",
        "next_recurrence_below": "maybe",
    })
    assert len(errors) == 5, errors


def test_errors_never_echo_an_unbounded_value():
    errors = _refused({"excluded_folders": "../" + "x" * 5000})
    assert all(len(e) < 400 for e in errors)
