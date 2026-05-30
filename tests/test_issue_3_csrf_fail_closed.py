"""Regression test for GitHub issue #3: CSRF validation failing open.

`validate_csrf_token` previously returned True (passed) in two situations
that an attacker can trivially reproduce for a login-CSRF / session-fixation
attack:

  1. SessionMiddleware not in the stack -> `request.session` raises
     AssertionError/AttributeError. The old code returned True.
  2. SessionMiddleware active but the request carries an empty session
     (no cookie, or a cookie without a `csrf_nonce`). Under SameSite=Lax a
     cross-site POST sends no session cookie, so `nonce is None` and the old
     code returned True.

Both must now fail CLOSED (return False) for state-changing requests, while
the legitimate flow — a GET form-render establishes the nonce + embeds the
matching signed token, then the POST submits it — must still pass.

Runs fully offline: no DB, no network, no embedding provider. `secret_key`
comes from the `SECRET_KEY=test` default set in tests/conftest.py.
"""
# Importing `src.csrf` pulls in `src.config`, which instantiates a module
# level `Settings()`. By default pydantic-settings reads a relative `.env`,
# which on a dev host may carry host-only keys the model forbids. Point the
# env-file at a path that cannot exist BEFORE the first import so config
# loads purely from process env + defaults — keeping this test hermetic and
# independent of the developer's `.env`.
import pydantic_settings  # noqa: E402

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from src.csrf import generate_csrf_token, validate_csrf_token
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


class _NoSessionRequest:
    """Stand-in for a Request when SessionMiddleware isn't installed.

    Starlette raises AssertionError on `request.session` access in that
    case; we mimic that by raising AssertionError from the property.
    """

    @property
    def session(self):
        raise AssertionError("SessionMiddleware must be installed")


class _SessionRequest:
    """Stand-in for a Request with SessionMiddleware active."""

    def __init__(self, session: dict | None = None):
        self.session = {} if session is None else session


def test_missing_session_middleware_fails_closed():
    # Branch 1: no SessionMiddleware -> must reject, not pass.
    assert validate_csrf_token(_NoSessionRequest(), "anything") is False


def test_empty_session_fails_closed():
    # Branch 2: session exists but has no csrf_nonce -> must reject.
    # This is the exact login-CSRF vector: victim's browser sends no/empty
    # session cookie on the attacker's cross-site POST.
    assert validate_csrf_token(_SessionRequest(), "anything") is False


def test_empty_session_fails_closed_even_without_token():
    assert validate_csrf_token(_SessionRequest(), None) is False


def test_legitimate_flow_still_passes():
    # GET form-render establishes the nonce and returns the signed token;
    # the subsequent POST submits that token against the same session.
    req = _SessionRequest()
    token = generate_csrf_token(req)
    assert token  # a non-empty signed token was issued
    assert "csrf_nonce" in req.session
    assert validate_csrf_token(req, token) is True


def test_token_from_other_session_rejected():
    # A token minted against a different nonce must not validate against a
    # session whose nonce differs.
    issuer = _SessionRequest()
    token = generate_csrf_token(issuer)

    victim = _SessionRequest({"csrf_nonce": "deadbeefdeadbeefdeadbeefdeadbeef"})
    assert validate_csrf_token(victim, token) is False
