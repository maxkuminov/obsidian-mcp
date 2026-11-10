"""The closed recurrence grammar and its dates (#428, design D7; `task-write`
spec, "Recurrence SHALL be a closed grammar of rule texts whose dates match
the Tasks plugin's"). The oracle is the plugin reference, R§2b/2c."""
import ast
import datetime
import pathlib

import pytest

from src.services import task_recurrence as R
from src.services.task_recurrence import Rule, Shift, Unsupported, next_date, next_occurrence, parse_rule

D = datetime.date


def rule(text):
    r = parse_rule(text)
    assert isinstance(r, Rule), (text, r)
    return r


def complete(text, *, due=None, scheduled=None, start=None, done_on=D(2026, 11, 10)):
    return next_occurrence(rule(text), due=due, scheduled=scheduled, start=start, done_on=done_on)


# ── R§2b/2c table ───────────────────────────────────────────────────────────


def test_month_end_falls_back_chain():
    # Spec "Month-end falls back"; R§2c: 2021-10-31 → 11-30 → 12-30 → 01-30 → 02-28 → 03-28.
    due = D(2021, 10, 31)
    seen = []
    for _ in range(5):
        shift = complete("every month", due=due, done_on=due)
        due = shift.due
        seen.append(due)
    assert seen == [D(2021, 11, 30), D(2021, 12, 30), D(2022, 1, 30), D(2022, 2, 28), D(2022, 3, 28)]


def test_every_three_months_from_january_31():
    # Spec "Every three months from January 31"; R§2c: → 04-30 → 07-30.
    first = complete("every 3 months", due=D(2022, 1, 31))
    assert first.due == D(2022, 4, 30)
    assert complete("every 3 months", due=first.due).due == D(2022, 7, 30)


def test_a_fixed_day_skips_short_months():
    # Spec "A fixed day skips short months"; R§2c: 01-31 → 03-31 → 05-31 → 07-31 → 08-31.
    due = D(2022, 1, 31)
    seen = []
    for _ in range(4):
        due = complete("every month on the 31st", due=due).due
        seen.append(due)
    assert seen == [D(2022, 3, 31), D(2022, 5, 31), D(2022, 7, 31), D(2022, 8, 31)]


def test_offsets_are_kept():
    # Spec "Offsets are kept"; R§2b: ⏳ 2021-10-28 📅 2021-10-30 every 2 weeks.
    shift = complete("every 2 weeks", scheduled=D(2021, 10, 28), due=D(2021, 10, 30))
    assert (shift.scheduled, shift.due) == (D(2021, 11, 11), D(2021, 11, 13))
    assert shift.reference == "due" and shift.start is None


def test_a_late_completion_does_not_skip_missed_occurrences():
    # Spec scenario; R§2b docs example (scheduled 2021-02-06, completed 2022-02-13).
    shift = complete("every week", scheduled=D(2021, 2, 6), done_on=D(2022, 2, 13))
    assert shift.scheduled == D(2021, 2, 13)
    assert shift.reference == "scheduled" and shift.due is None


def test_when_done_anchors_on_the_completion_date():
    # Spec "When done anchors on the completion date".
    shift = complete("every 10 days when done", due=D(2026, 11, 1), done_on=D(2026, 11, 5))
    assert shift.due == D(2026, 11, 15)


def test_when_done_keeps_offsets_from_the_reference():
    shift = complete("every week when done", start=D(2026, 10, 30), due=D(2026, 11, 1),
                     done_on=D(2026, 11, 5))
    assert shift.due == D(2026, 11, 12)
    assert shift.start == D(2026, 11, 10)


def test_no_dates_still_gives_a_next_occurrence_without_dates():
    # R§2b: `- [ ] this is a recurring task 🔁 every day` gets a dateless next line.
    shift = complete("every day")
    assert shift == Shift(None, None, None, None, None)
    assert shift.as_mapping() == {}
    assert complete("every year when done", done_on=D(2024, 2, 29)).as_mapping() == {}


def test_reference_order_is_due_scheduled_start():
    assert complete("every day", start=D(2026, 1, 1), scheduled=D(2026, 1, 5)).reference == "scheduled"
    assert complete("every day", start=D(2026, 1, 1)).reference == "start"
    shift = complete("every day", start=D(2026, 1, 1), scheduled=D(2026, 1, 5), due=D(2026, 1, 9))
    assert shift.reference == "due"
    assert shift.as_mapping() == {"start": D(2026, 1, 2), "scheduled": D(2026, 1, 6), "due": D(2026, 1, 10)}


def test_weekday_lists():
    # Spec "Weekday lists": Friday 2026-11-13, every week on Tuesday, Friday → Tuesday 2026-11-17.
    assert D(2026, 11, 13).weekday() == 4
    assert complete("every week on Tuesday, Friday", due=D(2026, 11, 13)).due == D(2026, 11, 17)
    assert complete("every Tuesday,Friday", due=D(2026, 11, 13)).due == D(2026, 11, 17)
    assert complete("every week on Tuesday, Friday", due=D(2026, 11, 17)).due == D(2026, 11, 20)


@pytest.mark.parametrize(
    "text, anchor, expected",
    [
        ("every sunday", D(2026, 11, 14), D(2026, 11, 15)),  # Sat → Sun
        ("every sunday", D(2026, 11, 15), D(2026, 11, 22)),  # Sun → next Sun (strictly after)
        ("every monday", D(2026, 12, 31), D(2027, 1, 4)),  # across a year boundary
        ("every week on saturday, sunday", D(2026, 11, 15), D(2026, 11, 21)),
        ("every weekday", D(2026, 11, 13), D(2026, 11, 16)),  # Fri → Mon
        ("every weekday", D(2026, 11, 14), D(2026, 11, 16)),  # Sat → Mon
        ("every weekday", D(2026, 11, 16), D(2026, 11, 17)),
        ("every day", D(2026, 12, 31), D(2027, 1, 1)),
        ("every 3 days", D(2026, 2, 27), D(2026, 3, 2)),
        ("every week", D(2026, 12, 28), D(2027, 1, 4)),
        ("every 2 weeks", D(2026, 1, 1), D(2026, 1, 15)),
        ("every month on the last", D(2024, 2, 10), D(2024, 2, 29)),  # leap February
        ("every month on the last", D(2023, 2, 10), D(2023, 2, 28)),  # non-leap February
        ("every month on the last", D(2024, 1, 31), D(2024, 2, 29)),
        ("every month on the last", D(2023, 1, 31), D(2023, 2, 28)),
        ("every month on the last", D(2026, 12, 31), D(2027, 1, 31)),
        ("every month on the 1st", D(2026, 12, 15), D(2027, 1, 1)),
        ("every month on the 15th", D(2026, 1, 14), D(2026, 1, 15)),
        ("every month on the 15th", D(2026, 1, 15), D(2026, 2, 15)),
        ("every month on the 29th", D(2023, 1, 29), D(2023, 3, 29)),
        ("every month on the 29th", D(2024, 1, 29), D(2024, 2, 29)),
        ("every 6 months", D(2026, 8, 31), D(2027, 2, 28)),
        ("every 12 months", D(2026, 1, 31), D(2027, 1, 31)),
        ("every year", D(2026, 2, 28), D(2027, 2, 28)),  # Feb 28 accepted
        ("every 4 years", D(2023, 3, 1), D(2027, 3, 1)),
        ("every january on the 15th", D(2026, 1, 14), D(2026, 1, 15)),
        ("every january on the 15th", D(2026, 1, 15), D(2027, 1, 15)),
        ("every february on the 28th", D(2026, 3, 1), D(2027, 2, 28)),
        ("every december on the 31st", D(2026, 12, 31), D(2027, 12, 31)),
    ],
)
def test_next_date_table(text, anchor, expected):
    assert next_date(rule(text), anchor) == expected


def test_yearly_from_february_29_is_refused():
    assert isinstance(complete("every year", due=D(2024, 2, 29)), Unsupported)
    assert isinstance(complete("every 4 years", due=D(2024, 2, 29)), Unsupported)
    assert isinstance(complete("every year when done", due=D(2024, 1, 1), done_on=D(2024, 2, 29)), Unsupported)
    # Feb 29 as a non-reference date keeps its whole-day offset (one day
    # before the due date).
    shift = complete("every year", start=D(2024, 2, 29), due=D(2024, 3, 1))
    assert shift.due == D(2025, 3, 1) and shift.start == D(2025, 2, 28)


def test_out_of_range_is_unsupported_not_an_error():
    assert isinstance(complete("every day", due=D(9999, 12, 31)), Unsupported)
    assert isinstance(complete("every year", due=D(9999, 6, 1)), Unsupported)


# ── the grammar ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, kind, interval, when_done",
    [
        ("every day", R.DAY, 1, False),
        ("Every Day", R.DAY, 1, False),
        ("EVERY   DAY", R.DAY, 1, False),
        ("  every day  ", R.DAY, 1, False),
        ("every 2 days", R.DAY, 2, False),
        ("every 999 days", R.DAY, 999, False),
        ("every day when done", R.DAY, 1, True),
        ("every 10 days WHEN DONE", R.DAY, 10, True),
        ("every day  when done", R.DAY, 1, True),
        ("every weekday", R.WEEKDAY, 1, False),
        ("every week", R.WEEK, 1, False),
        ("every 3 weeks", R.WEEK, 3, False),
        ("every week on Sunday", R.WEEK_ON, 1, False),
        ("every Sunday", R.WEEK_ON, 1, False),
        ("every Sunday when done", R.WEEK_ON, 1, True),
        ("every week on Tuesday, Friday", R.WEEK_ON, 1, False),
        ("every month", R.MONTH, 1, False),
        ("every 6 months", R.MONTH, 6, False),
        ("every month on the 1st", R.MONTH_ON_DAY, 1, False),
        ("every month on the 31st", R.MONTH_ON_DAY, 1, False),
        ("every month on the last", R.MONTH_ON_LAST, 1, False),
        ("every year", R.YEAR, 1, False),
        ("every year when done", R.YEAR, 1, True),
        ("every 2 years", R.YEAR, 2, False),
        ("every January on the 15th", R.YEAR_ON, 1, False),
        ("every February on the 28th", R.YEAR_ON, 1, False),
    ],
)
def test_tier_one_shapes_parse(text, kind, interval, when_done):
    r = rule(text)
    assert (r.kind, r.interval, r.when_done) == (kind, interval, when_done)
    assert r.text == text.strip(" ")


@pytest.mark.parametrize("k", range(1, 32))
def test_every_ordinal_suffix(k):
    suffix = "th" if k in (11, 12, 13) else {1: "st", 2: "nd", 3: "rd"}.get(k % 10, "th")
    assert rule(f"every month on the {k}{suffix}").day == k
    for wrong in {"st", "nd", "rd", "th"} - {suffix}:
        assert isinstance(parse_rule(f"every month on the {k}{wrong}"), Unsupported)


TIER_TWO_AND_GARBAGE = [
    # Tier 2 (D7): no dates documented in R, or lossy.
    "every 2 weeks on Monday",
    "every 3 weeks on Friday",
    "every month on the last Friday",
    "every month on the 2nd Wednesday",
    "every month on the 2nd last Friday",
    "every 6 months on the 2nd Wednesday",
    "every 2 months on the 15th",
    "every April and December on the 1st and 24th",
    "every February on the last",
    "every weekend",
    "every mon",
    "every Tues",
    "every wk",
    "every 1 day",
    "every 1 week",
    "every 01 days",
    "every 1000 days",
    "every 0 days",
    "every day for 3 times",
    "every 2 days for 3 times",
    "every day until January 1",
    "every other week",
    "every Monday and Friday",
    "every Monday, Monday",
    "every Monday,",
    "every Monday ,Friday",
    "every month on the 32nd",
    "every month on the 0th",
    "every February on the 29th",
    "every February on the 30th",
    "every April on the 31st",
    # Garbage.
    "",
    "   ",
    "every",
    "daily",
    "weekly",
    "every day!",
    "every day when done when done",
    "every day when  done",
    "every day whendone",
    "every hour",
    "every 2 hours",
    "every day at 10",
    "invalid rule",
    "every day 📅",
    "every-day",
    "every day.",
    "every\tday",
    "every\u00a0day",
    "\u0435very day",  # Cyrillic \u0435
]


@pytest.mark.parametrize("text", TIER_TWO_AND_GARBAGE)
def test_tier_two_and_garbage_are_refused(text):
    assert isinstance(parse_rule(text), Unsupported), text


def test_spec_unsupported_scenario():
    # Spec "Unsupported and lossy rules are refused".
    for text in ("every weekend", "every 2 weeks on Monday", "every month on the 2nd Wednesday"):
        assert isinstance(parse_rule(text), Unsupported)
    assert isinstance(complete("every year", due=D(2024, 2, 29)), Unsupported)


def test_non_string_is_unsupported():
    assert isinstance(parse_rule(None), Unsupported)


def test_module_is_pure_stdlib():
    source = pathlib.Path(R.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "calendar", "datetime", "re", "dataclasses"}
