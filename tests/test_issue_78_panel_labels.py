"""Regression tests (#78): two panel labels that named the wrong thing.

1. The dashboard's "Last run" was `max(notes_metadata.indexed_at)` — a column
   only written for notes a pass actually upserted or moved. A pass that finds
   no changed hashes writes it nowhere, so on an idle vault a perfectly
   healthy 5-minute indexer reported a last run of hours or days ago. The
   operator's move from there is Settings → Danger zone: a full re-embed that
   costs time and, on the OpenAI provider, money, to fix nothing.

   The fix records the pass itself — an in-process heartbeat set at the end of
   each tick, success and failure distinctly — and keeps `max(indexed_at)` as
   a separately labelled "Last change detected".

2. `usage_logs.tool` recorded "search_notes" for a tool registered as
   `keyword_search` (FastMCP takes the function name), so `/admin/usage`
   named a tool no client is ever offered and `WHERE tool = 'keyword_search'`
   returned nothing. The tell was `_usage_detail` already special-casing both
   spellings.
"""
import asyncio
import inspect
import os
from datetime import datetime, timedelta, timezone

import pydantic_settings

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from starlette.templating import Jinja2Templates

    from src.control_panel import routes
    from src.mcp_server import tools
    from src.services import indexer
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


TEMPLATES_DIR = os.path.join(os.path.dirname(routes.__file__), "templates")


# --- 1a. the heartbeat itself ---------------------------------------------


def test_indexer_exposes_a_run_heartbeat():
    assert hasattr(indexer, "last_index_run_at")
    assert hasattr(indexer, "last_index_run_ok")


def test_record_index_run_stamps_utc_and_outcome(monkeypatch):
    monkeypatch.setattr(indexer, "last_index_run_at", None)
    monkeypatch.setattr(indexer, "last_index_run_ok", None)

    indexer._record_index_run(True)
    first = indexer.last_index_run_at
    assert first is not None and first.tzinfo is not None
    assert indexer.last_index_run_ok is True

    indexer._record_index_run(False)
    assert indexer.last_index_run_ok is False
    assert indexer.last_index_run_at >= first


def test_the_periodic_tick_records_a_run_even_when_nothing_changed(monkeypatch):
    """The scenario the label got wrong: a pass over an untouched vault. It
    writes no `indexed_at` anywhere, and must still count as a run."""
    monkeypatch.setattr(indexer, "last_index_run_at", None)
    monkeypatch.setattr(indexer, "last_index_run_ok", None)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False)
    monkeypatch.setattr(indexer.settings, "index_interval_seconds", 0)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    calls = {"n": 0}

    async def _noop_index(*_a, **_k):
        # An idle pass: finds nothing to do, writes no indexed_at.
        return None

    async def _noop(*_a, **_k):
        return None

    async def _tick_counter(*_a, **_k):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(indexer, "index_vault", _noop_index)
    monkeypatch.setattr(indexer, "embed_vault", _noop)
    monkeypatch.setattr(indexer, "link_backfill_pass", _noop)
    monkeypatch.setattr(indexer, "prewarm_search_caches", _noop)
    # Ends the loop after one full tick.
    monkeypatch.setattr(indexer, "cleanup_expired_tokens", _tick_counter)

    with_cancel = indexer.run_indexer_loop()
    try:
        asyncio.run(with_cancel)
    except asyncio.CancelledError:
        pass

    assert indexer.last_index_run_at is not None
    assert indexer.last_index_run_ok is True


def test_a_failing_tick_is_recorded_as_a_failed_run(monkeypatch):
    monkeypatch.setattr(indexer, "last_index_run_at", None)
    monkeypatch.setattr(indexer, "last_index_run_ok", None)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False)
    monkeypatch.setattr(indexer.settings, "index_interval_seconds", 0)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    state = {"ticks": 0}

    async def _noop(*_a, **_k):
        return None

    async def _boom(*_a, **_k):
        state["ticks"] += 1
        if state["ticks"] == 1:
            # Let the startup pass through, then fail the first real tick.
            return None
        if state["ticks"] > 2:
            raise asyncio.CancelledError
        raise RuntimeError("database is down")

    monkeypatch.setattr(indexer, "index_vault", _boom)
    monkeypatch.setattr(indexer, "embed_vault", _noop)
    monkeypatch.setattr(indexer, "link_backfill_pass", _noop)
    monkeypatch.setattr(indexer, "prewarm_search_caches", _noop)
    monkeypatch.setattr(indexer, "cleanup_expired_tokens", _noop)

    try:
        asyncio.run(indexer.run_indexer_loop())
    except asyncio.CancelledError:
        pass

    assert indexer.last_index_run_at is not None
    assert indexer.last_index_run_ok is False


def test_multi_user_tick_with_a_failing_user_is_not_a_healthy_run(monkeypatch):
    """`_index_pass_once` swallows per-user exceptions so one broken vault
    cannot stop the others — but the tick must not then be stamped `ok=True`.
    Swallow-and-report-healthy is the same "reports fine, is not" defect the
    heartbeat exists to remove."""
    monkeypatch.setattr(indexer, "last_index_run_at", None)
    monkeypatch.setattr(indexer, "last_index_run_ok", None)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", True)
    monkeypatch.setattr(indexer.settings, "index_interval_seconds", 0)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    seen = []
    phase = {"startup": True}

    async def _users(*_a, **_k):
        return [1, 2, 3]

    async def _index(user_id=None, **_k):
        if phase["startup"]:
            return None
        seen.append(user_id)
        if user_id == 2:
            raise RuntimeError("user 2's vault is gone")
        return None

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "_active_user_ids", _users)
    monkeypatch.setattr(indexer, "index_vault", _index)
    monkeypatch.setattr(indexer, "embed_vault", _noop)
    monkeypatch.setattr(indexer, "link_backfill_pass", _noop)
    monkeypatch.setattr(indexer, "prewarm_search_caches", _noop)
    monkeypatch.setattr(indexer, "cleanup_expired_tokens", _noop)

    sleeps = {"n": 0}

    async def _sleep(*_a, **_k):
        # First sleep starts the first periodic tick; the second means that
        # tick finished — and, crucially, that it has already recorded its
        # heartbeat, which is what this test reads.
        sleeps["n"] += 1
        phase["startup"] = False
        if sleeps["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(indexer.asyncio, "sleep", _sleep)

    try:
        asyncio.run(indexer.run_indexer_loop())
    except asyncio.CancelledError:
        pass

    assert seen == [1, 2, 3], "one user's failure must not abort the others"
    assert indexer.last_index_run_ok is False, (
        "a tick that swallowed a per-user failure was stamped healthy"
    )


def test_index_pass_once_reports_each_stage(monkeypatch):
    async def _ok(*_a, **_k):
        return None

    async def _fail(*_a, **_k):
        raise RuntimeError("nope")

    monkeypatch.setattr(indexer, "index_vault", _ok)
    monkeypatch.setattr(indexer, "embed_vault", _ok)
    assert asyncio.run(indexer._index_pass_once(1)) is True

    monkeypatch.setattr(indexer, "index_vault", _fail)
    assert asyncio.run(indexer._index_pass_once(1)) is False

    monkeypatch.setattr(indexer, "index_vault", _ok)
    monkeypatch.setattr(indexer, "embed_vault", _fail)
    assert asyncio.run(indexer._index_pass_once(1)) is False


def test_startup_pass_records_a_run(monkeypatch):
    """Otherwise the dashboard reads "Never" for a whole interval after every
    restart — the same false alarm from the other direction."""
    monkeypatch.setattr(indexer, "last_index_run_at", None)
    monkeypatch.setattr(indexer, "last_index_run_ok", None)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def _noop(*_a, **_k):
        return None

    async def _stop(*_a, **_k):
        raise asyncio.CancelledError

    monkeypatch.setattr(indexer, "index_vault", _noop)
    monkeypatch.setattr(indexer, "embed_vault", _noop)
    monkeypatch.setattr(indexer, "link_backfill_pass", _noop)
    # Cancel while the loop is asleep, i.e. before any tick can record.
    monkeypatch.setattr(indexer.asyncio, "sleep", _stop)

    try:
        asyncio.run(indexer.run_indexer_loop())
    except asyncio.CancelledError:
        pass

    assert indexer.last_index_run_at is not None
    assert indexer.last_index_run_ok is True


# --- 1b. the dashboard reads the heartbeat, not max(indexed_at) -----------


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalars(self):
        outer = self

        class _S:
            def all(self_inner):
                return outer._value if isinstance(outer._value, list) else []

        return _S()

    def fetchall(self):
        return []


class _DashboardSession:
    """Feeds `dashboard` scalars; `max(indexed_at)` is the one that matters."""

    def __init__(self, last_indexed):
        self._last_indexed = last_indexed
        self.n = 0

    async def execute(self, stmt, *_a, **_k):
        self.n += 1
        text = str(stmt).lower()
        if "max(" in text:
            return _Scalar(self._last_indexed)
        if "select" in text and "usage_logs" in text and "count" not in text:
            return _Scalar([])
        return _Scalar(0)


class _Request:
    def __init__(self):
        self.session = {}
        self.scope = {}
        self.query_params = {}


class _AdminUser:
    id = 1
    username = "max"
    is_admin = True
    is_active = True


def _dashboard_context(monkeypatch, last_indexed, last_run, last_run_ok=True):
    captured = {}

    def _fake_response(request, name, context):
        captured["context"] = context
        return None

    monkeypatch.setattr(routes.templates, "TemplateResponse", _fake_response)
    monkeypatch.setattr(routes, "generate_csrf_token", lambda _r: "csrf")
    monkeypatch.setattr(indexer, "last_index_run_at", last_run)
    monkeypatch.setattr(indexer, "last_index_run_ok", last_run_ok)

    async def _graph(*_a, **_k):
        return {}

    monkeypatch.setattr(routes, "_graph_stats", _graph)
    asyncio.run(
        routes.dashboard(
            request=_Request(),
            session=_DashboardSession(last_indexed),
            user=_AdminUser(),
        )
    )
    return captured["context"]


def test_dashboard_last_run_is_the_heartbeat_not_the_newest_note(monkeypatch):
    """The weekend scenario: nothing has changed since Friday, the indexer ran
    a minute ago. "Last run" must say a minute ago."""
    now = datetime.now(timezone.utc)
    ctx = _dashboard_context(
        monkeypatch,
        last_indexed=now - timedelta(days=3),
        last_run=now - timedelta(seconds=30),
    )
    assert ctx["last_run_rel"] == "just now"
    assert "day" in ctx["last_indexed_rel"], "the note-change stat is still reported"
    assert ctx["last_run_iso"] != ctx["last_indexed_iso"]
    assert ctx["last_run_ok"] is True


def test_dashboard_reports_a_failed_last_pass(monkeypatch):
    ctx = _dashboard_context(
        monkeypatch,
        last_indexed=None,
        last_run=datetime.now(timezone.utc),
        last_run_ok=False,
    )
    assert ctx["last_run_ok"] is False


def test_dashboard_before_the_first_pass_says_never(monkeypatch):
    ctx = _dashboard_context(monkeypatch, last_indexed=None, last_run=None)
    assert ctx["last_run_iso"] is None
    assert ctx["last_run_rel"] == "never"


# --- 1c. the template labels -----------------------------------------------


def _render_dashboard(**overrides) -> str:
    context = {
        "active": "dashboard",
        "is_admin": True,
        "multi_user_mode": False,
        "username": "max",
        "csrf_token": "csrf",
        "stats": {
            "notes_indexed": 10,
            "notes_with_embeddings": 10,
            "embedding_pct": 100,
            "active_keys": 1,
            "requests_today": 3,
        },
        "recent_usage": [],
        "reindexed_24h": 0,
        "last_indexed_iso": "2026-08-17T00:00:00+00:00",
        "last_indexed_rel": "3 days ago",
        "last_run_iso": "2026-08-20T12:00:00+00:00",
        "last_run_rel": "just now",
        "last_run_ok": True,
        "index_interval": 300,
        "graph": {},
        "graph_backfill_running": False,
    }
    context.update(overrides)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    response = templates.TemplateResponse(
        request=None,  # dashboard.html never touches `request` directly
        name="dashboard.html",
        context=context,
    )
    return response.body.decode()


def test_last_run_row_renders_the_heartbeat_timestamp():
    html = _render_dashboard()
    idx = html.find(">Last run<")
    assert idx != -1
    window = html[idx : idx + 400]
    assert "2026-08-20T12:00:00+00:00" in window
    assert "2026-08-17T00:00:00+00:00" not in window


def test_last_change_detected_row_exists_and_carries_max_indexed_at():
    html = _render_dashboard()
    idx = html.find(">Last change detected<")
    assert idx != -1, "max(indexed_at) lost its own label"
    window = html[idx : idx + 400]
    assert "2026-08-17T00:00:00+00:00" in window


def test_a_failed_pass_is_visible_on_the_card():
    ok = _render_dashboard()
    failed = _render_dashboard(last_run_ok=False)
    assert "failed" in failed.lower()
    assert failed.lower().count("failed") > ok.lower().count("failed")


# --- 2. the usage log records a tool that exists --------------------------


def test_keyword_search_is_logged_under_its_registered_name():
    from src.mcp_server import server

    registered = server.keyword_search.__name__
    assert registered == "keyword_search"

    src = inspect.getsource(tools)
    assert '@_tracked("keyword_search"' in src
    assert '@_tracked("search_notes"' not in src


def test_usage_detail_still_renders_historical_search_notes_rows():
    """Rows written before the rename keep the old string; the panel must
    still show their query."""
    src = inspect.getsource(routes.dashboard)
    assert '"search_notes"' in src

    # And the behaviour, not just the source: pull `_usage_detail` out of the
    # handler's own code object so the tuple under test is the live one.
    detail = None
    for const in routes.dashboard.__code__.co_consts:
        if getattr(const, "co_name", None) == "_usage_detail":
            detail = const
            break
    assert detail is not None
    tool_tuples = [c for c in detail.co_consts if isinstance(c, tuple)]
    search_tuple = next(t for t in tool_tuples if "semantic_search" in t)
    assert "search_notes" in search_tuple, "historical rows lost their detail"
    assert "keyword_search" in search_tuple
