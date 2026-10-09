"""Absolute OAuth grant lifetime and its disclosure (#326) — the offline half.

The behaviour that *is* the database — rotation inheriting `grant_issued_at`
under the grant lock, concurrent refreshes near the deadline, the migration's
backfill, transfer capabilities dying with their grant — is pinned on real
Postgres in `tests/integration/test_oauth_grant_lifetime_pg.py` and
`tests/integration/test_schema_check.py`. What is here is what can be decided
without one: the setting's domain, the clamp arithmetic and its boundaries,
the AST guard that no mint site relies on the server default, the consent
page's text and CSP cleanliness, and the three non-token-endpoint readers of
the deadline (the panel status, the MCP middleware's reason, the transfer
credential predicate).
"""
import ast
import pathlib
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import Settings
from src.control_panel import routes as panel_routes
from src.limiter import limiter
from src.models.db import OAuthToken
from src.oauth import grants
from src.oauth import routes
from src.services import transfer

ROOT = pathlib.Path(__file__).resolve().parent.parent
UTC = timezone.utc
NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def _settings(**overrides):
    return Settings(
        secret_key="x" * 48,
        database_url="postgresql+asyncpg://u:p@localhost/db",
        vault_path="/tmp",
        **overrides,
    )


@pytest.fixture
def lifetime(monkeypatch):
    """Set the absolute lifetime on the shared settings singleton."""

    def _set(days):
        monkeypatch.setattr(
            grants.settings, "oauth_grant_absolute_lifetime_days", days, raising=False
        )

    _set(90)
    return _set


# ── the setting ────────────────────────────────────────────────────────────


def test_the_default_is_ninety_days():
    assert _settings().oauth_grant_absolute_lifetime_days == 90


@pytest.mark.parametrize("value", [1, 365])
def test_the_bounds_are_accepted(value):
    assert _settings(oauth_grant_absolute_lifetime_days=value).oauth_grant_absolute_lifetime_days == value


@pytest.mark.parametrize("value", [0, 366, -1, "", "null", "none", "None", "off"])
def test_zero_out_of_range_and_every_off_spelling_are_refused(value):
    """Not a `NullableLimit`: an absolute lifetime one blank line removes is
    the defect #326 closes."""
    with pytest.raises(Exception) as excinfo:
        _settings(oauth_grant_absolute_lifetime_days=value)
    assert "oauth_grant_absolute_lifetime_days" in str(excinfo.value)


# ── the deadline and the clamp ─────────────────────────────────────────────


def test_the_deadline_is_issuance_plus_the_current_setting(lifetime):
    assert grants.grant_deadline(NOW) == NOW + timedelta(days=90)
    lifetime(7)
    # Derived at use: shortening the setting moves every family's deadline.
    assert grants.grant_deadline(NOW) == NOW + timedelta(days=7)


def test_a_missing_issuance_time_fails_closed(lifetime):
    assert grants.grant_expired(None, NOW) is True


def test_the_grant_is_dead_at_its_deadline_not_only_after(lifetime):
    deadline = NOW + timedelta(days=90)
    assert grants.grant_expired(NOW, deadline - timedelta(microseconds=1)) is False
    assert grants.grant_expired(NOW, deadline) is True


def test_a_naive_issuance_time_is_read_as_utc(lifetime):
    naive = NOW.replace(tzinfo=None)
    assert grants.grant_deadline(naive) == NOW + timedelta(days=90)


def test_far_from_the_deadline_nothing_is_clamped(lifetime):
    clamped = grants.clamp_token_expiry(NOW, NOW + timedelta(days=60))
    assert clamped.access_expires_at == NOW + timedelta(hours=1)
    assert clamped.refresh_expires_at == NOW + timedelta(days=30)
    assert clamped.expires_in == 3600


def test_two_days_before_the_deadline_the_refresh_token_is_clamped(lifetime):
    deadline = NOW + timedelta(days=2)
    clamped = grants.clamp_token_expiry(NOW, deadline)
    assert clamped.refresh_expires_at == deadline
    assert clamped.access_expires_at == NOW + timedelta(hours=1)
    assert clamped.expires_in == 3600


def test_inside_the_last_hour_both_are_clamped_and_expires_in_says_so(lifetime):
    deadline = NOW + timedelta(minutes=30, milliseconds=900)
    clamped = grants.clamp_token_expiry(NOW, deadline)
    assert clamped.access_expires_at == deadline
    assert clamped.refresh_expires_at == deadline
    assert clamped.expires_in == 1800  # rounded down


@pytest.mark.parametrize(
    "remaining",
    [timedelta(0), timedelta(milliseconds=999), timedelta(seconds=-5)],
)
def test_under_a_second_left_refuses(remaining, lifetime):
    assert grants.clamp_token_expiry(NOW, NOW + remaining) is None


def test_exactly_one_second_left_mints_a_one_second_token(lifetime):
    clamped = grants.clamp_token_expiry(NOW, NOW + timedelta(seconds=1))
    assert clamped.expires_in == 1


# ── consent disclosure ─────────────────────────────────────────────────────


def test_default_lifetimes(lifetime):
    assert grants.consent_lifetimes() == {
        "access": "1 hour",
        "refresh": "30 days",
        "absolute": "90 days",
    }


def test_a_short_policy_is_never_overstated(lifetime):
    lifetime(7)
    assert grants.consent_lifetimes() == {
        "access": "1 hour",
        "refresh": "7 days",
        "absolute": "7 days",
    }
    lifetime(1)
    assert grants.consent_lifetimes()["refresh"] == "1 day"
    assert grants.consent_lifetimes()["absolute"] == "1 day"


class _FakeClient:
    client_id = "a3f19c7e5b2d4081a3f19c7e5b2d4081"
    client_name = "Test Client"
    scope = "read readwrite offline_access"
    redirect_uris = ["https://claude.ai/api/mcp/auth_callback"]
    created_at = datetime(2026, 3, 14, tzinfo=UTC)


class _FakeResult:
    def scalar_one_or_none(self):
        return _FakeClient()


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        return _FakeResult()


def _consent_html(monkeypatch) -> str:
    monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(routes, "async_session", lambda: _FakeSession())
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(routes.router)
    response = TestClient(app).get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": _FakeClient.client_id,
            "redirect_uri": _FakeClient.redirect_uris[0],
            "code_challenge": "a" * 43,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 200, response.text
    return response.text


def _lifetime_block(html: str) -> str:
    match = re.search(r'<div class="lifetime-box">(.*?)</div>', html, re.S)
    assert match, "the lifetime block is missing from the consent page"
    return " ".join(match.group(1).split())


def test_the_consent_page_states_the_default_lifetimes(monkeypatch, lifetime):
    block = _lifetime_block(_consent_html(monkeypatch))
    assert "access token valid for 1 hour" in block
    assert "refresh token valid for 30 days from its last renewal" in block
    assert "Renewal stops 90 days after the application first receives its tokens" in block
    assert "revoke this access at any time from the control panel" in block


def test_the_consent_page_anchors_the_period_to_token_issuance_not_approval(
    monkeypatch, lifetime
):
    """Codex spec review: `grant_issued_at` is the code exchange, so the page
    must not promise a period counted from the approval click."""
    block = _lifetime_block(_consent_html(monkeypatch))
    assert "days after you approve" not in block


def test_a_seven_day_policy_shows_no_longer_period(monkeypatch, lifetime):
    lifetime(7)
    block = _lifetime_block(_consent_html(monkeypatch))
    assert "refresh token valid for 7 days" in block
    assert "Renewal stops 7 days after" in block
    assert "30 days" not in block
    assert "90 days" not in block


def test_the_consent_page_is_csp_clean(monkeypatch, lifetime):
    html = _consent_html(monkeypatch)
    # Attributes only — the nonce'd script body legitimately assigns
    # `onSystemChange = …` in JavaScript.
    for tag in re.findall(r"<[a-zA-Z][^>]*>", html):
        assert not re.search(r"\sstyle\s*=", tag, re.I), tag
        assert not re.search(r"\son[a-z]+\s*=", tag, re.I), tag
    # Every inline block carries the nonce.
    for tag in re.findall(r"<(?:style|script)\b[^>]*>", html, re.I):
        assert "nonce=" in tag, tag


# ── no mint site may rely on the server default ───────────────────────────


def _oauth_token_constructions():
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "OAuthToken":
                yield path, node


def test_every_oauth_token_construction_sets_grant_issued_at_explicitly():
    """The column's server default exists only for a rolling deploy's old
    image. A new mint site that relied on it would restart its family's clock
    on every rotation — #326 reintroduced silently."""
    found = list(_oauth_token_constructions())
    assert len(found) >= 4, found  # two at the exchange, two at the rotation
    for path, call in found:
        keywords = {kw.arg for kw in call.keywords}
        assert "grant_issued_at" in keywords, (
            f"{path.relative_to(ROOT)}:{call.lineno} constructs OAuthToken without "
            "an explicit grant_issued_at"
        )


def test_rotation_copies_issuance_from_the_locked_row():
    """Both rotation-site constructions read `old_token.grant_issued_at` — the
    row re-read under the grant lock — and never a fresh clock."""
    source = (ROOT / "src" / "oauth" / "routes.py").read_text()
    tree = ast.parse(source)
    handler = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_refresh"
    )
    values = [
        ast.unparse(kw.value)
        for call in ast.walk(handler)
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "OAuthToken"
        for kw in call.keywords
        if kw.arg == "grant_issued_at"
    ]
    assert values == ["old_token.grant_issued_at", "old_token.grant_issued_at"]


# ── the other readers of the deadline ─────────────────────────────────────


def _row(**overrides):
    base = dict(
        revoked=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        grant_issued_at=datetime.now(UTC),
        scope="readwrite",
        user_id=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_panel_shows_a_grant_past_its_deadline_as_expired(lifetime):
    now = datetime.now(UTC)
    live = _row()
    aged = _row(grant_issued_at=now - timedelta(days=91))
    assert panel_routes._token_status(live, now, True) == "active"
    assert panel_routes._token_status(aged, now, True) == "expired"


def test_a_shortened_policy_reaches_the_panel_at_once(lifetime):
    now = datetime.now(UTC)
    row = _row(grant_issued_at=now - timedelta(days=10))
    assert panel_routes._token_status(row, now, True) == "active"
    lifetime(7)
    assert panel_routes._token_status(row, now, True) == "expired"


def _oauth_cred(**overrides):
    values = dict(
        id=41,
        token_hash="t" * 64,
        token_type="access",
        client_id="c",
        scope="readwrite",
        grant_id="g",
        user_id=3,
        revoked=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        grant_issued_at=datetime.now(UTC),
    )
    values.update(overrides)
    return OAuthToken(**values)


def test_transfer_credential_expiry_is_the_earlier_of_token_and_grant(lifetime):
    now = datetime.now(UTC)
    cred = _oauth_cred(grant_issued_at=now - timedelta(days=90) + timedelta(minutes=5))
    expiry = transfer.credential_expires_at(cred)
    assert expiry == cred.grant_issued_at + timedelta(days=90)
    assert expiry < cred.expires_at


def test_transfer_refuses_an_oauth_credential_past_its_grant_deadline(lifetime):
    now = datetime.now(UTC)
    row = SimpleNamespace(user_id=3)
    live = _oauth_cred()
    aged = _oauth_cred(grant_issued_at=now - timedelta(days=91))
    assert transfer._credential_ok(live, need_write=True, row=row) is True
    assert transfer._credential_ok(aged, need_write=True, row=row) is False
    lifetime(365)
    # Raising the setting extends the grant (accepted L1).
    assert transfer._credential_ok(aged, need_write=True, row=row) is True
