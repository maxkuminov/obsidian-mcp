"""#183/#198 — a dead cookie cannot reach the consent screen or approve a grant.

The consent form is the one panel surface that stays open across a logout: a
user starts an OAuth authorization, wanders off, signs out in another tab, and
the form is still sitting there with its cookie attached. Before the session
registry that cookie was still a credential — `logout()` cleared it and nothing
else, and a signed cookie cannot be un-signed — so the abandoned form could
still mint an authorization code minutes after the browser was "signed out".

The `oauth-authorization-integrity` delta says both the display **and** the
approval resolve identity through the one validator, and that a cookie killed
by *any* of the four invalidating actions is unable to do either. So every
action is driven end to end against both handlers rather than one example of
each:

| action | what it does to the session |
| --- | --- |
| logout | the real handler revokes this row |
| administrator reset | bumps `session_version` **and** revokes every row |
| deactivation | clears `is_active` and revokes every row |
| revocation | revokes every row, account untouched |

Nothing here stubs `get_active_session_user`. The sibling tests in
`test_followon_auth_routing.py` monkeypatch the resolver to prove the handlers
*call* it; these drive the production resolver against
`session_helpers.FakeRegistry` so each refusal comes from a real dead row —
and each asserts the cookie is left **empty**, which is what stops the same
form being resubmitted against a different route.
"""
from __future__ import annotations

import pytest

import session_helpers as sh
from src.auth import routes as auth_routes
from src.auth.session import SESSION_ID_KEY, revoke_user_sessions
from src.oauth import routes as oauth_routes

#: The consent form's constant arguments. The PKCE challenge only has to be a
#: well-formed S256 value; nothing here exercises the verifier.
CHALLENGE = "a" * 43
CLIENT_ID = "client-under-test"
REDIRECT_URI = "https://client.example/callback"
AUTHORIZE_QUERY = (
    f"response_type=code&client_id={CLIENT_ID}"
    f"&redirect_uri={REDIRECT_URI}&code_challenge={CHALLENGE}"
    "&code_challenge_method=S256&scope=read&state=client-state"
)


@pytest.fixture
def multi_user(monkeypatch):
    """Both handlers only resolve an identity in multi-user mode."""
    monkeypatch.setattr(oauth_routes.settings, "multi_user_mode", True)


@pytest.fixture
def registry_backed(monkeypatch):
    """Point `oauth_routes.async_session` at a registry the test controls.

    `FakeRegistry` is its own async context manager, so it stands in for the
    session factory directly. It raises on any statement it does not
    recognise — which is what proves these refusals happen *before* the client
    lookup, since it never learns what an `oauth_clients` row is.
    """

    def _install(registry):
        monkeypatch.setattr(oauth_routes, "async_session", lambda: registry)
        return registry

    return _install


# --- the four ways a session dies -----------------------------------------


async def _logout(user, registry, request):
    await auth_routes.logout(request=request, session=registry)


async def _admin_reset(user, registry, request):
    """`reset_password`: a new hash, a bumped generation, every row revoked."""
    user.password_hash = "$2b$12$" + "z" * 53
    user.session_version = (user.session_version or 0) + 1
    await revoke_user_sessions(registry, user.id)
    await registry.commit()


async def _deactivation(user, registry, request):
    user.is_active = False
    await revoke_user_sessions(registry, user.id)
    await registry.commit()


async def _revocation(user, registry, request):
    """The sweep on its own — the account itself is untouched."""
    assert await revoke_user_sessions(registry, user.id) == 1
    await registry.commit()


INVALIDATORS = {
    "logout": _logout,
    "admin_reset": _admin_reset,
    "deactivation": _deactivation,
    "revocation": _revocation,
}


async def _signed_in(registry_backed):
    user = sh.fake_user(user_id=7, session_version=3)
    sid, request, registry = await sh.sign_in(user)
    registry_backed(registry)
    assert request.session[SESSION_ID_KEY] == sid
    return user, request, registry


async def _authorize_get(session_cookie):
    opening = sh.browser_request(
        method="GET", path="/authorize", query=AUTHORIZE_QUERY, session=session_cookie
    )
    response = await oauth_routes.authorize_get(
        request=opening,
        response_type="code",
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        code_challenge=CHALLENGE,
        code_challenge_method="S256",
        scope="read",
        state="client-state",
    )
    return opening, response


async def _authorize_post(session_cookie):
    server_state = "server-state"
    signed_state = oauth_routes._state_serializer().dumps(server_state)
    submitting = sh.browser_request(
        method="POST",
        path="/authorize",
        session=session_cookie,
        cookies={"oauth_state": signed_state},
    )
    response = await oauth_routes.authorize_post(
        request=submitting,
        action="approve",
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        code_challenge=CHALLENGE,
        code_challenge_method="S256",
        scope="read",
        state=server_state,
        client_state="client-state",
    )
    return submitting, response


def _codes(registry):
    return [obj for obj in registry.added if type(obj).__name__ == "OAuthCode"]


# --- the approval half ----------------------------------------------------


@pytest.mark.parametrize("action", sorted(INVALIDATORS))
async def test_a_dead_cookie_cannot_approve_a_grant(multi_user, registry_backed, action):
    """The scenario "A logged-out cookie cannot approve a grant", for each of
    the four actions that can kill the session under an open consent form."""
    user, request, registry = await _signed_in(registry_backed)

    # The copy the abandoned form still holds, taken while it works.
    held = dict(request.session)

    await INVALIDATORS[action](user, registry, request)

    submitting, response = await _authorize_post(held)

    assert response.status_code == 401
    assert b"login_required" in response.body
    assert _codes(registry) == [], "no authorization code was minted"
    # The refusal takes the cookie with it, so the same form cannot be
    # resubmitted against a different route either.
    assert submitting.session == {}


# --- the display half -----------------------------------------------------


@pytest.mark.parametrize("action", sorted(INVALIDATORS))
async def test_a_dead_cookie_cannot_open_the_consent_screen(
    multi_user, registry_backed, action
):
    """The scenario "A revoked session cannot open the consent screen" — the
    GET is not merely unable to reach the panel, it cannot render consent."""
    user, request, registry = await _signed_in(registry_backed)
    held = dict(request.session)

    await INVALIDATORS[action](user, registry, request)

    opening, response = await _authorize_get(held)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("/admin/auth/login?")
    # The whole /authorize URL is preserved so the client need not re-issue it.
    assert "%2Fauthorize" in location
    assert _codes(registry) == []
    assert opening.session == {}


# --- the negative controls ------------------------------------------------


@pytest.mark.parametrize("handler", [_authorize_get, _authorize_post])
async def test_a_live_session_still_reaches_the_client_lookup(
    multi_user, registry_backed, handler
):
    """Without this the sixteen cases above pass just as well against handlers
    that refuse every consent request.

    `FakeRegistry` does not model `oauth_clients`, so a live session gets past
    the identity check and dies on the client lookup instead — which is the
    next thing either handler does.
    """
    _user, request, _registry = await _signed_in(registry_backed)

    with pytest.raises(AssertionError, match="unexpected statement"):
        await handler(dict(request.session))
