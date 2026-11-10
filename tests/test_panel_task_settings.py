"""The control panel's Tasks settings card (#428, design D14, task-settings spec).

One card, two homes: on `/admin/account` in multi-user mode, editing the
signed-in user's own `task_settings` row (`POST /admin/account/tasks`), and on
`/admin/settings` in single-user mode, editing the NULL-scope row
(`POST /admin/settings/tasks`, administrators only).

The persistence layer is `src.services.task_settings`, tested against a real
database by its own slice. Here its two database touch points (`stored`, which
`effective` reads through, and `save`'s upsert) are replaced by an in-memory
store keyed by scope, while validation stays the service's real `validate`.
What is under test is the panel: which scope a request can address, the
transaction (commit on success, rollback and nothing stored on refusal), the
flash and the 303, the three-way selects, and the CSP rules.
"""
from __future__ import annotations

import re
import zoneinfo
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import main
from src.auth.session import _SINGLE_USER_SENTINEL
from src.config import settings
from src.control_panel import routes as panel_routes
from src.database import get_session
from src.limiter import limiter
from src.models.db import User
from src.services import task_settings

ACCOUNT_POST = "/admin/account/tasks"
SETTINGS_POST = "/admin/settings/tasks"


# ── fakes ───────────────────────────────────────────────────────────────────


def _user(uid: int, *, is_admin: bool = False) -> User:
    user = User(
        username=f"user{uid}",
        password_hash="x",
        is_admin=is_admin,
        is_active=True,
        vault_path=f"/tmp/vault-{uid}",
    )
    user.id = uid
    user.session_version = 1
    return user


class _Result:
    rowcount = 0

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None


class _Session:
    """Counts the route's transaction decisions; every other read is empty."""

    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, *args, **kwargs):
        return _Result()

    async def scalar(self, *args, **kwargs):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def close(self):
        pass


class _Store:
    """`task_settings_rows[scope]`, written only by a validated save."""

    def __init__(self):
        self.rows: dict[int | None, SimpleNamespace] = {}
        self.saves: list[int | None] = []

    def seed(self, uid, **fields):
        row = dict(
            timezone=None, excluded_folders=[], excluded_note_tags=[],
            set_done_date=None, set_cancelled_date=None, set_created_date=None,
            next_recurrence_below=None,
        )
        row.update(fields)
        self.rows[uid] = SimpleNamespace(**row)

    async def stored(self, session, uid):
        return self.rows.get(uid)

    async def save(self, session, uid, form):
        self.saves.append(uid)
        values, errors = task_settings.validate(form)
        if errors:
            return errors
        self.rows[uid] = SimpleNamespace(**vars(values))
        return []


@pytest.fixture
def harness(monkeypatch):
    """`harness(mode, user)` → (TestClient on `src.main.app`, store, session)."""
    store = _Store()
    db = _Session()
    monkeypatch.setattr(task_settings, "stored", store.stored)
    monkeypatch.setattr(task_settings, "save", store.save)
    monkeypatch.setattr(settings, "panel_csp", "enforce")
    # The settings page probes the embedding provider; keep it off the network.
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "tasks_default_timezone", "UTC")
    monkeypatch.setattr(settings, "tasks_set_done_date", True)
    monkeypatch.setattr(settings, "tasks_set_cancelled_date", True)
    monkeypatch.setattr(settings, "tasks_set_created_date", False)
    monkeypatch.setattr(settings, "tasks_next_recurrence_below", False)

    overrides = dict(main.app.dependency_overrides)

    async def _get_session():
        yield db

    def _factory(mode: str, user=None):
        monkeypatch.setattr(settings, "multi_user_mode", mode == "multi")
        acting = user if user is not None else (
            _user(7) if mode == "multi" else _SINGLE_USER_SENTINEL
        )

        async def _panel_user():
            return acting

        main.app.dependency_overrides[get_session] = _get_session
        main.app.dependency_overrides[panel_routes.require_user_panel] = _panel_user
        return TestClient(main.app, base_url="https://localhost"), store, db

    limiter.reset()
    yield _factory
    main.app.dependency_overrides.clear()
    main.app.dependency_overrides.update(overrides)
    limiter.reset()


def _page(mode: str) -> str:
    return "/admin/account" if mode == "multi" else "/admin/settings"


def _post_path(mode: str) -> str:
    return ACCOUNT_POST if mode == "multi" else SETTINGS_POST


def _csrf(body: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', body)
    assert match, "no CSRF token on the page"
    return match.group(1)


def _form(token: str, **fields) -> dict:
    form = {
        "csrf_token": token,
        "timezone": "",
        "excluded_folders": "",
        "excluded_note_tags": "",
        "set_done_date": "default",
        "set_cancelled_date": "default",
        "set_created_date": "default",
        "next_recurrence_below": "default",
    }
    form.update(fields)
    return form


def _card(body: str) -> str:
    start = body.index('<span class="card-title">Tasks</span>')
    end = body.index("Save task settings", start)
    return body[start:end]


def _selected(body: str, field: str) -> str:
    select = re.search(rf'<select name="{field}".*?</select>', body, re.S).group(0)
    return re.search(r'<option value="(\w+)" selected>', select).group(1)


# ── GET: where the card is ──────────────────────────────────────────────────


def test_multi_user_account_page_shows_the_card_for_the_users_own_row(harness):
    client, _store, _db = harness("multi")
    response = client.get("/admin/account")
    assert response.status_code == 200
    body = response.text
    card = _card(body)
    assert f'action="{ACCOUNT_POST}"' in body
    assert '<input type="text" name="timezone"' in card
    assert 'list="task-timezone-names"' in card and '<datalist id="task-timezone-names">' in card
    assert '<option value="Europe/Berlin"></option>' in card
    assert 'name="excluded_folders"' in card and 'name="excluded_note_tags"' in card
    # No row: every flag follows the server default, and says what it is.
    for field in task_settings.BOOLEAN_FIELDS:
        assert _selected(body, field) == "default"
    assert "Server default (on)" in card and "Server default (off)" in card
    assert 'placeholder="UTC (server default)"' in card


def test_multi_user_settings_page_has_no_card(harness):
    client, _store, _db = harness("multi", _user(1, is_admin=True))
    response = client.get("/admin/settings")
    assert response.status_code == 200
    assert "Save task settings" not in response.text
    assert SETTINGS_POST not in response.text


def test_single_user_settings_page_shows_the_card_and_account_is_404(harness):
    client, _store, _db = harness("single")
    response = client.get("/admin/settings")
    assert response.status_code == 200
    assert f'action="{SETTINGS_POST}"' in response.text
    assert "Save task settings" in response.text
    assert client.get("/admin/account").status_code == 404


def test_the_card_shows_the_stored_values(harness):
    client, store, _db = harness("multi")
    store.seed(7, timezone="Europe/Berlin", excluded_folders=["Archive/", "Templates/"],
               excluded_note_tags=["ticket"], set_created_date=True, set_done_date=False)
    body = client.get("/admin/account").text
    card = _card(body)
    assert 'value="Europe/Berlin"' in card
    assert ">Archive/\nTemplates/</textarea>" in card
    assert ">ticket</textarea>" in card
    assert _selected(body, "set_done_date") == "off"
    assert _selected(body, "set_created_date") == "on"
    assert _selected(body, "set_cancelled_date") == "default"


def test_an_unloadable_stored_timezone_is_shown_as_the_utc_fallback(harness):
    client, store, _db = harness("multi")
    store.seed(7, timezone="Mars/Olympus")
    card = _card(client.get("/admin/account").text)
    assert "In use now: <code>UTC</code>" in card
    assert "cannot be loaded by this server" in card


# ── POST: saving the right row ──────────────────────────────────────────────


def test_multi_user_save_writes_the_signed_in_users_row_only(harness):
    client, store, db = harness("multi")
    token = _csrf(client.get("/admin/account").text)
    response = client.post(ACCOUNT_POST, data=_form(
        token, timezone="Europe/Berlin", excluded_folders="Archive\n",
        user_id="99", uid="99",  # never read: the scope is the session's user
    ), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/account"
    assert set(store.rows) == {7}
    assert store.rows[7].timezone == "Europe/Berlin"
    assert store.rows[7].excluded_folders == ["Archive/"]
    assert db.commits == 1 and db.rollbacks == 0
    page = client.get("/admin/account").text
    assert "Task settings saved." in page
    assert "Task settings saved." not in client.get("/admin/account").text  # shown once


def test_single_user_save_writes_the_null_scope_row(harness):
    client, store, db = harness("single")
    token = _csrf(client.get("/admin/settings").text)
    response = client.post(SETTINGS_POST, data=_form(
        token, timezone="America/New_York", excluded_note_tags="#Ticket",
    ), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/settings"
    assert set(store.rows) == {None}
    assert store.rows[None].timezone == "America/New_York"
    assert store.rows[None].excluded_note_tags == ["ticket"]
    assert db.commits == 1
    assert "Task settings saved." in client.get("/admin/settings").text


def test_tri_state_round_trip(harness):
    client, store, _db = harness("multi")
    token = _csrf(client.get("/admin/account").text)
    client.post(ACCOUNT_POST, data=_form(
        token, set_done_date="off", set_cancelled_date="default",
        set_created_date="on", next_recurrence_below="on",
    ), follow_redirects=False)
    row = store.rows[7]
    assert (row.set_done_date, row.set_cancelled_date, row.set_created_date,
            row.next_recurrence_below) == (False, None, True, True)
    body = client.get("/admin/account").text
    assert [_selected(body, f) for f in task_settings.BOOLEAN_FIELDS] == [
        "off", "default", "on", "on"
    ]
    card = _card(body)
    assert "overridden here" in card and "(server default)" in card
    # And back to the server default.
    token = _csrf(body)
    client.post(ACCOUNT_POST, data=_form(token), follow_redirects=False)
    row = store.rows[7]
    assert (row.set_done_date, row.set_cancelled_date, row.set_created_date,
            row.next_recurrence_below) == (None, None, None, None)


@pytest.mark.parametrize("fields, message", [
    ({"timezone": "Mars/Olympus"}, "is not an available IANA timezone"),
    ({"excluded_folders": "../other"}, "must be a vault-relative folder path"),
    ({"excluded_folders": "/abs"}, "must be vault-relative"),
    ({"excluded_note_tags": "#1bad"}, "is not a tag"),
    ({"excluded_folders": "\n".join(f"F{i}" for i in range(33))}, "At most 32 excluded folders"),
    ({"set_done_date": "maybe"}, "Unrecognised value for"),
])
@pytest.mark.parametrize("mode", ["multi", "single"])
def test_a_refused_save_flashes_and_leaves_the_row_unchanged(harness, mode, fields, message):
    client, store, db = harness(mode)
    scope = 7 if mode == "multi" else None
    store.seed(scope, timezone="Europe/Paris", excluded_folders=["Keep/"])
    before = vars(store.rows[scope]).copy()
    token = _csrf(client.get(_page(mode)).text)
    response = client.post(_post_path(mode), data=_form(token, **fields), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == _page(mode)
    assert vars(store.rows[scope]) == before
    assert db.commits == 0 and db.rollbacks == 1
    page = client.get(_page(mode)).text
    assert "Task settings not saved." in page and message in page
    # The form re-renders with what is still stored.
    assert 'value="Europe/Paris"' in _card(page)


# ── gates ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["multi", "single"])
def test_a_post_without_a_csrf_token_is_refused(harness, mode):
    client, store, db = harness(mode)
    client.get(_page(mode))
    form = _form("ignored", timezone="Europe/Berlin")
    del form["csrf_token"]
    response = client.post(_post_path(mode), data=form, follow_redirects=False)
    assert response.status_code == 403
    assert store.saves == [] and store.rows == {} and db.commits == 0


def test_a_post_with_a_forged_csrf_token_is_refused(harness):
    client, store, _db = harness("multi")
    client.get("/admin/account")
    response = client.post(ACCOUNT_POST, data=_form("forged"), follow_redirects=False)
    assert response.status_code == 403
    assert store.saves == []


def test_account_route_is_404_in_single_user_mode(harness):
    client, store, _db = harness("single")
    token = _csrf(client.get("/admin/settings").text)
    response = client.post(ACCOUNT_POST, data=_form(token), follow_redirects=False)
    assert response.status_code == 404
    assert store.saves == []


def test_settings_route_is_404_in_multi_user_mode_even_for_an_admin(harness):
    client, store, _db = harness("multi", _user(1, is_admin=True))
    token = _csrf(client.get("/admin/account").text)
    response = client.post(SETTINGS_POST, data=_form(token), follow_redirects=False)
    assert response.status_code == 404
    assert store.saves == [] and None not in store.rows


def test_settings_route_is_admin_only(harness):
    # Single-user mode's sentinel is always an admin; a non-admin principal is
    # refused by `require_admin_panel` before anything is read.
    # CSRF is switched off here so the 403 can only be the admin gate's.
    from src.csrf import verify_csrf

    client, store, _db = harness("single", _user(3, is_admin=False))
    main.app.dependency_overrides[verify_csrf] = lambda: None
    response = client.post(SETTINGS_POST, data=_form("unused"), follow_redirects=False)
    assert response.status_code == 403
    assert response.json() == {"detail": "Admin required"}
    assert store.saves == []


def test_two_users_each_reach_only_their_own_row(harness):
    client, store, _db = harness("multi", _user(7))
    store.seed(8, timezone="Asia/Tokyo")
    body = client.get("/admin/account").text
    assert "Asia/Tokyo" not in _card(body).split("<datalist")[0]
    client.post(ACCOUNT_POST, data=_form(_csrf(body), timezone="Europe/Berlin"),
                follow_redirects=False)
    assert store.rows[8].timezone == "Asia/Tokyo"
    assert store.rows[7].timezone == "Europe/Berlin"


# ── CSP ─────────────────────────────────────────────────────────────────────


class _Tags(HTMLParser):
    """Every start tag and its attributes, as the browser would parse them."""

    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k.lower(): (v or "") for k, v in attrs}))


def _scan(html: str) -> list[tuple[str, dict]]:
    parser = _Tags()
    parser.feed(html)
    return parser.tags


@pytest.mark.parametrize("mode", ["multi", "single"])
def test_the_rendered_page_and_card_pass_the_csp_scan(harness, mode):
    client, store, _db = harness(mode)
    store.seed(7 if mode == "multi" else None, excluded_folders=["A/"])
    response = client.get(_page(mode))
    body = response.text
    assert "Save task settings" in body
    nonce = re.search(r"'nonce-([^']+)'", response.headers["content-security-policy"]).group(1)
    tags = _scan(body)
    for tag, attrs in tags:
        assert not [a for a in attrs if a.startswith("on")], (tag, attrs)
        assert "style" not in attrs, (tag, attrs)
        assert not any(v.strip().lower().startswith("javascript:") for v in attrs.values())
        if tag in ("script", "style"):
            assert attrs.get("nonce") == nonce, (tag, attrs)
    card_tags = [t for t, _ in _scan(_card(body))]
    assert "script" not in card_tags and "style" not in card_tags
    assert {"form", "input", "datalist", "textarea", "select"} <= set(card_tags)


def test_the_card_template_carries_no_handler_style_or_script():
    from pathlib import Path

    source = (Path(panel_routes.__file__).parent / "templates" / "_task_settings_card.html").read_text()
    source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)  # Jinja comments
    assert not re.search(r"\son[a-z]+\s*=", source, re.I)
    assert "style=" not in source and "javascript:" not in source
    assert "<script" not in source and "hx-" not in source


# ── the timezone suggestions ────────────────────────────────────────────────


def test_timezone_suggestions_are_fixed_unique_and_available():
    names = panel_routes.TASK_TIMEZONE_SUGGESTIONS
    assert len(names) == len(set(names))
    assert names[0] == "UTC"
    available = zoneinfo.available_timezones()
    assert [n for n in names if n not in available] == []
    for name in names:  # every suggestion is one `save` would accept
        assert task_settings.normalise_timezone(name) == (name, None)


# ── the task rule block (#434, D10) ─────────────────────────────────────────


def test_the_rule_block_shows_the_server_default_and_its_controls(harness, monkeypatch):
    monkeypatch.setattr(settings, "tasks_rule", "fields")
    monkeypatch.setattr(settings, "tasks_rule_tags", ["someday"])
    monkeypatch.setattr(settings, "tasks_global_filter", "")
    client, _store, _db = harness("multi")
    body = client.get("/admin/account").text
    card = _card(body)
    assert "Which checkboxes are tasks" in card
    assert _selected(body, "task_rule") == "default"
    assert ("Server default (only checkboxes with task details or a task tag, task tags: "
            "#someday)") in card
    for words in ("Every checkbox is a task", "Only checkboxes with task details or a task tag",
                  "Only checkboxes that contain the global filter text"):
        assert words in card
    assert 'name="task_tags"' in card and 'name="global_filter"' in card
    assert 'maxlength="64"' in card and 'placeholder="#task"' in card
    assert "In use now: only checkboxes with task details or a task tag, task tags: " \
        "#someday\n                    (server default)." in card
    # The help text is for people without the plugin.
    for phrase in ("A task is a\n                    Markdown checkbox", "Global task\n",
                   "Agents can\n                    always ask for every checkbox"):
        assert phrase in card, phrase


def test_the_card_shows_the_rule_set_here(harness):
    client, store, _db = harness("multi")
    store.seed(7, task_rule="fields", task_tags=["someday"], global_filter=None)
    body = client.get("/admin/account").text
    card = _card(body)
    assert _selected(body, "task_rule") == "fields"
    assert ">someday</textarea>" in card
    assert "In use now: only checkboxes with task details or a task tag, task tags: " \
        "#someday\n                    (set here)." in card


def test_the_card_states_an_unusable_stored_rule(harness):
    client, store, _db = harness("multi")
    store.seed(7, task_rule="filter", task_tags=[], global_filter=None)
    card = _card(client.get("/admin/account").text)
    assert "cannot be applied (filter rule without filter text)" in card
    assert "In use now: every checkbox is a task" in card


def test_a_rule_save_passes_the_three_fields_through(harness):
    client, store, db = harness("multi")
    token = _csrf(client.get("/admin/account").text)
    response = client.post(ACCOUNT_POST, data=_form(
        token, task_rule="filter", task_tags="#Someday\nwaiting-for", global_filter=" #task ",
        user_id="99",
    ), follow_redirects=False)
    assert response.status_code == 303 and set(store.rows) == {7}
    row = store.rows[7]
    assert (row.task_rule, row.task_tags, row.global_filter) == (
        "filter", ["someday", "waiting-for"], "#task")
    assert db.commits == 1


def test_absent_rule_fields_reach_the_service_as_absent(harness, monkeypatch):
    """The route passes only the card's names that were posted: a client
    posting part of the card leaves the rule unchanged (the service's
    absent-key rule, `rule_unchanged`)."""
    client, store, _db = harness("multi")
    seen = {}

    async def save(session, uid, form):
        seen.update(form)
        values, errors = task_settings.validate(form)
        assert values.rule_unchanged == frozenset(task_settings.RULE_FIELDS)
        return errors

    monkeypatch.setattr(task_settings, "save", save)
    token = _csrf(client.get("/admin/account").text)
    client.post(ACCOUNT_POST, data={"csrf_token": token, "timezone": "UTC",
                                    "user_id": "99"}, follow_redirects=False)
    assert set(seen) == {"timezone"}


@pytest.mark.parametrize("fields, message", [
    ({"task_rule": "any"}, "Unrecognised value for the task rule."),
    ({"task_tags": "waiting-for/"}, "is not a tag"),
    ({"task_tags": "\n".join(f"t{i}" for i in range(33))}, "At most 32 task tags"),
    ({"global_filter": "%%"}, "Obsidian comment marker"),
    ({"global_filter": "&lt;!-- x --&gt;"}, "HTML comment marker"),
    ({"global_filter": "x" * 65}, "longer than 64"),
    ({"global_filter": "[t]"}, "checkbox"),
])
@pytest.mark.parametrize("mode", ["multi", "single"])
def test_a_refused_rule_save_flashes_and_stores_nothing(harness, mode, fields, message):
    client, store, db = harness(mode)
    scope = 7 if mode == "multi" else None
    store.seed(scope, task_rule="fields", task_tags=["keep"], global_filter=None)
    before = vars(store.rows[scope]).copy()
    if fields.get("global_filter", "").startswith("&lt;"):
        fields = {"global_filter": "<!-- x -->"}
    token = _csrf(client.get(_page(mode)).text)
    client.post(_post_path(mode), data=_form(token, **fields), follow_redirects=False)
    assert vars(store.rows[scope]) == before
    assert db.commits == 0 and db.rollbacks == 1
    page = client.get(_page(mode)).text
    assert "Task settings not saved." in page and message in page


@pytest.mark.parametrize("mode", ["multi", "single"])
def test_the_filter_needs_text_refusal_is_flashed(harness, mode, monkeypatch):
    client, _store, db = harness(mode)

    async def save(session, uid, form):
        return [task_settings.task_rule.FILTER_NEEDS_TEXT]

    monkeypatch.setattr(task_settings, "save", save)
    token = _csrf(client.get(_page(mode)).text)
    client.post(_post_path(mode), data=_form(token, task_rule="filter", global_filter=""),
                follow_redirects=False)
    assert db.rollbacks == 1 and db.commits == 0
    page = client.get(_page(mode)).text
    assert "needs a global filter text" in page
