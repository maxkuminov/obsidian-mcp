"""`move_task`'s pure planner and verifiers (#430, design D3–D9, D13, D19;
tasks.md 1.9).

No filesystem, no database: `plan_move`, `plan_move_within`,
`plan_relocated_removal`, `destination_still_holds`, `verify_move_insert` and
`verify_removal` over note texts. The corpus property test at the end checks,
for every task of every note, that a two-note move puts every original line's
content of both notes into the two new texts exactly once (or refuses), and
that a same-note move is a permutation of lines.
"""
from __future__ import annotations

import collections
import dataclasses
import os
import re
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

from src.services import task_line  # noqa: E402
from src.services import task_write as tw  # noqa: E402
from src.services.tasks import tasks_from_note_text  # noqa: E402


def key_of(line: str) -> str:
    parsed = task_line.parse(line)
    assert parsed is not None, line
    return task_line.task_key(parsed.full_text)


def args(line_or_id: str, **kw) -> tw.MoveArgs:
    tid = line_or_id if re.fullmatch(r"[0-9a-f]{12}(\.\d+of\d+)?", line_or_id) else key_of(line_or_id)
    return tw.MoveArgs(id=tid, hash_supplied=kw.pop("hash_supplied", "." in tid), **kw)


def move(src: str, dst: str, line: str, **kw):
    return tw.plan_move(src, dst, args(line, **kw), None)


def ok(src: str, dst: str, line: str, **kw) -> tw.MovePlan:
    got = move(src, dst, line, **kw)
    assert not isinstance(got, tw.TaskWriteRefusal), got
    return got


def refused(src: str, dst: str, line: str, code: str, **kw) -> tw.TaskWriteRefusal:
    got = move(src, dst, line, **kw)
    assert isinstance(got, tw.TaskWriteRefusal), got
    assert got.code == code, got
    return got


# ── D3: the moved block, and the scope refusals ─────────────────────────────


def test_a_task_moves_with_its_context_and_sub_task():
    """Scenario: A task moves with its context and sub-task."""
    src = "- [ ] Call the bank 📅 2026-11-20\n    - ask about the fee\n    - [ ] find the account number\n"
    plan = ok(src, "", "- [ ] Call the bank 📅 2026-11-20")
    assert plan.removal.new_text == ""
    assert plan.insert.new_text == src.rstrip("\n")
    assert plan.moved.lines == tuple(src.rstrip("\n").split("\n"))
    assert plan.dest_lines == (1, 3)
    assert [i for _l, i in plan.dest_ids] == [key_of("- [ ] Call the bank 📅 2026-11-20"),
                                               key_of("- [ ] find the account number")]


def test_the_block_is_uncapped_and_crosses_blank_lines():
    ctx = "".join(f"    - context {n}\n" for n in range(20))
    src = f"- [ ] t\n{ctx}\n    - after a blank\n- [ ] next\n"
    plan = ok(src, "", "- [ ] t")
    assert len(plan.moved.lines) == 23
    assert plan.removal.new_text == "- [ ] next\n"


@pytest.mark.parametrize("src,line,needle", [
    ("- [ ] Project\n    - [ ] step one\n    - [ ] step two\n", "- [ ] step one", "nested"),
    ("> - [ ] Renew passport\n", "- [ ] Renew passport", "blockquote or callout"),
    ("> [!todo]\n> - [ ] in a callout\n", "- [ ] in a callout", "blockquote or callout"),
    ("- [ ] t\n    > quoted context\n", "- [ ] t", "blockquote line"),
    ("- [ ] t\n> quoted\n", "- [ ] t", "blockquote line"),
    ("- [ ] t\n    ```\n    code\n    ```\n", "- [ ] t", "fenced code block"),
    ("- [ ] t\n    ~~~\n    code\n", "- [ ] t", "fenced code block"),
    ("- [ ] t\n  # Notes\n", "- [ ] t", "heading"),
    ("- [ ] t\n    ## Deep\n", "- [ ] t", "heading"),
])
def test_scope_refusals(src, line, needle):
    """Scenarios: A nested task is refused; A callout task is refused; A
    blockquote line in the block is refused; A fence line in the block is
    refused; A heading under the task is refused."""
    r = refused(src, "- [ ] d\n", line, "task_malformed")
    assert needle in r.reason and "edit_note" in r.reason
    assert r.note == "source"


def test_a_tab_indented_child_moves_byte_for_byte():
    """Scenario 13 (second half): a tab-indented child, no fence, byte for byte."""
    src = "- [ ] t\n\t- child `code` trailing  \n"
    plan = ok(src, "Notes\n", "- [ ] t")
    assert plan.insert.new_text == "Notes\n- [ ] t\n\t- child `code` trailing  \n"


def test_a_hash_tag_line_is_not_a_heading():
    src = "- [ ] t\n    #tag and words\n"
    plan = ok(src, "", "- [ ] t")
    assert plan.insert.new_text == "- [ ] t\n    #tag and words"


def test_a_quoted_destination_anchor_is_refused():
    """Scenario: A quoted destination anchor is refused."""
    r = refused("- [ ] t\n", "> - [ ] a\n", "- [ ] t", "task_malformed")
    assert r.note == "destination" and "line 1" in r.reason


def test_a_plain_task_arrives_byte_for_byte():
    """Scenario: A plain task arrives byte for byte (D4): fields, block link,
    status, trailing whitespace and an ordered marker all unchanged."""
    src = ("3. [/] Draft ⏫ 🔁 every week 🆔 abc ⛔ def 🏁 keep 📅 2026-11-20 ^blk  \n"
           "    context one\n    context two\n    - [ ] sub\n")
    plan = ok(src, "- [ ] a\n", "3. [/] Draft ⏫ 🔁 every week 🆔 abc ⛔ def 🏁 keep 📅 2026-11-20 ^blk")
    assert plan.insert.new_text == "- [ ] a\n" + src
    assert plan.moved.block_ids == ("blk",)


# ── D5: placement ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("dst,section,want", [
    # After the last top-level task's whole block.
    ("Intro\n- [ ] a\n- [ ] b\n    - ctx\n\nMore prose\n", None,
     "Intro\n- [ ] a\n- [ ] b\n    - ctx\n- [ ] t\n\nMore prose\n"),
    # No task: after the region's last non-blank line.
    ("# S\nIntro\n\n# T\n", "S", "# S\nIntro\n- [ ] t\n\n# T\n"),
    # An empty region: directly after the heading.
    ("# S\n# T\n", "S", "# S\n- [ ] t\n# T\n"),
    # An empty body after frontmatter: at body start.
    ("---\na: 1\n---\n", None, "---\na: 1\n---\n- [ ] t\n"),
    # A nested task is never the anchor.
    ("- [ ] a\n    - [ ] deep\nprose\n", None, "- [ ] a\n    - [ ] deep\n- [ ] t\nprose\n"),
])
def test_placement_is_add_tasks(dst, section, want):
    plan = ok("- [ ] t\n", dst, "- [ ] t", to_section=section)
    assert plan.insert.new_text == want


def test_the_marker_is_kept_and_no_prefix_is_copied():
    plan = ok("7. [ ] t\n", "1. [ ] a\n", "7. [ ] t")
    assert plan.insert.new_text == "1. [ ] a\n7. [ ] t\n"


def test_section_refusals_are_add_tasks():
    r = refused("- [ ] t\n", "# S\n", "- [ ] t", "selector_unresolved", to_section="Nope")
    assert r.note == "destination"
    r = refused("- [ ] t\n", "# S\n  ```\ncode\n", "- [ ] t", "content_unsafe", to_section="S")
    assert r.detail[0] == "unmatched_fence"
    r = refused("- [ ] t\n", "---\nkey: [unclosed\n---\n# S\n", "- [ ] t", "content_unsafe")
    assert r.note == "destination"


# ── D6, D7: terminators ─────────────────────────────────────────────────────


def test_end_of_file_without_a_newline_at_both_ends():
    """Scenario: End of file without a newline, at both ends."""
    plan = ok("Intro\n- [ ] x", "Notes", "- [ ] x")
    assert plan.removal.new_text == "Intro"
    assert plan.insert.new_text == "Notes\n- [ ] x"


def test_the_only_task_of_a_note_into_an_empty_note():
    """Scenario: The only task of a note, into an empty note."""
    plan = ok("- [ ] x", "", "- [ ] x")
    assert plan.removal.new_text == "" and plan.insert.new_text == "- [ ] x"


def test_terminated_files_keep_every_terminator():
    """Scenario: Terminated files keep every terminator."""
    plan = ok("Intro\n- [ ] x\n", "Notes\n", "- [ ] x")
    assert plan.removal.new_text == "Intro\n" and plan.insert.new_text == "Notes\n- [ ] x\n"


def test_terminators_follow_the_destination():
    """Scenario: Terminators follow the destination (both directions)."""
    plan = ok("A\r\n- [ ] x\r\n    - c\r\nB\r\n", "N\n", "- [ ] x")
    assert plan.removal.new_text == "A\r\nB\r\n" and plan.insert.new_text == "N\n- [ ] x\n    - c\n"
    plan = ok("A\n- [ ] x\n    - c\n", "N\r\n", "- [ ] x")
    assert plan.insert.new_text == "N\r\n- [ ] x\r\n    - c\r\n"


def test_a_multi_line_block_into_an_empty_body_is_joined():
    plan = ok("- [ ] x\n    - c\n", "---\na: 1\n---", "- [ ] x")
    assert plan.insert.new_text == "---\na: 1\n---\n- [ ] x\n    - c"


def test_removing_the_whole_body_after_frontmatter_keeps_the_frontmatter():
    plan = ok("---\na: 1\n---\n- [ ] x\n", "", "- [ ] x")
    assert plan.removal.new_text == "---\na: 1\n---\n"


def test_blank_lines_around_the_block_are_untouched():
    plan = ok("A\n\n- [ ] x\n\nB\n", "", "- [ ] x")
    assert plan.removal.new_text == "A\n\n\nB\n"


# ── D8: the verifiers refuse every hand-built bad plan ─────────────────────


def _good():
    return ok("Intro\n- [ ] x\n    - c\nEnd\n", "N\n- [ ] a\n", "- [ ] x")


def _with(plan: tw.Plan, spans, new_text=None):
    new = new_text if new_text is not None else tw._compose(_old(plan), spans)
    return dataclasses.replace(plan, spans=tuple(spans), new_text=new)


_OLD_D = "N\n- [ ] a\n"
_OLD_S = "Intro\n- [ ] x\n    - c\nEnd\n"


def _old(plan):
    return _OLD_D if plan.removed is None else _OLD_S


def test_the_good_plans_verify():
    plan = _good()
    assert tw.verify_move_insert(_OLD_D, plan.insert.new_text, plan.insert, plan.moved) is None
    assert tw.verify_removal(_OLD_S, plan.removal.new_text, plan.removal, plan.moved) is None


@pytest.mark.parametrize("mutate", [
    # A pre-existing destination line changes (new text not the declared insert).
    lambda p: (p.spans, p.new_text.replace("- [ ] a", "- [ ] A")),
    # A pre-existing terminator outside the two end-of-file cases changes.
    lambda p: ([tw.Span(p.spans[0].start, p.spans[0].start, p.spans[0].text.replace("\n", "\r\n"))], None),
    # The inserted lines differ from the moved block.
    lambda p: ([tw.Span(p.spans[0].start, p.spans[0].start, "- [ ] y\n    - c\n")], None),
    # Inserted mid-line.
    lambda p: ([tw.Span(1, 1, p.spans[0].text)], None),
])
def test_a_failed_destination_verification_refuses(mutate):
    """Scenario: A failed verification writes nothing (destination)."""
    plan = _good()
    ins = dataclasses.replace(plan.insert, inserted_at=None)
    spans, new = mutate(ins)
    bad = _with(ins, spans, new)
    got = tw.verify_move_insert(_OLD_D, bad.new_text, bad, plan.moved)
    assert isinstance(got, tw.TaskWriteRefusal) and got.code == "task_malformed", got


def test_an_insertion_that_steals_a_context_line_is_refused():
    plan = ok("- [ ] x\n", "- [ ] a\n    - ctx\n", "- [ ] x")
    bad = dataclasses.replace(plan.insert, spans=(tw.Span(8, 8, "- [ ] x\n"),), inserted_at=None,
                              new_text="- [ ] a\n- [ ] x\n    - ctx\n")
    got = tw.verify_move_insert("- [ ] a\n    - ctx\n", bad.new_text, bad, plan.moved)
    assert got is not None and "owning task" in got.reason


def test_an_insertion_into_code_is_refused():
    plan = ok("- [ ] x\n", "```\n\n```\n", "- [ ] x")
    bad = dataclasses.replace(plan.insert, spans=(tw.Span(4, 4, "- [ ] x\n"),), inserted_at=None,
                              new_text="```\n- [ ] x\n\n```\n")
    got = tw.verify_move_insert("```\n\n```\n", bad.new_text, bad, plan.moved)
    assert got is not None and got.code == "task_malformed"


def test_an_eof_insertion_that_does_not_lead_with_the_terminator_is_refused():
    plan = ok("- [ ] x\n", "Notes", "- [ ] x")
    bad = dataclasses.replace(plan.insert, spans=(tw.Span(5, 5, "- [ ] x\n"),), new_text="Notes- [ ] x\n",
                              inserted_at=None)
    assert tw.verify_move_insert("Notes", bad.new_text, bad, plan.moved) is not None


@pytest.mark.parametrize("spans,removed", [
    # One line too many.
    ([tw.Span(6, 26, "")], (1, 3)),
    # A terminator outside the end-of-file case: take the preceding newline.
    ([tw.Span(5, 22, "")], (1, 3)),
    # Not the whole block (the context line left behind, re-parented).
    ([tw.Span(6, 14, "")], (1, 2)),
])
def test_a_failed_source_verification_refuses(spans, removed):
    """Scenario: A failed verification writes nothing (source)."""
    plan = _good()
    bad = dataclasses.replace(plan.removal, spans=tuple(spans), removed=removed,
                              new_text=tw._compose(_OLD_S, spans))
    got = tw.verify_removal(_OLD_S, bad.new_text, bad, plan.moved)
    assert isinstance(got, tw.TaskWriteRefusal) and got.code == "task_malformed", got


def test_size_limits_in_either_note(monkeypatch):
    """Scenario 24: over the per-note task cap in either note (the cap
    lowered to 3 for the test)."""
    import src.services.tasks as tasks_mod

    real = tasks_mod.tasks_from_note_text
    monkeypatch.setattr(tw.tasks_mod, "tasks_from_note_text", lambda raw: real(raw, max_tasks=3))
    tw._extract_cached.cache_clear()
    try:
        refused("- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d\n", "", "- [ ] a", "size_limit")
        r = refused("- [ ] t\n", "- [ ] a\n- [ ] b\n- [ ] c\n", "- [ ] t", "size_limit")
        assert r.note == "destination"
    finally:
        tw._extract_cached.cache_clear()


# ── D9: duplicates and resume ───────────────────────────────────────────────


def test_an_identical_block_is_a_duplicate_naming_both_ways_on():
    """Scenario: An independent identical task at the destination is not
    deleted (planner half)."""
    note = "- [ ] Call Alex\n    - lease\n"
    r = refused(note, note, "- [ ] Call Alex", "duplicate_task")
    assert "byte-identical" in r.reason and "resume=true" in r.reason and "allow_duplicate=true" in r.reason
    assert r.note == "destination" and r.matches[0].line == 1


def test_a_differing_block_with_the_key_is_a_duplicate_too():
    r = refused("- [ ] Call Alex\n    - lease\n", "- [ ] Call Alex\n    - other\n", "- [ ] Call Alex",
                "duplicate_task")
    assert "byte-identical" not in r.reason


def test_an_open_description_duplicate_is_refused_and_allowed():
    """Scenario: An open duplicate at the destination is refused (planner)."""
    refused("- [ ] Call the bank\n", "- [ ] call the  Bank\n", "- [ ] Call the bank", "duplicate_task")
    plan = ok("- [ ] Call the bank\n", "- [ ] call the  Bank\n", "- [ ] Call the bank", allow_duplicate=True)
    assert plan.insert.new_text == "- [ ] call the  Bank\n- [ ] Call the bank\n"


def test_a_done_task_is_checked_by_key_only():
    """Scenario: A done task into an archive is not refused for its description."""
    ok("- [x] Water plants ✅ 2026-11-01\n", "- [x] Water plants ✅ 2026-10-01\n",
       "- [x] Water plants ✅ 2026-11-01")


@pytest.mark.parametrize("copies,code", [(0, "resume_unmatched"), (1, None), (2, "resume_unmatched")])
def test_resume_needs_exactly_one_identical_copy(copies, code):
    """Scenario: Resume without exactly one copy is refused; Scenario: Resume
    completes a partial move (planner half)."""
    block = "- [ ] Call\n    - ctx\n"
    src = "Intro\n" + block
    dst = "N\n" + block * copies
    got = move(src, dst, "- [ ] Call", resume=True)
    if code is None:
        assert got.insert is None and got.dest_lines == (2, 2)
        assert got.removal.new_text == "Intro\n"
    else:
        assert got.code == code and got.note == "destination"


def test_resume_ignores_a_copy_with_a_different_block():
    got = move("- [ ] Call\n    - ctx\n", "- [ ] Call\n    - other\n", "- [ ] Call", resume=True)
    assert got.code == "resume_unmatched"


@pytest.mark.parametrize("kw", [dict(resume=True, allow_duplicate=True), dict(resume=True, to_section="S")])
def test_forbidden_combinations(kw):
    assert move("- [ ] t\n", "", "- [ ] t", **kw).code == "invalid_argument"
    assert tw.plan_move_within("- [ ] t\n", args("- [ ] t", resume=True), None).code == "invalid_argument"


def test_a_block_id_collision_is_refused_case_insensitively():
    """Scenario: A block id that already exists at the destination is refused."""
    r = refused("- [ ] t ^followup\n", "x\nAnother ^FollowUp\n", "- [ ] t ^followup", "block_id_conflict")
    assert "followup" in r.reason and "line 2" in r.reason and r.note == "destination"


def test_a_block_id_on_a_context_line_collides_too():
    refused("- [ ] t\n    - see ^ref1\n", "a line ^REF1\n", "- [ ] t", "block_id_conflict")


def test_a_same_note_move_runs_no_block_id_check():
    note = "# A\n- [ ] t ^x\n# B\n"
    plan = tw.plan_move_within(note, args("- [ ] t ^x", to_section="B"), None)
    assert plan.changed and plan.new_text == "# A\n# B\n- [ ] t ^x\n"


# ── D10 6(b) and rows 9–12 ──────────────────────────────────────────────────


def test_destination_still_holds():
    inserted = ("- [ ] x", "    - c")
    d = "N\n- [ ] x\n    - c\n"
    assert tw.destination_still_holds(d, 2, inserted)
    assert not tw.destination_still_holds("N\n", 2, inserted)  # an older save
    assert not tw.destination_still_holds("Top\n" + d, 2, inserted)  # a line inserted above
    assert not tw.destination_still_holds(d, 3, inserted)
    assert not tw.destination_still_holds("N\r\n- [ ] x\r\n    - c changed\r\n", 2, inserted)
    assert tw.destination_still_holds("N\r\n- [ ] x\r\n    - c", 2, inserted)


def test_plan_relocated_removal():
    src = "A\n- [ ] x\n    - c\nB\n"
    plan = ok(src, "", "- [ ] x")
    moved = plan.moved
    again = tw.plan_relocated_removal("Z\n" + src, moved)  # unchanged block
    assert isinstance(again, tw.Plan) and again.new_text == "Z\nA\nB\n"
    changed = tw.plan_relocated_removal(src.replace("- c", "- c\n    - d"), moved)
    assert changed.code == "task_move_incomplete" and changed.detail == ("source_changed", key_of("- [ ] x"))
    missing = tw.plan_relocated_removal(src.replace("- [ ] x", "- [x] x ✅ 2026-11-16"), moved)
    assert missing.detail == ("source_missing", None)
    ambiguous = tw.plan_relocated_removal(src + "- [ ] x\n", moved)
    assert ambiguous.detail == ("source_ambiguous", None)


# ── D13: the same note ──────────────────────────────────────────────────────


NOTE = "# Today\n- [ ] a\n    - ctx\n- [ ] b\n# Later\n- [ ] c\n"


def test_same_note_below():
    """Scenario: Moving a task to another section of its note (planner)."""
    plan = tw.plan_move_within(NOTE, args("- [ ] a", to_section="Later"), None)
    assert plan.new_text == "# Today\n- [ ] b\n# Later\n- [ ] c\n- [ ] a\n    - ctx\n"
    assert plan.moved_ids[0] == (5, key_of("- [ ] a"))
    assert tw._compose(NOTE, plan.spans) == plan.new_text


def test_same_note_above():
    plan = tw.plan_move_within(NOTE, args("- [ ] c", to_section="Today"), None)
    assert plan.new_text == "# Today\n- [ ] a\n    - ctx\n- [ ] b\n- [ ] c\n# Later\n"


def test_same_note_no_change():
    """Scenario: A task already in place (planner)."""
    plan = tw.plan_move_within(NOTE, args("- [ ] c", to_section="Later"), None)
    assert plan.changed is False and plan.spans == ()


def test_same_note_heading_in_block():
    got = tw.plan_move_within("- [ ] t\n  # Later\n# Later\n", args("- [ ] t", to_section="Later"), None)
    assert got.code == "task_malformed"


def test_same_note_eof_without_terminator():
    plan = tw.plan_move_within("# A\n# B\n- [ ] x", args("- [ ] x", to_section="A"), None)
    assert plan.new_text == "# A\n- [ ] x\n# B"


# ── D1: ids ─────────────────────────────────────────────────────────────────


def test_keys_are_unchanged_and_ordinals_renumber_on_both_sides():
    """Scenario: The id survives the move; Scenario: A remaining sibling loses
    its suffix (planner); scenario 18."""
    k = key_of("- [ ] Call")
    src = "- [ ] Call\n- [ ] Call\n"
    plan = ok(src, "- [ ] Call\n", f"{k}.1of2", allow_duplicate=True)
    assert plan.removal.new_text == "- [ ] Call\n"
    assert plan.removal.renumbered is True  # 2of2 → bare
    assert plan.insert.renumbered is True   # the destination's bare → 1of2
    assert plan.dest_ids == ((2, f"{k}.2of2"),)
    plain = ok("- [ ] Solo\n", "", "- [ ] Solo")
    assert plain.dest_ids == ((1, key_of("- [ ] Solo")),)
    assert plain.removal.renumbered is False and plain.insert.renumbered is False


# ── the corpus property test ────────────────────────────────────────────────

CORPUS = [
    "",
    "Notes",
    "Notes\n",
    "# Inbox\n- [ ] a\n    - ctx a\n- [ ] b\n\n- [x] c ✅ 2026-11-01\n",
    "---\ntitle: x\n---\n- [ ] fm task\n    - [ ] fm sub\n",
    "---\ntitle: y\n---",
    "Intro\r\n- [ ] crlf one\r\n\t- tabbed\r\n- [ ] crlf two\r\nEnd\r\n",
    "# A\n- [ ] dup\n# B\n- [ ] dup\n    - second\n# C\n",
    "- [ ] Project\n    - [ ] step one\n    - [ ] step two\n> - [ ] quoted\n",
    "1. [ ] ordered\n2. [ ] two\n\n   context under two\n\nPara\n- [ ] last",
    "- [ ] t ^blk\n    - see `code` here\n- [/] in progress\n- [-] cancelled\n- [>] forwarded\n",
    "# H\n\n\n- [ ] spaced\n\n\n# I\n- [ ] x\n    ```\n    code\n    ```\n",
]


def _split(text: str) -> list[str]:
    if text == "":
        return []
    parts = re.split(r"\r\n|\n|\r", text)
    if text.endswith(("\n", "\r")):
        parts.pop()
    return parts


def _ids(text: str) -> list[str]:
    got, truncated = tasks_from_note_text(text)
    assert not truncated
    return [task_line.render_id(t.task_key, t.key_ordinal, t.key_count) for t in got]


_ACCEPTABLE = {"task_malformed", "duplicate_task", "block_id_conflict", "selector_unresolved",
               "content_unsafe", "size_limit"}


@pytest.mark.parametrize("si", range(len(CORPUS)))
def test_two_note_moves_conserve_every_line(si):
    src = CORPUS[si]
    for tid in _ids(src):
        for dj, dst in enumerate(CORPUS):
            got = tw.plan_move(src, dst, tw.MoveArgs(id=tid, hash_supplied=True, allow_duplicate=True), None)
            if isinstance(got, tw.TaskWriteRefusal):
                assert got.code in _ACCEPTABLE, (si, dj, tid, got)
                continue
            before = collections.Counter(_split(src) + _split(dst))
            after = collections.Counter(_split(got.removal.new_text) + _split(got.insert.new_text))
            assert before == after, (si, dj, tid)
            # Every moved line arrived byte for byte at the declared position.
            k, n = got.dest_lines
            assert tw.destination_still_holds(got.insert.new_text, k, got.moved.lines)
            assert n == len(got.moved.lines)


@pytest.mark.parametrize("si", range(len(CORPUS)))
def test_same_note_moves_are_permutations(si):
    note = CORPUS[si]
    headings = [None] + [m.group(1) for m in re.finditer(r"(?m)^#{1,6} (.+?)\s*$", note)]
    for tid in _ids(note):
        for section in headings:
            got = tw.plan_move_within(
                note, tw.MoveArgs(id=tid, hash_supplied=True, to_section=section, allow_duplicate=True), None)
            if isinstance(got, tw.TaskWriteRefusal):
                assert got.code in _ACCEPTABLE, (si, tid, section, got)
                continue
            assert sorted(_split(got.new_text)) == sorted(_split(note)), (si, tid, section)
            assert tw._compose(note, got.spans) == got.new_text


def test_a_task_over_the_stored_text_cut_resolves_and_moves():
    """Scenario 23: a task text over 1,000 characters (stored cut): the key is
    over the uncut text, the move resolves, and the line arrives whole."""
    line = "- [ ] " + "long " * 300 + "end"
    plan = ok(line + "\n", "", line)
    assert plan.insert.new_text == line
    assert plan.moved.tasks[0].text_cut is True
    assert plan.dest_ids[0][1] == key_of(line)


@pytest.mark.parametrize("between", ["\n", "A paragraph.\n", "\nA paragraph.\n\n"])
def test_same_note_already_in_place_with_lines_before_it(between):
    """Scenario: A task already in place — the last top-level task of the
    section stays put (no change) even with a blank line or a paragraph
    between it and the previous task (spec verifier)."""
    note = f"# Later\n- [ ] b\n{between}- [ ] c\n    - ctx\n# Next\n"
    plan = tw.plan_move_within(note, args("- [ ] c", to_section="Later"), None)
    assert plan.changed is False and plan.spans == () and plan.new_text == note
    whole = tw.plan_move_within(f"- [ ] b\n{between}- [ ] c\n", args("- [ ] c"), None)
    assert whole.changed is False


def test_destination_still_holds_task_requires_the_task_not_just_the_bytes():
    """D10 6(b) full form (review r2): bytes in place but hidden by an
    unclosed fence, or a block that gained a line, do not hold."""
    plan = ok("- [ ] x\n    - c\n", "Notes\n", "- [ ] x")
    k = plan.dest_lines[0]
    assert tw.destination_still_holds_task(plan.insert.new_text, k, plan.moved)
    assert not tw.destination_still_holds_task("```\n- [ ] x\n    - c\n", k, plan.moved)
    assert not tw.destination_still_holds_task("Notes\n- [ ] x\n    - c\n    - extra\n", k, plan.moved)
    assert not tw.destination_still_holds_task("Top\nNotes\n- [ ] x\n    - c\n", k, plan.moved)
