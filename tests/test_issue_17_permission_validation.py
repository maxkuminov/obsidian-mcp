"""Regression test for GitHub issue #17: the control-panel key-creation form
persists the `permission` field verbatim with no validation.

`create_key_form` (the @router.post("/keys/create") handler in
`src/control_panel/routes.py`) accepted `permission: str = Form("read")` and
wrote it straight into the `APIKey` ORM object. The `permission` column is an
unconstrained `String(20)`, so any value up to 20 chars persisted. The keys.html
`<select>` only constrains the *UI*, so a scripted/tampered POST could submit
e.g. "admin" (nonsense) or "readwrite " with a trailing space (silently behaves
as read-only because the write gate checks an exact `!= "readwrite"`). The JSON
API path validated the same field, so the two creation paths were inconsistent.

The fix normalizes the value before constructing the ORM object: anything that
is not exactly "read" or "readwrite" is coerced to "read", failing safe and
matching the JSON API's invariant.

Runs fully offline: no DB, no network, no embedding provider. The DB layer is a
tiny in-memory fake `_FakeSession` that captures the `add()`ed `APIKey`; the
request/user are minimal stand-ins. We assert on the persisted `permission`.
"""
# Point pydantic-settings at a non-existent env file BEFORE importing
# `src.control_panel.routes` (which imports `src.config`) so config loads purely
# from process env + conftest defaults, independent of the dev host's `.env`.
import pydantic_settings  # noqa: E402

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    import asyncio

    import pytest

    from src.control_panel import routes
    from src.models.db import APIKey
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


class _FakeSession:
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


class _FakeUser:
    id = 1


class _FakeRequest:
    """Minimal stand-in: create_key_form only touches `request.session`."""

    def __init__(self):
        self.session = {}


def _create(permission):
    """Run create_key_form with the given raw permission value and return the
    persisted APIKey object."""
    session = _FakeSession()
    resp = asyncio.run(
        routes.create_key_form(
            request=_FakeRequest(),
            name="my-key",
            permission=permission,
            # Empty is "unlimited" (#162). Passed explicitly because calling the
            # handler directly bypasses FastAPI's form binding, so an omitted
            # argument arrives as the `Form(...)` marker rather than as its
            # default value.
            daily_request_limit="",
            session=session,
            user=_FakeUser(),
        )
    )
    # The handler always redirects on success and persists exactly one key.
    assert resp.status_code == 303
    assert len(session.added) == 1
    assert isinstance(session.added[0], APIKey)
    return session.added[0]


# --- The core regression: junk / non-canonical values fail safe to read ---


def test_nonsense_permission_is_coerced_to_read():
    key = _create("admin")
    assert key.permission == "read"


def test_trailing_space_readwrite_is_coerced_to_read():
    # "readwrite " behaves as read-only at the write gate but used to persist
    # verbatim, producing silently-broken data. It must normalize to "read".
    key = _create("readwrite ")
    assert key.permission == "read"


def test_empty_permission_is_coerced_to_read():
    key = _create("")
    assert key.permission == "read"


# --- Legitimate values are preserved unchanged ---


def test_read_is_preserved():
    key = _create("read")
    assert key.permission == "read"


def test_readwrite_is_preserved():
    key = _create("readwrite")
    assert key.permission == "readwrite"
