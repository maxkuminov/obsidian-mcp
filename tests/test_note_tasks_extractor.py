"""The task extractor's grammar (#350, design D2/D3, `note-tasks` spec)."""
import datetime
import time

import pytest

from src.config import MAX_TASKS_PER_NOTE, TASK_TEXT_MAX_CHARS
from src.services import tasks as task_mod
from src.services.tasks import (
    TASKS_SINCE_EXTRACTION_VERSION,
    body_line_offset,
    extract_tasks_bounded,
)


def extract(body, offset=0, **kw):
    return extract_tasks_bounded(body, line_offset=offset, **kw)


def only(body, offset=0):
    tasks, truncated = extract(body, offset)
    assert not truncated
    assert len(tasks) == 1, tasks
    return tasks[0]


# ── recognition ─────────────────────────────────────────────────────────────


def test_open_and_done_checkboxes_are_tasks():
    body = "intro\n\n- [ ] write report\n* [x] send invoice\n"
    tasks, truncated = extract(body)
    assert not truncated
    assert [(t.line, t.status_char, t.text) for t in tasks] == [
        (3, " ", "write report"),
        (4, "x", "send invoice"),
    ]


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_a_checkbox_in_a_fenced_block_is_not_a_task(fence):
    body = f"{fence}\n- [ ] not a task\n{fence}\n- [ ] real\n"
    tasks, _ = extract(body)
    assert [t.text for t in tasks] == ["real"]
    assert tasks[0].line == 4


def test_a_checkbox_decided_by_inline_code_masking_is_not_a_task():
    assert extract("`- [ ]` example syntax\n")[0] == []
    # A marker character that masking changed is not a task either.
    assert extract("- [`] x`\n")[0] == []


def test_inline_code_after_the_marker_does_not_unmake_the_task():
    t = only("- [ ] run `make test` now")
    assert t.text == "run `make test` now"


def test_indented_ordered_and_callout_tasks_are_tasks():
    body = "    - [ ] subtask\n\t- [ ] tabbed\n1. [ ] numbered\n> - [ ] in a callout\n"
    tasks, _ = extract(body)
    assert [t.text for t in tasks] == ["subtask", "tabbed", "numbered", "in a callout"]
    assert extract("2) [ ] paren\n> > + [ ] nested quote\n")[0][1].text == "nested quote"


@pytest.mark.parametrize(
    "line",
    ["- [ ]no space", "[ ] no bullet", "- [xx] two chars", "- [\t] tab marker",
     "-[ ] no gap", "- [ ] nbsp marker", "- [[] bracket", "- []] bracket",
     "1234567890. [ ] ten digits"],
)
def test_shapes_that_are_not_checkboxes(line):
    assert extract(line + "\n")[0] == []


def test_a_checkbox_at_end_of_line_with_no_text():
    t = only("- [ ]")
    assert t.text == "" and t.state == "open"


def test_a_tasks_plugin_query_block_is_not_evaluated():
    body = "```tasks\nnot done\n- [ ] inside the query\n```\n\n[[Other]] and [[Tasks/List]]\n"
    assert extract(body)[0] == []


# ── line numbers ────────────────────────────────────────────────────────────


def test_line_numbers_count_the_frontmatter():
    # A 4-line block (delimiters included) and a task on the body's 2nd line.
    assert body_line_offset("---\na: 1\nb: 2\n---\n") == 4
    t = only("first\n- [ ] second", offset=4)
    assert t.line == 6


@pytest.mark.parametrize("nl", ["\r\n", "\r", "\n"])
def test_crlf_and_cr_line_endings(nl):
    body = nl.join(["# H", "x", "- [ ] a", "- [x] b", ""])
    tasks, _ = extract(body)
    assert [(t.line, t.text, t.heading_path) for t in tasks] == [
        (3, "a", "H"), (4, "b", "H"),
    ]
    assert body_line_offset(f"---{nl}a: 1{nl}---{nl}") == 3


# ── status mapping ──────────────────────────────────────────────────────────


def test_status_mapping():
    body = "- [ ] a\n- [x] b\n- [X] c\n- [-] d\n- [/] e\n- [>] f\n"
    tasks, _ = extract(body)
    assert [(t.status_char, t.state) for t in tasks] == [
        (" ", "open"), ("x", "done"), ("X", "done"), ("-", "cancelled"),
        ("/", "open"), (">", "open"),
    ]


# ── fields ──────────────────────────────────────────────────────────────────


def test_dates_priority_and_tags_are_parsed():
    t = only("- [ ] ship 📅 2026-11-01 ⏳ 2026-10-30 🛫 2026-10-28 ⏫ #work #alpha")
    assert t.due == datetime.date(2026, 11, 1)
    assert t.scheduled == datetime.date(2026, 10, 30)
    assert t.start == datetime.date(2026, 10, 28)
    assert t.priority == "high"
    assert t.tags == ["work", "alpha"]
    assert t.tags_cut is False
    assert t.text == "ship 📅 2026-11-01 ⏳ 2026-10-30 🛫 2026-10-28 ⏫ #work #alpha"


def test_done_date():
    t = only("- [x] shipped ✅ 2026-10-01")
    assert t.done_on == datetime.date(2026, 10, 1)


@pytest.mark.parametrize("bad", ["2026-02-30", "0000-01-01", "2026-13-01"])
def test_an_invalid_date_stores_null_and_keeps_the_text(bad):
    t = only(f"- [ ] pay 📅 {bad}")
    assert t.due is None
    assert t.text == f"pay 📅 {bad}"


def test_a_signifier_with_no_date_stores_null():
    assert only("- [ ] pay 📅 soon").due is None
    assert only("- [ ] pay 📅 2026-01-011").due is None  # followed by a digit


def test_a_signifier_inside_inline_code_is_not_a_field():
    t = only("- [ ] document the `📅 2026-01-01` syntax")
    assert t.due is None
    t = only("- [ ] the `#tag` and `🔺` syntax")
    assert t.tags is None and t.priority is None


def test_the_last_occurrence_wins():
    assert only("- [ ] move 📅 2026-01-01 then 📅 2026-02-01").due == datetime.date(2026, 2, 1)
    assert only("- [ ] p 🔺 then 🔽").priority == "low"
    # The last signifier decides, even when it carries no valid date.
    assert only("- [ ] d 📅 2026-01-01 📅 2026-02-30").due is None


def test_fe0f_and_variant_signifiers():
    assert only("- [ ] a 📅️ 2026-03-01").due == datetime.date(2026, 3, 1)
    assert only("- [ ] a 📆 2026-03-02").due == datetime.date(2026, 3, 2)
    assert only("- [ ] a 🗓️ 2026-03-03").due == datetime.date(2026, 3, 3)
    assert only("- [ ] a 🗓2026-03-04").due == datetime.date(2026, 3, 4)
    assert only("- [ ] a ⌛ 2026-03-05").scheduled == datetime.date(2026, 3, 5)
    assert only("- [ ] a ⏳️2026-03-06").scheduled == datetime.date(2026, 3, 6)


@pytest.mark.parametrize(
    "emoji,name",
    [("🔺", "highest"), ("⏫", "high"), ("🔼", "medium"), ("🔽", "low"), ("⏬", "lowest")],
)
def test_priorities(emoji, name):
    assert only(f"- [ ] p {emoji}️").priority == name
    assert only(f"- [ ] p {emoji}").priority == name


def test_no_priority_is_null():
    assert only("- [ ] plain").priority is None


# ── tags ────────────────────────────────────────────────────────────────────


def test_the_tag_cap_keeps_the_first_64_distinct_and_says_so():
    tags = " ".join(f"#t{i:02d}" for i in range(1, 66))
    t = only(f"- [ ] many {tags} #t01")
    assert t.tags == [f"t{i:02d}" for i in range(1, 65)]
    assert t.tags_cut is True
    assert "t65" not in t.tags


def test_an_over_long_tag_is_not_stored_and_is_flagged():
    t = only("- [ ] x #ok #" + "a" * 129)
    assert t.tags == ["ok"]
    assert t.tags_cut is True
    t = only("- [ ] x #" + "a" * 128)
    assert t.tags == ["a" * 128] and t.tags_cut is False


def test_dedup_keeps_the_first_occurrence():
    t = only("- [ ] x #b #a #b #c #a")
    assert t.tags == ["b", "a", "c"]
    assert t.tags_cut is False


# ── heading path, text, cap ─────────────────────────────────────────────────


def test_heading_path():
    body = "# Sprint\n## Backend\n### API\n## Frontend\n- [ ] task\n"
    assert only(body).heading_path == "Sprint > Frontend"


def test_no_heading_is_null_and_headings_in_code_are_ignored():
    assert only("- [ ] top").heading_path is None
    body = "```\n# not a heading\n```\n- [ ] t\n"
    assert only(body).heading_path is None
    assert only("  # indented is not a heading\n- [ ] t").heading_path is None


def test_heading_path_caps():
    body = "# " + "A" * 300 + "\n## " + "B" * 300 + "\n### " + "C" * 300 + "\n- [ ] t\n"
    path = only(body).heading_path
    assert path.startswith("A" * 200 + " > " + "B" * 200 + " > ")
    assert len(path) == 512


def test_text_cut():
    t = only("- [ ] " + "é" * (TASK_TEXT_MAX_CHARS + 5) + "   ")
    assert t.text == "é" * TASK_TEXT_MAX_CHARS
    assert t.text_cut is True
    t = only("- [ ] " + "é" * TASK_TEXT_MAX_CHARS + "   ")
    assert t.text_cut is False and len(t.text) == TASK_TEXT_MAX_CHARS


def test_the_per_note_cap_returns_truncated():
    body = "\n".join("- [ ] t%d" % i for i in range(MAX_TASKS_PER_NOTE + 1))
    tasks, truncated = extract(body)
    assert truncated is True
    assert len(tasks) == MAX_TASKS_PER_NOTE
    assert tasks[-1].text == "t%d" % (MAX_TASKS_PER_NOTE - 1)
    tasks, truncated = extract("\n".join("- [ ] t" for _ in range(MAX_TASKS_PER_NOTE)))
    assert truncated is False and len(tasks) == MAX_TASKS_PER_NOTE


def test_a_nul_is_refused_not_repaired():
    with pytest.raises(ValueError):
        extract("- [ ] a\x00b")


def test_the_backfill_constant():
    assert TASKS_SINCE_EXTRACTION_VERSION == 4


def test_the_shared_rules_are_imported_not_copied():
    from src.services import vault

    assert task_mod._ATX_HEADING_RE is vault._ATX_HEADING_RE
    assert task_mod.INLINE_TAG_RE is vault.INLINE_TAG_RE


# ── linear time ─────────────────────────────────────────────────────────────

TEN_MIB = 10 * 1024 * 1024
TIME_BOUND_SECONDS = 30.0


@pytest.mark.parametrize(
    "fragment",
    ["> > - [", "- [ ] ", "- [ ] x 📅 " + "1" * 64 + " ", "- [ ] \n", "> > - [\n",
     "1" * 9 + ". [", "#a" * 50 + "\n"],
)
def test_the_extractor_is_linear_on_adversarial_input(fragment):
    body = (fragment * (TEN_MIB // len(fragment) + 1))[:TEN_MIB]
    started = time.monotonic()
    tasks, truncated = extract(body)
    elapsed = time.monotonic() - started
    assert elapsed < TIME_BOUND_SECONDS, f"{elapsed:.1f}s on {fragment!r}"
    assert len(tasks) <= MAX_TASKS_PER_NOTE


# ══════════════════════════════════════════════════════════════════════════
# Version 5 (#428, design D1, D9, D11; `note-tasks` delta)
# ══════════════════════════════════════════════════════════════════════════

import ast  # noqa: E402
import inspect  # noqa: E402

from src.services import indexer  # noqa: E402
from src.services import task_index  # noqa: E402
from src.services import task_line  # noqa: E402
from src.services.tasks import (  # noqa: E402
    TASK_IDS_SINCE_EXTRACTION_VERSION,
    note_task_row,
    tasks_from_note_text,
)


def test_created_cancelled_and_recurrence_are_parsed():
    t = only("- [-] Rent 🔁 every month ➕ 2026-01-01 📅 2026-11-01 ❌ 2026-11-02")
    assert t.recurrence == "every month" and t.recurrence_cut is False
    assert t.created == datetime.date(2026, 1, 1)
    assert t.cancelled == datetime.date(2026, 11, 2)
    assert t.due == datetime.date(2026, 11, 1)


def test_a_rule_outside_the_trailing_run_is_not_a_recurrence():
    assert only("- [ ] the 🔁 every day plan is late [[Notes]]").recurrence is None


def test_the_recurrence_is_the_runs_rule_trimmed_and_cut():
    assert only("- [ ] a 🔁 every week when done 📅 2026-01-01").recurrence == (
        "every week when done"
    )
    long = only("- [ ] a 🔁 " + "every " * 60 + "day")
    assert long.recurrence_cut is True and len(long.recurrence) == 256
    assert only("- [ ] a 🔁   ").recurrence is None  # no value is no rule
    # Created and cancelled follow the #350 date rules: invalid -> NULL.
    t = only("- [ ] a ➕ 2026-02-30 ❌")
    assert t.created is None and t.cancelled is None


def test_tag_prefixes_support_hierarchical_matching():
    t = only("- [ ] chase #Waiting-For/Chase")
    assert t.tags == ["Waiting-For/Chase"]
    assert t.tag_prefixes == ["waiting-for", "waiting-for/chase"]


def test_empty_segments_are_normalised():
    t = only("- [ ] chase #Waiting-For//Chase/")
    assert t.tag_prefixes == ["waiting-for", "waiting-for/chase"]


def test_no_tags_store_an_empty_prefix_array_not_null():
    assert only("- [ ] plain").tag_prefixes == []


def test_tag_prefixes_are_lossless_at_the_largest_buildable_case():
    tags = [f"t{i:02d}" + "/b" * 62 for i in range(64)]
    assert all(len(tag) == 127 for tag in tags)
    t = only("- [ ] x " + " ".join("#" + tag for tag in tags))
    assert t.tags_cut is False and t.tags == tags
    assert len(t.tag_prefixes) == 64 * 63 == 4_032
    assert set(t.tag_prefixes) == set(task_line.tag_prefixes(tags))


def test_the_4096_prefix_bound_is_reached():
    firsts = [chr(97 + i // 26) + chr(97 + i % 26) for i in range(64)]
    tags = [first + "/b" * 63 for first in firsts]
    assert all(len(tag) == 128 for tag in tags)
    t = only("- [ ] x " + " ".join("#" + tag for tag in tags))
    assert t.tags_cut is False
    assert len(t.tag_prefixes) == 4_096 == len(set(t.tag_prefixes))


def test_context_lines_are_captured():
    tasks, _ = extract("- [ ] Call bank\n    - ref 1234\n    more notes\n\n- [ ] next\n")
    assert tasks[0].context == "- ref 1234\nmore notes"
    assert tasks[0].context_cut is False
    assert tasks[1].context is None and tasks[1].context_cut is False


@pytest.mark.parametrize("stop", ["", "    # heading", "    ```", "    ~~~", "- sibling",
                                  "not indented"])
def test_context_stops_at_a_blank_heading_fence_or_non_belonging_line(stop):
    t = extract(f"- [ ] t\n    one\n{stop}\n    after\n")[0][0]
    assert t.context == "one" and t.context_cut is False


def test_context_tab_counts_as_four_and_blockquotes_nest():
    t = extract("\t- [ ] t\n\t\tdeeper\n    \tdeeper too\n\tsame\n")[0][0]
    assert t.context == "deeper\ndeeper too"
    q = extract("> - [ ] quoted\n>   more\n>> deeper quote\n> - [ ] next\n")[0][0]
    assert q.context == ">   more\n>> deeper quote"


def test_context_includes_a_child_task_and_the_child_has_its_own():
    tasks, _ = extract("- [ ] parent\n    - [ ] child\n        detail\n")
    assert tasks[0].context == "- [ ] child\ndetail"
    assert tasks[1].context == "detail"


def test_context_caps_lines_chars_and_total():
    eight = "".join(f"    line {i}\n" for i in range(8))
    assert extract("- [ ] t\n" + eight)[0][0].context_cut is False
    nine = extract("- [ ] t\n" + eight + "    line 8\n")[0][0]
    assert nine.context.count("\n") == 7 and nine.context_cut is True
    wide = extract("- [ ] t\n    " + "w" * 200 + "\n")[0][0]
    assert wide.context == "w" * 160 and wide.context_cut is True
    total = extract("- [ ] t\n" + "".join("    " + "z" * 150 + "\n" for _ in range(8)))[0][0]
    assert len(total.context) <= 600 and total.context_cut is True
    assert not total.context.endswith("\n")


def test_stored_context_crosses_no_blank_line():
    # The uncapped continuation block (D8, write placement) crosses blank
    # lines; the stored context does not.
    assert extract("- [ ] t\n    a\n\n    b\n")[0][0].context == "a"


def test_the_task_key_is_over_the_uncut_text():
    text_ = "k" * 1_500
    t = only("- [x] " + text_ + "   ")
    assert t.text_cut is True and len(t.text) == TASK_TEXT_MAX_CHARS
    assert t.task_key == task_line.task_key(text_)
    # Status, indentation, marker and quote prefix are excluded.
    assert only("> \t1. [ ] " + text_).task_key == t.task_key


def test_ordinals_count_identical_tasks_in_document_order():
    tasks, _ = extract("- [ ] same\n- [ ] other\n* [x] same\n    - [ ] same\n")
    same = [t for t in tasks if t.text == "same"]
    assert [(t.key_ordinal, t.key_count) for t in same] == [(1, 3), (2, 3), (3, 3)]
    assert {t.task_key for t in same} == {task_line.task_key("same")}
    other = next(t for t in tasks if t.text == "other")
    assert (other.key_ordinal, other.key_count) == (1, 1)
    assert task_line.render_id(other.task_key, 1, 1) == other.task_key


def test_ordinals_are_counted_within_the_cap():
    tasks, truncated = extract("- [ ] same\n" * 5, max_tasks=3)
    assert truncated is True
    assert [(t.key_ordinal, t.key_count) for t in tasks] == [(1, 3), (2, 3), (3, 3)]


def test_text_fold_is_nfc_casefold_of_the_stored_text():
    t = only("- [ ] Позвонить в БАНК Straße ΟΔΟΣ")
    assert t.text_fold == task_line.fold_text(t.text)
    assert "банк" in t.text_fold and "strasse" in t.text_fold and "οδοσ" in t.text_fold


CORPUS = [
    "- [ ] plain",
    "- [ ] Pay rent 🔁 every month 📅 2026-11-01 #home ^abc-1",
    "- [x] Done 🔁 every week when done ✅ 2026-11-10 ⏫",
    "- [ ] the 🔁 every day plan is late [[Notes]]",
    "- [ ] Mixed CASE #Tag/Sub   #two 📅 2026-01-01  ",
    "- [ ] code `🔁 every day` 🔁 every 2 days",
    "> - [/] quoted 🔁 every weekday 🏁 keep 🆔 abc ⛔ x,y",
    "- [ ] 🔁 every year #a #b ⏬",
    "\t- [-] cancelled ❌ 2026-01-02 🔁 every month on the 31st",
    "- [ ] Ünïcödé  SPACING\tand  tabs #x",
]


@pytest.mark.parametrize("line", CORPUS)
def test_one_run_parse_matches_the_write_planners_definitions(line):
    """`_run_derived` spells two `task_line` derivations over one parse; pin
    them to `task_line.parse(...).recurrence` and `description_key(
    normalise_description(...))`, which the write planner uses."""
    t = only(line)
    tl = task_line.parse(line)
    assert t.recurrence == (tl.recurrence or None)
    assert t.description_key == task_line.description_key(
        task_line.normalise_description(tl.full_text)
    )


def test_tasks_from_note_text_is_split_offset_and_extraction():
    raw = "---\ntitle: x\ntags: [a]\n---\nintro\n- [ ] first 📅 2026-01-01\n- [x] second\n"
    tasks, truncated = tasks_from_note_text(raw)
    body = "intro\n- [ ] first 📅 2026-01-01\n- [x] second\n"
    assert (tasks, truncated) == extract(body, offset=4)
    assert [t.line for t in tasks] == [6, 7]
    # Bytes and text agree; CRLF, a lone CR and a NUL are canonicalised
    # exactly as the index hashes them.
    crlf = raw.replace("\n", "\r\n").replace("first", "fi\x00rst")
    assert tasks_from_note_text(crlf.encode("utf-8")) == (tasks, False)
    assert tasks_from_note_text(crlf) == (tasks, False)
    with pytest.raises(UnicodeDecodeError):
        tasks_from_note_text(b"- [ ] \xff\n")


def test_a_frontmatter_lookalike_is_not_a_task():
    tasks, _ = tasks_from_note_text("---\nnote: |\n  - [ ] not a task\n---\n- [ ] real\n")
    assert [(t.line, t.text) for t in tasks] == [(5, "real")]


def test_note_task_row_carries_every_column_of_the_model():
    from src.models.db import NoteTask

    row = note_task_row(7, only("- [ ] a #t 🔁 every day ➕ 2026-01-01"))
    assert set(row) == {c.name for c in NoteTask.__table__.columns} - {"id"}
    assert row["note_id"] == 7


def test_version_five_is_the_task_id_bump_and_re_embeds_nothing():
    from src.services.embeddings import clean_at_version

    assert indexer.CURRENT_EXTRACTION_VERSION == 5 == TASK_IDS_SINCE_EXTRACTION_VERSION
    for body in ["- [ ] t 🔁 every day\n```\nx\n```\n", "[[L]] `c` #t\n~~~\ny\n~~~\n", ""]:
        assert clean_at_version(5, body) == clean_at_version(4, body)
        assert indexer._grammar_changed_the_embedding_text(4, body) is False
        assert indexer._grammar_changed_the_embedding_text(3, body) is False


def test_a_snapshot_row_is_task_dirty_unless_its_tasks_describe_its_hash():
    from types import SimpleNamespace

    base = dict(content_hash="h", extraction_version=5, stat_size=None,
                stat_mtime_ns=None, stat_ctime_ns=None, stat_ino=None,
                derived_under=None)
    of = indexer.SnapshotRow.of
    assert of(SimpleNamespace(**base, tasks_source_hash="h")).task_dirty is False
    assert of(SimpleNamespace(**base, tasks_source_hash="other")).task_dirty is True
    assert of(SimpleNamespace(**base, tasks_source_hash=None)).task_dirty is True
    assert of(SimpleNamespace(**base)).task_dirty is False  # no such column
    assert of(SimpleNamespace(**base, tasks_source_hash="x")) != of(
        SimpleNamespace(**base, tasks_source_hash="h")
    ), "part of the C4 comparison"


def test_a_task_dirty_row_needs_its_body():
    v = indexer.CURRENT_EXTRACTION_VERSION
    dirty = indexer.SnapshotRow("h", v, None, None, True)
    assert indexer._needs_body("a.md", "h", {"a.md": dirty}, False) is True
    clean = indexer.SnapshotRow("h", v, None, None, False)
    assert indexer._needs_body("a.md", "h", {"a.md": clean}, False) is False


# ── the write-time refresh's structural guards (D10) ────────────────────────


def _module_tree(module):
    return ast.parse(inspect.getsource(module))


def test_the_refresh_never_takes_the_generation_lock():
    tree = _module_tree(task_index)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not {n for n in names if n.startswith("acquire_generation_lock")}
    assert "advisory" not in inspect.getsource(task_index)
    imported = {
        alias.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
        for alias in n.names
    }
    assert not {n for n in imported if "generation" in n or n == "indexer"}


def test_the_refresh_writes_only_the_r1_columns():
    tree = _module_tree(task_index)
    touched = {
        n.attr for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and getattr(n.value, "id", None) == "NoteMetadata"
    }
    assert touched <= {"id", "file_path", "user_id"}, touched
    stamp = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "_stamp")
    keys = {k.value for n in ast.walk(stamp) if isinstance(n, ast.Dict)
            for k in n.keys if isinstance(k, ast.Constant)}
    keys |= {t.slice.value for n in ast.walk(stamp) if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Subscript)}
    assert keys == {"tasks_source_hash", "tasks_refreshed_at", "tasks_failed",
                    "tasks_truncated"}


def test_refresh_result_renders_the_d16_and_d17_forms():
    ok = task_index.RefreshResult(task_index.REFRESHED)
    assert (ok.refreshed, ok.usage, ok.line) == (True, "refreshed", "index: refreshed")
    lag = task_index.RefreshResult(task_index.LAGGED, task_index.LAG_SUPERSEDED)
    assert lag.usage == "lagged:superseded"
    assert lag.line == "index: may lag (superseded)"


# ── #432: the generic writers' refresh gate (`may_hold_tasks`, design D2) ──


import random  # noqa: E402

from src.services.tasks import may_hold_tasks, tasks_from_note_text  # noqa: E402

#: Task-shaped fragments. A generated line is a prefix, a marker, a gap, a
#: bracketed status and a tail, each drawn from these, so most generated notes
#: hold tasks — and the near misses (masked code, a missing gap, a code span
#: right after `]`) are exactly where a gate could fall short.
_GEN_PREFIX = ["", "", " ", "  ", "    ", "\t", "> ", ">", "> > ", ">\t", "> [!note] ",
               " ", "　", "`", "``", " > "]
_GEN_MARKER = ["-", "*", "+", "1.", "7)", "123456789.", "0)", "1234567890.", "a.", "--"]
_GEN_GAP = [" ", "  ", "\t", "", " ", " \t"]
_GEN_STATUS = ["[ ]", "[x]", "[X]", "[/]", "[-]", "[>]", "[é]", "[\U0001F600]",
               "[ ]", "[]", "[  ]", "[[]", "[x", "[\x00x]", "[x\x00]"]
_GEN_TAIL = ["", " task", "\ttask", " `code` x", "`code` x", " \U0001F4C5 2026-01-01",
             " #tag", "x", " [[link]]", "]", " %%c%%"]
_GEN_BLOCK = ["```\n", "~~~\n", "    code\n", "---\n", "title: x\n", "# Head\n", "\n",
              "text `inline\n", "`\n", "<!-- c -->\n", "%%\n"]
_GEN_EOL = ["\n", "\n", "\r\n", "\r", ""]


def _generated_notes(count: int, seed: int = 432):
    rng = random.Random(seed)
    for _ in range(count):
        parts = []
        if rng.random() < 0.2:
            parts.append("---\n" + rng.choice(["a: 1\n", "tags: [x]\n", "- [ ] y\n"]) + "---\n")
        for _ in range(rng.randint(1, 12)):
            roll = rng.random()
            if roll < 0.1:
                # Real nesting: a parent item, then indented child tasks.
                parent = rng.choice(["- ", "* ", "1. ", "- [ ] "]) + "parent\n"
                kids = "".join(
                    rng.choice(["  ", "    ", "\t", "      "]) + rng.choice(_GEN_MARKER)
                    + " " + rng.choice(_GEN_STATUS) + rng.choice(_GEN_TAIL) + "\n"
                    for _ in range(rng.randint(1, 4))
                )
                parts.append(parent + kids)
            elif roll < 0.2:
                # A multi-line callout: header, then quoted task lines.
                head = rng.choice(["> [!note]", "> [!todo]- Folded", ">[!tip] Title"]) + "\n"
                body = "".join(
                    rng.choice(["> ", ">", "> > ", ">  "]) + rng.choice(_GEN_MARKER) + " "
                    + rng.choice(_GEN_STATUS) + rng.choice(_GEN_TAIL) + "\n"
                    for _ in range(rng.randint(1, 4))
                )
                parts.append(head + body)
            elif roll < 0.4:
                parts.append(rng.choice(_GEN_BLOCK))
            else:
                parts.append(
                    rng.choice(_GEN_PREFIX) + rng.choice(_GEN_MARKER) + rng.choice(_GEN_GAP)
                    + rng.choice(_GEN_STATUS) + rng.choice(_GEN_TAIL) + rng.choice(_GEN_EOL)
                )
        text = "".join(parts)
        if rng.random() < 0.05:
            text = "﻿" + text
        yield text.encode("utf-8")


def test_the_gate_is_a_superset_of_the_extractor():
    """Deterministic property test: wherever the extractor finds a task, the
    gate says so. The generator must produce thousands of task-holding notes,
    or the property is vacuous."""
    with_tasks = 0
    misses = []
    for data in _generated_notes(12_000):
        found, _ = tasks_from_note_text(data)
        if found:
            with_tasks += 1
            if not may_hold_tasks(data):
                misses.append(data)
    assert with_tasks >= 3_000, with_tasks
    assert misses == [], misses[:3]


def test_the_gate_matches_a_code_span_directly_after_the_bracket():
    # The extractor tests its lookahead on MASKED text, where the code span
    # reads as spaces; a gate that kept the lookahead would miss this task.
    data = b"- [ ]`code` rest\n"
    assert tasks_from_note_text(data)[0]
    assert may_hold_tasks(data)


def test_the_gate_cases():
    assert may_hold_tasks(b"- [ ] a")
    assert may_hold_tasks(b"x\r\n> 1. [x] done\r\n")
    assert may_hold_tasks(b"- [\x00x] a")  # NUL removed -> `[x]`
    assert may_hold_tasks(b"\xff\xfe")  # not UTF-8: the refresh decides
    assert not may_hold_tasks(b"")
    assert not may_hold_tasks(b"# Title\n\nPlain text with [a] link [1].\n- item\n")
    assert not may_hold_tasks(b"-[ ] no gap\n")


def test_the_gate_is_built_from_the_extractor_pattern():
    assert task_mod._CHECKBOX_RE.pattern == (
        task_mod._CHECKBOX_PREFIX + task_mod._CHECKBOX_LOOKAHEAD
    )
    assert task_mod._CHECKBOX_GATE_RE.pattern == "^" + task_mod._CHECKBOX_PREFIX


@pytest.mark.parametrize("unit", ["- ", "> ", "7", " ", "-\t", "1)"])
def test_the_gate_is_linear(unit):
    data = (unit * (10 * 1024 * 1024 // len(unit))).encode()
    started = time.monotonic()
    may_hold_tasks(data)
    assert time.monotonic() - started < 2.0


def test_a_refresh_writes_its_task_rows_in_one_transaction_with_one_commit():
    """#432, review r2: a cancellation during or after the commit can leave
    the new rows committed, but never a partial set, because every task-row
    write of a refresh (both forms) runs in one session with one commit."""
    tree = _module_tree(task_index)
    commits = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "commit"
    ]
    assert len(commits) == 1
    run_locked = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_locked")
    assert commits[0] in list(ast.walk(run_locked))
    # Sessions are opened only there, so the writes in `_refresh_locked` and
    # `_stamp` (handed that one session) cannot land in another transaction.
    opened = [n for n in ast.walk(tree)
              if isinstance(n, ast.Attribute) and n.attr == "async_session"]
    assert len(opened) == 1 and opened[0] in list(ast.walk(run_locked))


def test_the_generic_refresh_helper_never_takes_the_generation_lock():
    from src.mcp_server import tools

    for fn in (tools._refresh_after_write, tools._gate_may_hold_tasks,
               tools._refresh_task_rows):
        source = inspect.getsource(fn)
        assert "generation" not in source and "advisory" not in source, fn.__name__
