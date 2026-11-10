"""Relative-date resolution (#428, design D12, D19; `note-tasks` spec,
"Relative dates SHALL be resolved once per call in the caller's task
timezone")."""
import datetime
from zoneinfo import ZoneInfo

import pytest

from src.services import task_dates
from src.services.task_dates import InvalidDate, effective_zone, is_relative, resolve, today

D = datetime.date
UTC = datetime.timezone.utc


def at(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_today_differs_by_time_zone():
    # Spec scenario "Today differs by time zone".
    now = at("2026-11-10T23:30:00")
    assert resolve("today", "UTC", now=now) == D(2026, 11, 10)
    assert resolve("today", "Pacific/Kiritimati", now=now) == D(2026, 11, 11)


@pytest.mark.parametrize(
    "instant, zone, expected_today",
    [
        # Kiritimati is UTC+14: midnight local is 10:00Z the day before.
        ("2026-11-10T09:59:59", "Pacific/Kiritimati", D(2026, 11, 10)),
        ("2026-11-10T10:00:00", "Pacific/Kiritimati", D(2026, 11, 11)),
        # Adak is UTC-10 in winter (HST/HDT zone): midnight local is 10:00Z.
        ("2026-11-11T09:59:59", "America/Adak", D(2026, 11, 10)),
        ("2026-11-11T10:00:00", "America/Adak", D(2026, 11, 11)),
        ("2026-11-10T23:59:59", "UTC", D(2026, 11, 10)),
        ("2026-11-11T00:00:00", "UTC", D(2026, 11, 11)),
    ],
)
@pytest.mark.parametrize(
    "form, offset",
    [("today", 0), ("TODAY", 0), ("tomorrow", 1), ("Yesterday", -1),
     ("+0d", 0), ("-0d", 0), ("+7d", 7), ("-7D", -7), ("+3660d", 3660), ("-3660d", -3660)],
)
def test_every_relative_form_around_midnight(instant, zone, expected_today, form, offset):
    now = at(instant)
    assert today(zone, now=now) == expected_today
    assert resolve(form, zone, now=now) == expected_today + datetime.timedelta(days=offset)
    assert is_relative(form)


def test_iso_dates_resolve_without_the_clock():
    assert resolve("2026-02-28", "UTC", now=at("2000-01-01T00:00:00")) == D(2026, 2, 28)
    assert not is_relative("2026-02-28")
    assert resolve(D(2026, 1, 2), "UTC") == D(2026, 1, 2)


@pytest.mark.parametrize(
    "value",
    ["+3661d", "-3661d", "+99999d", "next friday", "2026-02-30", "2026-2-1", " today",
     "today ", "+7", "7d", "+07d", "+1w", "", "2026-11-10T00:00", "now"],
)
def test_other_forms_are_invalid(value):
    # Spec scenario "Out-of-range offsets are refused".
    assert isinstance(resolve(value, "UTC", now=at("2026-11-10T12:00:00")), InvalidDate)


def test_a_datetime_is_not_a_date():
    assert isinstance(resolve(datetime.datetime(2026, 1, 1), "UTC"), InvalidDate)


def test_naive_clock_is_refused():
    with pytest.raises(ValueError):
        today("UTC", now=datetime.datetime(2026, 1, 1))


def test_injected_clock_is_the_only_clock(monkeypatch):
    import types

    class NoClock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError("the real clock was read")

    fake = types.SimpleNamespace(
        datetime=NoClock, date=datetime.date, timedelta=datetime.timedelta
    )
    monkeypatch.setattr(task_dates, "datetime", fake)
    assert resolve("tomorrow", ZoneInfo("UTC"), now=at("2026-12-31T12:00:00")) == D(2027, 1, 1)
    with pytest.raises(AssertionError):
        resolve("tomorrow", "UTC")


def test_missing_zone_falls_back_to_utc_and_says_so():
    zone = effective_zone("Mars/Olympus_Mons")
    assert zone.name == "UTC" and zone.unavailable
    assert resolve("today", zone, now=at("2026-11-10T23:30:00")) == D(2026, 11, 10)
    ok = effective_zone("Pacific/Kiritimati")
    assert ok.name == "Pacific/Kiritimati" and not ok.unavailable
    assert effective_zone(None).name == "UTC"
    assert not effective_zone("../../etc/passwd").name.startswith(".")
