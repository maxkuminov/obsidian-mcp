"""The pure task-line parser and one-line editor (#428, design D1, D4, D6,
D8, D9; `task-write` and `note-tasks` specs). The plugin reference R§1.4 is
the parsing oracle."""
import ast
import datetime
import pathlib
import random
import re

import pytest

from src.services import links, task_line as T
from src.services.task_line import (
    LineChanges,
    LineEdit,
    TaskLineRefusal,
    apply_changes,
    compose,
    parse,
)
from src.services.tasks import extract_tasks_bounded
from src.services.vault import INLINE_TAG_RE as VAULT_INLINE_TAG_RE

D = datetime.date
DUE, SCHED, START, CREATED, DONE, CANC = "📅", "⏳", "🛫", "➕", "✅", "❌"


def p(line):
    tl = parse(line)
    assert tl is not None, line
    return tl


def edit(line, **changes):
    result = apply_changes(p(line), LineChanges(**changes))
    assert isinstance(result, LineEdit), result
    assert result.line == compose(line, result.edits)
    return result.line


def refused(line, code="task_malformed", **changes):
    result = apply_changes(p(line), LineChanges(**changes))
    assert isinstance(result, TaskLineRefusal), result
    assert result.code == code, result
    return result


def fs(*names):
    return frozenset(names)


# ── drift guards against the extractor ─────────────────────────────────────


def test_inline_tag_rule_is_the_shared_one():
    assert T.INLINE_TAG_RE.pattern == VAULT_INLINE_TAG_RE.pattern


ANATOMY = [
    "- [ ] plain",
    "* [x] star done",
    "+ [X] plus done",
    "1. [ ] ordered",
    "123456789) [/] ordered paren",
    "  - [ ] indented",
    "\t- [-] tab indented",
    "> - [ ] quoted",
    "> > 2. [?] nested quote",
    ">\t- [>] quote tab",
    "> [!note] callout title",
    "-  [ ]  two spaces",
    "- [ ]",
    "- [ ] ",
    "- [ ] hard break  ",
    "- [ ] trailing tab\t",
    "- [ ]\ttab gap",
    "-[ ] no space",
    "- [] empty box",
    "- [  ] two spaces box",
    "- [ ]x glued",
    "- [x]",
    "1234567890. [ ] ten digits",
    "    - [ ] four space indent",
    "`- [ ] code`",
    "- [`] backtick status",
    "- [ ] `📅 2026-01-01` in code",
    "- [ ] text with ⏫ and #tag 📅 2026-01-01",
    "- [Ω] unicode status",
]


@pytest.mark.parametrize("line", ANATOMY)
def test_task_recognition_and_text_agree_with_the_extractor(line):
    tasks, _ = extract_tasks_bounded(line, line_offset=0)
    tl = parse(line)
    if not tasks:
        assert tl is None
        return
    (task,) = tasks
    assert tl is not None
    assert tl.status == task.status_char
    assert tl.full_text == task.text
    assert tl.prefix + tl.box + tl.gap + tl.full_text + tl.tail_ws == line


def test_anatomy_parts():
    tl = p("> \t1. [ ]\t Ship it ⏫  ")
    assert tl.prefix == "> \t1. "
    assert tl.box == "[ ]"
    assert tl.gap == "\t "
    assert tl.full_text == "Ship it ⏫"
    assert tl.description == "Ship it"
    assert tl.tail_ws == "  "


def test_a_line_must_exclude_its_terminator():
    with pytest.raises(ValueError):
        parse("- [ ] x\n")


def test_masked_slice_from_the_body_is_honoured():
    # The caller's slice of the whole-body mask is what counts (the extractor
    # masks the body, not the line).
    line = "- [ ] a 📅 2026-01-01"
    masked = line[:8] + " " * (len(line) - 8)
    assert parse(line).family_count("due") == 1
    assert parse(line, masked=masked).family_count("due") == 0
    with pytest.raises(ValueError):
        parse(line, masked=masked[:-1])
    # A body mask that hid the marker makes the line not a task (#218).
    assert parse(line, masked="  " + line[2:]) is None


# ── the trailing run (R§1.4) ───────────────────────────────────────────────


def test_tags_among_and_after_signifiers_belong_to_the_run():
    # R§1.4: 'Do something #tag1 (due) tomorrow #tag2 (start) today' → description 'Do something #tag1 #tag2'.
    text = "Do something #tag1 📅 2021-01-01 #tag2 🛫 2021-01-02"
    run = T.parse_run(text)
    assert run.description == "Do something"
    assert run.trailing_tags == ("#tag1", "#tag2")
    assert T.plugin_description(text) == "Do something #tag1 #tag2"
    assert [t.family for t in run.tokens if t.kind == "field"] == ["due", "start"]


def test_fields_in_any_order_as_an_unbroken_run():
    run = T.parse_run("x 📅 2021-01-01 ⏫ 🔁 every day ➕ 2020-01-01 ✅ 2021-01-02 ❌ 2021-01-03")
    assert run.description == "x"
    assert [t.family for t in run.tokens] == ["due", "priority", "recurrence", "created", "done", "cancelled"]


def test_unrecognised_text_ends_the_run():
    run = T.parse_run("x 📅 2021-01-01 [[link]]")
    assert run.tokens == () and run.description == "x 📅 2021-01-01 [[link]]"
    run = T.parse_run("x 📅 2021-01-01 then words")
    assert run.tokens == ()


def test_block_link_is_stripped_first():
    run = T.parse_run("x 📅 2021-01-01 ^abc-9")
    assert run.block.value == "^abc-9"
    assert [t.family for t in run.tokens] == ["due"]
    # An underscore is not a block link character: parsing stops.
    run = T.parse_run("x 📅 2021-01-01 ^block_id")
    assert run.block is None and run.tokens == ()
    # A block link needs its leading space.
    assert T.parse_run("^abc").block is None


def test_signifier_spacing_and_variation_selector():
    run = T.parse_run("x📅2021-01-01")
    assert run.description == "x" and run.tokens[0].value == "2021-01-01"
    run = T.parse_run("x 📅\ufe0f   2021-01-01")
    assert run.tokens[0].symbol == "📅\ufe0f"
    for variant in ("📆", "🗓", "🗓\ufe0f"):
        assert T.parse_run(f"x {variant} 2021-01-01").tokens[0].family == "due"
    for variant in ("⏳", "⌛"):
        assert T.parse_run(f"x {variant} 2021-01-01").tokens[0].family == "scheduled"
    # A tab is not the plugin's ' *': the date is not a run field.
    assert T.parse_run("x 📅\t2021-01-01").tokens == ()


def test_recurrence_dependencies_and_completion_values():
    run = T.parse_run("x 🆔 abc-1 ⛔ a1, b_2 🔁 every day when done 🏁 Delete 📅 2021-01-01")
    vals = {t.family: t.value for t in run.tokens}
    assert vals == {"id": "abc-1", "depends_on": "a1, b_2", "recurrence": "every day when done",
                    "on_completion": "Delete", "due": "2021-01-01"}


def test_a_rule_outside_the_run_is_not_a_recurrence():
    # note-tasks scenario "A rule outside the trailing run is not a recurrence".
    tl = p("- [ ] the 🔁 every day plan is late [[Notes]]")
    assert tl.recurrence is None
    assert p("- [-] Rent 🔁 every month ➕ 2026-01-01 📅 2026-11-01 ❌ 2026-11-02").recurrence == "every month"


def test_duplicate_fields_in_the_run_leftmost_wins_for_reading():
    tl = p("- [ ] x 🔁 every day 🔁 every week")
    assert len(tl.run_fields("recurrence")) == 2
    assert tl.recurrence == "every day"


def test_at_most_21_passes():
    tags = " ".join(f"#t{i}" for i in range(25))
    run = T.parse_run("x " + tags)
    assert len(run.trailing_tags) == 21
    assert run.description == "x #t0 #t1 #t2 #t3"


def test_depends_on_has_no_catastrophic_backtracking():
    import time

    text = "x ⛔ " + "a , " * 3000 + "!"
    t0 = time.monotonic()
    T.parse_run(text)
    assert time.monotonic() - t0 < 1.0


# ── D4 operations: the spec scenarios ─────────────────────────────────────


def test_a_non_canonical_order_is_preserved():
    line = "- [ ] Pay #home 📅 2026-11-01 🔁 every month ^rent"
    assert edit(line, set_fields={"due": D(2026, 11, 5)}) == "- [ ] Pay #home 📅 2026-11-05 🔁 every month ^rent"


def test_an_added_field_goes_to_its_canonical_slot():
    line = "- [ ] Pay 🔁 every month 📅 2026-11-05"
    assert edit(line, set_fields={"scheduled": D(2026, 11, 3)}) == "- [ ] Pay 🔁 every month ⏳ 2026-11-03 📅 2026-11-05"


def test_prefix_hard_break_and_crlf_are_preserved():
    note = "intro\n> \t1. [ ] Ship it  \r\nafter\n"
    lines = re.split(r"(\r\n|\n|\r)", note)
    target = lines[2]
    new = edit(target, status="x", set_fields={"done": D(2026, 11, 10)})
    assert new == "> \t1. [x] Ship it ✅ 2026-11-10  "
    lines[2] = new
    assert "".join(lines) == "intro\n> \t1. [x] Ship it ✅ 2026-11-10  \r\nafter\n"


def test_a_variant_signifier_is_kept():
    assert edit("- [ ] x 📆 2026-01-01", set_fields={"due": D(2026, 2, 1)}) == "- [ ] x 📆 2026-02-01"
    assert edit("- [ ] x 📅\ufe0f 2026-01-01", set_fields={"due": D(2026, 2, 1)}) == "- [ ] x 📅\ufe0f 2026-02-01"
    assert edit("- [ ] x ⌛2026-01-01", set_fields={"scheduled": D(2026, 2, 1)}) == "- [ ] x ⌛2026-02-01"


def test_add_field_before_block_link_and_tail():
    assert edit("- [ ] x ^b1  ", set_fields={"due": D(2026, 1, 1)}) == "- [ ] x 📅 2026-01-01 ^b1  "
    assert edit("- [ ] x 📅 2026-01-01 ^b1", set_fields={"done": D(2026, 1, 2)}) == "- [ ] x 📅 2026-01-01 ✅ 2026-01-02 ^b1"


def test_add_field_in_a_non_canonical_run_goes_to_the_end_of_the_run():
    line = "- [ ] x 📅 2026-01-01 ⏳ 2026-01-01 #t ^b"
    assert edit(line, set_fields={"priority": "high"}) == "- [ ] x 📅 2026-01-01 ⏳ 2026-01-01 #t ⏫ ^b"


def test_add_field_with_tags_in_a_canonical_run():
    line = "- [ ] x #a 🔁 every day #b 📅 2026-01-01 #c"
    assert edit(line, set_fields={"start": D(2025, 12, 1)}) == "- [ ] x #a 🔁 every day #b 🛫 2025-12-01 📅 2026-01-01 #c"
    assert edit(line, set_fields={"done": D(2026, 1, 2)}) == "- [ ] x #a 🔁 every day #b 📅 2026-01-01 ✅ 2026-01-02 #c"


def test_several_fields_added_at_one_slot_keep_canonical_order():
    line = "- [ ] x 📅 2026-01-01"
    new = edit(line, set_fields={"priority": "low", "scheduled": D(2025, 1, 1), "start": D(2024, 1, 1)})
    assert new == "- [ ] x 🔽 🛫 2024-01-01 ⏳ 2025-01-01 📅 2026-01-01"


def test_priority_change_replaces_the_emoji_only():
    assert edit("- [ ] x ⏫\ufe0f 📅 2026-01-01", set_fields={"priority": "lowest"}) == "- [ ] x ⏬\ufe0f 📅 2026-01-01"


def test_recurrence_change_replaces_the_rule_text_only():
    assert edit("- [ ] x 🔁  every week 📅 2026-01-01", set_fields={"recurrence": "every 2 weeks"}) == \
        "- [ ] x 🔁  every 2 weeks 📅 2026-01-01"


def test_remove_field_with_one_adjacent_space():
    assert edit("- [ ] x 📅 2026-01-01 ⏫", remove_fields=fs("due")) == "- [ ] x ⏫"
    assert edit("- [ ] x  📅 2026-01-01", remove_fields=fs("due")) == "- [ ] x "
    # No preceding space inside the text: the following one goes, the gap stays.
    assert edit("- [ ] 📅 2026-01-01 ⏫", remove_fields=fs("due")) == "- [ ] ⏫"
    # Never the trailing whitespace.
    assert edit("- [ ] 📅 2026-01-01  ", remove_fields=fs("due")) == "- [ ]   "
    assert edit("- [ ] 📅 2026-01-01 ✅ 2026-01-01", remove_fields=fs("due", "done")) == "- [ ] "


def test_removing_an_absent_field_is_a_no_op():
    result = apply_changes(p("- [ ] x"), LineChanges(remove_fields=fs("due")))
    assert isinstance(result, LineEdit) and not result.changed


def test_setting_a_value_to_itself_is_a_no_op():
    result = apply_changes(p("- [ ] x 📅 2026-01-01"), LineChanges(set_fields={"due": D(2026, 1, 1)}))
    assert isinstance(result, LineEdit) and not result.changed and result.edits == ()


# ── D6 status transitions at line level ────────────────────────────────────


def test_plain_completion():
    line = "- [ ] Send invoice 📅 2026-11-10"
    assert edit(line, status="x", set_fields={"done": D(2026, 11, 10)}) == "- [x] Send invoice 📅 2026-11-10 ✅ 2026-11-10"
    assert edit(line, status="x") == "- [x] Send invoice 📅 2026-11-10"


def test_custom_marker_completes_to_x():
    assert edit("- [>] Forwarded item", status="x", set_fields={"done": D(2026, 1, 1)}) == "- [x] Forwarded item ✅ 2026-01-01"


def test_reopening_removes_the_done_date():
    assert edit("- [x] Ship ✅ 2026-11-01", status=" ", remove_fields=fs("done", "cancelled")) == "- [ ] Ship"


def test_cancelling_adds_the_cancelled_date():
    line = "- [ ] Rent 🔁 every month 📅 2026-11-01"
    assert edit(line, status="-", remove_fields=fs("done"), set_fields={"cancelled": D(2026, 11, 2)}) == \
        "- [-] Rent 🔁 every month 📅 2026-11-01 ❌ 2026-11-02"
    # Done is replaced by cancelled in the same slot.
    assert edit("- [x] a 📅 2026-01-01 ✅ 2026-01-02", status="-", remove_fields=fs("done"),
                set_fields={"cancelled": D(2026, 1, 3)}) == "- [-] a 📅 2026-01-01 ❌ 2026-01-03"
    assert edit("- [x] ✅ 2026-01-02", status="-", remove_fields=fs("done"),
                set_fields={"cancelled": D(2026, 1, 3)}) == "- [-] ❌ 2026-01-03"


def test_invalid_status_is_an_argument_error():
    for bad in ("", "xx", "[", "]", "\t"):
        refused("- [ ] x", code="invalid_argument", status=bad)


# ── refusal preconditions D4 (a)–(d) ───────────────────────────────────────


def test_a_duplicated_due_date_is_refused():
    refused("- [ ] x 📅 2026-01-01 📅 2026-02-01", set_fields={"due": D(2026, 3, 1)})
    refused("- [ ] x 📅 2026-01-01 📆 2026-02-01", remove_fields=fs("due"))
    # Duplicated anywhere in the text, even if one copy is in the description.
    refused("- [ ] move 📅 2026-01-01 then 📅 2026-02-01", set_fields={"due": D(2026, 3, 1)})


def test_a_stranded_signifier_is_refused():
    refused("- [ ] move 📅 2026-01-01 to the archive", set_fields={"due": D(2026, 3, 1)})
    refused("- [ ] the 🔁 every day plan [[N]]", set_fields={"recurrence": "every week"})
    refused("- [ ] bare 📅 soon", remove_fields=fs("due"))


def test_untouched_duplicates_do_not_block_other_edits():
    assert edit("- [ ] x 📅 2026-01-01 📅 2026-02-01", status="/") == "- [/] x 📅 2026-01-01 📅 2026-02-01"
    assert edit("- [ ] x 📅 2026-01-01 📅 2026-02-01", add_tags=("t",)) == "- [ ] x #t 📅 2026-01-01 📅 2026-02-01"


def test_code_inside_the_run_is_refused_for_field_edits():
    # Inline code inside the run: the masked and original text differ.
    refused("- [ ] x 📅 2026-01-01 #a`b` ⏫", set_fields={"priority": "low"})
    # A status-only edit does not look at fields.
    assert edit("- [ ] x 📅 2026-01-01 #a`b` ⏫", status="x") == "- [x] x 📅 2026-01-01 #a`b` ⏫"


def test_signifier_in_code_is_not_counted():
    line = "- [ ] document `📅 2026-01-01` 📅 2026-02-01"
    assert edit(line, set_fields={"due": D(2026, 3, 1)}) == "- [ ] document `📅 2026-01-01` 📅 2026-03-01"


def test_invalid_date_in_a_changed_field_is_refused():
    refused("- [ ] pay 📅 2026-02-30", set_fields={"due": D(2026, 3, 1)})
    # Removing it is not a change of its value.
    assert edit("- [ ] pay 📅 2026-02-30", remove_fields=fs("due")) == "- [ ] pay"
    # A strict family (recurring completion) is checked even when untouched.
    refused("- [ ] pay ⏳ 2026-02-30 📅 2026-03-01", status="x", strict_families=T.DATE_FAMILIES)
    refused("- [ ] pay ⏳ 2026-01-01 later ⏳ 2026-01-02 📅 2026-03-01", status="x", strict_families=T.DATE_FAMILIES)


def test_argument_errors():
    refused("- [ ] x", code="invalid_argument", set_fields={"due": "2026-01-01"})
    refused("- [ ] x", code="invalid_argument", set_fields={"priority": "urgent"})
    refused("- [ ] x", code="invalid_argument", set_fields={"recurrence": "every day 📅"})
    refused("- [ ] x", code="invalid_argument", set_fields={"id": "a"})
    refused("- [ ] x", code="invalid_argument", set_fields={"nope": 1})
    refused("- [ ] x 📅 2026-01-01", code="invalid_argument", set_fields={"due": D(2026, 1, 2)}, remove_fields=fs("due"))
    refused("- [ ] x", code="invalid_argument", add_tags=("9lives",))
    refused("- [ ] x", code="invalid_argument", remove_tags=("has space",))


def test_an_empty_task_gains_text_after_its_box():
    assert edit("- [ ]", add_tags=("a",)) == "- [ ] #a"
    assert edit("- [ ] ", set_fields={"due": D(2026, 1, 1)}) == "- [ ] 📅 2026-01-01"
    assert edit("- [ ]  ", description="Pay") == "- [ ]  Pay"


# ── tags ────────────────────────────────────────────────────────────────────


def test_add_tag_at_the_end_of_the_description_region():
    assert edit("- [ ] Pay rent 📅 2026-01-01", add_tags=("home",)) == "- [ ] Pay rent #home 📅 2026-01-01"
    assert edit("- [ ] Pay rent", add_tags=("#home",)) == "- [ ] Pay rent #home"
    assert edit("- [ ] Pay rent ^b", add_tags=("home", "work")) == "- [ ] Pay rent #home #work ^b"


def test_add_tag_already_present_is_a_no_op():
    result = apply_changes(p("- [ ] Pay #Home rent"), LineChanges(add_tags=("home",)))
    assert isinstance(result, LineEdit) and not result.changed
    # A nested tag is a different tag.
    assert edit("- [ ] Pay #home/sub", add_tags=("home",)) == "- [ ] Pay #home #home/sub"


def test_removing_a_tag_is_not_hierarchical():
    # Spec scenario "Removing a tag is not hierarchical".
    result = apply_changes(p("- [ ] Chase #waiting-for/bob"), LineChanges(remove_tags=("waiting-for",)))
    assert isinstance(result, LineEdit) and not result.changed


def test_remove_tag_everywhere_case_insensitively():
    assert edit("- [ ] #Home call #bob, then #HOME 📅 2026-01-01 #home", remove_tags=("home",)) == \
        "- [ ] call #bob, then 📅 2026-01-01"
    assert edit("- [ ] call #bob then go", remove_tags=("bob",)) == "- [ ] call then go"


@pytest.mark.parametrize(
    "line, tag",
    [("- [ ] x #foo.bar y", "foo"), ("- [ ] #q'x", "q"), ("- [ ] call #bob, then go", "bob"),
     ("- [ ] a #t b #t.x", "t")],
)
def test_an_attached_tag_is_refused_not_cut(line, tag):
    # Audit item 3: only a delimited #tag token is removed; never a partial cut.
    r = refused(line, remove_tags=(tag,))
    assert "attached" in r.reason
    # With a description replacement the attached occurrence is replaced
    # with the old words, so the tag simply goes.
    result = apply_changes(p(line), LineChanges(remove_tags=(tag,), description="new words"))
    assert isinstance(result, LineEdit), result
    assert tag not in {t.lower() for t in p(result.line).tags}


def test_removing_a_tag_the_task_does_not_carry_stays_a_no_op():
    result = apply_changes(p("- [ ] x #foo.bar y"), LineChanges(remove_tags=("other",)))
    assert isinstance(result, LineEdit) and not result.changed


def test_a_tag_in_code_is_not_removed():
    assert edit("- [ ] use `#x` and #x", remove_tags=("x",)) == "- [ ] use `#x` and"


# ── description replacement (D4, Codex r2 #1) ───────────────────────────────


def test_replacing_the_description_keeps_a_trailing_tag():
    assert edit("- [ ] Pay rent #finance 📅 2026-12-01", description="Pay landlord") == \
        "- [ ] Pay landlord #finance 📅 2026-12-01"


def test_replacing_the_description_keeps_a_tag_from_its_middle():
    assert edit("- [ ] Call #bob about the [[Lease]] 📅 2026-12-01", description="Call the agent") == \
        "- [ ] Call the agent #bob 📅 2026-12-01"


def test_a_tag_already_in_the_new_text_is_not_duplicated():
    assert edit("- [ ] Call #bob about the lease", description="Ping #Bob about keys") == "- [ ] Ping #Bob about keys"


def test_text_combined_with_remove_tags():
    assert edit("- [ ] Pay rent #finance #home 📅 2026-12-01", description="Pay landlord", remove_tags=("finance",)) == \
        "- [ ] Pay landlord #home 📅 2026-12-01"


def test_text_combined_with_add_tags_and_original_order_of_reappended_tags():
    line = "- [ ] a #one b #Two c #one 📅 2026-01-01"
    # The last "#one" is a trailing tag (a run token) and stays where it is.
    assert edit(line, description="new words", add_tags=("three",)) == \
        "- [ ] new words #one #Two #three #one 📅 2026-01-01"


def test_replacing_an_empty_description():
    assert edit("- [ ] 📅 2026-01-01", description="Pay") == "- [ ] Pay 📅 2026-01-01"
    assert edit("- [ ] #a ⏫", description="Pay") == "- [ ] Pay #a ⏫"


@pytest.mark.parametrize(
    "text",
    ["", "   ", "a\nb", "a\rb", "a\x85b", "a\u2028b", "a\u2029b", "due 📅 2026-01-01", "pri ⏫", "x ^block",
     "^block", "a\x00b", "y" * 1001, "rule 🔁 every day", "a 🆔 b", "a ⛔ b", "a 🏁 keep", "a ➕", "a ⌛"],
)
def test_description_rules(text):
    refused("- [ ] x", code="invalid_argument", description=text)
    assert isinstance(T.serialize_new_task(text), TaskLineRefusal)


def test_description_is_stripped_and_may_hold_links_and_tags():
    assert edit("- [ ] x", description="  see [[Note]] #ok  ") == "- [ ] see [[Note]] #ok"
    assert T.validate_description("y" * 1000) == "y" * 1000


# ── the next occurrence (D7 "The next line") ────────────────────────────────


def test_the_plugins_dropped_fields_are_dropped():
    line = "- [ ] Rent 🆔 r1 ⛔ a2 🔁 every month ➕ 2026-01-01 📅 2026-11-01 ✅ 2026-10-01 ^b1"
    nxt = T.next_occurrence_line(p(line), {"due": D(2026, 12, 1)}, created=None)
    assert nxt.line == "- [ ] Rent 🔁 every month 📅 2026-12-01"
    completed = edit(line, status="x", set_fields={"done": D(2026, 11, 2)})
    assert completed == "- [x] Rent 🆔 r1 ⛔ a2 🔁 every month ➕ 2026-01-01 📅 2026-11-01 ✅ 2026-11-02 ^b1"


def test_next_line_sets_created_when_asked_and_keeps_the_rest():
    line = "  * [/] Water #plants 🔼 🔁 every Sunday 🏁 keep ⏳ 2026-11-14 📅 2026-11-15  "
    nxt = T.next_occurrence_line(
        p(line), {"scheduled": D(2026, 11, 21), "due": D(2026, 11, 22)}, created=D(2026, 11, 16)
    )
    assert nxt.line == "  * [ ] Water #plants 🔼 🔁 every Sunday 🏁 keep ➕ 2026-11-16 ⏳ 2026-11-21 📅 2026-11-22  "
    with_created = "- [ ] a ➕ 2020-01-01 📅 2026-01-01"
    assert T.next_occurrence_line(p(with_created), {"due": D(2026, 1, 2)}, created=D(2026, 1, 1)).line == \
        "- [ ] a ➕ 2026-01-01 📅 2026-01-02"


def test_rule_text_is_not_canonicalised():
    nxt = T.next_occurrence_line(p("- [ ] Bin 🔁 every Sunday 📅 2026-11-15"), {"due": D(2026, 11, 22)}, created=None)
    assert "🔁 every Sunday" in nxt.line


def test_next_line_without_dates():
    nxt = T.next_occurrence_line(p("- [x] this is a recurring task 🔁 every day ✅ 2023-05-16"), {}, created=None)
    assert nxt.line == "- [ ] this is a recurring task 🔁 every day"


def test_next_line_refuses_an_invalid_lower_date():
    result = T.next_occurrence_line(p("- [ ] a 🛫 2026-02-30 📅 2026-03-01"), {"due": D(2026, 4, 1)}, created=None)
    assert isinstance(result, TaskLineRefusal) and result.code == "task_malformed"


def test_next_line_rejects_a_date_for_an_absent_field():
    with pytest.raises(ValueError):
        T.next_occurrence_line(p("- [ ] a 📅 2026-03-01"), {"start": D(2026, 4, 1)}, created=None)


# ── a brand-new line (D8) ───────────────────────────────────────────────────


def test_canonical_serialisation():
    line = T.serialize_new_task(
        "Book flights", tags=["travel"], priority="high", recurrence="every year",
        created=D(2026, 11, 10), scheduled=D(2026, 11, 20), due=D(2026, 12, 1),
    )
    assert line == "- [ ] Book flights #travel ⏫ 🔁 every year ➕ 2026-11-10 ⏳ 2026-11-20 📅 2026-12-01"


def test_serialisation_markers_and_tags():
    assert T.serialize_new_task("c", due=D(2026, 11, 12)) == "- [ ] c 📅 2026-11-12"
    assert T.serialize_new_task("c", marker="> 3. ", start=D(2026, 1, 1)) == "> 3. [ ] c 🛫 2026-01-01"
    assert T.serialize_new_task("c #A", tags=["a", "#b", "B"]) == "- [ ] c #A #b"
    assert isinstance(T.serialize_new_task("c", tags=["1x"]), TaskLineRefusal)
    assert isinstance(T.serialize_new_task("c", priority="none"), TaskLineRefusal)
    assert isinstance(T.serialize_new_task("c", marker="x "), TaskLineRefusal)


def test_serialisation_enforces_tier_one_rules_and_the_tag_cap():
    # Audit item 5: D8's rule and tag-count checks live in the serialiser too.
    for rule in ("every weekend", "every 2 weeks on Monday", "every day!", "nonsense"):
        r = T.serialize_new_task("c", recurrence=rule)
        assert isinstance(r, TaskLineRefusal) and r.code == "recurrence_unsupported", rule
    assert T.serialize_new_task("c", recurrence="every day when done") == "- [ ] c 🔁 every day when done"
    many = [f"t{i}" for i in range(33)]
    r = T.serialize_new_task("c", tags=many)
    assert isinstance(r, TaskLineRefusal) and r.code == "invalid_argument"
    assert isinstance(T.serialize_new_task("c", tags=many[:32]), str)


# ── audit regressions: spacing around insertions (items 2, 4) and clean
#    re-append (item 6) ─────────────────────────────────────────────────────


def test_every_inserted_token_is_separated_when_everything_between_is_removed():
    assert edit("- [ ] 🔁 every week", remove_fields=fs("recurrence"), set_fields={"due": D(2026, 1, 1)},
                add_tags=("t",)) == "- [ ] #t 📅 2026-01-01"
    assert edit("- [ ] 📅 2026-01-01", remove_fields=fs("due"), set_fields={"done": D(2026, 1, 1)},
                description="hello") == "- [ ] hello ✅ 2026-01-01"
    assert edit("- [ ] 📅 2026-01-01 ^b", remove_fields=fs("due"), set_fields={"done": D(2026, 1, 1)},
                add_tags=("t",)) == "- [ ] #t ✅ 2026-01-01 ^b"


def test_a_first_run_token_removal_does_not_block_a_placement():
    assert edit("- [ ] ⏫ 📅 2026-01-01", remove_fields=fs("priority"),
                set_fields={"scheduled": D(2025, 12, 1)}) == "- [ ] ⏳ 2025-12-01 📅 2026-01-01"
    assert edit("- [ ] x ⏫ 📅 2026-01-01", remove_fields=fs("priority"),
                set_fields={"scheduled": D(2025, 12, 1)}) == "- [ ] x ⏳ 2025-12-01 📅 2026-01-01"
    assert edit("- [ ] ⏫ 🛫 2025-01-01 📅 2026-01-01", remove_fields=fs("priority", "start"),
                set_fields={"scheduled": D(2025, 12, 1)}) == "- [ ] ⏳ 2025-12-01 📅 2026-01-01"


def test_a_preserved_tag_is_reappended_clean():
    assert edit("- [ ] x [[n #a]] 📅 2026-01-01", description="y") == "- [ ] y #a 📅 2026-01-01"
    assert edit("- [ ] x #Ab's 📅 2026-01-01 #ok", description="y") == "- [ ] y #Ab 📅 2026-01-01 #ok"


def _inserted_tokens_are_separated(result):
    out = []
    for e in result.edits:
        if e.start == e.end and e.text.strip():
            out.append(e)
    return out


def test_property_insertions_are_always_space_separated():
    rng = random.Random(4281)
    checked = 0
    for _ in range(3000):
        line, _clean = _random_line(rng)
        tl = parse(line)
        changes = _random_changes(rng, tl)
        result = apply_changes(tl, changes)
        if not isinstance(result, LineEdit):
            continue
        # Locate each insertion in the new line and check both neighbours.
        shift = 0
        for e in result.edits:
            pos = e.start + shift
            if e.start == e.end and e.text.strip():
                core = e.text.strip(" ")
                at = pos + (len(e.text) - len(e.text.lstrip(" ")))
                before = result.line[at - 1] if at > 0 else " "
                after = result.line[at + len(core)] if at + len(core) < len(result.line) else " "
                assert before.isspace() and after.isspace(), (line, changes, result.line)
                checked += 1
            shift += len(e.text) - (e.end - e.start)
    assert checked > 500


# ── identity (D1) and normalisation (D9, D11) ────────────────────────────────


def test_the_key_survives_the_text_cut():
    text = "a" * 1500
    key = T.task_key(text)
    assert key != T.task_key(text[:1000])
    assert re.fullmatch(r"[0-9a-f]{12}", key)
    import hashlib

    assert key == hashlib.sha256(b"omcp-task-v1\x00" + text.encode()).hexdigest()[:12]


def test_indentation_and_status_do_not_change_the_key():
    a, b = p("- [ ] Pay rent"), p("\t> 3. [/] Pay rent  ")
    assert T.task_key(a.full_text) == T.task_key(b.full_text)


def test_identical_lines_get_ordinals():
    key = T.task_key("Call")
    ids = [T.render_id(key, i, 4) for i in range(1, 5)]
    assert ids == [f"{key}.{i}of4" for i in range(1, 5)]
    assert T.render_id(key, 1, 1) == key
    # The D1 grammar's longest id is 23 characters (12 + ".9999of9999").
    assert len(T.render_id(key, 9999, 9999)) == 23


def test_id_grammar_and_equivalence():
    key = "0123456789ab"
    assert T.parse_id(key) == T.TaskId(key)
    assert T.parse_id(f"{key}.1of1") == T.TaskId(key)
    assert T.parse_id(f"{key}.2of3") == T.TaskId(key, 2, 3)
    assert T.parse_id(f"{key}.2of3").suffixed
    for bad in [key.upper(), key[:11], key + "0", f"{key}.0of3", f"{key}.4of3", f"{key}.01of3",
                f"{key}.1of10000", f"{key}.2of", f"{key}.2", f" {key}", None, 5]:
        assert T.parse_id(bad) is None, bad
    with pytest.raises(ValueError):
        T.render_id(key, 3, 2)
    with pytest.raises(ValueError):
        T.render_id("xyz", 1, 1)


def test_normalised_description_and_key():
    a = T.normalise_description("Pay  Rent #Home 📅 2026-01-01 ✅ 2026-01-02 ^blk")
    b = T.normalise_description("pay rent\u00a0#home ⏫")
    assert a == b == "pay rent #home"
    assert T.description_key(a) == __import__("hashlib").sha256(b"omcp-desc-v1\x00pay rent #home").hexdigest()[:16]
    # NFC and casefold: decomposed é and German ß.
    assert T.normalise_description("Cafe\u0301 STRASSE") == T.normalise_description("café straße")
    # Trailing tags stay in the description (the plugin's description).
    assert T.normalise_description("x 📅 2026-01-01 #t") == "x #t"
    assert T.description_region("x 📅 2026-01-01 #t") == "x"


def test_fold_text():
    assert T.fold_text("ΣΊΣΥΦΟΣ") == T.fold_text("σίσυφος") or T.fold_text("ΣΊΣΥΦΟΣ") == "σίσυφοσ"
    assert T.fold_text("Straße") == T.fold_text("STRASSE")


def test_tag_prefixes():
    assert T.tag_prefixes(["Waiting-For/Chase"]) == ["waiting-for", "waiting-for/chase"]
    assert T.tag_prefixes(["a/b", "A/c", "a"]) == ["a", "a/b", "a/c"]
    assert T.tag_prefixes(["a//b"]) == ["a", "a/b"]


def test_empty_segments_are_normalised_identically_on_both_sides():
    # note-tasks scenario "Empty segments are normalised identically on both sides".
    stored = T.tag_prefixes(["Waiting-For//Chase/"])
    assert stored == ["waiting-for", "waiting-for/chase"]
    # Filter values go through the same normaliser and are found.
    for value in ("waiting-for//chase", "waiting-for", "Waiting-For/Chase"):
        assert T.normalise_tag(value) in stored
    assert T.normalise_tag("a///b//") == "a/b"
    assert T.tag_prefixes(["a/", "A"]) == ["a"]


def test_tag_prefixes_are_lossless_at_the_largest_buildable_case():
    # tasks.md 1.4: 64 distinct 127-character tags of a 3-character first
    # segment and 62 one-character segments give 4,032 prefixes, uncapped.
    letters = "abcdefghijklmnopqrstuvwxyz"
    tags = []
    for i in range(64):
        head = "t" + letters[i // 26] + letters[i % 26]
        tags.append("/".join([head] + [letters[(i + j) % 26] for j in range(62)]))
    assert all(len(t) == 127 and T.valid_tag(t) for t in tags)
    assert len(set(tags)) == 64
    prefixes = T.tag_prefixes(tags)
    assert len(prefixes) == 64 * 63 == 4032
    for tag in tags:
        assert tag in prefixes


# ── purity ──────────────────────────────────────────────────────────────────


def _imports(module):
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    out = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def test_task_line_is_pure():
    assert _imports(T) <= {"__future__", "datetime", "hashlib", "re", "unicodedata",
                           "collections.abc", "dataclasses", "src.services.links",
                           "src.services.task_recurrence"}
    assert not any(name.startswith("src.config") for name in _imports(links))


# ── property test: bytes outside the declared spans are identical ───────────

PREFIXES = ["- ", "* ", "+ ", "1. ", "12) ", "  - ", "\t- ", "> - ", "> > 1. ", ">\t* ", "    + "]
STATUSES = [" ", "x", "X", "/", "-", ">", "?"]
GAPS = [" ", "  ", "\t"]
WORDS = ["Pay", "rent", "the", "[[Lease]]", "#home", "#Work/Alpha", "`code`", "ünïcode", "Ω", "call#not",
         "a,b", "(paren)", "#bob,", "x"]
TAILS = ["", "", "  ", " ", "\t"]
SYMBOLS = {
    "due": ["📅", "📆", "🗓", "📅\ufe0f"], "scheduled": ["⏳", "⌛"], "start": ["🛫"], "created": ["➕"],
    "done": ["✅"], "cancelled": ["❌"],
}
RULES = ["every week", "every 2 days when done", "every Sunday", "every month on the last"]


def _random_date(rng):
    return D(rng.randint(2020, 2030), rng.randint(1, 12), rng.randint(1, 28))


def _random_token(rng, family):
    if family == "priority":
        return rng.choice(list(T.PRIORITY_SYMBOLS.values())) + rng.choice(["", "\ufe0f"])
    if family == "recurrence":
        return "🔁 " + rng.choice(RULES)
    if family == "id":
        return "🆔 " + rng.choice(["a1", "task-2"])
    if family == "depends_on":
        return "⛔ " + rng.choice(["a1", "a1,b2", "a1, b2"])
    if family == "on_completion":
        return "🏁 keep"
    return rng.choice(SYMBOLS[family]) + rng.choice([" ", "", "  "]) + _random_date(rng).isoformat()


def _random_line(rng):
    """A random task line; ``clean`` lines have every family at most once,
    a non-empty description and no code in the run."""
    families = rng.sample(sorted(T.FAMILIES), rng.randint(0, 6))
    clean = rng.random() < 0.8
    run = [_random_token(rng, f) for f in families]
    if not clean and run:
        run.append(_random_token(rng, rng.choice(families)))  # a duplicate
    for _ in range(rng.randint(0, 2)):
        run.insert(rng.randint(0, len(run)), "#" + rng.choice(["t1", "Tag/x", "home"]))
    words = [rng.choice(WORDS) for _ in range(rng.randint(1, 4))]
    if clean:
        # No code and no attached tag ("#bob," cannot be removed cleanly).
        words = [w for w in words if "`" not in w and w != "#bob,"] or ["task"]
    text = " ".join(words + run)
    if rng.random() < 0.3:
        text += " ^" + rng.choice(["b1", "blk-2"])
    line = rng.choice(PREFIXES) + "[" + rng.choice(STATUSES) + "]" + rng.choice(GAPS) + text + rng.choice(TAILS)
    return line, clean


def _random_changes(rng, tl):
    kw = {}
    if rng.random() < 0.4:
        kw["status"] = rng.choice(STATUSES)
    sets = {}
    for family in rng.sample(sorted(T.DATE_FAMILIES), rng.randint(0, 2)):
        sets[family] = _random_date(rng)
    if rng.random() < 0.2:
        sets["priority"] = rng.choice(list(T.PRIORITY_SYMBOLS))
    if rng.random() < 0.15:
        sets["recurrence"] = rng.choice(RULES)
    kw["set_fields"] = sets
    removable = sorted(T.FAMILIES - set(sets))
    kw["remove_fields"] = frozenset(rng.sample(removable, rng.randint(0, 2)))
    kw["remove_block_link"] = rng.random() < 0.1
    if rng.random() < 0.3:
        kw["add_tags"] = tuple(rng.sample(["home", "new", "t1", "Work/Alpha"], rng.randint(1, 2)))
    if rng.random() < 0.3:
        kw["remove_tags"] = tuple(rng.sample(["home", "bob", "Tag/x", "t1"], rng.randint(1, 2)))
    if rng.random() < 0.2:
        kw["description"] = " ".join(rng.choice(["Ring", "#bob", "the", "bank", "[[X]]"]) for _ in range(3))
    return LineChanges(**kw)


def _date_view(line):
    tasks, _ = extract_tasks_bounded(line, line_offset=0)
    (task,) = tasks
    return task


def test_property_bytes_outside_the_spans_are_identical():
    rng = random.Random(428)
    successes = 0
    for _ in range(4000):
        line, clean = _random_line(rng)
        tl = parse(line)
        assert tl is not None, line
        changes = _random_changes(rng, tl)
        result = apply_changes(tl, changes)
        if isinstance(result, TaskLineRefusal):
            assert result.code in ("task_malformed", "invalid_argument"), result
            # A clean line (every family at most once, a description, no code
            # in the run) is never refused: the editor must not refuse its
            # way to safety.
            assert not clean, (line, changes, result)
            continue
        successes += 1
        _check_success(line, tl, changes, result)
    assert successes > 2000


def _check_success(line, tl, changes, result):
    """The property: bytes outside the declared spans are identical, and the
    result re-parses with exactly the intended fields."""
    # 1. The new line is exactly the old one with the declared edits.
    assert result.line == compose(line, result.edits)
    # 2. No edit touches the prefix, the box outside its status character,
    #    the gap, or the trailing whitespace.
    status_at = tl.box_start + 1
    for e in result.edits:
        if (e.start, e.end) == (status_at, status_at + 1):
            continue
        assert tl.text_start <= e.start <= e.end <= tl.text_end, (line, e)
    # 3. No edit touches an untouched field token or a kept block link.
    touched = set(changes.set_fields) | set(changes.remove_fields)
    kept = [t for t in tl.tokens if t.kind == "field" and t.family not in touched]
    if tl.block is not None and not changes.remove_block_link:
        kept.append(tl.block)
    for t in kept:
        for e in result.edits:
            assert not (e.start < t.end and e.end > t.start), (line, t, e)
            assert not (t.start < e.start < t.end), (line, t, e)
    # 4. Re-parse through this parser and through the extractor.
    new = parse(result.line)
    assert new is not None
    assert new.status == (changes.status or tl.status)
    for family in T.CANONICAL_ORDER:
        if family not in touched:
            assert [result.line[t.start:t.end] for t in new.run_fields(family)] == \
                [line[t.start:t.end] for t in tl.run_fields(family)], (line, family)
    old_task = _date_view(line)
    new_task = _date_view(result.line)
    for family, attr in (("due", "due"), ("scheduled", "scheduled"), ("start", "start"), ("done", "done_on")):
        if family in changes.set_fields:
            assert getattr(new_task, attr) == changes.set_fields[family]
        elif family in changes.remove_fields:
            assert getattr(new_task, attr) is None
        else:
            assert getattr(new_task, attr) == getattr(old_task, attr)
    if "priority" in changes.set_fields:
        assert new_task.priority == changes.set_fields["priority"]
    elif "priority" in changes.remove_fields:
        assert new_task.priority is None
    else:
        assert new_task.priority == old_task.priority
    if "recurrence" in changes.set_fields:
        assert new.recurrence == changes.set_fields["recurrence"]
    # Tags (extractor view): conserved, plus added, minus removed (D5 check
    # 6), counting tags the caller typed into a new description.
    expected = {t.lower() for t in (old_task.tags or [])}
    if changes.description is not None:
        expected |= {m.group(1).lower() for m in T.INLINE_TAG_RE.finditer(changes.description)}
    expected -= {t.lower().lstrip("#") for t in changes.remove_tags}
    expected |= {t.lower().lstrip("#") for t in changes.add_tags}
    assert {t.lower() for t in (new_task.tags or [])} == expected, (line, changes, result.line)
    # 5. Spliced into a note with mixed terminators, only that line's content
    #    changes.
    note_lines = ["# H", line, "  - child", "tail"]
    terms = ["\r\n", "\n", "\r", ""]
    old_note = "".join(a + b for a, b in zip(note_lines, terms))
    note_lines[1] = result.line
    new_note = "".join(a + b for a, b in zip(note_lines, terms))
    start = len("# H\r\n")
    assert old_note[:start] == new_note[:start]
    assert old_note[start + len(line):] == new_note[start + len(result.line):]


CORPUS = [
    "- [ ] Pay #home 📅 2026-11-01 🔁 every month ^rent",
    "- [ ] Pay 🔁 every month 📅 2026-11-05",
    "> \t1. [ ] Ship it  ",
    "- [ ] x 📆 2026-01-01",
    "- [ ] Send invoice 📅 2026-11-10",
    "- [x] Ship ✅ 2026-11-01",
    "- [ ] Rent 🆔 r1 ⛔ a2 🔁 every month ➕ 2026-01-01 📅 2026-11-01 ✅ 2026-10-01 ^b1",
    "- [ ] Call #bob about the [[Lease]] 📅 2026-12-01",
    "- [ ] Pay rent #finance #home 📅 2026-12-01",
    "- [ ] ship 📅 2026-11-01 ⏳ 2026-10-30 🛫 2026-10-28 ⏫ #work #alpha",
    "- [-] Rent 🔁 every month ➕ 2026-01-01 📅 2026-11-01 ❌ 2026-11-02",
    "- [ ] Do something #tag1 📅 2021-01-01 #tag2 🛫 2021-01-02",
    "  * [/] Water #plants 🔼\ufe0f 🔁 every Sunday 🏁 keep ⏳ 2026-11-14 📅\ufe0f 2026-11-15  ",
    ">\t- [?] a`b` 📅2026-01-01",
    "- [ ] 📅 2026-01-01",
    "- [ ] ",
    "12) [ ] numbered ^blk-1",
]


@pytest.mark.parametrize("line", CORPUS)
def test_property_over_the_corpus(line):
    tl = p(line)
    change_sets = [
        LineChanges(status="x", set_fields={"done": D(2026, 11, 10)}),
        LineChanges(status=" ", remove_fields=fs("done", "cancelled")),
        LineChanges(status="-", remove_fields=fs("done"), set_fields={"cancelled": D(2026, 11, 2)}),
        LineChanges(set_fields={"due": D(2027, 1, 1), "scheduled": D(2026, 12, 1), "start": D(2026, 11, 1)}),
        LineChanges(remove_fields=fs("due", "scheduled", "start", "priority", "recurrence")),
        LineChanges(set_fields={"priority": "lowest", "recurrence": "every 3 days"}),
        LineChanges(add_tags=("added",), remove_tags=("home", "bob")),
        LineChanges(description="Replaced words", add_tags=("x1",)),
        LineChanges(remove_block_link=True, remove_fields=fs("id", "depends_on", "created")),
    ]
    successes = 0
    for changes in change_sets:
        result = apply_changes(tl, changes)
        if isinstance(result, TaskLineRefusal):
            assert result.code == "task_malformed", (line, changes, result)
            continue
        successes += 1
        _check_success(line, tl, changes, result)
    assert successes >= 7, line



# ── linear trailing-run parse (#428): differential against the frozen old
#    algorithm, and a linearity bound ─────────────────────────────────────────


def _frozen_parse_run(text, *, offset=0, floor=None):
    """The pre-optimisation `parse_run`, frozen verbatim: it rescanned the
    text from its start on every pass. The oracle for the linear version."""

    def ws_start(pos, lo):
        while pos > lo and text[pos - 1].isspace():
            pos -= 1
        return pos

    floor = offset if floor is None else floor
    end = len(text.rstrip())
    block = None
    m = T._BLOCK_LINK_RE.search(text, 0, end)
    if m is not None:
        b_start = m.start("val")
        block = T.Token(
            kind="block", family=None,
            start=offset + b_start, end=offset + m.end(),
            value_start=offset + b_start, value_end=offset + m.end(),
            value=m.group("val"), symbol="^",
            sep_start=offset + ws_start(b_start, floor - offset),
        )
        end = len(text[: m.start()].rstrip())
    found = []
    for _ in range(T._MAX_PASSES):
        matched = False
        for family, pattern in T._PLUGIN_PATTERNS:
            m = pattern.search(text, 0, end)
            if m is None:
                continue
            matched = True
            if family == "#tag":
                start = m.start("val")
                found.append(T.Token(
                    kind="tag", family=None, start=start, end=m.end(),
                    value_start=start, value_end=m.end(), value=m.group("val"),
                    symbol="#", sep_start=start,
                ))
            else:
                start = m.start("sym")
                if family == T.PRIORITY:
                    v_start, v_end = start, m.end("sym")
                    value = T.PRIORITY_NAMES[m.group("sym")]
                else:
                    v_start, v_end = m.start("val"), m.end("val")
                    value = m.group("val")
                symbol_end = m.end("sym")
                if text.startswith(T.VS16, symbol_end):
                    symbol_end += 1
                found.append(T.Token(
                    kind="field", family=family, start=start, end=m.end(),
                    value_start=v_start, value_end=v_end, value=value,
                    symbol=text[start:symbol_end], sep_start=start,
                ))
            end = len(text[: m.start()].rstrip())
        if not matched:
            break
    tokens = []
    for t in sorted(found, key=lambda t: t.start):
        sep = ws_start(t.start, floor - offset)
        tokens.append(T.Token(
            kind=t.kind, family=t.family,
            start=offset + t.start, end=offset + t.end,
            value_start=offset + t.value_start, value_end=offset + t.value_end,
            value=t.value, symbol=t.symbol, sep_start=offset + sep,
        ))
    return T.RunParse(text=text, description_end=end, tokens=tuple(tokens), block=block)


_DIFF_ATOMS = [
    "📅", "📆", "🗓", "⏳", "⌛", "🛫", "➕", "✅", "❌", "🔁", "🏁", "🆔", "⛔",
    "🔺", "⏫", "🔼", "🔽", "⏬", "\ufe0f", "#", "^", " ", "  ", "\t", "\u00a0",
    "　", "\x1c", "\x85", "\u2028", "​", ",", "!", "-", "_", ".", "'",
    "`", "[[", "]]", "a", "Z", "b1", "2026-01-01", "2026-02-30", "1234-56-78",
    "every", "day", "when done", "keep", "delete", "abc-1", "x_2", "é", "Ω",
]
_DIFF_TOKENS = [
    "📅 2026-01-01", "📅\ufe0f2026-01-01", "📆  2026-02-30", "⌛ 2026-01-01", "🛫2026-01-01",
    "➕ 2026-01-01", "✅ 2026-01-01", "❌ 2026-01-01", "🔁 every week", "🔁every 2 days when done",
    "🔁 every day!", "🏁 keep", "🏁\ufe0f Delete", "🆔 abc-1", "⛔ a1, b_2", "⛔a , b ,c",
    "⏫", "🔼\ufe0f", "#tag", "#Tag/sub", "#q'x", "#a]]", "#1234", "#", "^blk-1", "^b_1",
]


def _random_text(rng):
    parts = []
    for _ in range(rng.randint(0, 6)):
        parts.append(rng.choice(_DIFF_ATOMS))
    for _ in range(rng.randint(0, 6)):
        parts.append(rng.choice([" ", "", "  ", "\t"]) + rng.choice(_DIFF_TOKENS + _DIFF_ATOMS))
    if rng.random() < 0.3:
        parts.append(" ^" + rng.choice(["b1", "blk-2", "x_y", ""]))
    parts.append(rng.choice(["", " ", "  ", "\t", "\u00a0", "　"]))
    return "".join(parts)


def test_linear_parse_run_is_the_old_parse_byte_for_byte():
    rng = random.Random(4282)
    for i in range(25000):
        text = _random_text(rng)
        offset = rng.choice([0, 0, 6, 11])
        # A floor below the offset is outside the contract: the old walk
        # indexed text[-1] there (a wrap-around), the new one stops at 0.
        floor = rng.choice([None, None, offset, offset + 2])
        assert T.parse_run(text, offset=offset, floor=floor) == \
            _frozen_parse_run(text, offset=offset, floor=floor), repr(text)


@pytest.mark.parametrize("line", ANATOMY + CORPUS)
def test_linear_parse_run_matches_on_the_corpus(line):
    tl = parse(line)
    if tl is None:
        return
    text = line[tl.text_start : tl.text_end]
    assert T.parse_run(text, offset=tl.text_start) == _frozen_parse_run(text, offset=tl.text_start)


TEN_MIB = 10 * 1024 * 1024
# The old rescan-per-pass parse took ~25 s on the dates shape and ~22 s on the
# spaces shape at 10 MiB; the linear one takes well under half a second on the
# development host. 2 s is generous headroom for a shared runner and still
# fails at once on a return to the old algorithm.
LINEAR_BOUND_SECONDS = 2.0


def _tile(unit, size):
    return (unit * (size // len(unit) + 1))[:size]


LINEAR_SHAPES = {
    "plain": lambda n: _tile("word ", n),
    "desc+run": lambda n: _tile("word ", n - 40) + " 📅 2026-01-01 ⏫ #t ^b",
    "tags": lambda n: "x " + _tile("#a ", n),
    "dates": lambda n: "x " + _tile("📅 2026-01-01 ", n),
    "recurrence": lambda n: "x 🔁 " + _tile("every day ", n),
    "hashes": lambda n: _tile("#", n),
    "due emoji": lambda n: _tile("📅", n),
    "recurrence emoji": lambda n: _tile("🔁", n),
    "spaces then priorities": lambda n: "x" + _tile(" ", n) + " ⏫" * 30,
    "spaces before a date": lambda n: "x 📅" + _tile(" ", n) + "2026-01-01",
    "depends list": lambda n: "x ⛔ " + _tile("a , ", n),
}


@pytest.mark.parametrize("shape", sorted(LINEAR_SHAPES))
def test_parse_is_linear_on_adversarial_lines(shape):
    import time

    make = LINEAR_SHAPES[shape]
    text = make(TEN_MIB).rstrip() or "x"
    line = "- [ ] " + text
    started = time.perf_counter()
    T.parse_run(text)
    parse(line, masked=line)  # the mask is the caller's; time the parse
    elapsed = time.perf_counter() - started
    assert elapsed < LINEAR_BOUND_SECONDS, f"{elapsed:.2f}s on {shape}"
    # Doubling the input must not much more than double the time.
    small = make(TEN_MIB // 8).rstrip() or "x"
    large = make(TEN_MIB // 4).rstrip() or "x"
    t = []
    for sample in (small, large):
        best = None
        for _ in range(3):
            s0 = time.perf_counter()
            T.parse_run(sample)
            d = time.perf_counter() - s0
            best = d if best is None else min(best, d)
        t.append(best)
    assert t[1] / max(t[0], 1e-4) < 4, f"{shape}: n={t[0]:.4f}s 2n={t[1]:.4f}s — superlinear"
