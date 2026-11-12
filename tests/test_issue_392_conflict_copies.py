"""#392 — the opt-in index exclusion of sync-conflict copies (offline halves).

The matcher's table (every documented form excluded, every look-alike kept),
the scan's classification (decided by name, before any stat or read, never a
skip), the pass result's counts, and the one-line notice on `create_note` and
`move_note` — byte-identical output when the setting is off or the name does
not match. The real-PostgreSQL halves are in
`tests/integration/test_issue_392_conflict_copies_pg.py`.
"""

import logging
import os
import tempfile
import threading

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import indexer  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services.conflict_copies import CONFLICT_PATTERNS, is_conflict_copy  # noqa: E402

# ── The matcher (design D2) ───────────────────────────────────────────────

EXCLUDED = [
    # P1 Syncthing, with and without the device short id.
    ("syncthing", "N.sync-conflict-20260529-194539-ABC2DEF.md"),
    ("syncthing", "N.sync-conflict-20260529-194539-.md"),
    ("syncthing", "My note.v2.sync-conflict-20260529-194539-ZZZZZZZ.md"),
    # P2 Nextcloud, with and without a user name.
    ("nextcloud", "N (conflicted copy alice 2026-05-29 194539).md"),
    ("nextcloud", "N (conflicted copy 2026-05-29 194539).md"),
    ("nextcloud", "N (conflicted copy Alice Smith 2026-05-29 194539).md"),
    # P3 Nextcloud / ownCloud legacy.
    ("nextcloud-legacy", "N_conflict-20260529-194539.md"),
    # P4 Dropbox, both apostrophes, with the counter.
    ("dropbox", "N (Alice's conflicted copy 2026-05-29).md"),
    ("dropbox", "N (Alice’s conflicted copy 2026-05-29).md"),
    ("dropbox", "N (Alice's conflicted copy 2026-05-29 (1)).md"),
    # P5 Obsidian Sync.
    ("obsidian-sync", "N (Conflicted copy MyMacBook2 202411281430).md"),
    ("obsidian-sync", "N (Conflicted copy Max's Phone 202411281430).md"),
    # P6 the observed form.
    ("observed", "N (conflict 2026-05-29-19-45-39).md"),
    ("observed", "2026-08-01 - A long daily title (conflict 2026-08-01-22-11-31).md"),
]

KEPT = [
    "N.md",
    "Crazy weekend - birthday, conflict, and bubbling life.md",
    "2026-05-29 conflict.md",
    "Conflict resolution (draft).md",
    "Notes (conflict 2026-05-29).md",
    "Note (conflicted copy).md",
    "Note (CONFLICT 2026-05-29-19-45-39).md",
    "Note (conflict 2026-05-29-19-45-39) v2.md",
    "Note.sync-conflict-2026-05-29.md",
    "(conflict 2026-05-29-19-45-39).md",
    # Non-ASCII digits (ARABIC-INDIC DIGIT ZERO..NINE) in the timestamp.
    "Note (conflict ٢٠٢٦-05-29-19-45-39).md",
    "Note (conflict 2026-05-29-19-45-39).MD",
    "Note (conflict 2026-05-29-19-45-39).txt",
    "Note (conflict 2026-05-29-19-45-39).md.bak",
    "Note.sync-conflict-20260529-194539-abc2def.md",
    "Note.sync-conflict-20260529-194539-ABC2DE.md",
    "Note_conflict-2026-05-29.md",
    "Note (Alice's conflicted copy).md",
    "Note (conflicted copy 2026-05-29).md",
    "Note (Conflicted copy MyMacBook2).md",
    "Note (case clash from 2026-05-29 194539).md",
    "Syncthing conflicts explained.md",
]


@pytest.mark.parametrize("tool,name", EXCLUDED)
def test_every_documented_form_is_a_conflict_copy(tool, name):
    assert is_conflict_copy(name)
    matching = [t for t, p in CONFLICT_PATTERNS if p.fullmatch(name)]
    assert tool in matching


@pytest.mark.parametrize("name", KEPT)
def test_look_alikes_are_not_conflict_copies(name):
    assert not is_conflict_copy(name)


def test_the_pattern_set_is_the_documented_six():
    assert [t for t, _ in CONFLICT_PATTERNS] == [
        "syncthing", "nextcloud", "nextcloud-legacy", "dropbox",
        "obsidian-sync", "observed",
    ]


# ── The scan (design D3) ──────────────────────────────────────────────────


def scan(root, *, exclude, snapshot=None, force_read=True):
    with indexer.pinned_root(root) as root_fd:
        return indexer._scan_vault(
            root_fd,
            snapshot or {},
            force_read=force_read,
            re_derive=False,
            stop=threading.Event(),
            exclude_conflicts=exclude,
        )


@pytest.fixture
def conflict_vault(tmp_path):
    (tmp_path / "A.md").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "A (conflict 2026-05-29-19-45-39).md").write_text(
        "alpha old\n", encoding="utf-8"
    )
    folder = tmp_path / "Proj (conflict 2026-05-29-19-45-39)"
    folder.mkdir()
    (folder / "Plan.md").write_text("plan\n", encoding="utf-8")
    (folder / "Plan.sync-conflict-20260529-194539-ABC2DEF.md").write_text(
        "plan old\n", encoding="utf-8"
    )
    return tmp_path


COPIES = {
    "A (conflict 2026-05-29-19-45-39).md",
    "Proj (conflict 2026-05-29-19-45-39)/Plan.sync-conflict-20260529-194539-ABC2DEF.md",
}


def test_setting_off_the_scan_is_unchanged(conflict_vault):
    off = scan(conflict_vault, exclude=False)
    default = None
    with indexer.pinned_root(conflict_vault) as root_fd:
        default = indexer._scan_vault(
            root_fd, {}, force_read=True, re_derive=False, stop=threading.Event()
        )
    assert off.not_indexable == {} == default.not_indexable
    assert set(off.files) == set(default.files) == off.seen
    assert {k: v.content_hash for k, v in off.files.items()} == {
        k: v.content_hash for k, v in default.files.items()
    }


def test_excluded_copies_are_seen_not_indexable_and_never_skips(
    conflict_vault, caplog
):
    with caplog.at_level(logging.INFO, logger="src.services.indexer"):
        result = scan(conflict_vault, exclude=True)
    assert set(result.files) == {"A.md", "Proj (conflict 2026-05-29-19-45-39)/Plan.md"}
    assert result.not_indexable == {
        p: indexer.EXCLUDED_CONFLICT_COPY for p in COPIES
    }
    # Seen, so never a vanished file nor a move source.
    assert COPIES <= result.seen
    # Not a skip, nothing unverified: no stamp withheld, the backstop advances.
    assert result.skips == [] and result.unverified == [] and result.read_failures == []
    assert result.reads == 2
    # No per-path WARNING for an exclusion.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_an_excluded_copy_is_never_statted_or_read(conflict_vault, monkeypatch):
    opened: list[str] = []
    real_read, real_stat = indexer.read_note_at, indexer._stat_follow

    def spy_read(parent_fd, name, rel=None):
        opened.append(name)
        return real_read(parent_fd, name, rel)

    def spy_stat(parent_fd, name):
        opened.append(name)
        return real_stat(parent_fd, name)

    monkeypatch.setattr(indexer, "read_note_at", spy_read)
    monkeypatch.setattr(indexer, "_stat_follow", spy_stat)
    # Make the copy unreadable: an attempt would surface as a read failure.
    copy = conflict_vault / "A (conflict 2026-05-29-19-45-39).md"
    copy.chmod(0)
    try:
        result = scan(conflict_vault, exclude=True)
    finally:
        copy.chmod(0o644)
    assert not any(is_conflict_copy(n) for n in opened)
    assert result.read_failures == [] and result.unverified == []


def test_the_startup_line_is_logged_only_when_on(monkeypatch, caplog):
    import src.main as main

    # Importing `src.main` reconfigures logging (the root handlers are
    # replaced), so attach the capture handler to its logger directly.
    log = logging.getLogger("src.main")
    # Not propagated meanwhile: when the root still holds the capture handler
    # (src.main was imported earlier in the session) a record would be
    # captured twice.
    log.addHandler(caplog.handler)
    monkeypatch.setattr(log, "level", logging.INFO)
    monkeypatch.setattr(log, "propagate", False)
    try:
        _startup_line_cases(main, monkeypatch, caplog)
    finally:
        log.removeHandler(caplog.handler)


def _startup_line_cases(main, monkeypatch, caplog):
    monkeypatch.setattr(main.settings, "index_exclude_conflict_files", False)
    main._log_index_exclusion()
    assert not [r for r in caplog.records if "sync-conflict" in r.getMessage()]

    monkeypatch.setattr(main.settings, "index_exclude_conflict_files", True)
    main._log_index_exclusion()
    lines = [r for r in caplog.records if "sync-conflict" in r.getMessage()]
    assert [(r.levelno, r.getMessage()) for r in lines] == [(
        logging.INFO,
        "Index exclusion: sync-conflict copies are not indexed "
        "(INDEX_EXCLUDE_CONFLICT_FILES=true)",
    )]


def test_the_lifespan_calls_the_startup_line():
    import inspect

    import src.main as main

    assert "_log_index_exclusion()" in inspect.getsource(main.lifespan)


def test_the_pass_result_counts_excluded_apart_from_not_indexable():
    r = indexer.IndexPassResult(5, 1, not_indexable=1, excluded=2)
    assert tuple(r) == (5, 1)
    assert r.not_indexable == 1 and r.excluded == 2
    assert indexer.IndexPassResult(5, 1).excluded == 0


# ── The tool notice (spec review r1) ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)
    monkeypatch.setattr(tools, "_record_note_events", _noop)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    vault_service.clear_user_vault_cache()
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)


def _setting(monkeypatch, value: bool):
    monkeypatch.setattr(tools.settings, "index_exclude_conflict_files", value)


NOTICE_LINE = (
    "note: this name matches the sync-conflict exclusion "
    "(INDEX_EXCLUDE_CONFLICT_FILES); the note is stored but not indexed, so "
    "searches and listings will not return it once the next index pass has "
    "run. Read it by exact path."
)
COPY = "Review (conflict 2026-05-29-19-45-39).md"


def test_the_notice_text_is_the_spec_text():
    assert tools.CONFLICT_EXCLUSION_NOTICE == NOTICE_LINE


def test_the_rewrite_notice_text_is_the_spec_text():
    # Review r2: "may no longer be", because until the next pass removes the
    # row the source is still indexed and its backlinks are rewritten.
    assert tools.CONFLICT_REWRITE_NOTICE == (
        "note: the source name matches the sync-conflict exclusion "
        "(INDEX_EXCLUDE_CONFLICT_FILES), so it may no longer be in the index "
        "and links to its old name may not have been found or rewritten; "
        "search for the old name and fix any by hand."
    )
    assert "\n" not in tools.CONFLICT_REWRITE_NOTICE


async def test_create_note_notice_only_when_on_and_matching(vault, monkeypatch):
    _setting(monkeypatch, False)
    off = await tools.create_note_impl(COPY, "body\n")
    (vault / COPY).unlink()
    _setting(monkeypatch, True)
    on = await tools.create_note_impl(COPY, "body\n")
    plain = await tools.create_note_impl("Review.md", "body\n")
    (vault / "Review.md").unlink()
    _setting(monkeypatch, False)
    plain_off = await tools.create_note_impl("Review.md", "body\n")

    assert "\n" not in off and off.startswith(f"Created note: {COPY} — content_hash: ")
    assert on == off + "\n" + NOTICE_LINE
    # A non-matching name, and the setting off: byte-identical.
    assert plain == plain_off and "\n" not in plain
    assert (vault / COPY).read_text(encoding="utf-8") == "body\n"


async def test_move_note_notice_only_when_on_and_matching(vault, monkeypatch):
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement, *a, **k):
            class Empty:
                rowcount = 0

                def all(self):
                    return []

                def first(self):
                    return None

                def scalar_one_or_none(self):
                    return None

            return Empty()

        async def commit(self):
            return None

        def begin_nested(self):
            return self

    async def _noop(_session):
        return None

    monkeypatch.setattr(tools, "async_session", FakeSession)
    monkeypatch.setattr(tools, "_enter_keyword_generation", _noop)

    (vault / "E.md").write_text("e\n", encoding="utf-8")
    _setting(monkeypatch, False)
    off = await tools.move_note_impl("E.md", COPY)
    assert (vault / COPY).exists()
    await tools.move_note_impl(COPY, "E.md")  # back, setting off
    _setting(monkeypatch, True)
    on = await tools.move_note_impl("E.md", COPY)
    # Out of the excluded name: no notice.
    back = await tools.move_note_impl(COPY, "E2.md")
    _setting(monkeypatch, False)
    back_off = await tools.move_note_impl("E2.md", "E3.md")

    assert "Moved E.md → " in off and "\n" not in off
    assert on == off + "\n" + NOTICE_LINE
    assert "\n" not in back and "\n" not in back_off
    assert (vault / "E3.md").read_text(encoding="utf-8") == "e\n"


# ── The dashboard line (panel-ops-health delta) ───────────────────────────

from tests import test_issue_201_dashboard as dash  # noqa: E402


def _route_context(monkeypatch, *, user, on, multi, counts):
    import asyncio

    from src.control_panel import routes

    monkeypatch.setattr(routes.settings, "index_exclude_conflict_files", on)
    monkeypatch.setattr(routes.settings, "multi_user_mode", multi)
    monkeypatch.setattr(indexer, "excluded_conflict_copies", dict(counts))
    captured: dict = {}

    def _template_response(request, name, context):
        captured.update(context)
        return context

    monkeypatch.setattr(
        routes, "_panel_context", lambda request, user, extra=None: dict(extra or {})
    )
    monkeypatch.setattr(routes.templates, "TemplateResponse", _template_response)

    async def _empty(*_a, **_k):
        return {}

    monkeypatch.setattr(routes, "_graph_stats", _empty)
    monkeypatch.setattr(routes, "_health_strip_or_degraded", _empty)
    session = dash._DashboardSession(notes=1, embedded=1, pending=0, truncated=0)
    asyncio.run(routes.dashboard(request=object(), session=session, user=user))
    return captured["excluded_conflicts"]


def test_an_administrator_sees_their_own_vaults_count(monkeypatch):
    counts = {1: 2, 9: 5, None: 7}
    admin = dash._User(1, True)
    assert _route_context(monkeypatch, user=admin, on=True, multi=True, counts=counts) == 2
    tenant = dash._User(9, False)
    assert _route_context(monkeypatch, user=tenant, on=True, multi=True, counts=counts) == 5


def test_single_user_mode_reads_the_single_user_scope(monkeypatch):
    user = dash._User(1, True)
    assert _route_context(
        monkeypatch, user=user, on=True, multi=False, counts={None: 3, 1: 8}
    ) == 3


def test_no_count_when_off_or_not_yet_counted(monkeypatch):
    user = dash._User(4, False)
    assert _route_context(monkeypatch, user=user, on=False, multi=True, counts={4: 2}) is None
    assert _route_context(monkeypatch, user=user, on=True, multi=True, counts={}) is None


def _render_with(**extra) -> str:
    from starlette.templating import Jinja2Templates

    stats = {
        "notes_indexed": 1, "notes_with_embeddings": 1, "embedding_pct": 100,
        "notes_pending": 0, "notes_chunks_truncated": 0, "active_keys": 1,
        "requests_today": 0,
    }
    context = {
        "active": "dashboard", "is_admin": True, "multi_user_mode": False,
        "username": "max", "csrf_token": "csrf", "stats": stats,
        "recent_usage": [], "reindexed_24h": 0, "last_indexed_iso": None,
        "last_indexed_rel": None, "last_run_iso": None, "last_run_rel": None,
        "last_run_ok": True, "index_interval": 300, "graph": {},
        "graph_backfill_running": False,
        "health": {"show_ops": True, "last_run": None, "stale_after_days": 8},
        **extra,
    }
    templates = Jinja2Templates(directory=dash.TEMPLATES_DIR)
    return templates.TemplateResponse(
        request=None, name="dashboard.html", context=context
    ).body.decode()


def test_the_line_renders_singular_plural_and_zero():
    one = _render_with(excluded_conflicts=1)
    assert "· 1 sync-conflict copy not indexed (INDEX_EXCLUDE_CONFLICT_FILES)" in one
    two = _render_with(excluded_conflicts=2)
    assert "· 2 sync-conflict copies not indexed (INDEX_EXCLUDE_CONFLICT_FILES)" in two
    assert "· 0 sync-conflict copies not indexed" in _render_with(excluded_conflicts=0)


def test_off_the_page_is_byte_identical_to_the_page_without_the_key():
    """`None` (setting off, or nothing counted) renders exactly what a context
    without the key renders, which is the pre-change template's output."""
    without = _render_with()
    assert _render_with(excluded_conflicts=None) == without
    assert "sync-conflict" not in without
