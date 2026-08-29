"""The health page, the error ring buffer, and backup recency (#163).

Hermetic: no database, no container. What a real PostgreSQL has to answer —
does the `backups_log` row round-trip, does the Makefile guard take the branch
it claims — is `tests/integration/test_issue_163_ops_health_pg.py` and
`tests/integration/test_schema_check.py`. What is pinned here is what breaks
silently:

* **The buffer is bounded and cannot raise.** It sits on the root logger, so it
  runs inside every `logger.error(...)` in the process — including the ones in
  exception handlers, where a second exception is the last thing anybody wants.
* **The observation window renders beside the count, always.** "No errors"
  without "since 14:32" is a claim about the server; what it means is "this
  process has not failed yet", and a container restarted two minutes ago has an
  empty buffer for reasons unrelated to health.
* **Every section has an explicit empty state.** A fresh install has no passes,
  no errors and no recorded backup, and that is the page an operator opens when
  they are not sure the server works at all.
* **Errors and backup age are admin-only.** Neither has an owner to scope by,
  so the alternative to gating them is a non-admin reading other tenants' paths
  out of a traceback.
* **The migration's markers and its size floor are mirrored in the model.** A
  drifted marker is a dirty `alembic check` and a `downgrade()` that has quietly
  stopped recognising its own work.
"""
import asyncio
import datetime
import logging
import os
import re
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.control_panel import routes  # noqa: E402
from src.services import error_log, ops_health  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(HERE, "..", "src", "control_panel", "templates")


@pytest.fixture(autouse=True)
def _clean_buffer():
    """Every case starts with an empty buffer and no attached handler."""
    root = logging.getLogger()
    error_log.detach(root)
    error_log.reset()
    yield
    error_log.detach(root)
    error_log.reset()


# --------------------------------------------------------------------------
# 1. The ring buffer
# --------------------------------------------------------------------------


def test_it_captures_errors_and_ignores_everything_below():
    error_log.attach()
    log = logging.getLogger("src.services.thing")
    log.warning("a warning nobody needs on the page")
    log.info("an info line")
    log.error("the embed pass died")
    log.critical("the dim guard refused to start")

    entries = error_log.recent_errors()
    assert [e["message"] for e in entries] == [
        "the dim guard refused to start",
        "the embed pass died",
    ], "newest first, and nothing below ERROR"
    assert entries[0]["level"] == "CRITICAL"
    assert entries[1]["logger"] == "src.services.thing"
    assert entries[0]["timestamp"].tzinfo is not None


def test_only_the_first_line_of_a_traceback_shaped_message_is_kept():
    """The page is a pointer, not a log viewer. A hundred multi-thousand-line
    tracebacks rendered into one HTML page is a page nobody can read."""
    error_log.attach()
    logging.getLogger("src.mcp_server.tools").error(
        "OperationalError: connection reset\n"
        '  File "/app/src/x.py", line 12, in run\n'
        "    await session.execute(stmt)"
    )
    entry = error_log.recent_errors()[0]
    assert entry["message"] == "OperationalError: connection reset"
    assert "File " not in entry["message"]


def test_the_buffer_is_capped_and_evicts_the_oldest():
    error_log.attach()
    log = logging.getLogger("flood")
    for i in range(error_log.ERROR_BUFFER_SIZE + 25):
        log.error("error %d", i)

    entries = error_log.recent_errors()
    assert len(entries) == error_log.ERROR_BUFFER_SIZE == 100
    assert error_log.error_count() == 100
    assert error_log.is_full() is True
    # Newest first, and the first 25 are gone rather than the last 25.
    assert entries[0]["message"] == "error 124"
    assert entries[-1]["message"] == "error 25"


def test_a_record_whose_format_arguments_are_wrong_does_not_raise():
    """This handler runs inside every `logger.error(...)` in the process,
    including the ones in exception handlers. Raising there turns one error
    into two at the point least able to cope.

    The record is handed to the handler directly rather than logged: a
    `%d` fed a string makes *pytest's own* capture handler raise first, which
    would be a test of pytest rather than of this module.
    """
    handler = error_log.attach()
    record = logging.LogRecord(
        name="bad", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="%d items and %s", args=("not-an-int",), exc_info=None,
    )
    handler.emit(record)  # must not raise
    entry = error_log.recent_errors()[0]
    assert "%d items and %s" in entry["message"], (
        "the fallback is the raw msg, which is still the identifying half"
    )


def test_attaching_twice_does_not_double_record():
    root = logging.getLogger()
    first = error_log.attach(root)
    second = error_log.attach(root)
    assert first is second
    assert root.handlers.count(first) == 1
    logging.getLogger("once").error("only once")
    assert len(error_log.recent_errors()) == 1


def test_the_observation_window_starts_at_attach_and_is_unknown_before():
    assert error_log.observing_since() is None, (
        "before the lifespan has attached anything, the page must say the "
        "window is unknown rather than imply it has been watching"
    )
    before = datetime.datetime.now(datetime.timezone.utc)
    error_log.attach()
    since = error_log.observing_since()
    assert since is not None and since >= before


def test_entries_are_copies_so_a_reader_cannot_edit_the_buffer():
    error_log.attach()
    logging.getLogger("x").error("original")
    error_log.recent_errors()[0]["message"] = "tampered"
    assert error_log.recent_errors()[0]["message"] == "original"


class _Probe(logging.Handler):
    """Counts what reaches it. Used only to prove where propagation stops."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_an_unhandled_asgi_500_reaches_the_buffer_under_uvicorns_config():
    """`uvicorn.error` is where "Exception in ASGI application" is logged, and
    uvicorn's own `dictConfig` stops that record before the root logger. So the
    test asserts the *behaviour* — a root-only handler sees nothing, ours sees
    it exactly once — rather than which logger in the chain happens to carry
    `propagate: false` in this release."""
    import copy
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            list(logging.getLogger(name).handlers),
        )
        for name in names
    }
    root = logging.getLogger()
    saved_root = (root.level, list(root.handlers))
    probe = _Probe()
    try:
        logging.config.dictConfig(copy.deepcopy(LOGGING_CONFIG))
        root.addHandler(probe)
        error_log.attach()
        logging.getLogger("uvicorn.error").error("Exception in ASGI application")

        assert probe.records == [], (
            "a root-only handler must NOT see this record — that is the whole "
            "reason uvicorn.error is attached directly"
        )
        entries = error_log.recent_errors()
        assert len(entries) == 1, "captured exactly once, not zero and not twice"
        assert entries[0]["logger"] == "uvicorn.error"
        assert entries[0]["message"] == "Exception in ASGI application"
    finally:
        error_log.detach()
        root.removeHandler(probe)
        for name, (level, propagate, handlers) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.propagate = propagate
            logger.setLevel(level)
        root.handlers[:] = saved_root[1]
        root.setLevel(saved_root[0])


def test_a_propagating_uvicorn_error_is_still_recorded_only_once():
    """The same handler instance sits on `uvicorn.error` and on the root, so
    `callHandlers` reaches it twice for one record if propagation is ever on.
    The per-record flag is what makes that one entry."""
    uvicorn_error = logging.getLogger("uvicorn.error")
    saved = uvicorn_error.propagate
    try:
        uvicorn_error.propagate = True
        error_log.attach()
        uvicorn_error.error("one record, two handler visits")
        assert len(error_log.recent_errors()) == 1
    finally:
        uvicorn_error.propagate = saved


def test_detach_removes_the_handler_from_every_logger_attach_used():
    error_log.attach()
    error_log.detach()
    handlers = logging.getLogger("uvicorn.error").handlers
    assert not any(isinstance(h, error_log.RingBufferHandler) for h in handlers)
    assert not any(
        isinstance(h, error_log.RingBufferHandler)
        for h in logging.getLogger().handlers
    )


def test_the_lifespan_attaches_the_handler_before_the_sandbox_branch():
    """A process that dies in a startup guard is exactly the one whose errors
    an operator comes looking for, so the buffer must be live before any guard
    can call `sys.exit`."""
    import inspect

    from src import main

    source = inspect.getsource(main.lifespan)
    attach_at = source.index("error_log.attach()")
    sandbox_at = source.index("settings.mcp_sandbox_mode")
    assert attach_at < sandbox_at


# --------------------------------------------------------------------------
# 2. Backup recency
# --------------------------------------------------------------------------


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Result:
    def __init__(self, scalar=None, first=None, rows=None):
        self._scalar = scalar
        self._first = first
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def first(self):
        return self._first

    def fetchall(self):
        return self._rows

    def scalars(self):
        outer = self

        class _S:
            def all(self):
                value = outer._scalar
                return value if isinstance(value, list) else []

        return _S()


class _BackupSession:
    """Answers the two statements `latest_backup` issues, in order."""

    def __init__(self, table_present=True, row=None, runs=None):
        self.table_present = table_present
        self.row = row
        self.runs = runs or []
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        text = str(statement)
        self.statements.append(text)
        if "to_regclass" in text:
            return _Result(scalar="backups_log" if self.table_present else None)
        if "backups_log" in text:
            return _Result(first=self.row)
        if "indexer_runs" in text:
            return _Result(rows=self.runs)
        return _Result(scalar=0)


def _backup_row(age_days=1.0, size=4096, filename="backup_20260829_020000.sql.gz"):
    return _Row(
        created_at=datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=age_days),
        filename=filename,
        size_bytes=size,
    )


def test_a_pre_021_database_reads_as_no_backup_rather_than_a_500():
    """`make deploy` backs up **before** it migrates, and a database can sit
    below 021 for other reasons too. Reading a table that is not there would
    take the whole page down."""
    session = _BackupSession(table_present=False)
    assert asyncio.run(ops_health.latest_backup(session)) is None
    assert any("to_regclass" in s for s in session.statements), (
        "the read must probe before it selects"
    )
    assert not any("FROM public.backups_log" in s for s in session.statements)


def test_an_empty_table_reads_as_no_backup_too():
    session = _BackupSession(table_present=True, row=None)
    assert asyncio.run(ops_health.latest_backup(session)) is None


def test_a_recorded_backup_carries_its_age_and_size():
    session = _BackupSession(row=_backup_row(age_days=2, size=5 * 1024 * 1024))
    backup = asyncio.run(ops_health.latest_backup(session))
    assert backup["filename"] == "backup_20260829_020000.sql.gz"
    assert backup["size_human"] == "5.0 MiB"
    assert backup["age_days"] == 2
    assert backup["stale"] is False


@pytest.mark.parametrize(
    "age_days,stale",
    # Not exactly 8.0: the age is measured against `now()` at read time, so a
    # row seeded exactly 8 days ago is a few microseconds over by the time the
    # comparison runs. The threshold is strictly-greater, and 7.9/8.1 pin it
    # without pinning a race.
    [(0.0, False), (7.0, False), (7.9, False), (8.1, True), (30.0, True)],
)
def test_staleness_turns_over_at_eight_days(age_days, stale):
    """Eight and not seven: a backup taken every Sunday is at most 7 days old
    on a good week, and a 7-day threshold pages an operator every Saturday for
    a schedule that is working."""
    session = _BackupSession(row=_backup_row(age_days=age_days))
    assert asyncio.run(ops_health.latest_backup(session))["stale"] is stale


def test_a_missing_record_is_not_stale():
    """Unknown is not overdue. Warning "stale" on a fresh install is a warning
    about a backup nobody has failed to take yet."""
    assert ops_health.is_stale(None) is False


def test_human_size_is_base_1024():
    assert ops_health.human_size(512) == "512 B"
    assert ops_health.human_size(2048) == "2.0 KiB"
    assert ops_health.human_size(3 * 1024**3) == "3.0 GiB"
    assert ops_health.human_size(None) is None


# --------------------------------------------------------------------------
# 3. The model and the migration describe the same table
# --------------------------------------------------------------------------


def test_the_markers_and_the_size_floor_are_mirrored_byte_for_byte():
    """A drifted marker is a dirty `alembic check` (the model declares the
    table comment) *and* a `downgrade()` that has stopped recognising its own
    work."""
    import importlib.util

    from src.models.db import BACKUP_MIN_SIZE_BYTES, BackupLog

    path = os.path.join(HERE, "..", "alembic", "versions", "021_backups_log.py")
    spec = importlib.util.spec_from_file_location("_m021", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert BackupLog._TABLE_MARKER == module.MARKER
    assert BackupLog.__table_args__[-1]["comment"] == module.MARKER
    assert BACKUP_MIN_SIZE_BYTES == module.MIN_SIZE_BYTES
    assert module.TABLE == BackupLog.__tablename__
    assert [c[0] for c in module.EXPECTED_COLUMNS] == [
        c.name for c in BackupLog.__table__.columns
    ]


# --------------------------------------------------------------------------
# 3b. The Makefile wiring the guard depends on
# --------------------------------------------------------------------------
#
# What the guard *does* is exercised against real PostgreSQL in
# `tests/integration/test_issue_163_ops_health_pg.py`. What is pinned here is
# the wiring around it, because the reason the guard exists is an ordering fact
# about `make deploy` that a future edit could silently reverse.


def _makefile() -> str:
    with open(os.path.join(HERE, "..", "Makefile")) as fh:
        return fh.read()


def test_deploy_still_backs_up_before_it_migrates():
    """The whole reason the absent-table branch exists. If this ever inverts,
    the bootstrap warning becomes dead code and the guard's disposition should
    be revisited — not left as a comment describing an ordering that changed."""
    make = _makefile()
    body = make[make.index("\ndeploy:") : make.index("\nup:")]
    # Commands only: the recipe's comments name both steps, in the other order.
    deploy = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    assert deploy.index("db-backup") < deploy.index("alembic upgrade head")


def test_db_backup_records_the_dump_and_its_status_is_the_targets():
    """The recording call is the recipe's **last** command, so its exit status
    is the target's: once the table exists, a backup that cannot record itself
    fails the backup."""
    make = _makefile()
    recipe = make[make.index("\ndb-backup:") : make.index("\ndb-restore:")]
    invocations = [
        line for line in recipe.splitlines() if "bash docker/record-backup.sh" in line
    ]
    assert len(invocations) == 1, invocations
    # Passed the file that was actually written, and the container the dump
    # itself used — the same channel, not a second one.
    assert "$$BACKUP_FILE.gz" in invocations[0]
    assert "DB_CONTAINER=$(DB_CONTAINER)" in invocations[0]
    body = [line.strip() for line in recipe.strip().splitlines() if line.strip()]
    assert "record-backup.sh" in body[-1], (
        "the recording must be the last command in the recipe, or its failure "
        "cannot fail the target"
    )


def test_the_recording_script_goes_through_docker_exec_psql():
    """Same channel as `pg_dump`. A host-side script has no other handle on the
    database, and inventing one (a direct connection, a mounted socket) is how
    a public repo acquires a host-specific path."""
    with open(os.path.join(HERE, "..", "docker", "record-backup.sh")) as fh:
        script = fh.read()
    assert 'docker exec -i "$DB_CONTAINER" psql' in script
    assert "to_regclass('public.backups_log')" in script
    assert "ON_ERROR_STOP=1" in script
    # The filename and size travel as psql variables, not as interpolated
    # shell text — and the SQL arrives on stdin, because psql does not
    # interpolate `:'var'` into a `-c` command (it reaches the server as typed
    # and is a syntax error there).
    assert ":'filename'" in script and ":'size_bytes'" in script
    assert "-c \"INSERT" not in script


def test_the_probe_never_folds_stderr_into_the_value_it_classifies():
    """A healthy server writes warnings to stderr — a collation-version
    mismatch after a libc upgrade is emitted on connection. Folded into stdout,
    the probe's value becomes `WARNING: …\\nt`, which is not `t`, and the
    absent-table branch then exits 0 without recording anything, forever, on a
    table that exists."""
    with open(os.path.join(HERE, "..", "docker", "record-backup.sh")) as fh:
        script = fh.read()
    # Comments may discuss `2>&1`; no *command* may use it.
    commands = [
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    ]
    assert not any("2>&1" in line for line in commands), (
        "stderr must be captured separately from the value being classified"
    )
    assert '2>"$STDERR_FILE"' in script


def test_the_writer_and_the_reader_name_the_same_qualified_table():
    """An unqualified reference resolves through `search_path`, so a role
    pointing elsewhere would have the writer recording into a table the panel
    never reads — the page then warns that no backup has been taken while one
    is taken daily."""
    with open(os.path.join(HERE, "..", "docker", "record-backup.sh")) as fh:
        script = fh.read()
    assert "INSERT INTO public.backups_log" in script

    with open(os.path.join(HERE, "..", "src", "services", "ops_health.py")) as fh:
        reader = fh.read()
    assert "FROM public.backups_log" in reader

    with open(
        os.path.join(HERE, "..", "alembic", "versions", "021_backups_log.py")
    ) as fh:
        migration = fh.read()
    assert 'QUALIFIED = "public.backups_log"' in migration
    assert '{"table": TABLE}' not in migration, (
        "every catalog lookup resolves the qualified name"
    )
    # The DDL itself is unqualified on purpose — a schema-qualified table does
    # not match a model that declares none, and `alembic check` would report
    # drift forever after. What makes it land in public is the pin.
    assert "SET LOCAL search_path TO public" in migration
    assert migration.count("RESET search_path") >= 2, (
        "both upgrade() and downgrade() must reset it: alembic runs every "
        "pending revision in one transaction, so SET LOCAL would leak"
    )
    # No `op.*` call may carry a `schema=` keyword. Checked on the parse tree,
    # not the text: the docstring above explains at length why it must not,
    # and a substring search would match that explanation.
    import ast

    schema_kwargs = [
        node
        for node in ast.walk(ast.parse(migration))
        if isinstance(node, ast.Call)
        and any(kw.arg == "schema" for kw in node.keywords)
    ]
    assert schema_kwargs == []


# --------------------------------------------------------------------------
# 4. The page
# --------------------------------------------------------------------------


def _env():
    from jinja2 import ChainableUndefined, ChoiceLoader, DictLoader, Environment, FileSystemLoader

    return Environment(
        loader=ChoiceLoader([
            DictLoader({
                "base.html":
                    "{% block title %}{% endblock %}{% block content %}{% endblock %}"
            }),
            FileSystemLoader(TEMPLATES_DIR),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )


def _run(**kw):
    return dict({
        "id": 7, "started_at": "2026-08-29T10:00:00+00:00", "duration": "12.4s",
        "trigger": "scheduled", "user_id": None, "owner": None,
        "owner_missing": False, "notes_scanned": 100, "notes_indexed": 3,
        "notes_embedded": 3, "error": None,
    }, **kw)


def _render_health(**overrides):
    ctx = dict(
        active="health",
        runs=[_run()],
        runs_limit=ops_health.HEALTH_RUNS_LIMIT,
        show_ops=True,
        backup={
            "created_at_iso": "2026-08-29T02:00:00+00:00",
            "filename": "backup_20260829_020000.sql.gz",
            "size_human": "5.0 MiB",
            "age_rel": "9 hours ago",
            "stale": False,
        },
        stale_after_days=ops_health.STALE_AFTER_DAYS,
        errors=[{
            "timestamp": "2026-08-29T09:59:00+00:00",
            "logger": "src.services.indexer",
            "level": "ERROR",
            "message": "embed pass failed: connection reset",
        }],
        error_count=1,
        errors_capped=False,
        error_buffer_size=error_log.ERROR_BUFFER_SIZE,
        observing_since_iso="2026-08-29T08:00:00+00:00",
        observing_since_rel="2 hours ago",
    )
    ctx.update(overrides)
    return _env().get_template("health.html").render(**ctx)


def test_the_populated_page_renders_all_three_sections():
    html = _render_health()
    assert "backup_20260829_020000.sql.gz" in html
    assert "5.0 MiB" in html
    assert "embed pass failed: connection reset" in html
    assert "src.services.indexer" in html
    assert "Index passes" in html
    assert 'id="run-7"' in html, "the strip links a failed pass to its row"


def test_the_error_section_states_the_window_it_observed():
    html = _render_health()
    assert "Observing since process start" in html
    assert "2026-08-29T08:00:00+00:00" in html
    assert "2 hours ago" in html


def test_a_fresh_install_renders_three_explicit_empty_states():
    """No passes, no errors, no backup rows: the page an operator opens when
    they are not sure the server works at all."""
    html = _render_health(
        runs=[], errors=[], error_count=0, backup=None,
        observing_since_iso="2026-08-29T08:00:00+00:00",
    )
    assert "No backup recorded yet" in html
    assert "No errors logged since this process started" in html
    assert "No index or embed pass has been recorded yet" in html
    # And it still says when it started observing — "no errors" alone is a
    # claim about the server rather than about this process.
    assert "Observing since process start" in html


def test_an_unknown_observation_window_says_so():
    html = _render_health(observing_since_iso=None, observing_since_rel=None,
                          errors=[], error_count=0)
    assert "Observation window unknown" in html


def test_a_stale_backup_warns_and_names_the_threshold():
    html = _render_health(backup={
        "created_at_iso": "2026-08-01T02:00:00+00:00",
        "filename": "backup_20260801_020000.sql.gz",
        "size_human": "4.9 MiB",
        "age_rel": "28 days ago",
        "stale": True,
    })
    assert "stale" in html
    assert f"more than {ops_health.STALE_AFTER_DAYS} days" in html
    assert "make db-backup" in html


def test_the_page_never_claims_the_backup_was_verified():
    """Age is the whole signal: the panel cannot see the filesystem it reports
    on, which is the reason the record is a database row at all."""
    html = _render_health()
    assert "does not verify" in html


def test_a_capped_buffer_says_older_errors_were_evicted():
    html = _render_health(errors_capped=True, error_count=100)
    assert "have been evicted" in html


def test_a_non_admin_sees_only_their_passes_and_is_told_why():
    html = _render_health(show_ops=False, backup=None, errors=[])
    assert "shown to administrators only" in html
    assert "Last backup" not in html
    assert "Recent errors" not in html
    assert "Index passes" in html


def test_the_page_is_reachable_and_in_the_nav():
    paths = {r.path for r in routes.router.routes}
    assert "/admin/health" in paths

    with open(os.path.join(TEMPLATES_DIR, "base.html")) as fh:
        base = fh.read()
    assert 'href="/admin/health"' in base
    assert "active == 'health'" in base


# --------------------------------------------------------------------------
# 5. The dashboard strip
# --------------------------------------------------------------------------


def _render_strip(**health):
    ctx = dict(
        is_admin=True, username="max", multi_user_mode=False, csrf_token="csrf",
        stats={"notes_indexed": 1, "notes_with_embeddings": 1,
               "embedding_pct": 100, "active_keys": 1, "requests_today": 0},
        recent_usage=[], reindexed_24h=0, last_indexed_iso=None,
        last_indexed_rel="never", last_run_iso=None, last_run_rel="never",
        last_run_ok=True, index_interval=300, graph={},
        graph_backfill_running=False,
        health=dict({
            "show_ops": True, "last_run": None, "backup": None,
            "error_count": 0, "errors_capped": False,
            "observing_since_iso": "2026-08-29T08:00:00+00:00",
            "observing_since_rel": "2 hours ago",
            "stale_after_days": ops_health.STALE_AFTER_DAYS,
        }, **health),
    )
    return _env().get_template("dashboard.html").render(**ctx)


def test_the_strip_links_a_failed_pass_to_its_row_on_the_health_page():
    html = _render_strip(last_run=_run(id=42, error="OSError: no space left"))
    assert '/admin/health#run-42' in html
    assert "failed" in html


def test_the_strip_shows_ok_for_a_clean_pass_and_links_to_the_page():
    html = _render_strip(last_run=_run(id=42))
    assert "#run-42" not in html
    assert 'href="/admin/health"' in html


def test_the_strip_says_none_recorded_rather_than_ok_when_there_is_no_pass():
    """A fresh install has no pass row. "ok" would be a claim about a pass that
    never ran."""
    html = _render_strip(last_run=None)
    strip = html[: html.index("Stat row")]
    assert "none recorded" in strip
    assert "ok" not in re.sub(r"<[^>]+>", " ", strip)


def test_the_strip_states_the_error_window_beside_the_count():
    html = _render_strip(error_count=0)
    assert "since process start" in html
    assert "2 hours ago" in html


def test_a_stale_backup_is_flagged_on_the_strip():
    html = _render_strip(backup={"age_rel": "28 days ago", "stale": True})
    strip = html[: html.index("Stat row")]
    assert "28 days ago" in strip
    assert "badge-yellow" in strip


def test_a_non_admin_strip_carries_the_pass_and_neither_operator_cell():
    html = _render_strip(show_ops=False, last_run=_run(id=42))
    strip = html[: html.index("Stat row")]
    assert "Last pass" in strip
    assert "Backup" not in strip
    assert "Errors" not in strip


def test_a_degraded_strip_says_so_rather_than_claiming_a_state():
    html = _render_strip(unavailable=True)
    strip = html[: html.index("Stat row")]
    assert "Health summary unavailable" in strip
    # Never "ok" or "none recorded" — those are claims built on a query that
    # never returned.
    assert "none recorded" not in strip
    assert 'href="/admin/health"' in strip


# --------------------------------------------------------------------------
# 5b. The dashboard does not depend on the strip
# --------------------------------------------------------------------------


class _ExplodingStripSession:
    """Answers the dashboard's own queries and raises on the strip's.

    The realistic fault: `indexer_runs` unreadable — a `NOT VALID` FK left by a
    hand repair, a revoked permission, a 021 that has not run on the database
    the container was pointed at — while every other table on the page is fine.
    """

    def __init__(self):
        self.rolled_back = False
        self.n = 0

    async def execute(self, stmt, *_a, **_k):
        self.n += 1
        text = str(stmt).lower()
        if "indexer_runs" in text or "backups_log" in text or "to_regclass" in text:
            raise RuntimeError("relation \"indexer_runs\" does not exist")
        if "max(" in text:
            return _Result(scalar=None)
        if "select" in text and "usage_logs" in text and "count" not in text:
            return _Result(scalar=[])
        return _Result(scalar=0)

    async def rollback(self):
        self.rolled_back = True


class _Scalars:
    def __init__(self, value):
        self._value = value

    def all(self):
        return self._value if isinstance(self._value, list) else []


def _dashboard_context(monkeypatch, session):
    captured = {}

    def _fake_response(request, name, context):
        captured["context"] = context
        return "rendered"

    monkeypatch.setattr(routes.templates, "TemplateResponse", _fake_response)
    monkeypatch.setattr(routes, "generate_csrf_token", lambda _r: "csrf")

    async def _graph(*_a, **_k):
        return {}

    monkeypatch.setattr(routes, "_graph_stats", _graph)
    response = asyncio.run(
        routes.dashboard(request=_Request(), session=session, user=_User(True))
    )
    return captured["context"], response


def test_a_failing_strip_query_does_not_take_the_dashboard_down(monkeypatch):
    """The dashboard is the page an operator opens *because* something is
    wrong. Losing three cells beats losing the page."""
    session = _ExplodingStripSession()
    ctx, response = _dashboard_context(monkeypatch, session)

    assert response == "rendered", "the page still renders"
    assert ctx["health"] == {"unavailable": True, "show_ops": True}
    # And the dashboard's own data is untouched.
    assert "stats" in ctx and "graph" in ctx


def test_the_failed_strip_transaction_is_rolled_back(monkeypatch):
    """Without it every statement after the failure raises
    `InFailedSQLTransaction` instead of the real error — including the ones the
    render itself would issue."""
    session = _ExplodingStripSession()
    _dashboard_context(monkeypatch, session)
    assert session.rolled_back is True


def test_the_strip_failure_is_logged_at_error_so_the_page_shows_it(monkeypatch):
    error_log.attach()
    session = _ExplodingStripSession()
    _dashboard_context(monkeypatch, session)

    messages = [e["message"] for e in error_log.recent_errors()]
    assert any("health strip unavailable" in m.lower() for m in messages), messages


# --------------------------------------------------------------------------
# 6. The route's admin split
# --------------------------------------------------------------------------


class _Request:
    def __init__(self):
        self.session = {}
        self.scope = {}
        self.query_params = {}


class _User:
    def __init__(self, is_admin):
        self.id = 3
        self.username = "user"
        self.is_admin = is_admin
        self.is_active = True


def _health_context(monkeypatch, is_admin, table_present=True):
    captured = {}

    def _fake_response(request, name, context):
        captured["name"] = name
        captured["context"] = context
        return None

    monkeypatch.setattr(routes.templates, "TemplateResponse", _fake_response)
    monkeypatch.setattr(routes, "generate_csrf_token", lambda _r: "csrf")
    session = _BackupSession(table_present=table_present, row=None)
    asyncio.run(
        routes.health_page(
            request=_Request(), session=session, user=_User(is_admin)
        )
    )
    return captured, session


def test_the_route_withholds_errors_and_backups_from_a_non_admin(monkeypatch):
    """Neither section has an owner to scope by: the ring buffer holds whatever
    the process logged — other tenants' paths and identifiers included — and a
    backup covers the whole database. Gating is the only available answer."""
    error_log.attach()
    logging.getLogger("secretive").error("/vault/someone-else/private.md failed")

    captured, session = _health_context(monkeypatch, is_admin=False)
    ctx = captured["context"]
    assert ctx["show_ops"] is False
    assert ctx["backup"] is None
    assert "errors" not in ctx
    assert not any("backups_log" in s for s in session.statements), (
        "a non-admin request must not even ask"
    )


def test_the_route_gives_an_admin_both_sections(monkeypatch):
    error_log.attach()
    logging.getLogger("loud").error("something broke")

    captured, session = _health_context(monkeypatch, is_admin=True)
    ctx = captured["context"]
    assert captured["name"] == "health.html"
    assert ctx["show_ops"] is True
    assert ctx["error_count"] == 1
    assert ctx["errors"][0]["message"] == "something broke"
    assert ctx["observing_since_iso"] is not None
    assert ctx["runs_limit"] == ops_health.HEALTH_RUNS_LIMIT == 50
    assert any("to_regclass" in s for s in session.statements)


def test_the_run_history_asks_for_fifty_not_twenty(monkeypatch):
    """The performance page's 20 is a summary; #160 deferred the fuller view
    here."""
    seen = {}

    async def _fake_runs(session, user_id, limit=None):
        seen["limit"] = limit
        seen["user_id"] = user_id
        return []

    monkeypatch.setattr(routes, "recent_indexer_runs", _fake_runs)
    _health_context(monkeypatch, is_admin=True)
    assert seen["limit"] == 50
    assert seen["user_id"] is None, "an admin sees every pass"


def test_a_non_admins_run_history_is_scoped_to_them(monkeypatch):
    seen = {}

    async def _fake_runs(session, user_id, limit=None):
        seen["user_id"] = user_id
        return []

    monkeypatch.setattr(routes, "recent_indexer_runs", _fake_runs)
    _health_context(monkeypatch, is_admin=False)
    assert seen["user_id"] == 3
