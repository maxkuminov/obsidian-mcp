"""#309 D5: the panel's "Confirm vault is empty" action.

The confirmation page states the row count and issues a token bound to the
action, the scope, the issuing administrator, the scope's assignment, the
process epoch and a single-use nonce. Redemption grants exactly one
empty-prune permission for the posted scope and starts exactly one
scope-targeted pass (`indexer.index_scope_now`), or refuses with nothing
granted and nothing started.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import re

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from src import main
from src.config import settings
from src.control_panel import empty_vault_confirm as evc
from src.control_panel import routes as panel_routes
from src.control_panel import users as users_routes
from src.control_panel.flash import FLASH_SESSION_KEY
from src.csrf import verify_csrf
from src.database import get_session
from src.limiter import limiter
from src.models.db import User
from src.services import empty_prune, indexer, vault_overlap


# ── fakes ───────────────────────────────────────────────────────────────────


def _user(uid, *, admin=False, active=True, vault="/vaults/u"):
    u = User(
        username=f"user{uid}",
        password_hash="x",
        is_admin=admin,
        is_active=active,
        vault_path=vault,
    )
    u.id = uid
    u.session_version = 1
    return u


class _Result:
    rowcount = 0

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _Session:
    """Answers `select(User).where(id=…)` and the note count per scope."""

    def __init__(self, users=(), counts=None, strict=True):
        self.users = {u.id: u for u in users}
        self.counts = counts or {}
        self.strict = strict
        self.statements = []

    async def execute(self, stmt, *a, **k):
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.statements.append(sql)
        if "count(notes_metadata.id)" in sql:
            m = re.search(r"notes_metadata.user_id = (\d+)", sql)
            scope = int(m.group(1)) if m else None
            return _Result(self.counts.get(scope, 0))
        if sql.startswith("SELECT users."):
            m = re.search(r"users.id = (\d+)", sql)
            return _Result(self.users.get(int(m.group(1))) if m else None)
        if not self.strict:
            return _Loose()
        raise AssertionError(f"unexpected statement: {sql}")


class _Loose(_Result):
    """Whatever the other panel pages ask, answered empty."""

    def __init__(self):
        super().__init__(None)

    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None

    def __iter__(self):
        return iter([])


def _request(method="GET"):
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/admin/x",
            "headers": [],
            "query_string": b"",
            "session": {},
            "state": {},
        }
    )


def _flash(request):
    entry = request.session.get(FLASH_SESSION_KEY) or {}
    return entry.get("message"), entry.get("kind")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    empty_prune.reset()
    evc.reset()
    spawned = []
    passes = []

    def _spawn(coro):
        # Kept, not run: the handler is itself inside `asyncio.run`, so a
        # test drives the coroutine afterwards with `_drain`.
        spawned.append(coro)

    async def _fake_index_scope_now(user_id, *, trigger="manual"):
        passes.append(user_id)
        return True

    async def _no_reindex(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("_reindex_background must not be called")

    monkeypatch.setattr(panel_routes, "_spawn", _spawn)
    monkeypatch.setattr(indexer, "index_scope_now", _fake_index_scope_now)
    monkeypatch.setattr(panel_routes, "_reindex_background", _no_reindex)
    yield {"spawned": spawned, "passes": passes}
    for coro in spawned:
        coro.close()
    empty_prune.reset()
    evc.reset()


def _run(coro):
    return asyncio.run(coro)


def _drain(state):
    while state["spawned"]:
        _run(state["spawned"].pop(0))


# ── multi-user helpers ──────────────────────────────────────────────────────


ADMIN_X = _user(1, admin=True, vault="/vaults/x")
ADMIN_Y = _user(2, admin=True, vault="/vaults/y")


@pytest.fixture
def mu(monkeypatch):
    monkeypatch.setattr(settings, "multi_user_mode", True)


def _issue_for(scope, admin, assignment):
    return evc.issue_token(scope, admin.id, assignment)


def _post_mu(session, admin, target_id, token):
    req = _request("POST")
    resp = _run(
        users_routes.confirm_empty_vault(
            target_id, req, token=token, session=session, user=admin
        )
    )
    return req, resp


def _granted():
    return dict(empty_prune._registry)


# ── the page ────────────────────────────────────────────────────────────────


def test_admin_page_states_the_row_count_and_carries_a_token(mu):
    alice = _user(10, vault="/vaults/alice")
    session = _Session([alice], counts={10: 50})
    resp = _run(
        users_routes.confirm_empty_vault_page(
            10, _request(), session=session, user=ADMIN_X
        )
    )
    body = resp.body.decode()
    assert resp.status_code == 200
    assert ">50<" in body.replace("\n", "").replace(" ", "")
    assert 'name="token"' in body
    assert 'action="/admin/users/10/confirm-empty-vault"' in body
    # CSP: no inline style or handler, every script/style nonced.
    assert not re.search(r"\sstyle\s*=", body)
    markup = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "", body)
    for tag in re.findall(r"<[a-zA-Z][^>]*>", markup):
        assert not re.search(r"\son[a-z]+\s*=", tag, flags=re.I), tag
    for tag in re.findall(r"<(?:script|style)\b[^>]*>", body):
        assert 'nonce="' in tag, tag
    # The final control fails closed.
    assert re.search(r'<button type="button"[^>]*\n?[^>]*data-confirm=', body)
    # Issuing grants nothing.
    assert _granted() == {}


def test_single_user_page_states_the_row_count(monkeypatch):
    monkeypatch.setattr(settings, "multi_user_mode", False)
    session = _Session(counts={None: 7})
    resp = _run(
        panel_routes.confirm_empty_vault_page(
            _request(), session=session, user=ADMIN_X
        )
    )
    body = resp.body.decode()
    assert "7" in body and 'name="token"' in body
    assert 'action="/admin/settings/confirm-empty-vault"' in body


def test_zero_rows_says_nothing_to_confirm(mu):
    alice = _user(10)
    resp = _run(
        users_routes.confirm_empty_vault_page(
            10, _request(), session=_Session([alice], counts={}), user=ADMIN_X
        )
    )
    body = resp.body.decode()
    assert "Nothing to confirm" in body
    assert 'name="token"' not in body


def test_ineligible_page_issues_no_token(mu):
    bob = _user(11, active=False)
    resp = _run(
        users_routes.confirm_empty_vault_page(
            11, _request(), session=_Session([bob], counts={11: 3}), user=ADMIN_X
        )
    )
    body = resp.body.decode()
    assert "inactive" in body and 'name="token"' not in body


# ── non-admins ──────────────────────────────────────────────────────────────


@pytest.fixture
def client_as(monkeypatch):
    overrides = dict(main.app.dependency_overrides)

    def make(user, multi_user):
        monkeypatch.setattr(settings, "multi_user_mode", multi_user)

        async def _get_session():
            yield _Session([user], counts={None: 5, user.id: 5}, strict=False)

        async def _panel_user():
            return user

        async def _no_csrf():
            return None

        main.app.dependency_overrides[get_session] = _get_session
        main.app.dependency_overrides[panel_routes.require_user_panel] = _panel_user
        main.app.dependency_overrides[verify_csrf] = _no_csrf
        limiter.reset()
        return TestClient(main.app, base_url="https://localhost")

    yield make
    main.app.dependency_overrides.clear()
    main.app.dependency_overrides.update(overrides)
    limiter.reset()


@pytest.mark.parametrize(
    "multi_user,path",
    [
        (False, "/admin/settings/confirm-empty-vault"),
        (True, "/admin/users/3/confirm-empty-vault"),
    ],
)
def test_non_admin_is_refused(client_as, multi_user, path, _clean):
    client = client_as(_user(3, admin=False), multi_user)
    assert client.get(path, follow_redirects=False).status_code == 403
    token = evc.issue_token(None if not multi_user else 3, 3, "/vaults/u")
    resp = client.post(path, data={"token": token}, follow_redirects=False)
    assert resp.status_code == 403
    assert _granted() == {}
    assert _clean["spawned"] == []


def test_settings_control_and_user_edit_control_render_for_admin(client_as, monkeypatch):
    # The settings page probes the embedding provider; keep it offline.
    probed: list[str] = []

    class _Resp:
        status_code = 200

    class _Client:
        async def get(self, url, *a, **k):
            probed.append(url)
            return _Resp()

    @contextlib.asynccontextmanager
    async def _offline_client(*_a, **_k):
        yield _Client()

    monkeypatch.setattr(panel_routes.settings, "embedding_provider", "ollama")
    monkeypatch.setattr(panel_routes, "embedding_http_client", _offline_client)
    client = client_as(_user(1, admin=True), False)
    body = client.get("/admin/settings").text
    assert 'href="/admin/settings/confirm-empty-vault"' in body
    client = client_as(_user(1, admin=True), True)
    body = client.get("/admin/settings").text
    # Multi-user mode confirms per user, not from settings.
    assert "/admin/settings/confirm-empty-vault" not in body
    body = client.get("/admin/users/1/edit").text
    assert 'href="/admin/users/1/confirm-empty-vault"' in body
    assert probed and all(u.endswith("/api/tags") for u in probed), (
        "every settings render went through the offline stub"
    )


def test_settings_page_shows_the_native_tei_provider(client_as, monkeypatch):
    """A TEI deployment reports its own URL and probes `/health`, not an
    Ollama endpoint it does not serve."""
    probed: list[str] = []

    class _Resp:
        status_code = 200

    class _Client:
        async def get(self, url, *a, **k):
            probed.append(url)
            return _Resp()

    @contextlib.asynccontextmanager
    async def _offline_client(*_a, **_k):
        yield _Client()

    monkeypatch.setattr(panel_routes.settings, "embedding_provider", "tei")
    monkeypatch.setattr(panel_routes.settings, "tei_url", "http://tei:80")
    monkeypatch.setattr(panel_routes.settings, "tei_embedding_model", "BAAI/bge-m3")
    monkeypatch.setattr(panel_routes, "embedding_http_client", _offline_client)

    body = client_as(_user(1, admin=True), False).get("/admin/settings").text

    assert "BAAI/bge-m3" in body
    assert "http://tei:80" in body
    assert probed == ["http://tei:80/health"]


# ── redemption ──────────────────────────────────────────────────────────────


def test_valid_token_grants_one_permission_and_starts_one_targeted_pass(mu, _clean):
    alice = _user(10, vault="/vaults/alice")
    other = _user(12, vault="/vaults/other")
    token = _issue_for(10, ADMIN_X, "/vaults/alice")
    req, resp = _post_mu(_Session([alice, other], counts={10: 50}), ADMIN_X, 10, token)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/users/10/edit"
    granted = _granted()
    assert list(granted) == [10]
    assert granted[10].assignment == "/vaults/alice"
    assert len(_clean["spawned"]) == 1
    # Driving the one spawned task runs exactly one targeted pass, for 10
    # alone; `_reindex_background` (patched to raise) is never reached.
    _drain(_clean)
    assert _clean["passes"] == [10]
    _, kind = _flash(req)
    assert kind != "err"


def test_the_spawned_coroutine_runs_index_scope_now_for_the_scope(mu, monkeypatch):
    calls = []

    async def fake(user_id, *, trigger="manual"):
        calls.append((user_id, trigger))
        return True

    monkeypatch.setattr(indexer, "index_scope_now", fake)
    _run(evc._scope_pass(10))
    assert calls == [(10, "manual")]


def test_the_scope_pass_swallows_and_logs_an_exception(monkeypatch, caplog):
    async def boom(user_id, *, trigger="manual"):
        raise RuntimeError("x")

    monkeypatch.setattr(indexer, "index_scope_now", boom)
    _run(evc._scope_pass(None))
    assert "Confirm-vault-is-empty pass failed" in caplog.text


def test_single_user_valid_token_grants_the_none_scope(monkeypatch, _clean):
    monkeypatch.setattr(settings, "multi_user_mode", False)
    monkeypatch.setattr(settings, "vault_path", "/obsidian/")
    assignment = "/obsidian"  # canonical_vault_root strips the slash
    token = evc.issue_token(None, None, assignment)
    sentinel = type("S", (), {"id": None, "username": "admin", "is_admin": True})()
    req = _request("POST")
    resp = _run(
        panel_routes.confirm_empty_vault(
            req, token=token, session=_Session(counts={None: 3}), user=sentinel
        )
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/settings"
    assert list(_granted()) == [None]
    assert _granted()[None].assignment == assignment
    assert len(_clean["spawned"]) == 1
    _drain(_clean)
    assert _clean["passes"] == [None]


def _assert_refused(req, resp, _clean, location="/admin/users/10/edit"):
    assert resp.status_code == 303
    assert resp.headers["location"] == location
    assert _granted() == {}
    assert _clean["spawned"] == []
    msg, kind = _flash(req)
    assert kind == "err" and msg


def test_replayed_token_is_refused(mu, _clean):
    alice = _user(10, vault="/vaults/alice")
    token = _issue_for(10, ADMIN_X, "/vaults/alice")
    _post_mu(_Session([alice]), ADMIN_X, 10, token)
    empty_prune.reset()
    _drain(_clean)
    req, resp = _post_mu(_Session([alice]), ADMIN_X, 10, token)
    _assert_refused(req, resp, _clean)
    assert "already used" in _flash(req)[0]


def test_expired_token_is_refused(mu, monkeypatch, _clean):
    alice = _user(10, vault="/vaults/alice")
    now = [1_000_000.0]
    monkeypatch.setattr(evc, "_clock", lambda: now[0])
    token = _issue_for(10, ADMIN_X, "/vaults/alice")
    now[0] += evc.TOKEN_MAX_AGE_SECONDS + 2
    req, resp = _post_mu(_Session([alice]), ADMIN_X, 10, token)
    _assert_refused(req, resp, _clean)
    assert "expired" in _flash(req)[0]


def test_token_within_ten_minutes_is_accepted(mu, monkeypatch, _clean):
    alice = _user(10, vault="/vaults/alice")
    now = [1_000_000.0]
    monkeypatch.setattr(evc, "_clock", lambda: now[0])
    token = _issue_for(10, ADMIN_X, "/vaults/alice")
    now[0] += evc.TOKEN_MAX_AGE_SECONDS - 5
    _post_mu(_Session([alice]), ADMIN_X, 10, token)
    assert list(_granted()) == [10]


def test_token_for_user_a_posted_for_user_b_is_refused(mu, _clean):
    alice = _user(10, vault="/vaults/same")
    bob = _user(11, vault="/vaults/same")
    token = _issue_for(10, ADMIN_X, "/vaults/same")
    req, resp = _post_mu(_Session([alice, bob]), ADMIN_X, 11, token)
    _assert_refused(req, resp, _clean, "/admin/users/11/edit")


def test_token_issued_by_admin_x_redeemed_by_admin_y_is_refused(mu, _clean):
    alice = _user(10, vault="/vaults/alice")
    token = _issue_for(10, ADMIN_X, "/vaults/alice")
    req, resp = _post_mu(_Session([alice]), ADMIN_Y, 10, token)
    _assert_refused(req, resp, _clean)


def test_token_issued_before_an_assignment_change_is_refused(mu, _clean):
    alice = _user(10, vault="/vaults/new")
    token = _issue_for(10, ADMIN_X, "/vaults/old")
    req, resp = _post_mu(_Session([alice]), ADMIN_X, 10, token)
    _assert_refused(req, resp, _clean)


def test_token_from_a_previous_process_epoch_is_refused(mu, monkeypatch, _clean):
    alice = _user(10, vault="/vaults/alice")
    token = _issue_for(10, ADMIN_X, "/vaults/alice")
    monkeypatch.setattr(evc, "PROCESS_EPOCH", "a-new-process")
    req, resp = _post_mu(_Session([alice]), ADMIN_X, 10, token)
    _assert_refused(req, resp, _clean)


@pytest.mark.parametrize("kind", ["garbage", "other-salt", "reembed", "csrf", "wrong-action"])
def test_forged_or_other_salt_token_is_refused(mu, _clean, kind):
    from itsdangerous import URLSafeTimedSerializer

    alice = _user(10, vault="/vaults/alice")
    payload = {
        "a": evc.ACTION, "s": 10, "i": 1, "v": "/vaults/alice",
        "e": evc.PROCESS_EPOCH, "n": "abc",
    }
    if kind == "garbage":
        token = "not-a-token"
    elif kind == "other-salt":
        token = URLSafeTimedSerializer(settings.secret_key, salt="x").dumps(payload)
    elif kind == "reembed":
        token = panel_routes._reembed_serializer().dumps(payload)
    elif kind == "csrf":
        token = URLSafeTimedSerializer(settings.secret_key, salt="csrf-token").dumps(payload)
    else:
        token = evc._serializer().dumps({**payload, "a": "reembed"})
    req, resp = _post_mu(_Session([alice]), ADMIN_X, 10, token)
    _assert_refused(req, resp, _clean)


def test_empty_token_is_refused(mu, _clean):
    alice = _user(10, vault="/vaults/alice")
    req, resp = _post_mu(_Session([alice]), ADMIN_X, 10, "")
    _assert_refused(req, resp, _clean)


# ── eligibility ─────────────────────────────────────────────────────────────


def _quarantine(uid):
    vault_overlap.publish_synthetic_snapshot(
        [
            vault_overlap.QuarantineEntry(
                user_id=uid,
                username=f"user{uid}",
                assignment="/vaults/alice",
                reason=vault_overlap.RootUnexaminable(cause=2),
                detected_at=datetime.datetime.now(datetime.timezone.utc),
            )
        ]
    )


@pytest.mark.parametrize("case", ["inactive", "unassigned", "quarantined", "deleted", "unchecked"])
def test_ineligible_target_is_refused(mu, _clean, case):
    alice = _user(
        10,
        vault=None if case == "unassigned" else "/vaults/alice",
        active=case != "inactive",
    )
    users = [] if case == "deleted" else [alice]
    token = _issue_for(10, ADMIN_X, "/vaults/alice")
    if case == "quarantined":
        _quarantine(10)
    if case == "unchecked":
        vault_overlap.reset_snapshot_state()
    req, resp = _post_mu(_Session(users), ADMIN_X, 10, token)
    _assert_refused(req, resp, _clean)


def test_single_user_route_refuses_in_multi_user_mode(mu, _clean):
    token = evc.issue_token(None, ADMIN_X.id, "/obsidian")
    req = _request("POST")
    resp = _run(
        panel_routes.confirm_empty_vault(
            req, token=token, session=_Session(), user=ADMIN_X
        )
    )
    _assert_refused(req, resp, _clean, "/admin/settings")


# ── the token helper itself ─────────────────────────────────────────────────


def test_salt_is_distinct_from_every_other_serializer():
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src"
    salts = []
    for path in src.rglob("*.py"):
        salts += re.findall(r"salt\s*=\s*[\"']([^\"']+)[\"']", path.read_text())
    # Every literal salt elsewhere (csrf-token, reembed-confirm, …) differs.
    assert salts and evc.SALT not in salts
    assert len(set(salts)) == len(salts)


def test_consumed_nonces_are_purged_after_expiry(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(evc, "_clock", lambda: now[0])
    token = evc.issue_token(None, None, "/o")
    evc.redeem_token(token, scope=None, admin_id=None, assignment="/o")
    assert len(evc._consumed) == 1
    now[0] += evc.TOKEN_MAX_AGE_SECONDS + 5
    with pytest.raises(evc.TokenRefused, match="expired"):
        evc.redeem_token(token, scope=None, admin_id=None, assignment="/o")
    # The next successful redemption purges the expired entry.
    fresh = evc.issue_token(None, None, "/o")
    evc.redeem_token(fresh, scope=None, admin_id=None, assignment="/o")
    assert len(evc._consumed) == 1
