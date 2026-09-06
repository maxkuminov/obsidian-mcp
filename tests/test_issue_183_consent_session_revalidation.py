"""#183/#198 — a dead cookie cannot reach the consent screen or approve a grant.

The consent form is the one panel surface that stays open across a logout: a
user starts an OAuth authorization, wanders off, signs out in another tab, and
the form is still sitting there with its cookie attached. Before the session
registry that cookie was still a credential — `logout()` cleared it and nothing
else, and a signed cookie cannot be un-signed — so the abandoned form could
still mint an authorization code minutes after the browser was "signed out".

The two scenarios below are the ones the `oauth-authorization-integrity` delta
added for exactly that:

* *A logged-out cookie cannot approve a grant* — the **real** `logout` handler
  revokes the row, and the pre-logout copy of the cookie is then submitted to
  `authorize_post`.
* *A revoked session cannot open the consent screen* — an administrator's
  revocation, and then `authorize_get` with the cookie that revocation killed.

Nothing here stubs `get_active_session_user`. The sibling tests in
`test_followon_auth_routing.py` monkeypatch the resolver to prove the handlers
*call* it; these drive the production resolver against
`session_helpers.FakeRegistry` so the refusal comes from a real revoked row.
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
    recognise — which is the assertion that these refusals happen *before* the
    client lookup, since it never learns what an `oauth_clients` row is.
    """

    def _install(registry):
        monkeypatch.setattr(oauth_routes, "async_session", lambda: registry)
        return registry

    return _install


async def test_a_logged_out_cookie_cannot_approve_a_grant(multi_user, registry_backed):
    user = sh.fake_user(user_id=7)
    sid, request, registry = await sh.sign_in(user)
    registry_backed(registry)

    # The copy the abandoned consent form still holds, taken while it works.
    stolen = dict(request.session)
    assert stolen[SESSION_ID_KEY] == sid

    # ...and then the user signs out in another tab. The real handler.
    await auth_routes.logout(request=request, session=registry)
    assert registry.sessions[0].revoked_at is not None

    server_state = "server-state"
    signed_state = oauth_routes._state_serializer().dumps(server_state)
    replay = sh.browser_request(
        method="POST",
        path="/authorize",
        session=stolen,
        cookies={"oauth_state": signed_state},
    )

    response = await oauth_routes.authorize_post(
        request=replay,
        action="approve",
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        code_challenge=CHALLENGE,
        code_challenge_method="S256",
        scope="read",
        state=server_state,
        client_state="client-state",
    )

    assert response.status_code == 401
    assert b"login_required" in response.body
    # No code minted — and the refusal took the cookie with it, so the same
    # form cannot be resubmitted against a different route either.
    assert [obj for obj in registry.added if type(obj).__name__ == "OAuthCode"] == []
    assert replay.session == {}


async def test_a_revoked_session_cannot_open_the_consent_screen(
    multi_user, registry_backed
):
    user = sh.fake_user(user_id=7)
    sid, request, registry = await sh.sign_in(user)
    registry_backed(registry)

    held = dict(request.session)

    # An administrator resets the password / deactivates the account: every
    # row of this user's is revoked.
    assert await revoke_user_sessions(registry, user.id) == 1

    opening = sh.browser_request(
        method="GET",
        path="/authorize",
        query=AUTHORIZE_QUERY,
        session=held,
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

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("/admin/auth/login?")
    # The whole /authorize URL is preserved so the client need not re-issue it.
    assert "%2Fauthorize" in location
    assert opening.session == {}


async def test_the_live_cookie_still_reaches_the_client_lookup(
    multi_user, registry_backed
):
    """The negative control: an unrevoked session is *not* what refuses.

    Without this the two tests above pass just as well against a handler that
    refuses every consent request. `FakeRegistry` does not model
    `oauth_clients`, so a live session gets past the identity check and dies on
    the client lookup instead — which is the next thing `authorize_get` does.
    """
    user = sh.fake_user(user_id=7)
    _sid, request, registry = await sh.sign_in(user)
    registry_backed(registry)

    opening = sh.browser_request(
        method="GET",
        path="/authorize",
        query=AUTHORIZE_QUERY,
        session=dict(request.session),
    )

    with pytest.raises(AssertionError, match="unexpected statement"):
        await oauth_routes.authorize_get(
            request=opening,
            response_type="code",
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            code_challenge=CHALLENGE,
            code_challenge_method="S256",
            scope="read",
            state="client-state",
        )
