"""Per-owner task settings (#428, design D14, D18, D19).

One `task_settings` row per owner scope — a user id, or NULL for the
single-user scope — merged field by field over the environment defaults
(`Settings.tasks_*`). No row means every value is the default, and the
defaults exclude nothing, so a deployment that never saves the panel's Tasks
card behaves exactly as before.

- `effective(session, uid)` is the only reader. Each task tool calls it once
  per call; the panel calls it to show the card. It never refuses: a stored
  zone this build cannot load falls back to UTC and says so
  (`timezone_fallback`), and the tool's header carries that (D19).
- `save(session, uid, form)` is the only writer. It validates the whole form
  first and returns every problem as a message, writing nothing when there is
  one; otherwise it upserts the scope's row under `FOR UPDATE`. It does **not**
  commit: the caller (a panel route) owns the transaction.

Both are owner-scoped by the same total mapping as every note query (`None`
is `IS NULL`, never "no predicate"), so no caller can read or change another
scope's row.
"""
from __future__ import annotations

import dataclasses
import re
import zoneinfo
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import is_portable_zone_name, settings
from src.models.db import TaskSettings

#: Entries each exclusion list may hold (D14).
TASK_SETTINGS_MAX_ENTRIES = 32
#: `task_settings.timezone` is `varchar(64)`.
TIMEZONE_MAX_CHARS = 64
#: A folder prefix is a vault path (`vault.MAX_PATH_CHARS`, which is also the
#: column width `varchar(1024)`).
FOLDER_MAX_CHARS = 1024
#: The inline-tag limit the extractor stores (`config.TASK_TAG_MAX_CHARS`),
#: and the column width `varchar(128)`.
TAG_MAX_CHARS = 128
#: The tag grammar after one leading `#` is stripped and the tag lower-cased:
#: the inline-tag rule (`vault.INLINE_TAG_RE`), with no trailing `/` (D12).
_TAG_RE = re.compile(r"[a-z][a-z0-9_/-]*")
#: Every C0/C1 control and the line/paragraph separators: never part of a
#: folder name an operator meant to type.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")

_TRUE = frozenset({"on", "true", "1", "yes"})
_FALSE = frozenset({"off", "false", "0", "no"})
_DEFAULT = "default"

#: The four nullable write-behaviour fields, in the order the card shows them.
BOOLEAN_FIELDS = (
    "set_done_date",
    "set_cancelled_date",
    "set_created_date",
    "next_recurrence_below",
)


@dataclasses.dataclass(frozen=True)
class EffectiveTaskSettings:
    """A scope's settings merged over the environment defaults.

    `timezone` is the zone the tools use. `timezone_fallback` is True when the
    scope's stored zone could not be loaded by this build and `timezone` is
    therefore `"UTC"`; `configured_timezone` keeps what was stored (None when
    the scope has none and the environment default applies).
    """

    timezone: str
    timezone_fallback: bool
    configured_timezone: str | None
    excluded_folders: tuple[str, ...]
    excluded_note_tags: tuple[str, ...]
    set_done_date: bool
    set_cancelled_date: bool
    set_created_date: bool
    next_recurrence_below: bool

    @property
    def tzinfo(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.timezone)


def defaults() -> EffectiveTaskSettings:
    """The settings of a scope with no row: the environment defaults."""
    return _merge(None)


def _owner_predicate(uid: int | None):
    return TaskSettings.user_id.is_(None) if uid is None else TaskSettings.user_id == uid


def _loadable(name: str) -> bool:
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _merge(row: TaskSettings | None) -> EffectiveTaskSettings:
    configured = row.timezone if row is not None else None
    timezone = configured or settings.tasks_default_timezone
    fallback = False
    if not _loadable(timezone):
        timezone, fallback = "UTC", True

    def pick(field: str) -> bool:
        value = getattr(row, field) if row is not None else None
        return bool(getattr(settings, f"tasks_{field}")) if value is None else bool(value)

    return EffectiveTaskSettings(
        timezone=timezone,
        timezone_fallback=fallback,
        configured_timezone=configured,
        excluded_folders=tuple(row.excluded_folders or ()) if row is not None else (),
        excluded_note_tags=tuple(row.excluded_note_tags or ()) if row is not None else (),
        set_done_date=pick("set_done_date"),
        set_cancelled_date=pick("set_cancelled_date"),
        set_created_date=pick("set_created_date"),
        next_recurrence_below=pick("next_recurrence_below"),
    )


async def stored(session, uid: int | None) -> TaskSettings | None:
    """The scope's row as stored (None when it has none), for the panel to
    tell an explicit value from an inherited default."""
    result = await session.execute(select(TaskSettings).where(_owner_predicate(uid)))
    return result.scalar_one_or_none()


async def effective(session, uid: int | None) -> EffectiveTaskSettings:
    """The caller's effective settings: its row merged over the defaults.

    One indexed read (the unique key on `user_id`). Never refuses.
    """
    return _merge(await stored(session, uid))


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def _lines(value: Any) -> list[str]:
    """A textarea (one item per line) or a list of strings, as non-blank
    items with spaces and tabs stripped. Anything else is one item.

    Lines break only at `\\n` (after `\\r\\n` and a lone `\\r` are normalised
    to it), never at the other characters `str.splitlines` honours (`\\x1c`–
    `\\x1e`, `\\x85`, U+2028, U+2029), and only space and tab are stripped:
    such a character stays inside its entry, where the entry's own check
    refuses it, instead of quietly turning one entry into two valid-looking
    ones (or vanishing at an edge).
    """
    if value is None:
        return []
    if isinstance(value, str):
        items: Iterable[Any] = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = [value]
    out = []
    for item in items:
        text = item if isinstance(item, str) else str(item)
        text = text.strip(" \t")
        if text:
            out.append(text)
    return out


def normalise_timezone(value: Any) -> tuple[str | None, str | None]:
    """`(stored value, error)`. Blank means "the server default" (NULL)."""
    if value is None:
        return None, None
    name = (value if isinstance(value, str) else str(value)).strip()
    if not name:
        return None, None
    if (
        len(name) > TIMEZONE_MAX_CHARS
        or not is_portable_zone_name(name)
        or name not in zoneinfo.available_timezones()
    ):
        return None, (
            f"Timezone {name[:TIMEZONE_MAX_CHARS]!r} is not an available IANA "
            "timezone (for example UTC, Europe/Berlin or America/New_York)."
        )
    if not _loadable(name):
        return None, f"Timezone {name!r} cannot be loaded by this server."
    return name, None


def normalise_folder(value: str) -> tuple[str | None, str | None]:
    """`(folder ending in "/", error)` for one excluded-folder entry."""
    shown = value[:80]
    if _CONTROL_RE.search(value):
        return None, f"Excluded folder {shown!r} contains a control character."
    if value.startswith("/"):
        return None, (
            f"Excluded folder {shown!r} must be vault-relative (no leading '/')."
        )
    body = value.rstrip("/")
    segments = body.split("/")
    if not body or any(s in ("", ".", "..") for s in segments):
        return None, (
            f"Excluded folder {shown!r} must be a vault-relative folder path "
            "with no '..', '.' or empty segments."
        )
    folder = body + "/"
    if len(folder) > FOLDER_MAX_CHARS:
        return None, (
            f"Excluded folder {shown!r}… is longer than {FOLDER_MAX_CHARS} characters."
        )
    return folder, None


def normalise_tag(value: str) -> tuple[str | None, str | None]:
    """`(lower-case tag without '#', error)` for one excluded-note-tag entry."""
    shown = value[:80]
    tag = value[1:] if value.startswith("#") else value
    tag = tag.lower()
    if len(tag) > TAG_MAX_CHARS:
        return None, f"Excluded note tag {shown!r}… is longer than {TAG_MAX_CHARS} characters."
    if not _TAG_RE.fullmatch(tag) or tag.endswith("/"):
        return None, (
            f"Excluded note tag {shown!r} is not a tag: it must start with a "
            "letter and contain only letters, digits, '_', '-' and '/', with "
            "no trailing '/'."
        )
    return tag, None


def _normalise_list(raw: Any, normalise, label: str) -> tuple[list[str], list[str]]:
    values: list[str] = []
    errors: list[str] = []
    for item in _lines(raw):
        value, error = normalise(item)
        if error is not None:
            errors.append(error)
        elif value not in values:
            values.append(value)
    if len(values) > TASK_SETTINGS_MAX_ENTRIES:
        errors.append(
            f"At most {TASK_SETTINGS_MAX_ENTRIES} {label} are allowed "
            f"({len(values)} given)."
        )
    return values, errors


def _normalise_flag(form: Mapping[str, Any], field: str) -> tuple[bool | None, str | None]:
    """One write-behaviour field: `"on"`/`"true"` (and `1`, `yes`, `True`),
    `"off"`/`"false"` (and `0`, `no`, `False`), or `"default"` (store NULL,
    the environment default). An absent key is not a value: `validate` lists
    it in `unchanged` and `save` leaves the stored field as it is (NULL for a
    scope with no row) — a caller posting only `timezone` must not switch the
    done date off. Anything unrecognised is an error."""
    value = form[field]
    if isinstance(value, bool):
        return value, None
    text = ("" if value is None else str(value)).strip().lower()
    if text in _TRUE:
        return True, None
    if text in _FALSE:
        return False, None
    if text == _DEFAULT:
        return None, None
    return None, f"Unrecognised value for {field!r}."


@dataclasses.dataclass(frozen=True)
class ValidatedForm:
    timezone: str | None
    excluded_folders: list[str]
    excluded_note_tags: list[str]
    set_done_date: bool | None
    set_cancelled_date: bool | None
    set_created_date: bool | None
    next_recurrence_below: bool | None
    #: The boolean fields absent from the form: left as stored by `save`
    #: (their value above is None and is not written).
    unchanged: frozenset = frozenset()


def validate(form: Mapping[str, Any]) -> tuple[ValidatedForm | None, list[str]]:
    """`(validated values, [])`, or `(None, every error)`. Pure."""
    errors: list[str] = []
    timezone, error = normalise_timezone(form.get("timezone"))
    if error:
        errors.append(error)
    folders, folder_errors = _normalise_list(
        form.get("excluded_folders"), normalise_folder, "excluded folders"
    )
    tags, tag_errors = _normalise_list(
        form.get("excluded_note_tags"), normalise_tag, "excluded note tags"
    )
    errors += folder_errors + tag_errors
    flags: dict[str, bool | None] = {}
    unchanged = frozenset(f for f in BOOLEAN_FIELDS if f not in form)
    for field in BOOLEAN_FIELDS:
        if field in unchanged:
            flags[field] = None
            continue
        value, error = _normalise_flag(form, field)
        if error:
            errors.append(error)
        flags[field] = value
    if errors:
        return None, errors
    return ValidatedForm(
        timezone=timezone,
        excluded_folders=folders,
        excluded_note_tags=tags,
        unchanged=unchanged,
        **flags,
    ), []


async def save(session, uid: int | None, form: Mapping[str, Any]) -> list[str]:
    """Validate `form` and upsert the scope's row; return the errors.

    `form` keys: `timezone` (blank = server default), `excluded_folders` and
    `excluded_note_tags` (one per line, or a list), and the four checkbox
    fields in `BOOLEAN_FIELDS` (`"on"` / `"off"` / `"default"` = NULL, the
    environment default; an absent key leaves the stored value, or NULL for
    a scope with no row). Any error refuses the whole save with
    nothing written. Otherwise the scope's row is locked (`FOR UPDATE`) and
    updated, or inserted (`ON CONFLICT` on the scope's unique key, so two
    first saves cannot both insert). Does not commit.
    """
    values, errors = validate(form)
    if errors:
        return errors
    assert values is not None
    fields = {
        "timezone": values.timezone,
        "excluded_folders": values.excluded_folders,
        "excluded_note_tags": values.excluded_note_tags,
        "set_done_date": values.set_done_date,
        "set_cancelled_date": values.set_cancelled_date,
        "set_created_date": values.set_created_date,
        "next_recurrence_below": values.next_recurrence_below,
    }
    for name in values.unchanged:
        del fields[name]
    existing = (
        await session.execute(
            select(TaskSettings.id).where(_owner_predicate(uid)).with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        row = await session.get(TaskSettings, existing, populate_existing=True)
        for name, value in fields.items():
            setattr(row, name, value)
        row.updated_at = func.now()
    else:
        statement = pg_insert(TaskSettings).values(user_id=uid, **fields)
        statement = statement.on_conflict_do_update(
            constraint="uq_task_settings_user_id",
            set_={**fields, "updated_at": func.now()},
        )
        await session.execute(statement)
    await session.flush()
    return []
