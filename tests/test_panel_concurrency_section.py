"""The `/admin/performance` concurrency section (#188, design D6), through the
real app and the real template.

- An administrator sees the per-tool/class table plus the admin card: live
  mode, epoch, limits and pool demand, the durable counters, coverage gaps
  with the watermark, and the readiness verdict.
- A regular user sees only their own per-tool/class rows; no live occupancy,
  counter, pool gauge or verdict is computed, let alone rendered.
- Both render under the enforced nonce CSP with no `style` attribute, inline
  handler or un-nonced script/style.
- An empty window renders an explicit empty state.
"""
from __future__ import annotations

import datetime as dt
import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from src import main
from src.config import settings
from src.control_panel import routes as panel_routes
from src.database import get_session
from src.limiter import limiter
from src.models.db import User
from src.services import concurrency_readiness as cr
from src.services.concurrency_counters import Coverage
from tests.test_panel_csp_headers import BODY_NONCE_RE, _nonce_of, _Session, _style_attributes_in

UTC = dt.timezone.utc
# Relative to the wall clock: the panel only lists gaps between the page
# window's start (now − 24 h) and the watermark, so a fixed date stops
# rendering the gap a day after it.
WATERMARK = (dt.datetime.now(UTC) - dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
GAP_START = WATERMARK - dt.timedelta(hours=9)


def _user(admin: bool) -> User:
    user = User(username="admin" if admin else "regular", password_hash="x",
                is_admin=admin, is_active=True, vault_path="/tmp/test-vault")
    user.id = 1 if admin else 7
    user.session_version = 1
    return user


def _row(tool, cls, **kw):
    base = {"tool": tool, "cls": cls, "executed": 12, "tool_pressured": 3, "overruns": 0,
            "slot_timeouts": 0, "p50": 1.0, "p95": 9.0, "p99": 14.0, "max": 20.0}
    base.update(kw)
    return base


@pytest.fixture
def render(monkeypatch):
    """GET /admin/performance as an admin or a regular user, with the
    section's database reads replaced by populated fixtures."""
    calls = {"table_user_ids": [], "admin_reads": 0}

    async def fake_table(session, start, end, *, user_id=None, epoch=None, mode=None):
        calls["table_user_ids"].append(user_id)
        if calls.get("empty"):
            return {"tools": [], "classes": [], "total": None}
        return {
            "tools": [_row("read_note", "light", overruns=2),
                      _row("keyword_search", "scan", slot_timeouts=5)],
            "classes": [_row(None, "light", overruns=2), _row(None, "scan", slot_timeouts=5)],
            "total": _row(None, None),
        }

    async def fake_counters(session, start, end, *, epoch=None, mode=None):
        calls["admin_reads"] += 1
        return ({"requests": 420, "transport_pressured": 17, "pool_checkout_timeout": 1},
                {"pool_high_water": 11, "transport_wait_max_ms": 35})

    async def fake_coverage(session, start, end, epoch, mode):
        calls["admin_reads"] += 1
        gap = (GAP_START, GAP_START + dt.timedelta(seconds=40))
        return Coverage(WATERMARK, [gap] if end > start else [])

    async def fake_readiness(session, target, *, days=None, end=None, controller=None):
        calls["admin_reads"] += 1
        calls["verdict_days"] = days
        return {
            "target": target, "source_mode": "shadow", "epoch": "0123456789ab",
            "overall": "INSUFFICIENT_DATA",
            "window": {"start": "2026-09-20T11:59:00+00:00",
                       "end": "2026-09-23T11:59:00+00:00", "days": 3.0},
            "last_72h": None,
            "criteria": [
                {"id": cid, "description": cr.DESCRIPTIONS[cid],
                 "verdict": "INSUFFICIENT_DATA",
                 "reason": "window not covered: uncovered 2026-09-23T03:00:00+00:00 for 40 s",
                 "inputs": {}}
                for cid in ("Q1", "Q2", "Q3")
            ],
        }

    monkeypatch.setattr(cr, "tool_table", fake_table)
    monkeypatch.setattr(cr, "counter_totals", fake_counters)
    monkeypatch.setattr(cr, "_coverage", fake_coverage)
    monkeypatch.setattr(cr, "readiness", fake_readiness)
    monkeypatch.setattr(settings, "panel_csp", "enforce")
    monkeypatch.setattr(settings, "multi_user_mode", True)

    overrides = dict(main.app.dependency_overrides)

    def get(admin: bool, *, empty=False, window="24h"):
        calls["empty"] = empty
        user = _user(admin)

        async def _get_session():
            yield _Session({User: user})

        async def _panel_user():
            return user

        main.app.dependency_overrides[get_session] = _get_session
        main.app.dependency_overrides[panel_routes.require_user_panel] = _panel_user
        limiter.reset()
        client = TestClient(main.app, base_url="https://localhost")
        response = client.get(f"/admin/performance?window={window}", follow_redirects=False)
        assert response.status_code == 200, response.text[:300]
        return response

    try:
        yield get, calls
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(overrides)
        limiter.reset()


class _HandlerFinder(HTMLParser):
    """Every element attribute that is an inline handler or a `javascript:` URL."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.offenders: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name.startswith("on") or (value or "").strip().lower().startswith("javascript:"):
                self.offenders.append(f"<{tag} {name}={value!r}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def _assert_csp_clean(response):
    body = response.text
    nonce = _nonce_of(response.headers["content-security-policy"])
    assert _style_attributes_in(body) == []
    finder = _HandlerFinder()
    finder.feed(body)
    finder.close()
    assert finder.offenders == []
    for tag in re.findall(r"<(?:script|style)\b[^>]*>", body):
        assert f'nonce="{nonce}"' in tag, tag
    assert set(BODY_NONCE_RE.findall(body)) == {nonce}


def test_admin_sees_the_table_and_the_admin_card(render):
    get, calls = render
    response = get(admin=True)
    body = response.text
    _assert_csp_clean(response)

    assert "Concurrency pressure" in body
    assert "read_note" in body and "keyword_search" in body
    assert calls["table_user_ids"][-1] is None  # an admin sees every row

    assert "Concurrency control" in body
    # Live controller snapshot: mode, epoch, pool demand, limits, occupancy.
    from src.services import concurrency
    snap = concurrency.get_controller().snapshot()
    assert snap["epoch"] in body
    assert "Pool demand" in body and f"{snap['pool_demand']['total']} of" in body
    assert "Active now" in body and "Waiting now" in body
    assert "Effective limits" in body and "tools=" in body
    # Durable counters and gauges.
    assert "transport_pressured" in body and "pool_checkout_timeout" in body
    assert "pool_high_water (max)" in body
    # Coverage: the gap's start and length, and the watermark.
    assert "Durable watermark" in body and WATERMARK.isoformat() in body
    assert "uncovered from" in body and "40 s" in body
    # The verdict, every criterion INSUFFICIENT_DATA for a gapped window.
    assert "Next mode: queue" in body
    assert body.count("INSUFFICIENT_DATA") >= 4


def test_the_verdict_window_is_at_least_the_page_window(render):
    get, calls = render
    get(admin=True, window="30d")
    assert calls["verdict_days"] == 30
    get(admin=True, window="24h")
    assert calls["verdict_days"] == cr.QUEUE_MIN_DAYS


def test_a_regular_user_sees_only_their_rows_and_nothing_admin_only(render):
    get, calls = render
    response = get(admin=False)
    body = response.text
    _assert_csp_clean(response)

    assert "Concurrency pressure" in body and "read_note" in body
    assert calls["table_user_ids"][-1] == 7
    assert calls["admin_reads"] == 0
    from src.services import concurrency
    epoch = concurrency.get_controller().epoch
    for forbidden in ("Concurrency control", epoch, "Pool demand", "Active now",
                      "Durable watermark", "pool_high_water", "pool_checkout_timeout",
                      "Readiness", "INSUFFICIENT_DATA", "Effective limits"):
        assert forbidden not in body, forbidden


def test_an_empty_window_renders_an_explicit_empty_state(render):
    get, _ = render
    response = get(admin=False, empty=True)
    body = response.text
    assert "No calls carrying concurrency provenance were logged in this window" in " ".join(body.split())
    assert "Tool-pressured" not in body  # no table of zeroes
    _assert_csp_clean(response)


def test_the_real_section_renders_with_an_empty_database(monkeypatch):
    """No fakes inside the section: the real queries against a database that
    returns nothing still render the admin card (INSUFFICIENT_DATA)."""
    monkeypatch.setattr(settings, "panel_csp", "enforce")
    monkeypatch.setattr(settings, "multi_user_mode", True)
    user = _user(True)

    async def _get_session():
        yield _Session({User: user})

    async def _panel_user():
        return user

    overrides = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[get_session] = _get_session
    main.app.dependency_overrides[panel_routes.require_user_panel] = _panel_user
    try:
        limiter.reset()
        response = TestClient(main.app, base_url="https://localhost").get(
            "/admin/performance", follow_redirects=False)
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(overrides)
        limiter.reset()
    assert response.status_code == 200
    body = response.text
    assert "No calls carrying concurrency provenance" in body
    assert "Concurrency control" in body
    assert "no run recorded for this mode and epoch" in body
    _assert_csp_clean(response)
