"""New keys receive `DEFAULT_DAILY_REQUEST_LIMIT`; nothing else changes (#194).

The control is deliberately narrow, and every boundary of it is a place the
feature could quietly become something else:

* **Only new keys.** The default is applied in application code, never as a
  column default (D9). A `server_default` would be a schema change reaching
  every future insert path and could not express "grandfather the rows that
  exist" — which is the entire point, because five live production keys are
  unlimited and a backfill would start refusing their traffic the day this
  deploys.
* **Only where the creator did not choose.** On the JSON API an *omitted*
  field means "apply the default" and an *explicit null* still means unlimited;
  the two are separated by `model_fields_set`, not by the value's truthiness,
  because both read as `None`.
* **Exactly one place on the panel.** The default is the create form's
  pre-filled value and nothing else: the POST handler substitutes nothing, so
  an operator who clears the box gets an unlimited key. A server-side
  substitution would mean the field the operator emptied came back — the
  surprise that gets a quota feature turned off — and would give the default
  two places to be overridden instead of one.
* **The edit path is untouched.** Changing a key's limit never consults the
  default, on either surface.

Hermetic: the request models and handlers are exercised directly against fake
sessions, because what is under test is the substitution rule and the rendered
form, and standing up the app would add a database and CSRF for no coverage.
"""
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

import src.api.routes as api  # noqa: E402
import src.control_panel.routes as panel  # noqa: E402
from src.config import settings  # noqa: E402
from src.models.db import APIKey  # noqa: E402

#: What the shipped default is, so a change to it is a deliberate edit here
#: rather than a test that silently follows whatever the config says.
SHIPPED_DEFAULT = 5000


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


class _FakeUser:
    id = 1
    is_admin = True
    is_active = True
    username = "max"


class _FakeSession:
    """Captures what a handler persists, and every statement it issues.

    The statement list is what proves "existing keys are untouched": a create
    that applied the default to anything already stored would have to issue an
    UPDATE, and there is none to issue.
    """

    def __init__(self, key=None):
        self.added = []
        self.committed = 0
        self.statements = []
        self._key = key

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = 99

    async def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        key = self._key
        return SimpleNamespace(
            scalar_one_or_none=lambda: key,
            scalar=lambda: None,
            fetchall=lambda: [],
            all=lambda: [],
        )


class _FakeRequest:
    """`create_key_form` and `_flash_key_error` only touch `request.session`."""

    def __init__(self):
        self.session = {}
        self.scope = {}


_addresses = iter(f"127.0.0.{n}" for n in range(1, 255))


def _http_request():
    """A real `starlette` request: `create_key` carries slowapi's limiter,
    which reaches into the request for the client address and refuses anything
    that is not the real type.

    Each call gets a **distinct** address. `POST /api/keys` is limited to five
    a minute per address, which is a control this module is not testing and
    would otherwise fail its sixth create with a 429 — a test failure that
    would say nothing about the default.
    """
    from starlette.requests import Request

    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/keys",
        "headers": [],
        "query_string": b"",
        "client": (next(_addresses), 1234),
        "state": {},
        "app": None,
    })


def _create_json(**payload):
    """`POST /api/keys` with exactly the fields named — omission is meaningful."""
    req = api.CreateKeyRequest(name="my-key", **payload)
    session = _FakeSession()
    response = asyncio.run(
        api.create_key(
            request=_http_request(), req=req, session=session, user=_FakeUser()
        )
    )
    return response, session


def _create_panel(raw_limit):
    """`POST /admin/keys/create` with the limit box holding `raw_limit`."""
    session = _FakeSession()
    response = asyncio.run(
        panel.create_key_form(
            request=_FakeRequest(),
            name="my-key",
            permission="read",
            # Passed explicitly: calling the handler directly bypasses
            # FastAPI's form binding, so an omitted argument would arrive as
            # the `Form(...)` marker rather than as its default.
            daily_request_limit=raw_limit,
            session=session,
            user=_FakeUser(),
        )
    )
    assert response.status_code == 303
    return session


def _existing_key(limit):
    key = APIKey(
        id=4, name="nightly", key_hash="x", key_prefix="omcp_a1b2c3",
        permission="read", is_active=True, user_id=1, expires_at=None,
        daily_request_limit=limit,
    )
    return key


# --------------------------------------------------------------------------
# 1. the JSON API: omitted, explicit null, explicit value
# --------------------------------------------------------------------------


def test_an_omitted_limit_receives_the_configured_default():
    """The change. Before #194 this created an unlimited key."""
    response, session = _create_json()

    assert session.added[0].daily_request_limit == SHIPPED_DEFAULT
    assert response.daily_request_limit == SHIPPED_DEFAULT, (
        "the response must echo what the key actually got, or the substitution "
        "is invisible to the caller that did not ask for it"
    )


def test_an_explicit_null_still_means_unlimited():
    """The documented way to ask for an unlimited key, unchanged. A `x or
    default` implementation would silently turn this into a limited key —
    omitted and null both read as `None`."""
    response, session = _create_json(daily_request_limit=None)

    assert session.added[0].daily_request_limit is None
    assert response.daily_request_limit is None


def test_an_explicit_value_wins_over_the_default():
    response, session = _create_json(daily_request_limit=250)

    assert session.added[0].daily_request_limit == 250
    assert response.daily_request_limit == 250


def test_the_two_are_separated_by_model_fields_set_not_by_the_value():
    """The mechanism itself, pinned: what distinguishes the two requests is
    whether the field was present, and both carry `None` as their value."""
    omitted = api.CreateKeyRequest(name="k")
    explicit = api.CreateKeyRequest(name="k", daily_request_limit=None)

    assert omitted.daily_request_limit is None
    assert explicit.daily_request_limit is None
    assert "daily_request_limit" not in omitted.model_fields_set
    assert "daily_request_limit" in explicit.model_fields_set
    assert api._created_key_limit(omitted) == SHIPPED_DEFAULT
    assert api._created_key_limit(explicit) is None


def test_the_default_can_be_turned_off(monkeypatch):
    """`DEFAULT_DAILY_REQUEST_LIMIT=null` restores the previous behaviour
    exactly: an omitted field creates an unlimited key again."""
    monkeypatch.setattr(settings, "default_daily_request_limit", None)

    response, session = _create_json()

    assert session.added[0].daily_request_limit is None
    assert response.daily_request_limit is None


def test_the_setting_is_read_per_request(monkeypatch):
    """Not captured at import: an operator changing it does not need this
    module reloaded, and a test does not need a re-import to exercise it."""
    monkeypatch.setattr(settings, "default_daily_request_limit", 7)
    assert _create_json()[1].added[0].daily_request_limit == 7

    monkeypatch.setattr(settings, "default_daily_request_limit", 11)
    assert _create_json()[1].added[0].daily_request_limit == 11


# --------------------------------------------------------------------------
# 2. existing keys are untouched — the grandfathering D9 exists for
# --------------------------------------------------------------------------


def test_the_default_is_not_a_column_default():
    """Applied in application code, never in the schema (D9).

    A `default` or `server_default` on the column would reach every future
    insert path and, worse, would be a migration — and no migration can say
    "apply to new rows but grandfather the ones already here", which is the
    requirement.
    """
    column = APIKey.__table__.c.daily_request_limit

    assert column.nullable is True
    assert column.default is None, "a client-side column default was introduced"
    assert column.server_default is None, "a server_default was introduced"


def test_creating_a_key_issues_no_statement_against_existing_keys():
    """The other half of grandfathering: creation adds one row and updates
    nothing, so no key that already exists can acquire the default."""
    _, json_session = _create_json()
    panel_session = _create_panel("")

    for session in (json_session, panel_session):
        assert len(session.added) == 1
        assert session.statements == [], (
            "key creation issued a statement; the default must never be "
            "written onto rows that already exist"
        )


def test_an_unlimited_existing_key_stays_unlimited_through_the_panel_edit():
    """The edit path never consults the default. An operator clearing the box
    on a limited key returns it to unlimited even though new keys get 5,000."""
    key = _existing_key(100)
    session = _FakeSession(key=key)

    response = asyncio.run(
        panel.set_key_limit_form(
            request=_FakeRequest(),
            key_id=4,
            daily_request_limit="",
            session=session,
            user=_FakeUser(),
        )
    )

    assert response.status_code == 303
    assert key.daily_request_limit is None, (
        "clearing an existing key's limit substituted the default instead"
    )


def test_the_json_edit_path_never_substitutes_the_default():
    """`PUT /api/keys/{id}/limit` with an explicit null clears the limit. The
    field is required there, so there is no "omitted" case to default — and
    that is deliberate: clearing a quota should have to be typed."""
    key = _existing_key(100)
    session = _FakeSession(key=key)

    result = asyncio.run(
        api.set_key_limit(
            key_id=4,
            req=api.SetKeyLimitRequest(daily_request_limit=None),
            request=None,
            session=session,
            user=_FakeUser(),
        )
    )

    assert result.daily_request_limit is None
    assert key.daily_request_limit is None


# --------------------------------------------------------------------------
# 3. the panel: pre-filled form, no POST-side substitution
# --------------------------------------------------------------------------


class _KeysPageSession:
    """The key/owner join, then today's counter rows."""

    async def execute(self, stmt, params=None):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

            def fetchall(self):
                return self._rows

        if params is not None:
            return _Result([])
        key = _existing_key(None)
        key.created_at = SimpleNamespace(isoformat=lambda: "2026-09-06T10:00:00+00:00")
        key.last_used_at = None
        return _Result([(key, "max", True)])


def _render_keys_page():
    """Run `keys_page` for real, then render the real template with its context.

    The context comes from the route and the template from disk, because a
    renamed context key is exactly the defect this shape catches: every unit
    test stubs `TemplateResponse`, so the template is otherwise compiled only
    when a browser asks for it.
    """
    from jinja2 import (
        ChainableUndefined,
        ChoiceLoader,
        DictLoader,
        Environment,
        FileSystemLoader,
    )

    captured = {}

    def _capture(request, name, context):
        captured["name"] = name
        captured["context"] = context
        return None

    mp = pytest.MonkeyPatch()
    mp.setattr(panel.templates, "TemplateResponse", _capture)
    mp.setattr(panel, "generate_csrf_token", lambda _r: "csrf-token")
    try:
        asyncio.run(
            panel.keys_page(
                request=SimpleNamespace(session={}, scope={}),
                session=_KeysPageSession(),
                user=SimpleNamespace(id=1, is_admin=True, username="max"),
            )
        )
    finally:
        mp.undo()

    assert captured["name"] == "keys.html"
    here = os.path.dirname(os.path.abspath(__file__))
    directory = os.path.join(here, "..", "src", "control_panel", "templates")
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({
                "base.html":
                    "{% block title %}{% endblock %}{% block content %}{% endblock %}"
            }),
            FileSystemLoader(directory),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )
    context = dict(captured["context"])
    context.pop("request", None)
    return context, env.get_template("keys.html").render(**context)


def test_the_keys_page_passes_the_default_to_the_template():
    context, _ = _render_keys_page()

    assert context["quota_limit_default"] == SHIPPED_DEFAULT


def test_the_create_form_is_prefilled_with_the_default():
    _, html = _render_keys_page()

    create_form = html.split('action="/admin/keys/create"', 1)[1]
    assert 'value="5000"' in create_form, (
        "the create form's limit field is not pre-filled with the default"
    )
    # The copy has to say all three things, or the pre-filled number reads as
    # a fixed ceiling rather than a suggestion the operator owns.
    assert "default" in create_form
    assert "clear it" in create_form
    assert "empty box creates an unlimited key" in create_form


def test_the_edit_modal_is_not_prefilled():
    """The default belongs to *creation*. The edit modal's input carries the
    key's own value, written by `omcpEditLimit`, and no default."""
    _, html = _render_keys_page()

    edit_modal = html.split('id="limit-modal"', 1)[1]
    assert "5000" not in edit_modal
    # The row's edit control still carries the key's own (empty) value.
    assert "omcpEditLimit(4, '')" in html


def test_no_default_configured_leaves_the_field_empty(monkeypatch):
    """`DEFAULT_DAILY_REQUEST_LIMIT=null` renders the page it rendered before
    the setting existed: an empty box, and no copy about a default."""
    monkeypatch.setattr(settings, "default_daily_request_limit", None)

    context, html = _render_keys_page()

    assert context["quota_limit_default"] is None
    create_form = html.split('action="/admin/keys/create"', 1)[1]
    assert 'value=""' in create_form
    assert "pre-filled" not in create_form


def test_a_blank_panel_submission_creates_an_unlimited_key():
    """**No POST-side substitution.** The operator cleared the box; the key is
    unlimited, even though the form offered 5,000 a moment earlier."""
    assert settings.default_daily_request_limit == SHIPPED_DEFAULT
    session = _create_panel("")

    assert session.added[0].daily_request_limit is None, (
        "the panel substituted the configured default for a blank field, so "
        "an operator who deliberately cleared it got a limited key anyway"
    )


def test_the_prefilled_value_submitted_unchanged_creates_a_limited_key():
    """The ordinary path: the operator accepts what the form offered."""
    session = _create_panel(str(SHIPPED_DEFAULT))

    assert session.added[0].daily_request_limit == SHIPPED_DEFAULT


def test_an_edited_panel_value_is_what_the_key_receives():
    session = _create_panel("250")

    assert session.added[0].daily_request_limit == 250


def test_the_panel_create_handler_never_reads_the_setting():
    """The guard against the substitution creeping back in by another route.

    Asserting only the blank case would still pass if somebody added a branch
    defaulting a *whitespace* field, or an "only when the operator did not
    change anything" heuristic. The handler must not read the setting at all —
    there is exactly one place the panel applies it, and it is the form.
    """
    import inspect

    assert "settings.default_daily_request_limit" not in inspect.getsource(
        panel.create_key_form
    )
