"""The task rule: predicates, validators, fallback, write statement (#434).

Pure: no database. The SQL side (every rule × every consumer, the D4
identity, backfill rows, tenancy, cursors, SQL ≡ Python) is
`tests/integration/test_task_rule_pg.py`.
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.models.db import TaskSettings
from src.services import task_rule as TR
from src.services import task_settings
from src.services import task_write as tw
from src.services.task_settings import EffectiveTaskSettings
from src.services.tasks import tasks_from_note_text

ROOT = Path(__file__).resolve().parents[1]
SECRET = "task-rule-test-secret-only-0123456789abcdef"


def eff(rule="all", tags=(), filt=None, **kw) -> EffectiveTaskSettings:
    return dataclasses.replace(task_settings.defaults(), task_rule=rule, task_tags=tuple(tags),
                               global_filter=filt, **kw)


def extracted(line: str):
    tasks, _ = tasks_from_note_text(line + "\n")
    assert len(tasks) == 1, line
    return tasks[0]


def is_task(line: str, e) -> bool:
    return TR.extracted_is_task(extracted(line), e)


# ── fields ───────────────────────────────────────────────────────────────────

FIELDS = eff("fields")


@pytest.mark.parametrize("line", [
    "- [ ] a 📅 2026-12-01", "- [ ] a ⏳ 2026-12-01", "- [ ] a 🛫 2026-12-01",
    "- [ ] a ➕ 2026-11-01", "- [x] a ✅ 2026-11-01", "- [-] a ❌ 2026-11-01",
    "- [ ] a 🔼", "- [ ] a 🔺", "- [ ] a ⏬", "- [ ] a 🔁 every week",
    # A field anywhere on the line counts (#428 L2, L2 here).
    "- [ ] mentions 🔼 mid-sentence and more",
])
def test_every_field_family_makes_a_task(line):
    assert is_task(line, FIELDS)


@pytest.mark.parametrize("line", [
    "- [ ] Step one", "- [ ] a 📅 2026-02-30",  # invalid date: stored NULL (L1)
    "- [ ] a 🆔 abc123", "- [ ] a ⛔ abc123", "- [x] done without a date",
    "- [-] cancelled without a date",
])
def test_no_field_no_task(line):
    assert not is_task(line, FIELDS)


def test_task_tags_are_hierarchical_and_case_insensitive():
    e = eff("fields", ["waiting-for"])
    assert is_task("- [ ] Chase #waiting-for", e)
    assert is_task("- [ ] Chase #Waiting-For/bob", e)
    assert not is_task("- [ ] Chase #waiting-forever", e)
    assert not is_task("- [ ] Chase #other", e)
    # No task tags: fields only.
    assert not is_task("- [ ] Chase #waiting-for", FIELDS)


def test_a_cut_tag_list_is_kept_only_when_task_tags_exist():
    line = "- [ ] many " + " ".join(f"#t{i:02d}" for i in range(65))
    task = extracted(line)
    assert task.tags_cut is True
    assert TR.extracted_is_task(task, eff("fields", ["zzz"]))
    assert not TR.extracted_is_task(task, FIELDS)


# ── filter ───────────────────────────────────────────────────────────────────


def test_the_filter_is_a_case_sensitive_substring():
    e = eff("filter", filt="#task")
    assert is_task("- [ ] #task Book flights", e)
    assert is_task("- [ ] Book hotel #task", e)
    assert not is_task("- [ ] #Task Rent car", e)
    assert not is_task("- [ ] Pack bag", e)
    assert is_task("- [ ] code `#task` counts", e)  # inline code counts (L3)
    # A filter text found only inside a field value counts (the whole body).
    assert is_task("- [ ] Call 🔁 every week", eff("filter", filt="week"))


def test_a_cut_text_is_kept_when_the_filter_may_be_past_the_cut():
    e = eff("filter", filt="#task")
    after = "- [ ] " + "x" * 1100 + " #task"
    before = "- [ ] #task " + "x" * 1100
    assert extracted(after).text_cut and is_task(after, e)
    assert extracted(before).text_cut and is_task(before, e)


def test_all_is_every_checkbox_and_builds_no_predicate():
    assert is_task("- [ ] Step one", eff())
    assert TR.rule_clause(eff()) is None
    assert TR.unsure_clause(eff()) is None
    assert TR.unsure_clause(FIELDS) is None  # fields only: nothing can be unsure
    assert TR.rule_clause(FIELDS) is not None
    assert TR.digest_part(eff()) is None
    assert not TR.line_present(eff()) and TR.line_present(FIELDS)
    assert TR.pending_approximate(FIELDS) and not TR.pending_approximate(eff("filter", filt="x"))


# ── validators ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value,fragment", [
    ("#task 📅", "Tasks field emoji"),
    ("a`b", "backtick"),
    ("x" * 65, "longer than 64"),
    ("a b", "single line"),
    ("a\nb", "single line"),
    ("a\u0085b", "single line"),
    ("%%", "%%"),
    ("%%x%%", "%%"),  # balanced is refused too
    ("<!--", "HTML comment"),
    ("<!-- x -->", "HTML comment"),  # a complete comment is refused too
    ("-->", "HTML comment"),
    ("~~~", "tildes"),
    ("- todo", "list marker"),
    ("[t]", "checkbox"),
    ("# task", "heading marker"),
    ("#", "heading marker"),
    ("> task", "'>'"),
    ("1. task", "list marker"),
    ("　", "whitespace"),
    ("a️b", "Tasks field emoji"),
])
def test_unsafe_filter_text_is_refused(value, fragment):
    got, error = TR.normalise_filter(value)
    assert got is None and error and fragment in error, (value, error)


@pytest.mark.parametrize("value", ["#task", "TODO", "[task]", " #task ", "a%b", "x" * 64])
def test_real_filters_are_accepted(value):
    got, error = TR.normalise_filter(value)
    assert error is None and got == value.strip(" \t")


def test_a_blank_filter_is_none():
    assert TR.normalise_filter("  \t") == (None, None)
    assert TR.normalise_filter(None) == (None, None)


def test_task_tags_are_normalised_and_bounded():
    tags, errors = TR.normalise_task_tags(["#Someday", "waiting-for", "someday"])
    assert tags == ["someday", "waiting-for"] and not errors
    _, errors = TR.normalise_task_tags(["waiting-for/"])
    assert errors and "trailing '/'" in errors[0]
    _, errors = TR.normalise_task_tags([f"t{i}" for i in range(33)])
    assert errors and "At most 32" in errors[0]
    _, errors = TR.normalise_task_tags(["a" * 129])
    assert errors and "longer than 128" in errors[0]


def test_the_structure_check_is_one_definition():
    """`task_write._structure_refusal` delegates to `task_rule.structure_problem`."""
    for text, ctx in (("%%", False), ("<!--", False), ("```", False), ("- x", True)):
        problem = TR.structure_problem(text, context=ctx)
        refusal = tw._structure_refusal(text, context=ctx, what="text")
        assert problem and refusal and problem in refusal.reason
    src = (ROOT / "src/services/task_write.py").read_text()
    assert "_FENCE_RUN_RE" not in src and "_CONTEXT_STRUCTURE_RE" not in src


# ── Settings: the environment default ────────────────────────────────────────


def make_settings(**kw):
    return Settings(_env_file=None, secret_key=SECRET, **kw)


def test_environment_defaults():
    s = make_settings()
    assert (s.tasks_rule, s.tasks_rule_tags, s.tasks_global_filter) == ("all", [], "")


def test_environment_values_are_normalised(tmp_path):
    env = tmp_path / "rule.env"
    env.write_text(f"SECRET_KEY={SECRET}\nTASKS_RULE= Fields \n"
                   "TASKS_RULE_TAGS=#Someday, waiting-for,,someday\n")
    s = Settings(_env_file=env)
    assert s.tasks_rule == "fields" and s.tasks_rule_tags == ["someday", "waiting-for"]


@pytest.mark.parametrize("env,names", [
    ("TASKS_RULE=any", ["TASKS_RULE"]),
    ("TASKS_RULE_TAGS=waiting-for/", ["TASKS_RULE_TAGS"]),
    ("TASKS_RULE_TAGS=" + ",".join(f"t{i}" for i in range(33)), ["TASKS_RULE_TAGS"]),
    ("TASKS_GLOBAL_FILTER=%%", ["TASKS_GLOBAL_FILTER"]),
    ("TASKS_GLOBAL_FILTER=<!--", ["TASKS_GLOBAL_FILTER"]),
    ("TASKS_GLOBAL_FILTER=%%x%%", ["TASKS_GLOBAL_FILTER"]),
    ("TASKS_RULE=filter", ["TASKS_RULE", "TASKS_GLOBAL_FILTER"]),
])
def test_an_invalid_environment_rule_refuses_startup_naming_the_variable(tmp_path, env, names):
    path = tmp_path / "rule.env"
    path.write_text(f"SECRET_KEY={SECRET}\n{env}\n")
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=path)
    for name in names:
        assert name in str(err.value)


def test_a_filter_rule_with_its_text_starts(tmp_path):
    path = tmp_path / "rule.env"
    # Quoted: in an env file an unquoted ` #` starts a comment (documented).
    path.write_text(f"SECRET_KEY={SECRET}\nTASKS_RULE=filter\nTASKS_GLOBAL_FILTER=\" #task \"\n")
    s = Settings(_env_file=path)
    assert (s.tasks_rule, s.tasks_global_filter) == ("filter", "#task")


# ── the effective rule: one unit, and the defensive fallback ────────────────


def test_no_row_and_a_null_rule_inherit_the_whole_server_rule(monkeypatch):
    monkeypatch.setattr(task_settings.settings, "tasks_rule", "fields")
    monkeypatch.setattr(task_settings.settings, "tasks_rule_tags", ["someday"])
    for row in (None, TaskSettings(task_rule=None, task_tags=["ignored"], global_filter="x")):
        e = task_settings._merge(row)
        assert (e.task_rule, e.task_tags, e.task_rule_inherited) == ("fields", ("someday",), True)


def test_a_row_rule_uses_the_rows_tags_not_the_servers(monkeypatch):
    monkeypatch.setattr(task_settings.settings, "tasks_rule", "fields")
    monkeypatch.setattr(task_settings.settings, "tasks_rule_tags", ["someday"])
    e = task_settings._merge(TaskSettings(task_rule="fields", task_tags=[]))
    assert (e.task_rule, e.task_tags, e.task_rule_inherited) == ("fields", (), False)
    assert e.task_rule_unusable is None  # fields with no tags is valid


@pytest.mark.parametrize("rule,filt,reason", [
    ("any", None, TR.UNUSABLE_UNKNOWN),
    ("filter", None, TR.UNUSABLE_NO_FILTER),
    ("filter", "   ", TR.UNUSABLE_NO_FILTER),
    ("filter", "", TR.UNUSABLE_NO_FILTER),
    ("filter", "%%", TR.UNUSABLE_BAD_FILTER),
    ("filter", "<!--", TR.UNUSABLE_BAD_FILTER),
    ("filter", " #task", TR.UNUSABLE_BAD_FILTER),
])
def test_an_unevaluable_stored_rule_is_all_with_a_reason(rule, filt, reason):
    e = task_settings._merge(TaskSettings(task_rule=rule, task_tags=["x"], global_filter=filt))
    assert (e.task_rule, e.task_tags, e.global_filter, e.task_rule_unusable) == (
        "all", (), None, reason)
    assert TR.rule_clause(e) is None and TR.digest_part(e) is None
    assert TR.line_present(e) and not TR.in_force(e)
    assert TR.not_task_note(e) is None


def test_stored_tags_outside_the_grammar_are_dropped():
    e = task_settings._merge(TaskSettings(task_rule="fields",
                                          task_tags=["Ok", "a] — excluded by rule: 0", "b/"]))
    assert e.task_tags == ("ok",)


# ── the write statement ──────────────────────────────────────────────────────


def test_the_note_lines():
    assert TR.not_task_note(FIELDS) == (
        'note: not a task under your task rule "fields" (no task field and no task tag), '
        'so default listings will not show it; list_tasks(scope="all") will.')
    assert TR.not_task_note(eff("filter", filt="TODO")) == (
        'note: not a task under your task rule "filter" (the line does not contain "TODO"), '
        'so default listings will not show it; list_tasks(scope="all") will.')
    assert TR.not_task_note(eff()) is None


def test_quote_escapes_every_line_break():
    for value in ('a"b', "a\\b", "a b", "a b", "a\u0085b", "a\nb"):
        out = TR.quote(value)
        assert "\n" not in out and " " not in out and " " not in out
        assert "\u0085" not in out and out.startswith('"') and out.endswith('"')
        assert json.loads(out) == value


# ── one implementation ───────────────────────────────────────────────────────


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("rule_clause", "unsure_clause"):
            if isinstance(node.value, ast.Name) and node.value.id in ("task_rule", "TR"):
                names.add(node.attr)
        if isinstance(node, ast.ImportFrom) and node.module and "task_rule" in node.module:
            names |= {a.name for a in node.names}
    return names


def test_only_task_query_reads_the_rule_predicate():
    """D4: no module builds the rule predicate except `task_query` (whose
    `scope_clause` composes it); every consumer goes through `scope_clause`."""
    for path in (ROOT / "src").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ("src/services/task_rule.py", "src/services/task_query.py"):
            continue
        assert not ({"rule_clause", "unsure_clause"} & _imports_of(path)), rel
        assert "task_rule.rule_clause" not in path.read_text(), rel


def test_every_consumer_calls_scope_clause():
    import inspect

    from src.services import task_query as Q

    assert "scope_clause(settings)" in inspect.getsource(tw.duplicate_scan)
    assert "scope_clause(task_settings)" in inspect.getsource(Q._Filters.__init__)
    assert "scope_clause(settings)" in inspect.getsource(Q.task_agenda_query)
    assert "rule_clause(task_settings)" in inspect.getsource(Q.scope_clause)


def test_the_filter_digest_is_unchanged_under_all_and_scope_all():
    from src.services import task_query as Q

    now = dt.datetime(2026, 11, 10, 12, tzinfo=dt.timezone.utc)
    base = task_settings.defaults()
    n = Q.normalise_list_args(Q.ListTasksArgs(), base, now=now)
    n_all = Q.normalise_list_args(Q.ListTasksArgs(scope="all"), base, now=now)
    plain = Q.filters_digest(n, base)
    # A stored rule `all` with tags and filter filled in: byte-identical.
    assert Q.filters_digest(n, eff("all", ["x"], "#task")) == plain
    # Under scope="all" the rule never enters the digest.
    for e in (eff("fields", ["x"]), eff("filter", filt="#task")):
        assert Q.filters_digest(n_all, e) == Q.filters_digest(n_all, base)
    f1 = Q.filters_digest(n, eff("fields", ["x"]))
    assert f1 != plain
    assert Q.filters_digest(n, eff("fields", ["y"])) != f1  # tags matter under fields
    assert Q.filters_digest(n, eff("fields", ["x"], "#other")) == f1  # filter does not
    g1 = Q.filters_digest(n, eff("filter", ["x"], "#task"))
    assert g1 not in (plain, f1)
    assert Q.filters_digest(n, eff("filter", ["y"], "#task")) == g1  # tags do not
    assert Q.filters_digest(n, eff("filter", ["x"], "#todo")) != g1  # filter does
    # The defensive fallback digests as `all`.
    bad = task_settings._merge(TaskSettings(task_rule="filter", global_filter=None))
    assert Q.filters_digest(n, bad) == plain


# ── byte identity under `all`, pinned against the previous release ──────────
#
# These literals were produced by the `origin/main` code (58564cd, v1.3.0):
# `git show origin/main:src/services/task_query.py` was loaded into a
# temporary module (registered in `sys.modules` so its dataclasses build) and
# its `filters_digest`, `scope_clause` (compiled for the postgresql dialect
# with `literal_binds`) and `encode_cursor` were called with the inputs below
# (NOW, `ListTasksArgs()` / `ListTasksArgs(scope="all")`, owner None, the row
# `Projects/Alpha.md:3` due 2026-11-30). They are pasted here, never recomputed.

V130_DIGEST = {
    ("none", "default"): "562eb91873ee9f43", ("none", "all"): "c3f2515bfea4c4cf",
    ("exclusions", "default"): "9e8db59d9358f4ae", ("exclusions", "all"): "e833abac771dfb61",
}
V130_EXCLUSIONS_SQL = (
    "notes_metadata.file_path NOT LIKE 'Archive/%%' ESCAPE '\\\\' AND notes_metadata.file_path "
    "NOT LIKE 'Work/Tickets/%%' ESCAPE '\\\\' AND NOT (EXISTS (SELECT 1 AS anon_1 \n"
    "FROM unnest(notes_metadata.tags) AS t(v) \n"
    "WHERE rtrim(regexp_replace(lower(regexp_replace(t.v, '^#', '')), '/{2,}', '/', 'g'), '/') "
    "= 'private' OR left(rtrim(regexp_replace(lower(regexp_replace(t.v, '^#', '')), '/{2,}', "
    "'/', 'g'), '/'), 8) = 'private/' OR rtrim(regexp_replace(lower(regexp_replace(t.v, '^#', "
    "'')), '/{2,}', '/', 'g'), '/') = 'ticket/ops' OR left(rtrim(regexp_replace(lower("
    "regexp_replace(t.v, '^#', '')), '/{2,}', '/', 'g'), '/'), 11) = 'ticket/ops/'))"
)
V130_CURSOR = (
    "eyJ2IjoyLCJvIjoiYzU3MTc4Nzg4ZmEwMTE0MCIsImYiOiI1NjJlYjkxODczZWU5ZjQzIiwib2IiOiJkdWUiLCJr"
    "IjpbIjIwMjYtMTEtMzAiXSwicCI6IlByb2plY3RzL0FscGhhLm1kIiwibCI6M30"
)


def _v130_settings():
    base = task_settings.defaults()
    leftover = task_settings._merge(TaskSettings(task_rule="all", task_tags=["waiting-for"],
                                                 global_filter="#task"))
    excl = dataclasses.replace(base, excluded_folders=("Archive/", "Work/Tickets/"),
                               excluded_note_tags=("private", "ticket/ops"))
    excl_leftover = dataclasses.replace(leftover, excluded_folders=excl.excluded_folders,
                                        excluded_note_tags=excl.excluded_note_tags)
    return {"none": base, "leftover": leftover, "exclusions": excl,
            "exclusions+leftover": excl_leftover}


def test_scope_sql_and_digest_under_all_equal_v130():
    from sqlalchemy.dialects import postgresql

    from src.services import task_query as Q

    now = dt.datetime(2026, 11, 10, 12, tzinfo=dt.timezone.utc)
    for name, ts in _v130_settings().items():
        assert ts.task_rule == "all" and ts.task_rule_unusable is None
        group = "exclusions" if name.startswith("exclusions") else "none"
        for scope in ("default", "all"):
            n = Q.normalise_list_args(Q.ListTasksArgs(scope=scope), ts, now=now)
            assert Q.filters_digest(n, ts) == V130_DIGEST[(group, scope)], (name, scope)
        clause = Q.scope_clause(ts)
        if group == "none":
            assert clause is None, name
        else:
            sql = str(clause.compile(dialect=postgresql.dialect(),
                                     compile_kwargs={"literal_binds": True}))
            assert sql == V130_EXCLUSIONS_SQL, name


def test_a_v130_cursor_is_accepted_under_all():
    from src.services import task_query as Q

    now = dt.datetime(2026, 11, 10, 12, tzinfo=dt.timezone.utc)
    for ts in _v130_settings().values():
        if ts.excluded_folders:
            continue
        n = Q.normalise_list_args(Q.ListTasksArgs(), ts, now=now)
        got = Q.decode_cursor(V130_CURSOR, owner=Q.owner_discriminator(None),
                              filters=Q.filters_digest(n, ts), order_by="due")
        assert not isinstance(got, Q.QueryRefusal), got
        assert got == {"k": ["2026-11-30"], "l": 3, "p": "Projects/Alpha.md"}
    # The same cursor is refused once a rule is in force (D6).
    fields = eff("fields")
    n = Q.normalise_list_args(Q.ListTasksArgs(), fields, now=now)
    assert isinstance(Q.decode_cursor(V130_CURSOR, owner=Q.owner_discriminator(None),
                                      filters=Q.filters_digest(n, fields), order_by="due"),
                      Q.QueryRefusal)
