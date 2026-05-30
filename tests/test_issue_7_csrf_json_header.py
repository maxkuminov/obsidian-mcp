"""Regression test for GitHub issue #7: CSRF unprotected JSON API.

`src/api/routes.py` attached only `require_user_panel` to its router and was
missing `verify_csrf`, which every other state-changing router uses. The
`/api/keys` POST and `/api/keys/{id}` DELETE endpoints are session-cookie
authed over a `same_site=lax` cookie, so SameSite was the *only* CSRF
defense for those state-changing JSON routes.

Wiring `verify_csrf` onto the router is only half the fix: the old
`verify_csrf` read the token exclusively from `await request.form()`, but
these endpoints take JSON bodies. Parsing a JSON body as a form yields no
`csrf_token`, so a legitimate JSON client could never satisfy the check, and
on some bodies `request.form()` raises. `verify_csrf` must therefore:

  1. read the token from the `X-CSRF-Token` header first (JSON clients), and
  2. fall back to the form field (legacy HTML forms) without crashing when
     the body is not form-encoded.

Runs fully offline: no DB, no network, no embedding provider. `secret_key`
comes from the `SECRET_KEY=test` default set in tests/conftest.py.
"""
# Importing `src.csrf` pulls in `src.config`, which instantiates a module
# level `Settings()`. By default pydantic-settings reads a relative `.env`,
# which on a dev host may carry host-only keys the model forbids. Point the
# env-file at a path that cannot exist BEFORE the first import so config
# loads purely from process env + defaults — keeping this test hermetic and
# independent of the developer's `.env`.
import asyncio

import pydantic_settings
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from src.csrf import generate_csrf_token, verify_csrf
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


class _FakeRequest:
    """Minimal stand-in for a Starlette Request exercising `verify_csrf`.

    - `headers` is a real case-insensitive `Headers`, matching production.
    - `form()` is awaitable; `form_raises=True` mimics a JSON body that
      Starlette refuses to parse as a form (it raises), which must NOT
      bubble up as a 500.
    """

    def __init__(
        self,
        method="POST",
        session=None,
        headers=None,
        form_data=None,
        form_raises=False,
    ):
        self.method = method
        self.session = {} if session is None else session
        self.headers = Headers(headers or {})
        self._form_data = form_data or {}
        self._form_raises = form_raises

    async def form(self):
        if self._form_raises:
            raise RuntimeError("body is not form-encoded")
        return self._form_data


def _issue_token():
    """Establish a session nonce + matching signed token, as a GET render would."""
    req = _FakeRequest(method="GET")
    token = generate_csrf_token(req)
    return req.session, token


def _run(coro):
    return asyncio.run(coro)


def test_valid_token_in_header_passes():
    # The core fix: a JSON client (no form body) supplies the token via the
    # X-CSRF-Token header. Before the fix the header was ignored, so this
    # could never pass.
    session, token = _issue_token()
    req = _FakeRequest(
        session=session,
        headers={"X-CSRF-Token": token},
        form_raises=True,  # JSON body — form parse would raise
    )
    # Returns None (no exception) when valid.
    assert _run(verify_csrf(req)) is None


def test_header_lookup_is_case_insensitive():
    session, token = _issue_token()
    req = _FakeRequest(
        session=session,
        headers={"x-csrf-token": token},
        form_raises=True,
    )
    assert _run(verify_csrf(req)) is None


def test_missing_token_on_json_body_fails_closed_without_500():
    # No header and an unparseable (JSON) body: must reject with 403, and
    # crucially must NOT let the form-parse exception escape as a 500.
    session, _ = _issue_token()
    req = _FakeRequest(session=session, form_raises=True)
    with pytest.raises(HTTPException) as exc:
        _run(verify_csrf(req))
    assert exc.value.status_code == 403


def test_forged_token_in_header_rejected():
    session, _ = _issue_token()
    req = _FakeRequest(
        session=session,
        headers={"X-CSRF-Token": "not-a-valid-signed-token"},
        form_raises=True,
    )
    with pytest.raises(HTTPException) as exc:
        _run(verify_csrf(req))
    assert exc.value.status_code == 403


def test_legacy_form_token_still_passes():
    # Backward compatibility: existing HTML form posts (no header) keep working.
    session, token = _issue_token()
    req = _FakeRequest(session=session, form_data={"csrf_token": token})
    assert _run(verify_csrf(req)) is None


def test_safe_methods_skip_validation():
    # GET/HEAD/OPTIONS must short-circuit before any token check.
    for method in ("GET", "HEAD", "OPTIONS"):
        req = _FakeRequest(method=method, form_raises=True)
        assert _run(verify_csrf(req)) is None


def test_router_has_verify_csrf_dependency():
    # The other half of the fix: the /api router must actually enforce CSRF.
    # Import lazily so the env-file guard above is already in place.
    _orig = pydantic_settings.BaseSettings.__init__
    pydantic_settings.BaseSettings.__init__ = _no_env_file_init
    try:
        from src.api.routes import router
    finally:
        pydantic_settings.BaseSettings.__init__ = _orig

    dep_calls = [d.dependency for d in router.dependencies]
    assert verify_csrf in dep_calls
