"""Recurrence: a CLOSED grammar over rule text, and its date arithmetic
(#428, design D7, supervisor ruling 3).

Pure and standard-library only (`datetime`, `calendar`). The Tasks plugin
hands rule text to rrule 2.8.1 (JavaScript), which no Python port reproduces
exactly, so this module admits only the shapes whose dates the plugin's
documentation and tests pin, and returns `Unsupported` for everything else —
it never guesses and never passes text to a general parser. A completion
whose rule is unsupported is refused with ``recurrence_unsupported``; nothing
is ever completed without its next occurrence.

## Rule text (R§2a)

The rule text is the value of the single ``🔁`` field in the run, trimmed.
A trailing `` when done`` (case-insensitive, split exactly as the plugin does
with ``/^(…+?)( when done)?$/i``) sets ``when_done``. Matching is
case-insensitive on ASCII; tokens are separated by one or more spaces; a
comma list allows optional spaces after each comma.

## Tier 1 — admitted (R§2a round trip; dates R§2b/2c)

``N`` is an integer from 2 to 999 with no leading zero; ``A`` is the anchor.

=====================================  =====================================
``every day`` / ``every N days``       ``A + 1`` / ``A + N`` days
``every weekday``                      first Mon–Fri date ``> A``
``every week`` / ``every N weeks``     ``A + 7`` / ``A + 7N`` days
``every week on D[, D…]`` /            first date ``> A`` whose weekday is
``every D[, D…]``                      in the set (full English names)
``every month`` / ``every N months``   ``A`` + N months, a missing day falls
                                       back to the month's last day (R§2c:
                                       2021-10-31 → 11-30 → 12-30 → 01-30 →
                                       02-28; 2022-01-31 every 3 months →
                                       04-30 → 07-30)
``every month on the K``               first date ``> A`` with day K; months
(``1st`` … ``31st``, correct suffix)   lacking K are skipped forward (R§2c:
                                       01-31 → 03-31 → 05-31 → 07-31 → 08-31)
``every month on the last``            last day of ``A``'s month if ``> A``,
                                       else of the next month
``every year`` / ``every N years``     same month and day N years later;
                                       ``A`` = Feb 29 is Unsupported (R§2c
                                       marks it UNVERIFIED)
``every M on the K``                   first date ``> A`` that is M-K (full
(full English month name)              month name, K valid for M; Feb 29 is
                                       refused)
=====================================  =====================================

## Tier 2 — refused (owner decision 2026-11-10, follow-up issue)

``every N weeks on D…``; ``every month on the last D``; ``every month on the
Kth D`` and ``the Kth last D``; ``every N months on the …``; ``every M and M
on the K and K``; ``every weekend`` (lossy, R§2a); abbreviations; ``every 1
day``; ``for N times``; ``until``; anything else.

## Dates (R§2b)

The reference date is the due date, else scheduled, else start ("remove
scheduled date on recurrence" off). Without ``when done`` the anchor is the
reference date (missed occurrences are not skipped, L8); with ``when done``
it is the completion date. The other dates among start, scheduled and due
keep their whole-day offset from the reference: ``new = old + (next_ref −
ref)``. With no reference date the next occurrence carries no dates.
"""
from __future__ import annotations

import calendar
import datetime
import re
from dataclasses import dataclass

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
# Days valid in every year for each month (Feb 29 refused).
_MONTH_MAX_DAY = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

DAY = "day"
WEEKDAY = "weekday"
WEEK = "week"
WEEK_ON = "week_on"
MONTH = "month"
MONTH_ON_DAY = "month_on_day"
MONTH_ON_LAST = "month_on_last"
YEAR = "year"
YEAR_ON = "year_on"


@dataclass(frozen=True)
class Rule:
    """A tier-1 rule. ``interval`` is N (1 for the plain forms);
    ``weekdays`` are 0 = Monday … 6 = Sunday; ``day`` is K; ``month`` is
    1–12."""

    kind: str
    interval: int = 1
    weekdays: frozenset[int] = frozenset()
    day: int | None = None
    month: int | None = None
    when_done: bool = False
    text: str = ""


@dataclass(frozen=True)
class Unsupported:
    """A rule (or a rule/anchor combination) outside tier 1. The caller
    refuses ``recurrence_unsupported`` and writes nothing."""

    reason: str


@dataclass(frozen=True)
class Shift:
    """The next occurrence's dates. Each of ``due``/``scheduled``/``start``
    is set exactly when the task had that date; ``reference`` names the
    family that was the reference date (None when the task had none)."""

    due: datetime.date | None
    scheduled: datetime.date | None
    start: datetime.date | None
    reference: str | None
    next_reference: datetime.date | None

    def as_mapping(self) -> dict[str, datetime.date]:
        return {
            family: value
            for family, value in (("start", self.start), ("scheduled", self.scheduled), ("due", self.due))
            if value is not None
        }


_ALLOWED = re.compile(r"[a-zA-Z0-9, !]+")
_WHEN_DONE = re.compile(r"^([a-zA-Z0-9, !]+?)( when done)?$", re.IGNORECASE)
_N = r"(?P<n>[2-9]|[1-9][0-9]{1,2})"
_WD = "(?:" + "|".join(WEEKDAYS) + ")"
_WD_LIST = rf"(?P<days>{_WD}(?:, *{_WD})*)"
_ORD = r"(?P<k>[1-9]|[12][0-9]|3[01])(?P<suf>st|nd|rd|th)"
_MON = "(?P<mon>" + "|".join(MONTHS) + ")"

_SHAPES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (kind, re.compile(pattern))
    for kind, pattern in (
        ("day1", r"every +day"),
        ("dayN", rf"every +{_N} +days"),
        ("weekday", r"every +weekday"),
        ("week1", r"every +week"),
        ("weekN", rf"every +{_N} +weeks"),
        ("week_on", rf"every +week +on +{_WD_LIST}"),
        ("week_on", rf"every +{_WD_LIST}"),
        ("month1", r"every +month"),
        ("monthN", rf"every +{_N} +months"),
        ("month_on_day", rf"every +month +on +the +{_ORD}"),
        ("month_on_last", r"every +month +on +the +last"),
        ("year1", r"every +year"),
        ("yearN", rf"every +{_N} +years"),
        ("year_on", rf"every +{_MON} +on +the +{_ORD}"),
    )
)


def _suffix(k: int) -> str:
    if k % 100 in (11, 12, 13):
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(k % 10, "th")


def parse_rule(text: str) -> Rule | Unsupported:
    """Parse rule text against the closed tier-1 grammar (module docstring)."""
    if not isinstance(text, str):
        return Unsupported("rule text must be a string")
    trimmed = text.strip(" ")
    if not trimmed or not _ALLOWED.fullmatch(trimmed):
        return Unsupported("rule text outside the plugin's character class")
    split = _WHEN_DONE.match(trimmed)
    if split is None:  # unreachable after the class check; kept as a belt
        return Unsupported("rule text not recognised")
    when_done = split.group(2) is not None
    body = split.group(1).strip(" ").lower()
    for kind, pattern in _SHAPES:
        m = pattern.fullmatch(body)
        if m is None:
            continue
        n = int(m.group("n")) if "n" in pattern.groupindex else 1
        if kind in ("day1", "dayN"):
            return Rule(DAY, n, when_done=when_done, text=trimmed)
        if kind == "weekday":
            return Rule(WEEKDAY, when_done=when_done, text=trimmed)
        if kind in ("week1", "weekN"):
            return Rule(WEEK, n, when_done=when_done, text=trimmed)
        if kind == "week_on":
            names = [d.strip(" ") for d in m.group("days").split(",")]
            days = [WEEKDAYS.index(d) for d in names]
            if len(set(days)) != len(days):
                return Unsupported("a weekday is repeated")
            return Rule(WEEK_ON, weekdays=frozenset(days), when_done=when_done, text=trimmed)
        if kind in ("month1", "monthN"):
            return Rule(MONTH, n, when_done=when_done, text=trimmed)
        if kind == "month_on_day":
            k = int(m.group("k"))
            if m.group("suf") != _suffix(k):
                return Unsupported("wrong ordinal suffix")
            return Rule(MONTH_ON_DAY, day=k, when_done=when_done, text=trimmed)
        if kind == "month_on_last":
            return Rule(MONTH_ON_LAST, when_done=when_done, text=trimmed)
        if kind in ("year1", "yearN"):
            return Rule(YEAR, n, when_done=when_done, text=trimmed)
        if kind == "year_on":
            k = int(m.group("k"))
            month = MONTHS.index(m.group("mon")) + 1
            if m.group("suf") != _suffix(k):
                return Unsupported("wrong ordinal suffix")
            if k > _MONTH_MAX_DAY[month - 1]:
                return Unsupported("day not valid for that month in every year")
            return Rule(YEAR_ON, day=k, month=month, when_done=when_done, text=trimmed)
    return Unsupported("rule shape outside tier 1")


def _add_months(anchor: datetime.date, months: int) -> datetime.date:
    total = anchor.year * 12 + (anchor.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    last = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, min(anchor.day, last))


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def next_date(rule: Rule, anchor: datetime.date) -> datetime.date | Unsupported:
    """The first date strictly after ``anchor`` satisfying ``rule``."""
    try:
        if rule.kind == DAY:
            return anchor + datetime.timedelta(days=rule.interval)
        if rule.kind == WEEK:
            return anchor + datetime.timedelta(days=7 * rule.interval)
        if rule.kind == WEEKDAY:
            d = anchor + datetime.timedelta(days=1)
            while calendar.weekday(d.year, d.month, d.day) > 4:
                d += datetime.timedelta(days=1)
            return d
        if rule.kind == WEEK_ON:
            for step in range(1, 8):
                d = anchor + datetime.timedelta(days=step)
                if calendar.weekday(d.year, d.month, d.day) in rule.weekdays:
                    return d
            return Unsupported("empty weekday set")
        if rule.kind == MONTH:
            return _add_months(anchor, rule.interval)
        if rule.kind == MONTH_ON_DAY:
            year, month = anchor.year, anchor.month
            if anchor.day >= rule.day:
                year, month = _next_month(year, month)
            for _ in range(24):
                if calendar.monthrange(year, month)[1] >= rule.day:
                    return datetime.date(year, month, rule.day)
                year, month = _next_month(year, month)
            return Unsupported("no month holds that day")
        if rule.kind == MONTH_ON_LAST:
            last = datetime.date(anchor.year, anchor.month, calendar.monthrange(anchor.year, anchor.month)[1])
            if last > anchor:
                return last
            year, month = _next_month(anchor.year, anchor.month)
            return datetime.date(year, month, calendar.monthrange(year, month)[1])
        if rule.kind == YEAR:
            if (anchor.month, anchor.day) == (2, 29):
                return Unsupported("yearly from February 29 is not verified")
            return datetime.date(anchor.year + rule.interval, anchor.month, anchor.day)
        if rule.kind == YEAR_ON:
            year = anchor.year if (rule.month, rule.day) > (anchor.month, anchor.day) else anchor.year + 1
            return datetime.date(year, rule.month, rule.day)
    except (OverflowError, ValueError):
        return Unsupported("next date out of range")
    return Unsupported(f"unknown rule kind {rule.kind!r}")


def next_occurrence(
    rule: Rule,
    *,
    due: datetime.date | None,
    scheduled: datetime.date | None,
    start: datetime.date | None,
    done_on: datetime.date,
) -> Shift | Unsupported:
    """The next occurrence's start/scheduled/due (R§2b).

    The arguments are the task's valid calendar dates (the caller refuses an
    invalid one first, D4 (d)). ``done_on`` is the completion date, used as
    the anchor only for ``when done``.
    """
    dates = (("due", due), ("scheduled", scheduled), ("start", start))
    reference = next(((family, value) for family, value in dates if value is not None), None)
    if reference is None:
        return Shift(None, None, None, None, None)
    ref_family, ref = reference
    anchor = done_on if rule.when_done else ref
    nxt = next_date(rule, anchor)
    if isinstance(nxt, Unsupported):
        return nxt
    delta = nxt - ref
    shifted: dict[str, datetime.date | None] = {}
    try:
        for family, value in dates:
            shifted[family] = None if value is None else value + delta
    except OverflowError:
        return Unsupported("shifted date out of range")
    return Shift(
        due=shifted["due"], scheduled=shifted["scheduled"], start=shifted["start"],
        reference=ref_family, next_reference=nxt,
    )


__all__ = [
    "DAY", "MONTH", "MONTHS", "MONTH_ON_DAY", "MONTH_ON_LAST", "Rule", "Shift",
    "Unsupported", "WEEK", "WEEKDAY", "WEEKDAYS", "WEEK_ON", "YEAR", "YEAR_ON",
    "next_date", "next_occurrence", "parse_rule",
]
