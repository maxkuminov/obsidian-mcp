"""`X-Content-Type-Options: nosniff` on the OAuth scope rejections (#92, item 3).

**This module pins behaviour that is already correct. It fixes nothing.**

`_validate_scope` in `src/oauth/routes.py` raises
`ValueError(f"Invalid scopes: {invalid}")` with the caller's own tokens
interpolated into it, and all three call sites — `/register`, `authorize_get`
and `authorize_post` — echo `str(exc)` into an `application/json` body. Issue
#92 item 3 asked that `nosniff` be *confirmed* on those responses, so a browser
cannot be talked into re-interpreting an attacker-chosen scope string as some
other content type. It was investigated and the header is set on every one of
them, by `add_security_headers` in `src/main.py`:

* it is an `@app.middleware("http")` on the application, registered *before*
  `app.include_router(oauth_router)`, and it stamps the header on whatever
  `call_next` returns — so it is a property of the response path, not of any
  handler remembering to set it;
* the three scope errors are ordinary `JSONResponse` *returns*, not propagating
  exceptions. A `ValueError` escaping to Starlette's `ServerErrorMiddleware`
  *would* bypass the header, because that middleware sits outside the user
  middleware stack — but each call site wraps `_validate_scope` in
  `try/except ValueError`, so no scope error takes that path.

So why a test. Before this module, exactly one test anywhere asserted this
header (`tests/test_transfer_routes.py`, on a route that sets it explicitly in
the handler). "Already correct" and "protected against regression" are
different states: reordering the middleware stack, or moving the OAuth routes
onto a sub-application with its own stack, would drop the header from all three
silently and nothing would fail. That is the regression this guards against,
which is why every request below goes through the real `src.main.app` — the
whole middleware stack, mounted the way production mounts it — rather than
through a hand-built router or a hand-built `JSONResponse`.

The media-type half is deliberately narrow. `application/json` is asserted on
the three scope *rejections* only. The successful consent screen reflects
caller-supplied input too and is `text/html` **on purpose**; it is governed by
"Consent renders client-supplied text as text", which requires the reflection
be escaped rather than requiring a media type. `test_the_consent_screen_stays_html_and_still_carries_nosniff`
pins that explicitly, so a future over-broad "make every reflecting OAuth
response JSON" change fails here instead of turning the consent page into JSON.

The routes are mounted with no prefix (`app.include_router(oauth_router)`), so
the live paths are `/register` and `/authorize` — which is what `/register` and
`/authorize` in the discovery metadata advertise.
"""
import re

import pydantic_settings
import pytest

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from fastapi.testclient import TestClient

    from src.limiter import limiter
    from src.main import app
    from src.oauth import routes
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


# `settings.allowed_hosts` is `["localhost"]` under the hermetic test config, and
# `TrustedHostMiddleware` would 400 TestClient's default `Host: testserver`
# before any OAuth handler ran. Asking for the right host keeps these tests
# about the security header rather than about the host guard.
BASE_URL = "http://localhost:8000"

REGISTERED_URI = "https://client.example.com/callback"
VALID_PKCE_CHALLENGE = "a" * 43

# A scope token no `VALID_SCOPES` entry can match, distinctive enough that
# finding it in a header would be unambiguous. Kept to `[a-z0-9_]` so nothing
# in the assertions is really testing URL or form encoding.
HOSTILE_SCOPE_TOKEN = "sniffme_scope_probe_92"
HOSTILE_SCOPE = f"read {HOSTILE_SCOPE_TOKEN}"


class _FakeClient:
    """The registered client `authorize_get` looks up to render consent."""

    client_id = "client123"
    client_name = "Test Client"
    scope = "read readwrite offline_access"
    redirect_uris = [REGISTERED_URI]


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


@pytest.fixture(autouse=True)
def _clean_rate_limiter():
    """`/register` is `@limiter.limit("3/minute")` and `limiter` is a
    module-level singleton shared by the whole session, so a neighbouring test
    module's registrations could otherwise turn one of these into a 429."""
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client(monkeypatch):
    """A `TestClient` on the real app, with the client lookup faked out.

    Only `authorize_get`'s `SELECT` needs it: scope validation runs *before*
    any database access at all three call sites, so the three rejection tests
    would pass without it. The consent screen is what needs a client row.
    """
    monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(routes, "async_session", lambda: _FakeSession())
    return TestClient(app, base_url=BASE_URL)


def _assert_nosniff(response):
    assert response.headers.get("x-content-type-options") == "nosniff"


def _assert_json_rejection(response):
    """The shared shape of a scope rejection: JSON, nosniff, 400."""
    assert response.status_code == 400, response.text
    assert response.headers["content-type"].split(";")[0].strip() == "application/json"
    _assert_nosniff(response)
    assert response.json()["error"] == "invalid_scope"


def _assert_token_only_in_body(response):
    """The offending token is echoed into the body and into nothing else.

    A reflected value that also reached a header would be a different bug —
    header injection rather than content sniffing — and the header set is small
    enough to check exhaustively.
    """
    assert HOSTILE_SCOPE_TOKEN in response.text
    for name, value in response.headers.items():
        assert HOSTILE_SCOPE_TOKEN not in value, f"echoed into header {name!r}"


def _authorize_params(**overrides):
    params = {
        "response_type": "code",
        "client_id": _FakeClient.client_id,
        "redirect_uri": REGISTERED_URI,
        "code_challenge": VALID_PKCE_CHALLENGE,
        "code_challenge_method": "S256",
        "scope": "read",
    }
    params.update(overrides)
    return params


def _consent_screen(client):
    """Render the real consent screen and return the response.

    Doubles as the setup for the POST rejection: `authorize_post` verifies the
    signed `oauth_state` cookie against the submitted `state` *before* it
    validates the scope, so reaching the scope branch at all requires a state
    this endpoint actually issued. Minting one by hand would be testing a
    forgery path rather than the rejection.
    """
    response = client.get("/authorize", params=_authorize_params())
    assert response.status_code == 200, response.text
    return response


def _submitted_state(html: str) -> str:
    match = re.search(r'<input[^>]*name="state"[^>]*value="([^"]*)"', html)
    assert match, "consent screen carried no CSRF state to submit"
    return match.group(1)


# --- the three scope rejections --------------------------------------------


def test_register_scope_rejection_is_json_and_nosniff(client):
    response = client.post(
        "/register",
        json={
            "client_name": "Test Client",
            "redirect_uris": [REGISTERED_URI],
            "scope": HOSTILE_SCOPE,
        },
    )

    _assert_json_rejection(response)
    _assert_token_only_in_body(response)


def test_authorize_get_scope_rejection_is_json_and_nosniff(client):
    response = client.get("/authorize", params=_authorize_params(scope=HOSTILE_SCOPE))

    _assert_json_rejection(response)
    _assert_token_only_in_body(response)


def test_authorize_post_scope_rejection_is_json_and_nosniff(client):
    consent = _consent_screen(client)
    state = _submitted_state(consent.text)

    response = client.post(
        "/authorize",
        data={
            "action": "approve",
            "client_id": _FakeClient.client_id,
            "redirect_uri": REGISTERED_URI,
            "code_challenge": VALID_PKCE_CHALLENGE,
            "code_challenge_method": "S256",
            "scope": HOSTILE_SCOPE,
            "state": state,
            "client_state": "",
        },
    )

    _assert_json_rejection(response)
    _assert_token_only_in_body(response)


# --- and the successes, so a regression that stamps only errors is caught ---


def test_successful_oauth_json_response_carries_nosniff(client):
    """A regression could plausibly special-case the error paths. It must not
    be able to pass by stamping only those."""
    response = client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0].strip() == "application/json"
    _assert_nosniff(response)


def test_the_consent_screen_stays_html_and_still_carries_nosniff(client):
    """The requirement is nosniff on all of these and `application/json` on the
    three rejections only.

    The consent screen reflects the client's registered name and the caller's
    own authorization parameters, and is HTML on purpose — escaping is what
    governs it, not a media type. Asserting `application/json` here would be
    asserting the wrong thing, so this asserts the opposite: it stays
    `text/html` *and* carries the header.
    """
    response = _consent_screen(client)

    assert response.headers["content-type"].split(";")[0].strip() == "text/html"
    _assert_nosniff(response)
    assert _FakeClient.client_name in response.text
