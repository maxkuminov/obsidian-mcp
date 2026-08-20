"""The admin panel's OAuth scope dropdown must reflect the *granted* permission.

`OAuthToken.scope` is a space-separated set (e.g. "offline_access readwrite" --
offline_access is requested by every DCR-registered client per
DEFAULT_CLIENT_SCOPE in src/oauth/routes.py). oauth.html's <select> originally
compared `token.scope == 'read'` / `== 'readwrite'` directly: neither literal
ever matches a scope string that also carries offline_access, so NEITHER
<option> got `selected`, the browser fell back to the first one, and a real
readwrite grant rendered as read-only. #62 fixed that by computing a
membership-based `has_write` in `oauth_page`.

**These tests exercise the route, not a hand-built context** -- that is issue
#65 gap 1. The original fixture supplied both `scope` and `has_write` to the
template, so setting `has_write=False` unconditionally in `oauth_page` left
every assertion green: the route half of the fix was untested. Here the fake
session yields `OAuthToken`-shaped rows and `oauth_page` derives everything,
so a regression in the derivation fails these tests.

Gap 2 (an assertion that only exercised Python's `in`) is replaced by calls
into the production helper `src.oauth.scope.token_has_write`, which is now also
what `src/mcp_server/auth.py` maps a token to a permission with -- those two
agreeing is the property that actually matters.
"""
import asyncio
import re

import pytest

from src.control_panel import routes
from src.mcp_server import auth as mcp_auth
from src.oauth.scope import token_has_write

from _oauth_grant_fakes import (
    FakeClient,
    FakeRequest,
    FakeSession,
    FakeToken,
    FakeUser,
    SingleUserSentinel,
    in_hours,
)


def render_oauth_page(*, clients, tokens, users=(), user=None) -> str:
    """Run the real `oauth_page` over fake rows and return the rendered HTML."""
    session = FakeSession(clients=clients, tokens=tokens, users=users)
    response = asyncio.run(
        routes.oauth_page(
            request=FakeRequest(),
            session=session,
            user=user or SingleUserSentinel(),
        )
    )
    return response.body.decode()


def selected_option(html: str) -> str:
    """The value= of whichever <option> in the scope <select> is selected."""
    found = []
    for value in ("read", "readwrite"):
        m = re.search(rf'<option value="{value}"\s*[^>]*>', html)
        if m is None:
            continue
        found.append(value)
        if "selected" in m.group(0):
            return value
    assert found, "no scope <option> rendered at all"
    return "NONE-SELECTED"


def rendered_options(html: str) -> set[str]:
    return set(re.findall(r'<option value="(read|readwrite)"', html))


# --- oauth_page derives has_write from the token's own scope (gap 1) -------


@pytest.mark.parametrize(
    "scope, expected",
    [
        ("offline_access readwrite", "readwrite"),
        ("offline_access read", "read"),
        ("readwrite", "readwrite"),
        ("read", "read"),
        # Order within the set must not matter.
        ("readwrite offline_access", "readwrite"),
    ],
)
def test_oauth_page_derives_selected_scope_from_token_scope(scope, expected):
    """The route -- not the fixture -- decides which option is selected.

    Nothing here supplies `has_write`; `oauth_page` computes it from `scope`.
    Hard-coding `has_write=False` in the route (issue #65's demonstration of
    the gap) flips every `readwrite` case to `read` and fails.
    """
    html = render_oauth_page(
        clients=[FakeClient()],
        tokens=[FakeToken(grant_id="g1", scope=scope)],
    )
    assert selected_option(html) == expected


def test_oauth_page_and_middleware_agree_on_every_scope_string():
    """The panel badge and the enforced permission come from one helper.

    Before this, `oauth_page` and `src/mcp_server/auth.py` each carried their
    own membership check. They agreed by coincidence, which is not a property
    anything could test; now there is a single definition and this pins both
    ends of it.
    """
    for scope in (
        "read",
        "readwrite",
        "offline_access read",
        "offline_access readwrite",
        "",
    ):
        middleware_permission = "readwrite" if token_has_write(scope) else "read"
        html = render_oauth_page(
            clients=[FakeClient()],
            tokens=[FakeToken(grant_id="g1", scope=scope)],
        )
        assert selected_option(html) == middleware_permission, scope

    # And the middleware really is built on that helper, not a private copy.
    source = mcp_auth.__file__
    with open(source) as fh:
        body = fh.read()
    assert "token_has_write(oauth_token.scope)" in body


# --- the helper itself (gap 2: was a tautology over Python builtins) -------


def test_token_has_write_is_membership_not_equality():
    """Calls production code. The assertion it replaces could not fail."""
    assert token_has_write("offline_access readwrite") is True
    assert token_has_write("readwrite offline_access") is True
    assert token_has_write("readwrite") is True
    assert token_has_write("offline_access read") is False
    assert token_has_write("read") is False
    assert token_has_write("") is False
    assert token_has_write(None) is False
    # The trap the display bug fell into: a set is not its longest member.
    assert ("offline_access readwrite" == "readwrite") is False


# --- update_oauth_token_scope preserves offline_access ---------------------


def _update_scope(tokens, clients, scope, token_id=None):
    session = FakeSession(clients=clients, tokens=tokens)
    asyncio.run(
        routes.update_oauth_token_scope(
            token_id=token_id if token_id is not None else tokens[0].id,
            scope=scope,
            session=session,
            user=SingleUserSentinel(),
        )
    )
    return session


def test_update_scope_preserves_offline_access_marker():
    token = FakeToken(grant_id="g1", scope="offline_access readwrite")
    session = _update_scope([token], [FakeClient()], "read")

    assert set(token.scope.split()) == {"read", "offline_access"}
    assert session.committed == 1


def test_update_scope_without_offline_access_stays_bare():
    token = FakeToken(grant_id="g1", scope="readwrite")
    _update_scope([token], [FakeClient()], "read")

    assert token.scope == "read"


# --- revoked and expired rows stay visible (issue #64) --------------------


def test_revoked_tokens_are_listed_so_a_revocation_is_visible():
    """Filtering revoked rows out made a no-op Revoke look like it worked."""
    html = render_oauth_page(
        clients=[FakeClient()],
        tokens=[
            FakeToken(grant_id="g1", token_type="access", revoked=True),
            FakeToken(grant_id="g1", token_type="refresh", revoked=True),
        ],
    )
    assert "Revoked" in html
    # A fully revoked grant offers no controls to act on.
    assert "/revoke" not in html
    assert "<option" not in html


def test_expired_token_renders_expired_not_active():
    html = render_oauth_page(
        clients=[FakeClient()],
        tokens=[FakeToken(grant_id="g1", expires_at=in_hours(-1))],
    )
    assert "Expired" in html
    assert 'class="badge badge-green"' not in html


# --- one control per grant, not one per row (issue #64) -------------------


def test_a_grant_renders_exactly_one_revoke_control_and_one_scope_select():
    """Two rows, one grant, one of each control.

    Per-row controls are what misled the operator: two independently editable
    scope selects for one grant, and a Revoke on the access row that the
    sibling refresh token undid within the hour.
    """
    html = render_oauth_page(
        clients=[FakeClient()],
        tokens=[
            FakeToken(grant_id="g1", token_type="access", scope="readwrite"),
            FakeToken(grant_id="g1", token_type="refresh", scope="readwrite"),
        ],
    )
    assert html.count("/revoke") == 1
    assert html.count("<select") == 1


def test_two_grants_for_one_client_get_separate_controls():
    html = render_oauth_page(
        clients=[FakeClient()],
        tokens=[
            FakeToken(grant_id="g1", token_type="access"),
            FakeToken(grant_id="g1", token_type="refresh"),
            FakeToken(grant_id="g2", token_type="access"),
            FakeToken(grant_id="g2", token_type="refresh"),
        ],
    )
    assert html.count("/revoke") == 2
    assert html.count("<select") == 2


# --- issue #76: the owner's is_active is part of "Active" ----------------


def test_deactivated_owner_makes_the_grant_badge_owner_inactive():
    """`APIKeyMiddleware` 401s these tokens; the panel used to badge them green."""
    html = render_oauth_page(
        clients=[FakeClient(user_id=7)],
        tokens=[FakeToken(grant_id="g1", user_id=7)],
        users=[FakeUser(id=7, is_active=False)],
    )
    assert "Owner inactive" in html
    assert 'class="badge badge-green"' not in html


def test_active_owner_still_badges_active():
    html = render_oauth_page(
        clients=[FakeClient(user_id=7)],
        tokens=[FakeToken(grant_id="g1", user_id=7)],
        users=[FakeUser(id=7, is_active=True)],
    )
    assert "Owner inactive" not in html
    assert 'class="badge badge-green"' in html


def test_single_user_tokens_have_no_owner_to_check():
    """`user_id IS NULL` is single-user mode; the middleware skips the check."""
    html = render_oauth_page(
        clients=[FakeClient()],
        tokens=[FakeToken(grant_id="g1", user_id=None)],
    )
    assert "Owner inactive" not in html
    assert 'class="badge badge-green"' in html


# --- issue #67: the readwrite option is gated on the client's registration -


def test_read_only_client_never_renders_the_readwrite_option():
    html = render_oauth_page(
        clients=[FakeClient(scope="read")],
        tokens=[FakeToken(grant_id="g1", scope="read")],
    )
    assert rendered_options(html) == {"read"}


def test_readwrite_client_renders_both_options():
    html = render_oauth_page(
        clients=[FakeClient(scope="read readwrite offline_access")],
        tokens=[FakeToken(grant_id="g1", scope="read")],
    )
    assert rendered_options(html) == {"read", "readwrite"}
