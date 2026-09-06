"""The application's shared slowapi limiter and the key functions routes use.

`get_remote_address` keys on the client address and is the default. The
password-change handler (#197) carries a **second, account-keyed** limit
alongside it, because neither key subsumes the other: an address-only key hands
an attacker a fresh allowance for every address they rotate through, and an
account-only key lets one address walk many accounts. Stacked decorators are
how slowapi expresses both.
"""
from starlette.requests import Request

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

# What `session_user_key` answers when the request carries no authenticated
# panel session. Every such request shares one bucket, which is the intended
# behaviour: the routes this key is used on are behind `require_user_panel`, so
# an unauthenticated caller never reaches the handler anyway, and a shared
# bucket is a bound rather than a bypass. It is deliberately not the client
# address — mixing the two key spaces would let an attacker's address collide
# with an account name.
ANONYMOUS_SESSION_KEY = "panel-session:anonymous"


def session_user_key(request: Request) -> str:
    """Rate-limit key naming the authenticated panel account, for slowapi.

    **Synchronous, and it must stay that way.** slowapi computes the key
    *before* the handler runs and cannot await anything: the account therefore
    has to come from somewhere already parsed, which is `request.session` —
    `SessionMiddleware` has decoded the signed cookie by the time any route
    dependency runs. Reading the request body here instead (to key on a
    submitted username, say) would consume the stream the handler needs.

    The value read is the cookie's `user_id`, not a database row. That is
    sufficient for a throttle: it is signed, so it cannot be forged, and the
    session validator refuses the request a moment later if the row behind it
    is revoked, expired or inactive. A limiter key is a bucket name, never an
    authorization decision.

    **A missing `SessionMiddleware` degrades rather than raises.** Starlette's
    `request.session` asserts the middleware is installed, and an
    `AssertionError` escaping a `key_func` would turn a throttled route into a
    500 for every caller. Test harnesses mount routers without the middleware,
    and so does any future ASGI app that borrows one — so the degraded answer
    is the shared anonymous bucket.
    """
    try:
        user_id = request.session.get("user_id")
    except (AssertionError, AttributeError, KeyError):
        return ANONYMOUS_SESSION_KEY
    if user_id is None:
        return ANONYMOUS_SESSION_KEY
    return f"panel-session:{user_id}"
