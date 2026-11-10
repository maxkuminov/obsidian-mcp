"""The task stage never reaches the quarantine (#350, design D4, task 3.4).

A task-derivation failure is isolated to the note's task rows: a savepoint,
one retry, then `tasks_failed`. It must never construct a `PoisonCandidate`
or raise `PoisonNote`, so the quarantine's attributable write sites stay the
four they are (move update, batch upsert row, keyword vector at the floor,
link inserts). Pinned here structurally (AST) and behaviourally (a scripted
session); the PostgreSQL behaviour is in
`tests/integration/test_note_tasks_pg.py`.
"""
import ast
import asyncio
import inspect
import textwrap

import pytest

from src.services import indexer


def _function_source(fn) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


@pytest.mark.parametrize("fn", [indexer._update_tasks_for_changed, indexer._task_savepoint])
def test_the_task_stage_never_names_the_quarantine(fn):
    names = {
        node.id for node in ast.walk(_function_source(fn)) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(_function_source(fn)) if isinstance(node, ast.Attribute)
    }
    for forbidden in ("PoisonCandidate", "PoisonNote", "_quarantine_note", "_confirm_poison",
                      "poisoned"):
        assert forbidden not in names, forbidden


def test_the_quarantine_sites_are_the_four_they_were():
    source = inspect.getsource(indexer)
    sites = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "PoisonCandidate":
            site = node.args[3] if len(node.args) > 3 else None
            if isinstance(site, ast.Constant):
                sites.add(site.value)
        if isinstance(node, ast.keyword) and node.arg == "site" and isinstance(node.value, ast.Constant):
            sites.add(node.value.value)
    assert "tasks" not in sites
    assert "links" in sites


class _Poison(Exception):
    sqlstate = "22021"


class _Nested:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.events.append("savepoint")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.session.events.append("rollback" if exc_type else "release")
        return False


class _Session:
    def __init__(self, fail_inserts: int):
        self.fail_inserts = fail_inserts
        self.events: list[str] = []

    def begin_nested(self):
        return _Nested(self)

    async def execute(self, stmt, *_a, **_k):
        sql = str(stmt)
        if sql.startswith("INSERT INTO note_tasks"):
            if self.fail_inserts:
                self.fail_inserts -= 1
                raise _Poison()
            self.events.append("insert")
        elif sql.startswith("UPDATE notes_metadata"):
            literal = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            self.events.append(
                "update failed" if "SET tasks_failed=true" in literal else "update"
            )
        elif sql.startswith("DELETE FROM note_tasks"):
            self.events.append("delete")


def run_stage(session):
    skips: list[str] = []
    skipped: set[str] = set()
    asyncio.run(indexer._update_tasks_for_changed(
        session, ["a.md"], user_id=None, paths_to_id={"a.md": 1},
        bodies={"a.md": "- [ ] one\n"}, line_offsets={"a.md": 0},
        skips=skips, skipped_paths=skipped,
    ))
    return skips, skipped


def test_a_failure_twice_marks_the_note_and_is_not_a_skip(caplog):
    session = _Session(fail_inserts=2)
    skips, skipped = run_stage(session)
    assert skips == [] and skipped == set()
    assert session.events[-4:] == ["savepoint", "delete", "update failed", "release"]
    assert session.events.count("rollback") == 2
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "a.md" in message and "_Poison" in message and "22021" in message
    assert "one" not in message.replace("not a quarantine", "").replace("on the", "")


def test_a_failure_the_retry_does_not_reproduce_leaves_no_mark():
    session = _Session(fail_inserts=1)
    run_stage(session)
    assert "update failed" not in session.events
    assert session.events.count("rollback") == 1
    assert session.events[-3:] == ["insert", "update", "release"]


def test_an_extractor_exception_is_isolated(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(indexer, "extract_tasks_bounded", boom)
    session = _Session(fail_inserts=0)
    skips, _ = run_stage(session)
    assert skips == []
    assert "update failed" in session.events


def test_a_non_poison_database_error_propagates():
    class _Session2(_Session):
        async def execute(self, stmt, *_a, **_k):
            if str(stmt).startswith("INSERT INTO note_tasks"):
                raise ConnectionError("gone")
            return await super().execute(stmt)

    with pytest.raises(ConnectionError):
        run_stage(_Session2(fail_inserts=0))


def test_a_missing_body_or_id_is_a_skip():
    session = _Session(fail_inserts=0)
    skips: list[str] = []
    skipped: set[str] = set()
    asyncio.run(indexer._update_tasks_for_changed(
        session, ["a.md", "b.md"], user_id=None, paths_to_id={"a.md": 1},
        bodies={}, line_offsets={}, skips=skips, skipped_paths=skipped,
    ))
    assert skipped == {"a.md", "b.md"}
    assert any("no buffered body for the task rebuild" in s for s in skips)
    assert any("no index row for the task rebuild" in s for s in skips)
    assert session.events == []


def test_the_extraction_version_carries_task_extraction():
    from src.services.tasks import TASKS_SINCE_EXTRACTION_VERSION

    assert indexer.CURRENT_EXTRACTION_VERSION == 5
    assert TASKS_SINCE_EXTRACTION_VERSION == 4


@pytest.mark.parametrize("body", [
    "- [ ] task 📅 2026-01-01 #t\n```\n- [ ] fenced\n```\n",
    "[[Link]] `code` #tag\n~~~\nx\n~~~\n", "", "🌍 prose",
])
def test_version_four_preserves_version_three_embedding_input(body):
    """The bump is a task-only bump: no note is re-embedded on its account."""
    from src.services.embeddings import clean_at_version

    assert clean_at_version(4, body) == clean_at_version(3, body)
    assert indexer._grammar_changed_the_embedding_text(3, body) is False
