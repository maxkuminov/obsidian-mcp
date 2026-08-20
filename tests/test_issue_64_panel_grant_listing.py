"""The OAuth page must never hide a live grant, or under-report its scope.

Two failures the adversarial pass found, both in `oauth_page`'s listing:

* **A bound applied before grants were identified.** One `LIMIT` over all of a
  client's tokens, newest first, meant a chatty grant's rotations could push
  another grant's live refresh token off the page — a working credential with
  no control to revoke it. Live rows are now read unbounded and only *history*
  is capped.
* **Scope read from the newest live row alone.** Migration 014's backfill
  groups by `(client_id, user_id)`, so two pre-014 sessions of the same
  connector legitimately merge into one family — and they can disagree, one
  `read` and one `readwrite`. The page showed "read" while an older live access
  token still held write.
"""
import asyncio
import re

import pytest

from src.control_panel import routes as panel

from _oauth_grant_fakes import (
    FakeClient,
    FakeRequest,
    FakeSession,
    FakeToken,
    SingleUserSentinel,
    in_hours,
    utcnow,
)

import datetime


def render(clients, tokens, users=(), user=None) -> str:
    session = FakeSession(clients=clients, tokens=tokens, users=users)
    response = asyncio.run(
        panel.oauth_page(
            request=FakeRequest(),
            session=session,
            user=user or SingleUserSentinel(),
        )
    )
    return response.body.decode()


def grants_of(clients, tokens, users=()):
    """The route's own per-grant view dicts, before the template sees them."""
    session = FakeSession(clients=clients, tokens=tokens, users=users)
    captured = {}
    real = panel.templates.TemplateResponse

    def _capture(request, name, context, *a, **kw):
        captured.update(context)
        return real(request, name, context, *a, **kw)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(panel.templates, "TemplateResponse", _capture)
        asyncio.run(
            panel.oauth_page(
                request=FakeRequest(), session=session, user=SingleUserSentinel()
            )
        )
    finally:
        monkeypatch.undo()
    return {g["grant_id"]: g for g in captured["clients"][0]["grants"]}


def aged(minutes: int) -> datetime.datetime:
    """A `created_at` `minutes` in the past — newer rows sort first."""
    return utcnow() - datetime.timedelta(minutes=minutes)


# --- a live grant is never pushed off the page ---------------------------


def test_a_chatty_grants_history_cannot_hide_another_grants_live_token():
    """The exact shape of the finding: 501 rotations on one grant.

    Under a single `LIMIT 1000` over all of a client's tokens ordered newest
    first, a grant whose live pair is *older* than a thousand rotated-away rows
    disappeared — no row, no Revoke button, no way to end it from the UI, while
    the credential kept working for its full 30 days.
    """
    noisy = []
    for i in range(1200):
        noisy.append(
            FakeToken(
                grant_id="chatty",
                token_type="refresh",
                revoked=True,
                created_at=aged(i + 1),
                expires_at=in_hours(720),
            )
        )
    quiet = [
        FakeToken(
            grant_id="quiet",
            token_type="access",
            created_at=aged(5000),
            expires_at=in_hours(1),
        ),
        FakeToken(
            grant_id="quiet",
            token_type="refresh",
            created_at=aged(5000),
            expires_at=in_hours(720),
        ),
    ]

    grants = grants_of([FakeClient()], noisy + quiet)

    assert "quiet" in grants, "a live grant was dropped from the listing"
    assert grants["quiet"]["token_id"] is not None, "no control to revoke it"
    assert grants["quiet"]["status"] == "active"


def test_the_live_query_is_unbounded_and_only_history_is_capped():
    """Stated as a property of the source, so the bound cannot creep back."""
    import inspect

    source = inspect.getsource(panel.oauth_page)
    assert "CLIENT_DEAD_TOKEN_SCAN_LIMIT" in source
    live_half, dead_half = source.split("dead_q", 1)
    assert ".limit(" not in live_half, "the live query must not be bounded"


def test_history_truncation_is_declared_rather_than_miscounted():
    """A count taken from a truncated window would understate silently."""
    dead = [
        FakeToken(
            grant_id="g1",
            token_type="refresh",
            revoked=True,
            created_at=aged(i + 1),
        )
        for i in range(panel.CLIENT_DEAD_TOKEN_SCAN_LIMIT + 50)
    ]
    live = [FakeToken(grant_id="g1", token_type="access", created_at=aged(0))]

    grants = grants_of([FakeClient()], dead + live)

    assert grants["g1"]["history_truncated"] is True
    html = render([FakeClient()], dead + live)
    assert "beyond the history window" in html


def test_a_small_client_is_not_marked_truncated():
    live = [FakeToken(grant_id="g1", token_type="access")]
    dead = [FakeToken(grant_id="g1", token_type="refresh", revoked=True)]

    grants = grants_of([FakeClient()], live + dead)

    assert grants["g1"]["history_truncated"] is False


def test_a_fully_revoked_grant_is_still_listed():
    """History is what makes a revocation visibly durable (issue #64)."""
    dead = [
        FakeToken(grant_id="g1", token_type="access", revoked=True),
        FakeToken(grant_id="g1", token_type="refresh", revoked=True),
    ]

    grants = grants_of([FakeClient()], dead)

    assert grants["g1"]["status"] == "revoked"
    assert grants["g1"]["token_id"] is None


# --- a mixed-scope family reports its strongest live permission ----------


def mixed_family():
    """What 014 produces from two pre-014 sessions of one client and user."""
    return [
        # Newest live row is read-only...
        FakeToken(
            grant_id="g1",
            token_type="refresh",
            scope="offline_access read",
            created_at=aged(1),
            expires_at=in_hours(720),
        ),
        # ...while an older live access token still carries write.
        FakeToken(
            grant_id="g1",
            token_type="access",
            scope="offline_access readwrite",
            created_at=aged(10),
            expires_at=in_hours(1),
        ),
    ]


def test_a_mixed_family_reports_write_not_the_newest_rows_scope():
    """`any`, not "the first one" — over-reporting capability is the safe side."""
    grants = grants_of([FakeClient()], mixed_family())

    assert grants["g1"]["has_write"] is True
    assert grants["g1"]["mixed_scope"] is True
    assert set(grants["g1"]["live_scopes"]) == {
        "offline_access read",
        "offline_access readwrite",
    }


def test_a_mixed_family_is_marked_in_the_rendered_page():
    html = render([FakeClient()], mixed_family())

    assert ">mixed<" in html
    # And the select reflects the strongest live permission.
    m = re.search(r'<option value="readwrite"\s*[^>]*>', html)
    assert m and "selected" in m.group(0)


def test_a_uniform_family_is_not_marked_mixed():
    family = [
        FakeToken(grant_id="g1", token_type="access", scope="readwrite"),
        FakeToken(grant_id="g1", token_type="refresh", scope="readwrite"),
    ]

    grants = grants_of([FakeClient()], family)

    assert grants["g1"]["mixed_scope"] is False
    assert grants["g1"]["has_write"] is True


def test_a_revoked_readwrite_row_does_not_make_a_read_grant_look_mixed():
    """Only *live* tokens decide; history is history."""
    family = [
        FakeToken(grant_id="g1", token_type="access", scope="read"),
        FakeToken(
            grant_id="g1", token_type="refresh", scope="readwrite", revoked=True
        ),
    ]

    grants = grants_of([FakeClient()], family)

    assert grants["g1"]["mixed_scope"] is False
    assert grants["g1"]["has_write"] is False


# --- and writing a scope makes the family uniform again ------------------


def test_saving_a_scope_writes_one_value_across_a_mixed_family():
    family = mixed_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)

    asyncio.run(
        panel.update_oauth_token_scope(
            token_id=family[0].id,
            request=None,
            scope="read",
            session=session,
            user=SingleUserSentinel(),
        )
    )

    assert len({t.scope for t in family}) == 1, "the family must end up uniform"
    for token in family:
        assert "readwrite" not in token.scope.split()
        assert "offline_access" in token.scope.split()


def test_offline_access_is_taken_from_the_whole_live_family():
    """Reading the marker off one row gets a mixed family wrong.

    Here the row the operator clicked has no `offline_access` and its sibling
    does; the write must not silently strip the marker from the sibling.
    """
    family = [
        FakeToken(grant_id="g1", token_type="access", scope="readwrite", created_at=aged(1)),
        FakeToken(
            grant_id="g1",
            token_type="refresh",
            scope="offline_access readwrite",
            created_at=aged(10),
            expires_at=in_hours(720),
        ),
    ]
    session = FakeSession(clients=[FakeClient()], tokens=family)

    asyncio.run(
        panel.update_oauth_token_scope(
            token_id=family[0].id,
            request=None,
            scope="read",
            session=session,
            user=SingleUserSentinel(),
        )
    )

    for token in family:
        assert set(token.scope.split()) == {"read", "offline_access"}
