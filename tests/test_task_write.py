"""Task write planning (#428, design D2–D9, D16; tasks.md 5.1–5.3).

Pure tests: every `task-write` scenario reachable without publishing or a
database, the D6 table, recurrence placement and terminators, `add_task`
placement, continuation blocks, every D4 (a)–(d) refusal, and `verify`
catching injected corruptions — plus seeded property tests over a generated
corpus of notes.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import random

import pytest

from src.services import refusals
from src.services import task_line as tl
from src.services import task_write as tw
from src.services.task_settings import EffectiveTaskSettings
from src.services.tasks import tasks_from_note_text

D = dt.date
TODAY = D(2026, 11, 10)


def settings(**kw) -> EffectiveTaskSettings:
    base = dict(
        timezone="UTC", timezone_fallback=False, configured_timezone=None,
        excluded_folders=(), excluded_note_tags=(), set_done_date=True,
        set_cancelled_date=True, set_created_date=False, next_recurrence_below=False,
    )
    base.update(kw)
    return EffectiveTaskSettings(**base)


def key(line: str) -> str:
    parsed = tl.parse(line)
    assert parsed is not None, line
    return tl.task_key(parsed.full_text)


def ok(plan) -> tw.Plan:
    assert isinstance(plan, tw.Plan), plan
    if plan.changed:
        assert tw.verify(_old_of(plan), plan.new_text, plan) is None
    return plan


_OLD: dict[int, str] = {}


def _old_of(plan):
    return _OLD[id(plan)]


def complete(note, line_or_id, done_on=TODAY, today=TODAY, hash_supplied=False, **s):
    ident = line_or_id if "[" not in line_or_id else key(line_or_id)
    plan = tw.plan_complete(note, tw.CompleteArgs(ident, done_on, hash_supplied), settings(**s), today)
    if isinstance(plan, tw.Plan):
        _OLD[id(plan)] = note
    return plan


def update(note, line_or_id, today=TODAY, s=None, hash_supplied=False, **kw):
    ident = line_or_id if "[" not in line_or_id else key(line_or_id)
    plan = tw.plan_update(note, tw.UpdateArgs(ident, hash_supplied, **kw), settings(**(s or {})), today)
    if isinstance(plan, tw.Plan):
        _OLD[id(plan)] = note
    return plan


def add(note, text, today=TODAY, s=None, **kw):
    plan = tw.plan_add(note, tw.AddArgs(text, **kw), settings(**(s or {})), today)
    if isinstance(plan, tw.Plan):
        _OLD[id(plan)] = note
    return plan


def refused(plan, code):
    assert isinstance(plan, tw.TaskWriteRefusal), plan
    assert plan.code == code, plan
    return plan


# ── the refusal catalogue (5.2) ──────────────────────────────────────────────

NEW_CODES = {
    "task_not_found", "task_ambiguous", "task_changed", "already_completed",
    "task_cancelled", "task_malformed", "recurrence_unsupported",
    "on_completion_unsupported", "duplicate_task", "response_cap_too_small",
}


def test_new_codes_are_declared_body_codes():
    assert NEW_CODES == set(refusals.TASK_CODES)
    assert NEW_CODES <= refusals.BODY_CODES <= refusals.CODES
    for code in NEW_CODES:
        r = refusals.Refusal(code, nothing_written=True)
        assert r.as_payload() == {"code": code, "nothing_written": True}
        assert refusals.sentinel_line(r) == f'MCP-REFUSAL {{"code":"{code}","nothing_written":true}}'


def test_refusal_type_rejects_undeclared_codes():
    with pytest.raises(ValueError):
        tw.TaskWriteRefusal("not_a_code", "x")


# ── ids and resolution (D1, D2) ──────────────────────────────────────────────


def tasks_of(note):
    got, truncated = tasks_from_note_text(note)
    assert not truncated
    return got


def test_identical_lines_get_ordinals():
    note = "- [ ] Call\n- [ ] Call\n- [ ] Call\n- [x] Call\n"
    ts = tasks_of(note)
    k = key("- [ ] Call")
    assert {t.task_key for t in ts} == {k}
    assert [tl.render_id(t.task_key, t.key_ordinal, t.key_count) for t in ts] == [
        f"{k}.1of4", f"{k}.2of4", f"{k}.3of4", f"{k}.4of4"]


def test_indentation_and_status_do_not_change_the_key():
    assert key("- [ ] Pay rent") == key("    - [ ] Pay rent") == key("- [/] Pay rent") == key("> 1. [x] Pay rent")


def test_the_id_survives_the_text_cut():
    long = "a" * 1500
    (t,) = tasks_of(f"- [ ] {long}\n")
    assert len(t.text) == 1000 and t.text_cut
    assert t.task_key == tl.task_key(long)


def test_resolve_table():
    note = "- [ ] a\n- [ ] dup\n- [ ] dup\n"
    ts = tasks_of(note)
    ka, kd = key("- [ ] a"), key("- [ ] dup")
    assert tw.resolve(ts, ka, hash_supplied=False).line == 1
    assert tw.resolve(ts, ka + ".1of1", hash_supplied=False).line == 1
    refused(tw.resolve(ts, "0" * 12, hash_supplied=False), "task_not_found")
    r = refused(tw.resolve(ts, kd, hash_supplied=False), "task_ambiguous")
    assert [(m.id, m.line) for m in r.matches] == [(f"{kd}.1of2", 2), (f"{kd}.2of2", 3)]
    r = refused(tw.resolve(ts, f"{kd}.2of2", hash_supplied=False), "precondition_required")
    assert "read_note" in r.reason and "content_hash" in r.reason
    assert tw.resolve(ts, f"{kd}.2of2", hash_supplied=True).line == 3
    refused(tw.resolve(ts, f"{kd}.1of3", hash_supplied=True), "task_changed")
    refused(tw.resolve(ts, "nothex", hash_supplied=False), "invalid_argument")
    # A suffixed id is refused before matching even when its key is unknown.
    refused(tw.resolve(ts, "0" * 12 + ".1of2", hash_supplied=False), "precondition_required")


def test_a_short_text_does_not_match_inside_a_longer_one():
    note = "- [ ] Call Bob\n- [ ] Call Bob about the lease\n"
    p = ok(complete(note, "- [ ] Call Bob"))
    assert p.new_text == f"- [x] Call Bob ✅ {TODAY}\n- [ ] Call Bob about the lease\n"


def test_a_bare_key_over_duplicates_is_refused():
    note = "- [ ] Water plants\n- [ ] Water plants\n"
    r = refused(complete(note, "- [ ] Water plants"), "task_ambiguous")
    assert len(r.matches) == 2


def test_a_suffixed_id_without_a_hash_is_refused():
    note = "- [ ] Water plants\n- [ ] Water plants\n"
    refused(complete(note, key("- [ ] Water plants") + ".2of2"), "precondition_required")


def test_swapped_identical_lines_resolve_against_the_current_bytes():
    k = key("- [ ] Call")
    current = "- [ ] Call\n    second context\n- [ ] Call\n    first context\n"
    p = ok(complete(current, f"{k}.1of2", hash_supplied=True))
    assert p.new_text.splitlines()[0] == f"- [x] Call ✅ {TODAY}"
    assert p.new_text.splitlines()[2] == "- [ ] Call"


def test_a_line_changed_since_the_listing_is_not_found():
    refused(update("- [ ] Pay the rent\n", key("- [ ] Pay rent"), due=D(2026, 1, 1)), "task_not_found")


@pytest.mark.parametrize("note", [
    "```\n- [ ] example\n```\n",
    "---\nnotes: |\n  - [ ] x\n---\nbody\n",
])
def test_checkboxes_the_extractor_does_not_recognise_are_never_targets(note):
    for line in ("- [ ] example", "- [ ] x"):
        refused(complete(note, line), "task_not_found")


def test_a_note_over_the_task_cap_is_refused_size_limit():
    note = "".join(f"- [ ] t{i}\n" for i in range(5001))
    refused(complete(note, "- [ ] t1"), "size_limit")
    refused(add(note, "new"), "size_limit")


def test_adding_beyond_the_cap_is_refused_size_limit():
    note = "".join(f"- [ ] t{i}\n" for i in range(5000))
    refused(add(note, "one more"), "size_limit")


def test_a_note_with_nul_is_refused():
    refused(complete("- [ ] a\x00\n", "- [ ] a"), "task_malformed")


# ── continuation blocks (D8) ─────────────────────────────────────────────────


def test_continuation_block_rules():
    lines = [
        "- [ ] a",        # 0
        "    c1",          # 1
        "",                # 2 blank, crossed
        "\tc2",            # 3 tab = 4
        "    - [ ] child", # 4
        "        deeper",  # 5
        "",                # 6 trailing blank
        "next",            # 7
    ]
    assert tw.continuation_block(lines, 0) == 6
    assert tw.continuation_block(lines, 4) == 6
    assert tw.continuation_block(["- [ ] a", "", "b"], 0) == 1
    # Blockquotes: greater depth belongs; same depth needs greater width.
    q = ["> - [ ] a", ">     more", "> > nested", "> back", "after"]
    assert tw.continuation_block(q, 0) == 3
    assert tw.continuation_block(["- [ ] a", "> quoted"], 0) == 2
    # No cap: a 200-line block.
    big = ["- [ ] a"] + ["    x"] * 200 + ["y"]
    assert tw.continuation_block(big, 0) == 201


def test_owners_agree_with_continuation_blocks_on_a_corpus():
    rng = random.Random(7)
    for _ in range(300):
        note = random_note(rng)
        view = tw._NoteView(note)
        lines = view.contents()
        task_set = {t.line - 1 for t in tasks_of(note)}
        owners = tw._owners(lines, task_set)
        for j in range(len(lines)):
            want = None
            for t in sorted(task_set):
                if t < j < tw.continuation_block(lines, t):
                    want = t
            assert owners[j] == want, (note, j)


# ── complete_task (D6, D7) ───────────────────────────────────────────────────


def test_a_plain_completion():
    p = ok(complete("- [ ] Send invoice 📅 2026-11-10\n", "- [ ] Send invoice 📅 2026-11-10"))
    assert p.new_text == "- [x] Send invoice 📅 2026-11-10 ✅ 2026-11-10\n"
    assert p.new_lines[0].kind == "target" and p.new_lines[0].line == 1
    assert p.new_ids["target"] == key("- [x] Send invoice 📅 2026-11-10 ✅ 2026-11-10")
    assert p.replaced_status == " "


def test_done_date_off_changes_only_the_marker():
    p = ok(complete("- [ ] Send invoice 📅 2026-11-10\n", "- [ ] Send invoice 📅 2026-11-10", set_done_date=False))
    assert p.new_text == "- [x] Send invoice 📅 2026-11-10\n"
    assert [s for s in p.spans if s.start != s.end or s.text] == [tw.Span(3, 4, "x")]


def test_an_existing_single_done_date_is_replaced():
    p = ok(complete("- [/] a ✅ 2020-01-01\n", "- [/] a ✅ 2020-01-01"))
    assert p.new_text == f"- [x] a ✅ {TODAY}\n"


@pytest.mark.parametrize("marker", ["x", "X"])
def test_completing_a_completed_task(marker):
    refused(complete(f"- [{marker}] a\n", f"- [{marker}] a"), "already_completed")


def test_completing_a_cancelled_task():
    refused(complete("- [-] a\n", "- [-] a"), "task_cancelled")


def test_a_custom_marker_completes_to_x():
    p = ok(complete("- [>] Forwarded item\n", "- [>] Forwarded item"))
    assert p.new_text == f"- [x] Forwarded item ✅ {TODAY}\n"
    assert p.replaced_status == ">"


def test_on_completion_delete_is_refused_and_keep_is_kept():
    refused(complete("- [ ] a 🏁 delete\n", "- [ ] a 🏁 delete"), "on_completion_unsupported")
    refused(complete("- [ ] a 🏁 Delete 📅 2026-01-01\n", "- [ ] a 🏁 Delete 📅 2026-01-01"), "on_completion_unsupported")
    p = ok(complete("- [ ] a 🏁 keep\n", "- [ ] a 🏁 keep"))
    assert p.new_text == f"- [x] a 🏁 keep ✅ {TODAY}\n"


@pytest.mark.parametrize("rule, due", [
    ("every month on the last Friday", "2026-11-27"),
    ("every weekend", "2026-11-14"),
    ("every 2 weeks on Monday", "2026-11-16"),
    ("every month on the 2nd Wednesday", "2026-11-11"),
    ("every year", "2024-02-29"),
    ("every 1 day", "2026-11-11"),
])
def test_unsupported_rules_refuse_the_whole_completion(rule, due):
    line = f"- [ ] Review 🔁 {rule} 📅 {due}"
    refused(complete(line + "\n", line), "recurrence_unsupported")


def _complete_chain(line, times, **s):
    note = line + "\n"
    dues = []
    for _ in range(times):
        tasks = [t for t in tasks_of(note) if t.state == "open"]
        (t,) = tasks
        p = ok(complete(note, tl.render_id(t.task_key, t.key_ordinal, t.key_count), done_on=t.due, **s))
        note = p.new_text
        nxt = next(n for n in p.new_lines if n.kind == "next")
        dues.append(tl.field_date(tl.parse(nxt.text), tl.DUE).isoformat())
        note = nxt.text + "\n"  # keep only the open occurrence for the next round
    return dues


def test_month_end_falls_back():
    assert _complete_chain("- [ ] x 🔁 every month 📅 2021-10-31", 4) == [
        "2021-11-30", "2021-12-30", "2022-01-30", "2022-02-28"]


def test_every_three_months_from_january_31():
    assert _complete_chain("- [ ] x 🔁 every 3 months 📅 2022-01-31", 1) == ["2022-04-30"]


def test_a_fixed_day_skips_short_months():
    assert _complete_chain("- [ ] x 🔁 every month on the 31st 📅 2022-01-31", 1) == ["2022-03-31"]


def test_weekday_lists():
    assert _complete_chain("- [ ] x 🔁 every week on Tuesday, Friday 📅 2026-11-13", 1) == ["2026-11-17"]


def test_offsets_are_kept():
    line = "- [ ] x 🔁 every 2 weeks ⏳ 2021-10-28 📅 2021-10-30"
    p = ok(complete(line + "\n", line, done_on=D(2021, 10, 30)))
    nxt = next(n for n in p.new_lines if n.kind == "next")
    assert nxt.text == "- [ ] x 🔁 every 2 weeks ⏳ 2021-11-11 📅 2021-11-13"


def test_a_late_completion_does_not_skip_missed_occurrences():
    line = "- [ ] x 🔁 every week ⏳ 2021-02-06"
    p = ok(complete(line + "\n", line, done_on=D(2022, 2, 13)))
    assert next(n for n in p.new_lines if n.kind == "next").text == "- [ ] x 🔁 every week ⏳ 2021-02-13"


def test_when_done_anchors_on_the_completion_date():
    line = "- [ ] x 🔁 every 10 days when done 📅 2026-11-01"
    p = ok(complete(line + "\n", line, done_on=D(2026, 11, 5)))
    assert next(n for n in p.new_lines if n.kind == "next").text == "- [ ] x 🔁 every 10 days when done 📅 2026-11-15"


def test_no_reference_date_still_writes_a_next_occurrence():
    line = "- [ ] x 🔁 every week"
    p = ok(complete(line + "\n", line))
    assert p.new_text == f"- [ ] x 🔁 every week\n- [x] x 🔁 every week ✅ {TODAY}\n"


def test_the_plugins_dropped_fields_are_dropped():
    line = "- [ ] Rent 🆔 r1 ⛔ a2 🔁 every month ➕ 2026-01-01 📅 2026-11-01 ✅ 2026-10-01 ^b1"
    p = ok(complete(line + "\n", line, done_on=D(2026, 11, 2), today=D(2026, 11, 2)))
    assert p.new_text == (
        "- [ ] Rent 🔁 every month 📅 2026-12-01\n"
        "- [x] Rent 🆔 r1 ⛔ a2 🔁 every month ➕ 2026-01-01 📅 2026-11-01 ✅ 2026-11-02 ^b1\n"
    )
    assert p.position == "above"
    nxt = next(n for n in p.new_lines if n.kind == "next")
    assert (nxt.line, p.new_ids["next"]) == (1, key(nxt.text))
    assert next(n for n in p.new_lines if n.kind == "target").line == 2


def test_created_date_on_the_next_line_follows_the_setting():
    line = "- [ ] Rent 🔁 every month ➕ 2026-01-01 📅 2026-11-01"
    p = ok(complete(line + "\n", line, done_on=D(2026, 11, 2), set_created_date=True))
    assert p.new_text.splitlines()[0] == f"- [ ] Rent 🔁 every month ➕ {TODAY} 📅 2026-12-01"
    line = "- [ ] Rent 🔁 every month 📅 2026-11-01"
    p = ok(complete(line + "\n", line, done_on=D(2026, 11, 2), set_created_date=True))
    assert p.new_text.splitlines()[0] == f"- [ ] Rent 🔁 every month ➕ {TODAY} 📅 2026-12-01"


def test_rule_text_is_not_canonicalised():
    line = "- [ ] Water 🔁 every Sunday 📅 2026-11-08"
    p = ok(complete(line + "\n", line))
    assert p.new_text.splitlines()[0] == "- [ ] Water 🔁 every Sunday 📅 2026-11-15"


def test_below_at_end_of_file_without_a_newline():
    note = "intro\n- [ ] x 🔁 every day 📅 2026-11-10"
    p = ok(complete(note, "- [ ] x 🔁 every day 📅 2026-11-10", next_recurrence_below=True))
    assert p.new_text == f"intro\n- [x] x 🔁 every day 📅 2026-11-10 ✅ {TODAY}\n- [ ] x 🔁 every day 📅 2026-11-11"
    assert p.position == "below"


def test_above_at_end_of_file_without_a_newline():
    note = "intro\r\n- [ ] x 🔁 every day 📅 2026-11-10"
    p = ok(complete(note, "- [ ] x 🔁 every day 📅 2026-11-10"))
    assert p.new_text == f"intro\r\n- [ ] x 🔁 every day 📅 2026-11-11\r\n- [x] x 🔁 every day 📅 2026-11-10 ✅ {TODAY}"


@pytest.mark.parametrize("below", [False, True])
def test_the_terminator_follows_the_original_line_in_a_mixed_note(below):
    note = "# h\n- [ ] x 🔁 every day 📅 2026-11-10\r\n    ctx\n    ctx2\nafter\n"
    p = ok(complete(note, "- [ ] x 🔁 every day 📅 2026-11-10", next_recurrence_below=below))
    nxt = "- [ ] x 🔁 every day 📅 2026-11-11"
    done = f"- [x] x 🔁 every day 📅 2026-11-10 ✅ {TODAY}"
    if below:
        assert p.new_text == f"# h\n{done}\r\n    ctx\n    ctx2\n{nxt}\r\nafter\n"
    else:
        assert p.new_text == f"# h\n{nxt}\r\n{done}\r\n    ctx\n    ctx2\nafter\n"


def test_below_keeps_the_completed_tasks_children():
    note = "- [ ] x 🔁 every day 📅 2026-11-10\n    a\n    b\n    c\n- [ ] y\n"
    p = ok(complete(note, "- [ ] x 🔁 every day 📅 2026-11-10", next_recurrence_below=True))
    lines = p.new_text.splitlines()
    assert lines[1:4] == ["    a", "    b", "    c"]
    assert lines[4] == "- [ ] x 🔁 every day 📅 2026-11-11"
    view = tw._NoteView(p.new_text)
    owners = tw._owners(view.contents(), {t.line - 1 for t in tasks_of(p.new_text)})
    assert owners[1:4] == [0, 0, 0]


def test_below_with_a_ten_line_block_with_a_blank_and_a_deeper_child():
    block = ["    c1", "    c2", "", "    - [ ] child", "        deep", "    c3", "    c4", "    c5", "    c6", "    c7"]
    note = "- [ ] x 🔁 every day 📅 2026-11-10\n" + "\n".join(block) + "\n\nprose\n"
    p = ok(complete(note, "- [ ] x 🔁 every day 📅 2026-11-10", next_recurrence_below=True))
    lines = p.new_text.splitlines()
    assert lines[1:11] == block
    assert lines[11] == "- [ ] x 🔁 every day 📅 2026-11-11"


def test_prefix_hard_break_and_crlf_are_preserved():
    note = "top\n> \t1. [ ] Ship it  \r\nbottom\n"
    p = ok(complete(note, "> \t1. [ ] Ship it  "))
    assert p.new_text == f"top\n> \t1. [x] Ship it ✅ {TODAY}  \r\nbottom\n"


# ── the shared extraction reads canonical text; plans edit the original ─────

_TERMINATOR_NOTES = [
    "a\r\n- [ ] Call\r\n    ctx one\r\n- [ ] Call\r\n    ctx two\r\n",
    "a\r- [ ] Call\r    ctx one\r- [ ] Call\r    ctx two",
    "---\r\nx: 1\r---\n# H\r- [ ] Call\n    ctx one\r\n- [ ] Call\r    ctx two\n",
    "```\r- [ ] fenced\r\n```\n- [ ] Call\r\n\r\n- [ ] Call\r",
]


@pytest.mark.parametrize("note", _TERMINATOR_NOTES)
def test_shared_extraction_lines_and_keys_match_the_original_lines(note):
    """Canonicalisation rewrites only terminators, so every extracted line
    number names the same ORIGINAL line, and the key the shared function
    computes equals the key of that original line as the planner parses it."""
    view = tw._NoteView(note)
    ts = tasks_of(note)
    assert ts
    for t in ts:
        parsed = view.parse(t.line - 1)
        assert parsed is not None
        assert tl.task_key(parsed.full_text) == t.task_key
        assert parsed.status == t.status_char


@pytest.mark.parametrize("note", _TERMINATOR_NOTES)
def test_an_ordinal_resolves_to_the_same_original_line_under_any_terminators(note):
    k = key("- [ ] Call")
    p = ok(complete(note, f"{k}.2of2", hash_supplied=True))
    view_old, view_new = tw._NoteView(note), tw._NoteView(p.new_text)
    calls = [i for i, c in enumerate(view_old.contents()) if c == "- [ ] Call"]
    second = calls[1]
    assert view_new.content(second) == f"- [x] Call ✅ {TODAY}"
    assert view_new.content(calls[0]) == "- [ ] Call"
    # Every terminator byte is the original one.
    assert [ln.term for ln in view_new.lines] == [ln.term for ln in view_old.lines]


@pytest.mark.parametrize("note", _TERMINATOR_NOTES)
@pytest.mark.parametrize("below", [False, True])
def test_recurring_and_added_lines_under_any_terminators(note, below):
    rec = note.replace("- [ ] Call", "- [ ] Call 🔁 every day 📅 2026-11-10", 1)
    p = ok(complete(rec, "- [ ] Call 🔁 every day 📅 2026-11-10", next_recurrence_below=below))
    assert "- [ ] Call 🔁 every day 📅 2026-11-11" in p.new_text
    ok(add(note, "fresh", context="why"))
    ok(update(note, key("- [ ] Call") + ".1of2", hash_supplied=True, append_context="later"))


def test_lone_cr_notes():
    note = "a\r- [ ] b\rc"
    p = ok(complete(note, "- [ ] b"))
    assert p.new_text == f"a\r- [x] b ✅ {TODAY}\rc"


@pytest.mark.parametrize("line", [
    "- [ ] x 🔁 every day 📅 2026-11-10 📅 2026-11-11",   # (b) duplicated
    "- [ ] x 📅 2026-11-10 later 🔁 every day",           # (c) stranded date
    "- [ ] x 🔁 every day 📅 2026-02-30",                 # (d) invalid reference date
    "- [ ] x 🔁 every day ⏳ 2026-13-01 📅 2026-11-10",   # (d) invalid other date
    "- [ ] x 🔁 every day ➕ 2026-01-01 ➕ 2026-01-02",    # created duplicated
])
def test_a_recurring_completion_refuses_defective_dates(line):
    refused(complete(line + "\n", line), "task_malformed")


def test_a_duplicated_done_date_is_refused_on_completion():
    line = "- [ ] x ✅ 2026-01-01 ✅ 2026-01-02"
    refused(complete(line + "\n", line), "task_malformed")


def test_a_recurrence_signifier_outside_the_run_is_refused():
    line = "- [ ] fix 🔁 every day — later 📅 2026-11-10"
    refused(complete(line + "\n", line), "task_malformed")


def test_completion_inside_a_callout_and_a_nested_task():
    note = "> [!todo]\n> - [ ] parent\n>     - [ ] child\n>       ctx\n"
    p = ok(complete(note, ">     - [ ] child"))
    assert p.new_text == f"> [!todo]\n> - [ ] parent\n>     - [x] child ✅ {TODAY}\n>       ctx\n"


def test_planners_are_rerunnable_on_fresh_text():
    note = "- [ ] a\n- [ ] b\n"
    p1 = ok(complete(note, "- [ ] a"))
    p2 = ok(complete(p1.new_text, "- [ ] b"))
    assert p2.new_text == f"- [x] a ✅ {TODAY}\n- [x] b ✅ {TODAY}\n"
    # A retry of the same completion against the completed bytes.
    refused(complete(p1.new_text, "- [ ] a"), "task_not_found")
    p3 = ok(complete(note, "- [ ] a", set_done_date=False))
    refused(complete(p3.new_text, "- [ ] a", set_done_date=False), "already_completed")


# ── update_task (D4, D6, D8) ─────────────────────────────────────────────────


def test_a_non_canonical_order_is_preserved():
    line = "- [ ] Pay #home 📅 2026-11-01 🔁 every month ^rent"
    p = ok(update(line, line, due=D(2026, 11, 5)))
    assert p.new_text == "- [ ] Pay #home 📅 2026-11-05 🔁 every month ^rent"
    assert p.fields_changed == ("due",)


def test_an_added_field_goes_to_its_canonical_slot():
    line = "- [ ] Pay 🔁 every month 📅 2026-11-05"
    p = ok(update(line, line, scheduled=D(2026, 11, 3)))
    assert p.new_text == "- [ ] Pay 🔁 every month ⏳ 2026-11-03 📅 2026-11-05"


def test_a_variant_signifier_is_kept():
    p = ok(update("- [ ] x 📆 2026-01-01", "- [ ] x 📆 2026-01-01", due=D(2026, 2, 2)))
    assert p.new_text == "- [ ] x 📆 2026-02-02"


def test_a_duplicated_due_date_is_refused():
    line = "- [ ] x 📅 2026-01-01 📅 2026-02-01"
    refused(update(line, line, due=D(2026, 3, 1)), "task_malformed")


def test_a_stranded_signifier_is_refused():
    line = "- [ ] move 📅 2026-01-01 to the archive"
    refused(update(line, line, due=D(2026, 3, 1)), "task_malformed")


def test_code_inside_the_run_is_refused():
    line = "- [ ] x `a` 📅 2026-01-01 `b`"
    refused(update(line, line, due=D(2026, 3, 1)), "task_malformed")


def test_an_invalid_date_in_a_changed_field_is_refused():
    line = "- [ ] x 📅 2026-02-30"
    refused(update(line, line, due=D(2026, 3, 1)), "task_malformed")


def test_a_context_line_that_would_create_a_task_is_refused():
    line = "- [ ] x"
    refused(update(line + "\n", line, append_context="[ ] sneaky"), "invalid_argument")


def test_reopening_removes_the_done_date():
    p = ok(update("- [x] Ship ✅ 2026-11-01", "- [x] Ship ✅ 2026-11-01", status="open"))
    assert p.new_text == "- [ ] Ship"


def test_in_progress_removes_done_and_cancelled():
    p = ok(update("- [-] Ship ❌ 2026-11-01", "- [-] Ship ❌ 2026-11-01", status="in_progress"))
    assert p.new_text == "- [/] Ship"


def test_cancelling_a_recurring_task_writes_no_next_occurrence():
    line = "- [ ] Rent 🔁 every month 📅 2026-11-01"
    p = ok(update(line, line, today=D(2026, 11, 2), status="cancelled"))
    assert p.new_text == "- [-] Rent 🔁 every month 📅 2026-11-01 ❌ 2026-11-02"
    assert p.inserted == ()


def test_cancelling_keeps_an_existing_cancelled_date_and_removes_done():
    line = "- [x] a ❌ 2026-01-01 ✅ 2026-01-02"
    p = ok(update(line, line, status="cancelled"))
    assert p.new_text == "- [-] a ❌ 2026-01-01"
    p = ok(update("- [ ] a", "- [ ] a", status="cancelled", s={"set_cancelled_date": False}))
    assert p.new_text == "- [-] a"


def test_asking_update_task_to_complete_is_refused():
    r = refused(update("- [ ] a", "- [ ] a", status="done"), "invalid_argument")
    assert "complete_task" in r.reason


def test_set_and_clear_of_one_field_is_refused_and_no_change_is_refused():
    refused(update("- [ ] a", "- [ ] a", due=D(2026, 1, 1), clear=frozenset({"due"})), "invalid_argument")
    refused(update("- [ ] a", "- [ ] a"), "invalid_argument")
    refused(update("- [ ] a", "- [ ] a", clear=frozenset({"done"})), "invalid_argument")


def test_clearing_fields_and_setting_priority_and_recurrence():
    line = "- [ ] a ⏫ 🔁 every week 🛫 2026-01-01 ⏳ 2026-01-02 📅 2026-01-03"
    p = ok(update(line, line, clear=frozenset({"due", "start", "priority", "recurrence"})))
    assert p.new_text == "- [ ] a ⏳ 2026-01-02"
    p = ok(update("- [ ] a 📅 2026-01-03", "- [ ] a 📅 2026-01-03", priority="low", recurrence="every 2 days"))
    assert p.new_text == "- [ ] a 🔽 🔁 every 2 days 📅 2026-01-03"
    refused(update("- [ ] a", "- [ ] a", recurrence="every weekend"), "recurrence_unsupported")
    refused(update("- [ ] a", "- [ ] a", priority="urgent"), "invalid_argument")


def test_replacing_the_description_keeps_a_trailing_tag():
    line = "- [ ] Pay rent #finance 📅 2026-12-01"
    p = ok(update(line, line, text="Pay landlord"))
    assert p.new_text == "- [ ] Pay landlord #finance 📅 2026-12-01"


def test_replacing_the_description_keeps_a_tag_from_its_middle():
    line = "- [ ] Call #bob about the [[Lease]] 📅 2026-12-01"
    p = ok(update(line, line, text="Call the agent"))
    assert p.new_text == "- [ ] Call the agent #bob 📅 2026-12-01"


def test_a_tag_already_in_the_new_text_is_not_duplicated():
    line = "- [ ] Call #bob about the lease"
    p = ok(update(line, line, text="Ping #Bob about keys"))
    assert p.new_text == "- [ ] Ping #Bob about keys"


def test_text_combined_with_remove_tags():
    line = "- [ ] Pay rent #finance #home 📅 2026-12-01"
    p = ok(update(line, line, text="Pay landlord", remove_tags=("finance",)))
    assert p.new_text == "- [ ] Pay landlord #home 📅 2026-12-01"


def test_a_trailing_tag_typed_in_the_new_text():
    line = "- [ ] Pay rent 📅 2026-12-01"
    p = ok(update(line, line, text="Pay landlord #home"))
    assert p.new_text == "- [ ] Pay landlord #home 📅 2026-12-01"


def test_removing_a_tag_is_not_hierarchical():
    line = "- [ ] Chase #waiting-for/bob"
    p = ok(update(line, line, remove_tags=("waiting-for",)))
    assert not p.changed and p.spans == () and p.new_text == line


def test_adding_and_removing_tags():
    line = "- [ ] a #x 📅 2026-01-01"
    p = ok(update(line, line, add_tags=("#y",), remove_tags=("X",)))
    assert p.new_text == "- [ ] a #y 📅 2026-01-01"


def test_a_context_line_is_appended_under_the_tasks_existing_context():
    note = "- [ ] Call bank\n    asked for form\n    form sent\n- [ ] other\n"
    p = ok(update(note, "- [ ] Call bank", append_context="called, left message"))
    assert p.new_text == "- [ ] Call bank\n    asked for form\n    form sent\n    - called, left message\n- [ ] other\n"
    assert [n.kind for n in p.new_lines] == ["target", "context"]


def test_context_lines_copy_the_prefix_and_use_a_tab_unit():
    p = ok(update("> \t- [ ] a\n", "> \t- [ ] a", append_context="note"))
    assert p.new_text == "> \t- [ ] a\n> \t\t- note\n"
    p = ok(update("> - [ ] a", "> - [ ] a", append_context="note"))
    assert p.new_text == "> - [ ] a\n>     - note"


def test_a_long_continuation_block_is_not_split_by_append_context():
    block = ["    c1", "    c2", "", "    - [ ] child", "        deep", "    c3", "    c4", "    c5", "    c6", "    c7"]
    note = "- [ ] a\n" + "\n".join(block) + "\nnext para\n"
    p = ok(update(note, "- [ ] a", append_context="added"))
    lines = p.new_text.splitlines()
    assert lines[1:11] == block and lines[11] == "    - added" and lines[12] == "next para"


def test_text_validation_refuses_signifiers_and_line_breaks():
    refused(update("- [ ] a", "- [ ] a", text="b 📅 2026-01-01"), "invalid_argument")
    refused(update("- [ ] a", "- [ ] a", text="b\nc"), "invalid_argument")
    refused(update("- [ ] a", "- [ ] a", append_context="x y"), "invalid_argument")


def test_a_value_set_to_itself_is_no_change():
    line = "- [ ] a 📅 2026-01-01"
    p = ok(update(line, line, due=D(2026, 1, 1)))
    assert not p.changed and p.new_text == line


# ── add_task (D8, D9) ────────────────────────────────────────────────────────


def test_added_after_the_sections_task_list():
    note = "## Today\npara\n- [ ] a\n- [ ] b\n    ctx under b\n\nmore prose\n## Later\n- [ ] z\n"
    p = ok(add(note, "c", section="Today", due=D(2026, 11, 12)))
    assert p.new_text == "## Today\npara\n- [ ] a\n- [ ] b\n    ctx under b\n- [ ] c 📅 2026-11-12\n\nmore prose\n## Later\n- [ ] z\n"
    assert p.new_lines == (tw.NewLine("task", 6, "- [ ] c 📅 2026-11-12"),)
    assert p.new_ids["task"] == key("- [ ] c 📅 2026-11-12")


def test_a_nine_line_continuation_block_is_not_split():
    block = [f"    line {k}" for k in range(1, 10)]
    note = "- [ ] a\n" + "\n".join(block) + "\nprose\n"
    p = ok(add(note, "b"))
    lines = p.new_text.splitlines()
    assert lines[1:10] == block and lines[10] == "- [ ] b"


def test_add_after_a_ten_line_block_with_a_blank_and_a_deeper_child():
    block = ["    c1", "    c2", "", "    - [ ] child", "        deep", "    c3", "    c4", "    c5", "    c6", "    c7"]
    note = "- [ ] a\n" + "\n".join(block) + "\n\nprose\n"
    p = ok(add(note, "b"))
    lines = p.new_text.splitlines()
    assert lines[1:11] == block and lines[11] == "- [ ] b"


def test_canonical_serialisation():
    p = ok(add("", "Book flights", tags=("travel",), priority="high", recurrence="every year",
               scheduled=D(2026, 11, 20), due=D(2026, 12, 1), s={"set_created_date": True}))
    assert p.new_text == "- [ ] Book flights #travel ⏫ 🔁 every year ➕ 2026-11-10 ⏳ 2026-11-20 📅 2026-12-01"


def test_never_at_end_of_file_when_the_region_is_a_section():
    note = "## A\nsome text\nmore\n\n## B\nother\n"
    p = ok(add(note, "c", section="A"))
    assert p.new_text == "## A\nsome text\nmore\n- [ ] c\n\n## B\nother\n"


def test_an_empty_section_gets_the_line_after_its_heading():
    note = "## A\n\n## B\nx\n"
    p = ok(add(note, "c", section="A"))
    assert p.new_text == "## A\n- [ ] c\n\n## B\nx\n"
    p = ok(add("# h\n## A", "c", section="A"))
    assert p.new_text == "# h\n## A\n- [ ] c"


def test_an_empty_body():
    assert ok(add("", "c")).new_text == "- [ ] c"
    assert ok(add("---\na: 1\n---\n", "c")).new_text == "---\na: 1\n---\n- [ ] c\n"
    assert ok(add("---\na: 1\n---\n\n\n", "c")).new_text == "---\na: 1\n---\n- [ ] c\n\n\n"


def test_a_frontmatter_block_that_ends_the_file():
    note = "---\ra: 1\r---"
    assert ok(add(note, "c")).new_text == "---\ra: 1\r---\r- [ ] c"


def test_a_suffixed_id_is_refused_before_the_text_is_read():
    k = key("- [ ] a")
    refused(complete("\x00 not even readable", f"{k}.1of2"), "precondition_required")
    refused(update("- [ ] a", f"{k}.1of2", due=D(2026, 1, 1)), "precondition_required")


def test_a_context_line_under_a_shallow_child_is_its_sibling():
    note = "- [ ] a\n  - [ ] child\n"
    p = ok(update(note, "- [ ] a", append_context="note"))
    assert p.new_text == "- [ ] a\n  - [ ] child\n  - note\n"


def test_a_large_note_plans_in_bounded_time():
    import time

    note = "".join(f"- [ ] task {k} 📅 2026-01-01\n    ctx {k}\n" for k in range(4999))
    start = time.perf_counter()
    ok(complete(note, "- [ ] task 2500 📅 2026-01-01"))
    ok(add(note, "fresh one"))
    assert time.perf_counter() - start < 30


def test_whole_note_without_tasks_goes_after_the_last_non_blank_line():
    assert ok(add("text\n\n", "c")).new_text == "text\n- [ ] c\n\n"
    assert ok(add("text", "c")).new_text == "text\n- [ ] c"
    assert ok(add("text\r\nmore", "c")).new_text == "text\r\nmore\r\n- [ ] c"


def test_an_ordered_anchor_is_incremented():
    note = "1. [ ] a\n2. [ ] b\n3) note\n"
    p = ok(add(note, "c"))
    assert p.new_text == "1. [ ] a\n2. [ ] b\n3. [ ] c\n3) note\n"
    p = ok(add("9) [ ] a", "c"))
    assert p.new_text == "9) [ ] a\n10) [ ] c"


def test_a_callout_anchor():
    note = "> [!todo] Today\n> - [ ] a\n>     ctx\n\nafter\n"
    p = ok(add(note, "b"))
    assert p.new_text == "> [!todo] Today\n> - [ ] a\n>     ctx\n> - [ ] b\n\nafter\n"


def test_the_anchor_is_the_last_top_level_task_and_bullets_are_copied():
    note = "* [ ] a\n    - [ ] nested\n"
    p = ok(add(note, "b"))
    assert p.new_text == "* [ ] a\n    - [ ] nested\n* [ ] b\n"


def test_at_end_of_file_without_a_final_newline():
    p = ok(add("- [ ] a\r\n- [ ] b", "c"))
    assert p.new_text == "- [ ] a\r\n- [ ] b\r\n- [ ] c"


def test_a_frontmatter_task_lookalike_is_never_an_anchor():
    note = "---\ntodo: |\n  - [ ] fm\n---\nbody text\n"
    p = ok(add(note, "c"))
    assert p.new_text == "---\ntodo: |\n  - [ ] fm\n---\nbody text\n- [ ] c\n"


def test_a_fenced_task_is_never_an_anchor():
    note = "- [ ] real\n```\n- [ ] fenced\n```\n"
    p = ok(add(note, "c"))
    assert p.new_text == "- [ ] real\n- [ ] c\n```\n- [ ] fenced\n```\n"


def test_add_with_a_context_line():
    p = ok(add("- [ ] a\n", "b", context="ref 1234"))
    assert p.new_text == "- [ ] a\n- [ ] b\n    - ref 1234\n"
    assert [n.kind for n in p.new_lines] == ["task", "context"]
    refused(add("- [ ] a\n", "b", context="[ ] x"), "invalid_argument")


def test_an_exact_duplicate_in_the_note_is_refused_then_allowed():
    note = "- [ ] Renew passport #admin 📅 2026-12-01\n- [x] Old done\n"
    r = refused(add(note, "renew  Passport #admin"), "duplicate_task")
    assert r.matches[0].line == 1 and r.matches[0].id == key("- [ ] Renew passport #admin 📅 2026-12-01")
    p = ok(add(note, "renew  Passport #admin", allow_duplicate=True))
    assert p.new_text.splitlines()[2] == "- [ ] renew  Passport #admin"
    # A completed task is not an open duplicate.
    ok(add(note, "Old done"))


def test_add_returns_the_normalised_description_and_terms():
    p = ok(add("", "Renew Passport for the trip #admin"))
    assert p.norm == "renew passport for the trip #admin"
    assert p.terms == ("passport", "renew", "admin", "trip", "for", "the")


def test_section_refusals():
    refused(add("## A\nx\n", "c", section="Nope"), "selector_unresolved")
    r = refused(add("---\na: [\n---\n## A\n", "c", section="A"), "content_unsafe")
    assert r.detail[0] == "frontmatter_defect"
    r = refused(add("## A\n  ```\ncode\n", "c", section="A"), "content_unsafe")
    assert r.detail[0] == "unmatched_fence"


def test_a_section_below_frontmatter():
    note = "---\ntags: [a]\n---\n## A\n- [ ] a\n## B\n"
    p = ok(add(note, "b", section="A"))
    assert p.new_text == "---\ntags: [a]\n---\n## A\n- [ ] a\n- [ ] b\n## B\n"


def test_add_argument_refusals():
    refused(add("", "c", tags=("1bad",)), "invalid_argument")
    refused(add("", "c", recurrence="every weekend"), "recurrence_unsupported")
    refused(add("", "c 📅 2026-01-01"), "invalid_argument")
    refused(add("", "c", tags=tuple(f"t{k}" for k in range(33))), "invalid_argument")


# ── D9 terms and Jaccard ─────────────────────────────────────────────────────


def test_terms_and_jaccard():
    assert tw.term_set("a bc 12 x-ray, déjà-vu") == frozenset({"bc", "ray", "déjà", "vu"})
    assert tw.similarity_terms("one two three four five six seven eight nine ten") == (
        "three", "seven", "eight", "four", "five", "nine", "one", "two")
    assert tw.jaccard(frozenset({"a", "b"}), frozenset({"a", "b", "c"})) == pytest.approx(2 / 3)
    assert tw.jaccard(frozenset(), frozenset()) == 0.0
    a, b = frozenset({"renew", "passport"}), frozenset({"renew", "passport", "for", "the", "trip"})
    assert tw.contains(a, b) and tw.contains(b, a)
    assert not tw.contains(frozenset({"passport"}), b)
    assert not tw.contains(frozenset({"renew", "visa"}), b)


# ── verify (D5): injected corruptions ────────────────────────────────────────


def _replace_target(plan: tw.Plan, old: str, new_content: str) -> tuple[str, tw.Plan]:
    """A corrupted plan whose target edit is ``new_content`` — the shape a
    planner bug would produce: spans and new text agree, the content is
    wrong."""
    view = tw._NoteView(old)
    tln = view.lines[plan.target_line - 1]
    spans = [s for s in plan.spans if tw._LINE_BREAK_RE.search(s.text)]
    spans.append(tw.Span(tln.start, tln.end, new_content))
    spans.sort(key=lambda s: (s.start, s.end))
    plan2 = dataclasses.replace(plan, spans=tuple(spans))
    return tw._compose(old, plan2.spans), plan2


def test_verify_catches_a_byte_changed_elsewhere():
    note = "intro\n- [ ] a 📅 2026-01-01\noutro\n"
    p = ok(update(note, "- [ ] a 📅 2026-01-01", due=D(2026, 2, 2)))
    bad = p.new_text.replace("outro", "outrO")
    assert tw.verify(note, bad, p).code == "task_malformed"
    # Declaring the change as a span does not help: it is outside the target.
    bad_plan = dataclasses.replace(p, spans=p.spans + (tw.Span(note.index("outro") + 4, note.index("outro") + 5, "O"),))
    assert tw.verify(note, bad, bad_plan).code == "task_malformed"


def test_verify_catches_a_dropped_field():
    note = "- [ ] a ⏳ 2026-01-01 📅 2026-01-02\n"
    p = ok(update(note, "- [ ] a ⏳ 2026-01-01 📅 2026-01-02", due=D(2026, 2, 2)))
    new2, p2 = _replace_target(p, note, "- [ ] a 📅 2026-02-02")
    assert tw.verify(note, new2, p2).code == "task_malformed"


def test_verify_catches_a_lost_tag():
    note = "- [ ] Pay rent #finance #home 📅 2026-12-01\n"
    p = ok(update(note, "- [ ] Pay rent #finance #home 📅 2026-12-01", text="Pay landlord"))
    new2, p2 = _replace_target(p, note, "- [ ] Pay landlord #home 📅 2026-12-01")
    r = tw.verify(note, new2, p2)
    assert r.code == "task_malformed"


def test_verify_catches_a_reparented_line():
    note = "- [ ] a\n    child of a\n"
    p = ok(add(note, "b"))
    # A planner bug placing the new task between `a` and its child.
    bad = tw.Span(len("- [ ] a\n"), len("- [ ] a\n"), "- [ ] b\n")
    p2 = dataclasses.replace(p, spans=(bad,))
    r = tw.verify(note, tw._compose(note, p2.spans), p2)
    assert r.code == "task_malformed" and "owning task" in r.reason


def test_verify_catches_an_extra_task_from_a_context_line():
    note = "- [ ] a\n"
    p = ok(update(note, "- [ ] a", append_context="plain"))
    span = p.spans[-1]
    p2 = dataclasses.replace(
        p, spans=p.spans[:-1] + (tw.Span(span.start, span.end, "    - [ ] plain\n"),),
        inserted=(dataclasses.replace(p.inserted[0], text="    - [ ] plain"),),
    )
    r = tw.verify(note, tw._compose(note, p2.spans), p2)
    assert r.code == "task_malformed"


def test_verify_catches_an_insertion_into_a_fence():
    note = "- [ ] a\n```\ncode\n```\n"
    p = ok(add(note, "b"))
    pos = note.index("code")
    p2 = dataclasses.replace(p, spans=(tw.Span(pos, pos, "    - ctx\n"),),
                             inserted=(tw.Inserted("context", "    - ctx", None, None),))
    r = tw.verify(note, tw._compose(note, p2.spans), p2)
    assert r.code == "task_malformed"


def test_verify_catches_a_merged_terminator():
    # Inserting a line ending in a lone CR before a line that starts with LF
    # would merge into CRLF and swallow a line.
    note = "- [ ] a\r\n\nb\n"
    p = ok(add(note, "c"))
    pos = len("- [ ] a\r")  # not a line boundary (inside the CRLF)
    p2 = dataclasses.replace(p, spans=(tw.Span(pos, pos, "- [ ] c\r"),))
    assert tw.verify(note, tw._compose(note, p2.spans), p2).code == "task_malformed"


def test_verify_catches_a_wrong_next_date():
    line = "- [ ] x 🔁 every day 📅 2026-11-10"
    p = ok(complete(line + "\n", line))
    span = p.spans[0]
    wrong = "- [ ] x 🔁 every day 📅 2026-11-12"
    p2 = dataclasses.replace(p, spans=(tw.Span(span.start, span.end, wrong + "\n"),) + p.spans[1:],
                             inserted=(dataclasses.replace(p.inserted[0], text=wrong),))
    assert tw.verify(line + "\n", tw._compose(line + "\n", p2.spans), p2).code == "task_malformed"


def test_verify_catches_a_changed_heading_and_frontmatter():
    note = "---\na: 1\n---\n# H\n- [ ] a\n"
    p = ok(complete(note, "- [ ] a"))
    p2 = dataclasses.replace(p, spans=(tw.Span(0, 0, "x\n"),) + p.spans)
    assert tw.verify(note, tw._compose(note, p2.spans), p2).code == "task_malformed"


# ── property tests ───────────────────────────────────────────────────────────

_WORDS = ["alpha", "beta", "gamma", "delta", "call", "pay", "rent", "water", "Ship", "déjà", "Ωmega"]
_TAGS = ["home", "work/deep", "Waiting-For", "admin"]


def random_task_line(rng: random.Random) -> str:
    prefix = rng.choice(["", "", "  ", "    ", "\t", "> ", "> > ", ">     "])
    marker = rng.choice(["- ", "* ", "+ ", "1. ", "2) ", "-\t"])
    status = rng.choice([" ", " ", " ", "x", "/", "-", ">", "X"])
    words = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 4)))
    parts = [words]
    if rng.random() < 0.4:
        parts.append("#" + rng.choice(_TAGS))
    if rng.random() < 0.2:
        parts.insert(1, "[[Link]]")
    if rng.random() < 0.3:
        parts.append(rng.choice(["⏫", "🔽", "🔺"]))
    if rng.random() < 0.3:
        parts.append("🔁 " + rng.choice(["every day", "every week", "every month", "every 2 weeks",
                                          "every weekday", "every month on the 15th", "every weekend"]))
    for sym in ("🛫", "⏳", "📅"):
        if rng.random() < 0.35:
            parts.append(f"{sym} 2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}")
    if rng.random() < 0.15:
        parts.append(f"✅ 2026-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}")
    if rng.random() < 0.1:
        parts.append("^blk" + str(rng.randint(1, 9)))
    tail = rng.choice(["", "", "  ", " "])
    return f"{prefix}{marker}[{status}] " + " ".join(parts) + tail


def random_note(rng: random.Random) -> str:
    lines: list[str] = []
    if rng.random() < 0.25:
        lines += ["---", "tags: [a]", "todo: |", "  - [ ] frontmatter lookalike", "---"]
    for _ in range(rng.randint(0, 14)):
        kind = rng.random()
        if kind < 0.45:
            lines.append(random_task_line(rng))
        elif kind < 0.55:
            lines.append("")
        elif kind < 0.65:
            lines.append(rng.choice(["    context line", "\tctx", ">     quoted ctx", "  two"]))
        elif kind < 0.72:
            lines.append(rng.choice(["## Today", "# Notes", "### Later"]))
        elif kind < 0.78:
            lines += ["```", "- [ ] fenced lookalike", "```"]
        elif kind < 0.82:
            lines.append("> [!note] callout")
        elif kind < 0.86:
            lines.append("| a | b |")
        else:
            lines.append(rng.choice(["plain prose", "more words here", "1. ordered prose"]))
    terms = [rng.choice(["\n", "\n", "\n", "\r\n", "\r"]) for _ in lines]
    out = []
    for k, line in enumerate(lines):
        out.append(line)
        if k < len(lines) - 1 or rng.random() < 0.6:
            out.append(terms[k])
    return "".join(out)


def _random_call(rng: random.Random, note: str):
    tasks = tasks_of(note)
    s = {"set_done_date": rng.random() < 0.8, "set_created_date": rng.random() < 0.3,
         "next_recurrence_below": rng.random() < 0.5, "set_cancelled_date": rng.random() < 0.8}
    op = rng.random()
    if not tasks:
        op = 0.95
    if op < 0.35:
        t = rng.choice(tasks)
        ident = tl.render_id(t.task_key, t.key_ordinal, t.key_count)
        return complete(note, ident, done_on=D(2026, 11, rng.randint(1, 28)), hash_supplied=True, **s)
    if op < 0.75:
        t = rng.choice(tasks)
        ident = tl.render_id(t.task_key, t.key_ordinal, t.key_count)
        kw: dict = {}
        if rng.random() < 0.3:
            kw["due"] = D(2026, 12, rng.randint(1, 28))
        if rng.random() < 0.2:
            kw["scheduled"] = D(2026, 12, rng.randint(1, 28))
        if rng.random() < 0.15:
            kw["clear"] = frozenset(rng.sample(["start", "priority", "recurrence"], 1))
        if rng.random() < 0.2:
            kw["status"] = rng.choice(["open", "in_progress", "cancelled"])
        if rng.random() < 0.2:
            kw["add_tags"] = (rng.choice(_TAGS),)
        if rng.random() < 0.15 and t.tags:
            kw["remove_tags"] = (t.tags[0],)
        if rng.random() < 0.2:
            kw["text"] = rng.choice(["New words", "Call #bob back", "x [[L]] y"])
        if rng.random() < 0.25 or not kw:
            kw["append_context"] = rng.choice(["note it", "called twice"])
        return update(note, ident, s=s, hash_supplied=True, **kw)
    kw = {}
    if rng.random() < 0.3:
        kw["section"] = rng.choice(["Today", "Notes", "Later"])
    if rng.random() < 0.3:
        kw["due"] = D(2026, 12, 1)
    if rng.random() < 0.2:
        kw["context"] = "ctx"
    if rng.random() < 0.2:
        kw["tags"] = ("home",)
    return add(note, rng.choice(_WORDS) + " " + str(rng.randint(1, 10**6)), s=s, **kw)


_ACCEPTABLE = {"task_malformed", "recurrence_unsupported", "already_completed", "task_cancelled",
               "on_completion_unsupported", "duplicate_task", "selector_unresolved", "content_unsafe"}


def _lines_keepends(text: str) -> list[str]:
    out, pos = [], 0
    for m in tw._LINE_BREAK_RE.finditer(text):
        out.append(text[pos:m.end()])
        pos = m.end()
    out.append(text[pos:])
    return out


def test_property_plans_verify_and_preserve_bytes_outside_their_spans():
    rng = random.Random(428)
    successes = refusals_seen = 0
    for _ in range(600):
        note = random_note(rng)
        plan = _random_call(rng, note)
        if isinstance(plan, tw.TaskWriteRefusal):
            assert plan.code in _ACCEPTABLE, (note, plan)
            refusals_seen += 1
            continue
        if not plan.changed:
            continue
        successes += 1
        new = plan.new_text
        assert tw.verify(note, new, plan) is None
        assert tw._compose(note, plan.spans) == new
        # An independent oracle: every old line but the target, with its
        # terminator, appears in order in the new text.
        old_lines = _lines_keepends(note)
        new_lines = _lines_keepends(new)
        target = None if plan.target_line is None else plan.target_line - 1
        it = iter(range(len(new_lines)))
        for k, line in enumerate(old_lines):
            if k == target or (k == len(old_lines) - 1 and line == ""):
                continue
            stripped = line.rstrip("\r\n")
            for j in it:
                if new_lines[j] == line or (k == len(old_lines) - 1 and new_lines[j].rstrip("\r\n") == stripped):
                    break
            else:
                raise AssertionError((note, new, k))
        # Injected corruptions are refused.
        for bad in _corruptions(rng, note, plan):
            assert tw.verify(note, *bad) is not None, (note, bad)
    assert successes > 250 and refusals_seen > 20, (successes, refusals_seen)


def _corruptions(rng, old, plan):
    new = plan.new_text
    view = tw._NoteView(new)
    touched = {n.line - 1 for n in plan.new_lines}
    others = [k for k in range(len(view.lines)) if k not in touched and view.content(k)]
    if others:
        k = rng.choice(others)
        pos = view.lines[k].start
        ch = new[pos]
        repl = "Z" if ch != "Z" else "Y"
        yield new[:pos] + repl + new[pos + 1:], plan
    if plan.target_line is not None:
        tline = next(n for n in plan.new_lines if n.kind == "target").text
        parsed = tl.parse(tline)
        intent = plan.intent
        for tok in parsed.tokens:
            if tok.kind == "field" and tok.family not in intent.set_fields and tok.family not in intent.remove_fields:
                dropped = tline[:tok.sep_start] + tline[tok.end:]
                yield _replace_target(plan, old, dropped)
                break
        for tag in parsed.tags:
            if tag.lower() not in {t.lower() for t in intent.remove_tags}:
                i = tline.index("#" + tag)
                lost = tline[:i] + tline[i + 1 + len(tag):]
                yield _replace_target(plan, old, lost)
                break
    for k, ins in enumerate(plan.inserted):
        if ins.kind == "context":
            spans = list(plan.spans)
            for j, s in enumerate(spans):
                if ins.text in s.text:
                    spans[j] = tw.Span(s.start, s.end, s.text.replace("- " + ins.text.split("- ", 1)[1], "- [ ] boom", 1))
            text2 = ins.text.split("- ", 1)[0] + "- [ ] boom"
            inserted = list(plan.inserted)
            inserted[k] = dataclasses.replace(ins, text=text2)
            p2 = dataclasses.replace(plan, spans=tuple(spans), inserted=tuple(inserted))
            yield tw._compose(old, p2.spans), p2


def test_property_every_resolvable_task_completes_or_refuses_by_code():
    rng = random.Random(4281)
    for _ in range(200):
        note = random_note(rng)
        for t in tasks_of(note):
            ident = tl.render_id(t.task_key, t.key_ordinal, t.key_count)
            plan = complete(note, ident, hash_supplied=True)
            if isinstance(plan, tw.TaskWriteRefusal):
                assert plan.code in _ACCEPTABLE, (note, plan)
                if t.status_char in "xX":
                    assert plan.code == "already_completed"
            else:
                assert t.status_char not in "xX-"
                assert tw.verify(note, plan.new_text, plan) is None


# ── S5 audit regressions (#428) ──────────────────────────────────────────────

# 1. A defective frontmatter block is never touched.

@pytest.mark.parametrize("note", [
    "---\n- [ ] x\n---\nbody\n",         # YAML error: the extractor sees line 2
    "---\n- [ ] x\nbody\n",               # an unclosed fence
    "---\r\nk: [\r\n---\r\n- [ ] x\r\n",  # malformed YAML, CRLF
])
def test_every_planner_refuses_a_defective_frontmatter_block(note):
    for plan in (complete(note, "- [ ] x"), update(note, "- [ ] x", due=D(2026, 1, 1)),
                 add(note, "new"), add(note, "new", section="A")):
        r = refused(plan, "content_unsafe")
        assert r.detail[0] == "frontmatter_defect"


def test_valid_frontmatter_and_a_later_rule_still_work():
    note = "---\na: 1\n---\n- [ ] x\n\n---\n\n- [ ] y\n"
    assert ok(complete(note, "- [ ] x")).new_text.splitlines()[3] == f"- [x] x ✅ {TODAY}"
    assert ok(add(note, "z")).new_text.endswith("- [ ] y\n- [ ] z\n")
    note = "intro\n---\n- [ ] x\n"  # a thematic break that is not on line 1
    assert ok(complete(note, "- [ ] x")).new_text == f"intro\n---\n- [x] x ✅ {TODAY}\n"


# 2. A stranded priority in the description does not block a completion.

@pytest.mark.parametrize("line", [
    "- [ ] ⏫ a 🔁 every day 📅 2026-01-01",
    "- [ ] fix ⏫ thing 🔁 every day 📅 2026-01-01",
])
def test_a_description_priority_does_not_block_a_recurring_completion(line):
    p = ok(complete(line + "\n", line))
    nxt = next(n for n in p.new_lines if n.kind == "next")
    assert nxt.text == line.replace("2026-01-01", "2026-01-02")


# 3. verify pins the target and next lines outside the fields.

def _hand_built(note, plan, new_target):
    new2, p2 = _replace_target(plan, note, new_target)
    return tw.verify(note, new2, p2)


@pytest.mark.parametrize("bad", [
    "- [ ] Pay rents #y #x 📅 2026-01-01",       # description rewritten under tag-only intent
    "- [ ] #y #x 📅 2026-01-01",                  # description deleted under tag-only intent
    "- [ ] Pay rent #y 📅 2026-01-01",             # #x lost: intent adds #y only
    "- [ ]  Pay rent #y #x 📅 2026-01-01",         # gap widened
    "- [ ] Pay rent #y  #x 📅 2026-01-01",         # extra space before a tag
    "- [ ] Pay rent #y #x  📅 2026-01-01",         # extra space before a field
])
def test_verify_refuses_changes_outside_the_named_tags(bad):
    note = "- [ ] Pay rent #x 📅 2026-01-01\n"
    p = ok(update(note, "- [ ] Pay rent #x 📅 2026-01-01", add_tags=("y",)))
    assert p.new_text == "- [ ] Pay rent #y #x 📅 2026-01-01\n"
    assert _hand_built(note, p, bad).code == "task_malformed"


def test_verify_refuses_text_after_a_replacement_description_and_reordered_tokens():
    note = "- [ ] Pay rent ⏳ 2026-01-01 📅 2026-01-02\n"
    p = ok(update(note, "- [ ] Pay rent ⏳ 2026-01-01 📅 2026-01-02", text="Pay landlord"))
    assert _hand_built(note, p, "- [ ] Pay landlord GARBAGE ⏳ 2026-01-01 📅 2026-01-02").code == "task_malformed"
    assert _hand_built(note, p, "- [ ] Pay landlord 📅 2026-01-02 ⏳ 2026-01-01").code == "task_malformed"
    p = ok(update(note, "- [ ] Pay rent ⏳ 2026-01-01 📅 2026-01-02", due=D(2026, 2, 2)))
    assert _hand_built(note, p, "- [ ] Pay rent 📅 2026-02-02 ⏳ 2026-01-01").code == "task_malformed"


@pytest.mark.parametrize("bad_next", [
    "- [ ] Rent changed 🔁 every day 📅 2026-11-11",      # description differs
    "  - [ ] Rent 🔁 every day 📅 2026-11-11",            # indentation differs
    "- [ ] Rent 🔁 every day 🏁 delete 📅 2026-11-11",    # gains 🏁 delete
    "- [ ] Rent 🔁 every day 📅 2026-11-11 ⏫",            # gains a priority
])
def test_verify_refuses_a_next_line_that_is_not_a_copy_of_the_original(bad_next):
    line = "- [ ] Rent 🔁 every day 📅 2026-11-10"
    note = line + "\n"
    p = ok(complete(note, line))
    span = p.spans[0]
    p2 = dataclasses.replace(p, spans=(tw.Span(span.start, span.end, bad_next + "\n"),) + p.spans[1:],
                             inserted=(dataclasses.replace(p.inserted[0], text=bad_next),))
    assert tw.verify(note, tw._compose(note, p2.spans), p2).code == "task_malformed"


# 4. Caller text cannot smuggle structure.

@pytest.mark.parametrize("ctx", ["- [ ] sneaky", "[ ] sneaky", "* item", "2. item", "# heading", "> quote",
                                 "a %% hidden", "<!-- open", "close -->", "``` fence", "~~~", "a ```` b"])
def test_context_text_cannot_smuggle_structure(ctx):
    refused(update("- [ ] a\n", "- [ ] a", append_context=ctx), "invalid_argument")
    refused(add("- [ ] a\n", "b", context=ctx), "invalid_argument")


@pytest.mark.parametrize("text", ["a %% hidden", "x <!-- open", "close --> y", "run ``` fence", "~~~ x"])
def test_description_text_cannot_smuggle_structure(text):
    refused(update("- [ ] a\n", "- [ ] a", text=text), "invalid_argument")
    refused(add("- [ ] a\n", text), "invalid_argument")


def test_balanced_markup_is_ordinary_text():
    ok(update("- [ ] a\n", "- [ ] a", append_context="see %%note%% and <!-- c --> and `code` #2"))
    ok(add("- [ ] a\n", "call #bob re `x` %%y%%"))


# 5. Context indentation adapts to the task's children.

@pytest.mark.parametrize("note, task, want", [
    ("- [ ] a\n  - [ ] child\n", "- [ ] a", "  - note"),                         # 2-space
    ("- [ ] a\n    - child\n", "- [ ] a", "    - note"),                         # 4-space
    ("- [ ] a\n\t- child\n", "- [ ] a", "\t- note"),                             # tab
    ("- [ ] a\n   - c1\n  - [ ] c2\n", "- [ ] a", "  - note"),                   # mixed: shallowest
    ("> - [ ] a\n>   - child\n", "> - [ ] a", ">   - note"),                     # blockquote
    ("1. [ ] a\n   - child\n", "1. [ ] a", "   - note"),                         # ordered
    ("- [ ] a\n  - c\n    - [ ] deep\n      ctx\n", "- [ ] a", "  - note"),      # deeper child
    ("- [ ] a\nx\n- [ ] b\n\t- c\n", "- [ ] a", "\t- note"),                     # tab-nested note
    ("- [ ] a\n", "- [ ] a", "    - note"),                                      # no children
])
def test_context_indentation_adapts(note, task, want):
    p = ok(update(note, task, append_context="note"))
    assert next(n for n in p.new_lines if n.kind == "context").text == want


def test_add_task_context_uses_tabs_in_a_tab_nested_note():
    p = ok(add("- [ ] a\n\t- c\n", "b", context="why"))
    assert p.new_text == "- [ ] a\n\t- c\n- [ ] b\n\t- why\n"


# ── the task rule (#434, D7) ─────────────────────────────────────────────────

INBOX = "# Inbox\n\n- [ ] existing\n"


def _added(plan) -> str:
    ok(plan)
    (nl,) = [n for n in plan.new_lines if n.kind == "task"]
    return nl.text


def filter_rule(text):
    return {"task_rule": "filter", "global_filter": text}


def test_add_prepends_the_global_filter_text():
    plan = add(INBOX, "Book flights", s=filter_rule("#task"), due=D(2026, 12, 1))
    assert _added(plan) == "- [ ] #task Book flights 📅 2026-12-01"
    assert plan.filter_added == "#task"


@pytest.mark.parametrize("text,tags", [("Book flights", ("task",)), ("#task Book flights", ())])
def test_a_filter_already_present_is_not_added_again(text, tags):
    plan = add(INBOX, text, s=filter_rule("#task"), tags=tags)
    line = _added(plan)
    assert line.count("#task") == 1 and plan.filter_added is None


def test_the_filter_match_is_case_sensitive():
    plan = add(INBOX, "Book flights #Task", s=filter_rule("#task"))
    assert _added(plan) == "- [ ] #task Book flights #Task"


def test_a_non_tag_filter_is_added_the_same_way():
    assert _added(add(INBOX, "Call the bank", s=filter_rule("TODO"))) == "- [ ] TODO Call the bank"
    assert _added(add(INBOX, "Call the bank", s=filter_rule("[task]"))) == \
        "- [ ] [task] Call the bank"


def test_the_combined_description_bound_names_the_global_filter():
    r = refused(add(INBOX, "x" * 1000, s=filter_rule("#task")), "invalid_argument")
    assert "global filter text (1006 characters) exceeds 1,000 characters" in r.reason
    # Exactly at the bound is accepted.
    assert _added(add(INBOX, "x" * 994, s=filter_rule("#task"))).startswith("- [ ] #task x")


@pytest.mark.parametrize("bad,fragment", [
    ("%%", "comment marker"), ("%%x%%", "comment marker"), ("<!--", "HTML comment marker"),
    ("-->", "HTML comment marker"), ("<!-- x -->", "HTML comment marker"),
    ("~~~", "tildes"), ("[ ]", "checkbox"), ("[x]", "checkbox"), ("- todo", "list marker"),
    ("1. todo", "list marker"), ("# task", "heading marker"), ("> task", "'>'"),
    ("a`b", "backtick"), ("#task 📅", "field emoji"), ("a\ufe0fb", "field emoji"),
    ("a\u2028b", "single line"), ("a\x07b", "single line"), ("x" * 65, "longer than 64"),
    ("\u3000", "whitespace"), (" #task", "surrounding whitespace"),
])
def test_the_planner_validates_a_filter_handed_in_directly(bad, fragment):
    """Layer (b): the planner handed the rule directly, bypassing the save
    validation and the settings fallback, applies the settings' own filter
    validator before prepending — for a dry run and a write alike (one
    planner), so nothing is planned and nothing can be published."""
    r = refused(add(INBOX, "Call bank", s=filter_rule(bad)), "invalid_argument")
    assert "global filter text cannot be added" in r.reason, r.reason
    assert fragment in r.reason and "nothing was written" in r.reason, r.reason


def test_under_fields_and_all_nothing_is_prepended():
    for s in ({"task_rule": "fields"}, {"task_rule": "all", "global_filter": "#task"}):
        plan = add(INBOX, "Read the report", s=s)
        assert _added(plan) == "- [ ] Read the report" and plan.filter_added is None


@pytest.mark.parametrize("existing", [
    "- [ ] Renew passport #task\n",           # the filter as a trailing tag
    "- [ ] #task Renew passport\n",           # added by the server earlier
    "- [ ] Renew passport #task 📅 2026-12-01\n",
    "- [ ] Renew passport\n",                 # the bare description, same note
])
def test_under_filter_a_duplicate_is_caught_in_front_trailing_or_bare(existing):
    refused(add(existing, "Renew passport", s=filter_rule("#task")), "duplicate_task")
    refused(add(existing, "#task Renew passport", s=filter_rule("#task")), "duplicate_task")
    plan = add(existing, "Renew passport", s=filter_rule("#task"), allow_duplicate=True)
    assert _added(plan) == "- [ ] #task Renew passport"


def test_under_filter_a_filter_in_the_middle_is_not_matched():  # L13
    note = "- [ ] Renew #task passport\n"
    assert _added(add(note, "Renew passport", s=filter_rule("#task"))) == \
        "- [ ] #task Renew passport"


def test_duplicate_candidates_per_rule():
    plan = ok(add(INBOX, "Renew passport", s=filter_rule("#task"), due=D(2026, 12, 1)))
    assert plan.norm == "#task renew passport"
    assert plan.dup_norms == ("#task renew passport", "renew passport #task")
    for s in ({"task_rule": "all", "global_filter": "#task"}, {"task_rule": "fields"}):
        plan = ok(add(INBOX, "Renew passport", s=s))
        assert plan.norm == "renew passport" and plan.dup_norms == ()
        # all / fields: a filter-tagged line is not the same description.
        assert _added(add("- [ ] Renew passport #task\n", "Renew passport", s=s)) == \
            "- [ ] Renew passport"


def test_the_filter_text_is_not_a_similarity_term():
    plan = ok(add(INBOX, "Renew the passport", s=filter_rule("#task")))
    assert "task" not in plan.terms and set(plan.terms) == {"renew", "the", "passport"}
    assert "task" in ok(add(INBOX, "Renew the passport #task")).terms
    s = settings(task_rule="filter", global_filter="#task")
    assert tw._similar_term_set("#task book flights", s) == {"book", "flights"}
    assert tw._similar_term_set("#task book flights", settings()) == {"task", "book", "flights"}


def test_update_never_re_adds_the_filter():
    note = "- [ ] TODO Call the bank\n"
    plan = ok(update(note, "- [ ] TODO Call the bank", s=filter_rule("TODO"),
                     text="Call the bank today"))
    assert plan.new_text == "- [ ] Call the bank today\n"


def test_extracted_task_at_reads_the_planned_bytes():
    plan = ok(add(INBOX, "Read the report", due=D(2026, 12, 1)))
    (nl,) = [n for n in plan.new_lines if n.kind == "task"]
    task = tw.extracted_task_at(plan.new_text, nl.line)
    assert task is not None and task.due == D(2026, 12, 1)
    assert tw.extracted_task_at(plan.new_text, 1) is None  # the heading
