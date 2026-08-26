"""One-shot panel flash messages, carried in the session and not the URL.

The panel's post-redirect-get messages used to ride the query string
(`/admin/users/?flash=…`, `?error=…`, `&flash_kind=err`) and the templates
rendered whatever was there. Jinja escapes it, so there was no XSS — but the
*text* an authenticated admin reads was chosen by whoever composed the link.
A crafted `/admin/users/?flash=Vault reassigned — click Delete to finish` is
a message from the server as far as the page is concerned, on the one surface
whose controls delete accounts (#138, A6 of #130).

So the message travels in the session cookie instead, which only this server
can write:

- `flash(request, message, kind=…)` stores it, immediately before returning
  the redirect;
- `pop_flash(request)` takes it back out, and `_panel_context` in
  `src/control_panel/routes.py` is the only caller — every panel render pops
  exactly once, so a flash is shown once and is gone on reload. A query
  parameter survived every reload and every re-share of the URL.

Two shapes are deliberate. **One entry, last write wins**: no handler emits
two messages, and a queue would let an abandoned redirect's message surface
on an unrelated page much later. And **the session may be absent**: some unit
-test harnesses build an app with no `SessionMiddleware`, where
`request.session` raises. Both helpers swallow that, exactly as
`create_key_form` and `_flash_oauth_error` already do — a message going
unshown must never turn a completed action into a 500.
"""
from typing import Any

from starlette.requests import Request

# The session key. Namespaced so it cannot collide with `user_id`,
# `session_version`, `flash_new_key` or anything else parked in the cookie.
FLASH_SESSION_KEY = "panel_flash"

# `ok` renders as a success alert, `err` as a warning one. Anything else is
# read as `ok` on the way out rather than rendered as an unknown class.
OK = "ok"
ERR = "err"


def flash(request: Request | None, message: str, kind: str = OK) -> None:
    """Park `message` for the next panel render of this session.

    `request` is tolerated as `None` because a handler may be called without
    one (unit tests, and `update_oauth_token_scope`'s `Request | None`
    signature): there is then nowhere to put the message, and the redirect
    still happens. The refusal itself never depends on the flash.
    """
    if request is None or not message:
        return
    if kind not in (OK, ERR):
        kind = OK
    try:
        request.session[FLASH_SESSION_KEY] = {"message": str(message), "kind": kind}
    except (AssertionError, AttributeError):
        # No SessionMiddleware in this app (some unit-test harnesses).
        pass


def pop_flash(request: Request | None) -> tuple[str | None, str]:
    """Return `(message, kind)` and remove it. `(None, "ok")` if there is none."""
    if request is None:
        return None, OK
    try:
        raw: Any = request.session.pop(FLASH_SESSION_KEY, None)
    except (AssertionError, AttributeError):
        return None, OK
    if not isinstance(raw, dict):
        # A shape written by an older deploy, or nothing at all. The cookie is
        # signed, so this is never attacker-chosen — but it must not 500 the
        # page either.
        return None, OK
    message = raw.get("message")
    if not isinstance(message, str) or not message:
        return None, OK
    kind = raw.get("kind")
    return message, kind if kind in (OK, ERR) else OK
