"""Issue #130 — hardening and polish minors found by the #122 audit.

One module per issue is the house style, and #130 is one issue with eight
independent surfaces, so they share a file and are grouped by the item they
cover. Nothing here needs a database: every case is either a pure function, a
dependency called directly, or a static asset served by the app object.
"""
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pydantic_settings
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.auth.routes import _safe_next
from src.control_panel import routes as panel_routes
from src.mcp_server import server as mcp_server
from src.models.db import NoteMetadata, User

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from src.main import app
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


_TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "control_panel" / "templates"
_VENDOR = Path(__file__).resolve().parents[1] / "src" / "control_panel" / "static" / "vendor"

# `TrustedHostMiddleware` allows "localhost" in the test settings and compares
# the Host header, so the default `http://testserver` base URL 400s before any
# route is reached.
_client = TestClient(app, base_url="http://localhost:8000")


# --- Item 1: the panel's JS is served from this app, not from a CDN --------


@pytest.mark.parametrize(
    "asset", ["htmx-2.0.4.min.js", "chart-4.4.7.umd.min.js"]
)
def test_the_vendored_asset_is_actually_served(asset):
    response = _client.get(f"/admin/static/vendor/{asset}")

    assert response.status_code == 200
    assert response.content == (_VENDOR / asset).read_bytes()


def test_no_template_loads_htmx_or_chartjs_from_a_cdn():
    offenders = {
        path.name
        for path in _TEMPLATES.glob("*.html")
        for line in path.read_text().splitlines()
        # The `<script src=` test is what keeps the provenance comment in
        # base.html — which names both upstream URLs on purpose — from reading
        # as a violation of the rule it documents.
        if "<script" in line and ("unpkg.com" in line or "cdn.jsdelivr.net" in line)
    }

    assert offenders == set()


def test_base_html_points_at_the_local_copies():
    markup = (_TEMPLATES / "base.html").read_text()

    assert '<script src="/admin/static/vendor/htmx-2.0.4.min.js">' in markup
    assert '<script src="/admin/static/vendor/chart-4.4.7.umd.min.js">' in markup


def test_the_static_mount_cannot_reach_outside_its_directory():
    # StaticFiles' own containment check; asserted because this mount is the
    # first one in the app and a future `directory=` change must not open the
    # source tree.
    assert _client.get("/admin/static/vendor/../../routes.py").status_code == 404


# --- Item 2: MCP transport security host/origin patterns ------------------


def test_host_patterns_add_the_any_port_form():
    # The SDK matches the Host header exactly, plus a trailing ":*". Starlette's
    # TrustedHostMiddleware strips the port, so "localhost" accepted
    # `Host: localhost:8000` at the app boundary while /mcp answered 421.
    assert mcp_server._host_patterns(["vault.example.com", "localhost"]) == [
        "vault.example.com",
        "vault.example.com:*",
        "localhost",
        "localhost:*",
    ]


def test_host_patterns_leave_an_explicit_port_alone():
    assert mcp_server._host_patterns(["localhost:8000"]) == ["localhost:8000"]


def test_host_patterns_drop_blanks_and_dedupe():
    assert mcp_server._host_patterns(["localhost", "  ", "localhost", ""]) == [
        "localhost",
        "localhost:*",
    ]


def test_origin_patterns_add_the_any_port_form_only_when_there_is_no_port():
    assert mcp_server._origin_patterns(
        ["https://vault.example.com", "http://localhost:8000"]
    ) == [
        "https://vault.example.com",
        "https://vault.example.com:*",
        "http://localhost:8000",
    ]


def test_origin_patterns_treat_an_unparseable_port_as_literal():
    assert mcp_server._origin_patterns(["https://host:notaport"]) == [
        "https://host:notaport"
    ]


def test_the_live_transport_settings_carry_origins_and_ported_hosts():
    security = mcp_server.mcp.settings.transport_security

    # An empty `allowed_origins` is not "no opinion" in the SDK: it rejects
    # every Origin header that is present.
    assert security.allowed_origins
    assert any(entry.endswith(":*") for entry in security.allowed_hosts)


# --- Item 4: the raw key does not linger in the session cookie ------------


class _FakeURL:
    def __init__(self, path: str, query: str = ""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, path: str, *, method: str = "GET", query: str = "", session=None):
        self.url = _FakeURL(path, query)
        self.method = method
        self.session = {} if session is None else session


@pytest.mark.parametrize(
    ("path", "method"),
    [("/admin/", "GET"), ("/admin/usage", "GET"), ("/admin/keys", "POST"), ("/api/keys", "GET")],
)
def test_any_other_request_forgets_the_new_key(path, method):
    request = _FakeRequest(path, method=method, session={"flash_new_key": "omcp_secret"})

    panel_routes._forget_new_key_flash(request)

    assert "flash_new_key" not in request.session


@pytest.mark.parametrize("path", ["/admin/keys", "/admin/keys/"])
def test_the_keys_page_render_keeps_it_for_its_own_pop(path):
    request = _FakeRequest(path, session={"flash_new_key": "omcp_secret"})

    panel_routes._forget_new_key_flash(request)

    assert request.session["flash_new_key"] == "omcp_secret"


def test_forgetting_survives_an_app_without_session_middleware():
    class _NoSession:
        url = _FakeURL("/admin/")
        method = "GET"

        @property
        def session(self):
            raise AssertionError("SessionMiddleware must be installed")

    panel_routes._forget_new_key_flash(_NoSession())  # must not raise


# --- Item 5: the user delete is a database cascade, not an ORM sweep ------


@pytest.mark.parametrize(
    "relationship", ["api_keys", "oauth_clients", "oauth_tokens", "notes"]
)
def test_user_children_are_deleted_by_the_database(relationship):
    assert User.__mapper__.relationships[relationship].passive_deletes is True


def test_note_embeddings_are_deleted_by_the_database():
    assert NoteMetadata.__mapper__.relationships["embeddings"].passive_deletes is True


def test_delete_user_nulls_the_usage_log_key_references():
    # `usage_logs.key_id` has no ON DELETE, so with `passive_deletes=True` the
    # ORM no longer NULLs it and the database would refuse to cascade the keys
    # away. Source-level assertion: the alternative needs a live Postgres, and
    # the integration module already owns that scenario for a single key.
    source = (
        Path(__file__).resolve().parents[1] / "src" / "control_panel" / "users.py"
    ).read_text()
    delete_body = source.split("async def delete_user")[1].split("async def ")[0]
    null_pass = delete_body.index("key_id=None")
    orm_delete = delete_body.index("session.delete(target)")

    assert null_pass < orm_delete


# --- Item 7: the create-key form validates what the JSON twin validates ---


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_a_blank_key_name_is_refused(name):
    assert panel_routes._key_name_error(name) == "Key name is required."


def test_an_overlong_key_name_is_refused_before_postgres_raises():
    message = panel_routes._key_name_error("k" * 256)

    assert message is not None and "255" in message


def test_a_name_at_the_limit_is_accepted():
    assert panel_routes._key_name_error("k" * 255) is None


@pytest.mark.parametrize("name", ["laptop/key", "key;drop", "emoji \U0001f600"])
def test_a_name_outside_the_json_pattern_is_refused(name):
    assert panel_routes._key_name_error(name) is not None


@pytest.mark.parametrize("name", ["laptop", "Max's laptop".replace("'", ""), "key-1.2 v3", "a_b"])
def test_the_ordinary_names_the_json_api_accepts_still_pass(name):
    assert panel_routes._key_name_error(name) is None


def test_the_form_and_the_json_api_share_one_pattern():
    from src.api.routes import CreateKeyRequest

    json_pattern = CreateKeyRequest.model_fields["name"].metadata
    patterns = {
        getattr(item, "pattern", None) for item in json_pattern
    } - {None}

    assert patterns == {panel_routes._KEY_NAME_RE.pattern}


# --- Item 8: overlapping danger-zone actions do not unpause each other ----


def test_the_pause_survives_an_overlapping_action():
    assert panel_routes.indexer_paused is False

    with panel_routes._pause_indexer():
        assert panel_routes.indexer_paused is True
        with panel_routes._pause_indexer():
            assert panel_routes.indexer_paused is True
        # The inner action finished; the outer one is still running, and the
        # bare `indexer_paused = False` this replaced reported "not paused"
        # here — to the indexer *and* to the progress endpoint.
        assert panel_routes.indexer_paused is True

    assert panel_routes.indexer_paused is False
    assert panel_routes._pause_depth == 0


def test_the_pause_is_released_on_an_exception():
    with pytest.raises(RuntimeError):
        with panel_routes._pause_indexer():
            raise RuntimeError("destructive SQL failed")

    assert panel_routes.indexer_paused is False
    assert panel_routes._pause_depth == 0


def test_the_indexer_still_reads_the_module_attribute():
    from src.services import indexer

    with panel_routes._pause_indexer():
        assert indexer._is_paused() is True
    assert indexer._is_paused() is False


# --- Item 9: the login redirect and the /api 401 --------------------------


@pytest.mark.asyncio
async def test_the_next_parameter_is_percent_encoded():
    from urllib.parse import parse_qs, urlparse

    request = _FakeRequest("/admin/usage", query="tool=keyword_search&limit=10")

    with pytest.raises(HTTPException) as exc:
        await panel_routes.require_user_panel(request, user=None, session=AsyncMock())

    location = exc.value.headers["Location"]
    carried = parse_qs(urlparse(location).query)["next"][0]
    # Unencoded, `&limit=10` became a parameter of the *login* URL and the
    # login form only ever saw "/admin/usage?tool=keyword_search".
    assert carried == "/admin/usage?tool=keyword_search&limit=10"
    assert _safe_next(carried) == carried


@pytest.mark.asyncio
async def test_an_html_route_still_redirects():
    request = _FakeRequest("/admin/usage")

    with pytest.raises(HTTPException) as exc:
        await panel_routes.require_user_panel(request, user=None, session=AsyncMock())

    assert exc.value.status_code == 302


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/keys", "/api/keys/3", "/api"])
async def test_the_rest_surface_gets_a_json_401(path):
    request = _FakeRequest(path, method="POST")

    with pytest.raises(HTTPException) as exc:
        await panel_routes.require_user_panel(request, user=None, session=AsyncMock())

    assert exc.value.status_code == 401
    assert exc.value.headers is None or "Location" not in (exc.value.headers or {})


@pytest.mark.asyncio
async def test_an_inactive_user_on_the_rest_surface_also_gets_401():
    request = _FakeRequest("/api/keys")
    inactive = User(id=3, username="bob", is_active=False)

    with pytest.raises(HTTPException) as exc:
        await panel_routes.require_user_panel(
            request, user=inactive, session=AsyncMock()
        )

    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "value",
    [
        "//evil.example/",
        "/\\evil.example/",
        "https://evil.example/",
        "/admin/\nSet-Cookie: x=1",
        "/admin/\tx",
    ],
)
def test_safe_next_refuses_an_off_site_or_smuggled_target(value):
    assert _safe_next(value) == "/admin/"


@pytest.mark.parametrize(
    "value", ["/admin/", "/admin/usage?tool=x&limit=10", "/admin/users/3/edit"]
)
def test_safe_next_keeps_an_in_app_path(value):
    assert _safe_next(value) == value


# --- Item 10: no user-controlled interpolation inside confirm() -----------


def test_no_template_interpolates_a_jinja_expression_into_confirm():
    # The `client_name`-in-confirm() defect class: Jinja escapes an apostrophe
    # to `&#39;`, the HTML parser restores it before the JS string is parsed,
    # the handler throws — and a throwing `onclick` submits the form
    # *unconfirmed*.
    pattern = re.compile(r"confirm\('[^']*\{\{\s*(?P<expr>[^}]+?)\s*\}\}")
    offenders = []
    for path in _TEMPLATES.glob("*.html"):
        for match in pattern.finditer(path.read_text()):
            expr = match.group("expr")
            # keys.html counts revoked rows and pluralises the word "key".
            # Both expressions are computed in the template from an integer,
            # carry no user or vault input, and cannot produce a quote.
            if expr not in ("revoked_count", "'s' if revoked_count != 1"):
                offenders.append(f"{path.name}: {expr}")

    assert offenders == []


def test_the_user_edit_confirms_are_static():
    markup = (_TEMPLATES / "user_edit.html").read_text()

    assert "confirm('Deactivate this user?" in markup
    assert "confirm('PERMANENTLY DELETE this user" in markup
    assert "target.username }}?" not in markup
