"""Relative-date resolution in a named IANA time zone (#428, design D12, D19).

Pure apart from the time-zone database: no settings, no database, and no
clock except the one injected through ``now`` (an aware instant; when it is
None the current instant is read once).

Accepted forms, case-insensitive: ``YYYY-MM-DD`` (a calendar date),
``today``, ``tomorrow``, ``yesterday``, ``+Nd`` and ``-Nd`` with N from 0 to
3660 (no leading zero). "Today" is ``now.astimezone(ZoneInfo(tz)).date()``.
A zone missing from this build's tzdata falls back to UTC and says so
(`effective_zone`); that is never a refusal.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_RELATIVE_DAYS = 3660
UTC = "UTC"

_ISO_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_OFFSET_RE = re.compile(r"(?P<sign>[+-])(?P<n>0|[1-9][0-9]{0,3})d")
_WORDS = {"today": 0, "tomorrow": 1, "yesterday": -1}


@dataclass(frozen=True)
class InvalidDate:
    """The value is not one of the accepted forms; the caller refuses
    ``invalid_argument``."""

    reason: str


@dataclass(frozen=True)
class EffectiveZone:
    """The zone a call resolves in. ``unavailable`` is True when the
    configured name was missing and UTC was used instead (D19: the header
    says ``timezone: UTC (configured zone unavailable)``)."""

    name: str
    zone: ZoneInfo
    unavailable: bool = False


def effective_zone(name: str | None) -> EffectiveZone:
    """``name`` as a `ZoneInfo`, or UTC with ``unavailable=True`` when this
    build cannot load it. None or empty means UTC."""
    if not name:
        return EffectiveZone(UTC, ZoneInfo(UTC))
    try:
        return EffectiveZone(name, ZoneInfo(name))
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return EffectiveZone(UTC, ZoneInfo(UTC), unavailable=True)


def _zone(tz: str | ZoneInfo | EffectiveZone) -> ZoneInfo:
    if isinstance(tz, EffectiveZone):
        return tz.zone
    if isinstance(tz, ZoneInfo):
        return tz
    return ZoneInfo(tz)


def today(tz: str | ZoneInfo | EffectiveZone, *, now: datetime.datetime | None = None) -> datetime.date:
    """The current date in ``tz``. ``now`` must be timezone-aware."""
    zone = _zone(tz)
    if now is None:
        return datetime.datetime.now(zone).date()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("task_dates.today: now must be timezone-aware")
    return now.astimezone(zone).date()


def is_relative(value: str) -> bool:
    """True for the forms resolved against today (echoed as
    ``+7d=2026-11-17`` in a query line)."""
    if not isinstance(value, str):
        return False
    low = value.lower()
    return low in _WORDS or _OFFSET_RE.fullmatch(low) is not None


def resolve(
    value: str | datetime.date,
    tz: str | ZoneInfo | EffectiveZone,
    *,
    now: datetime.datetime | None = None,
) -> datetime.date | InvalidDate:
    """Resolve one date argument (D12 forms) in ``tz``."""
    if isinstance(value, datetime.datetime):
        return InvalidDate("a date, not a datetime")
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        return InvalidDate("a date must be a string")
    low = value.lower()
    if _ISO_RE.fullmatch(low):
        try:
            return datetime.date.fromisoformat(low)
        except ValueError:
            return InvalidDate(f"{value!r} is not a calendar date")
    if low in _WORDS:
        offset = _WORDS[low]
    else:
        m = _OFFSET_RE.fullmatch(low)
        if m is None:
            return InvalidDate(
                f"{value[:32]!r} is not YYYY-MM-DD, today, tomorrow, yesterday, +Nd or -Nd"
            )
        n = int(m.group("n"))
        if n > MAX_RELATIVE_DAYS:
            return InvalidDate(f"relative offsets are limited to {MAX_RELATIVE_DAYS} days")
        offset = n if m.group("sign") == "+" else -n
    base = today(tz, now=now)
    try:
        return base + datetime.timedelta(days=offset)
    except OverflowError:
        return InvalidDate("the resolved date is out of range")


__all__ = [
    "EffectiveZone", "InvalidDate", "MAX_RELATIVE_DAYS", "effective_zone",
    "is_relative", "resolve", "today",
]
