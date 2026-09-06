"""The consent screen says who is asking and where the code would go (#183).

`/register` is unauthenticated (RFC 7591), so every OAuth client on this server
registered itself and every string it supplied is attacker-chosen text. Before
this change the consent screen rendered `client_name` and nothing else: a
client calling itself "Claude" with a redirect to an attacker's host produced a
page indistinguishable from the real connector's, and `/authorize` sits behind
the Google chain, so the reachable population is exactly the panel users who
hold vaults.

What is pinned here:

* the **destination host** is rendered, derived from the redirect URI's
  `hostname` — never its `netloc`, so `https://claude.ai@evil.example/cb`
  reads `evil.example` — and never Unicode-decoded, so a homograph host reads
  `xn--…`;
* the server-assigned `client_id` and the registration date are rendered, both
  values the client cannot choose;
* the **self-registration notice is unconditional** — allow-listed or not,
  because nothing on this server verifies an application;
* the known-destination badge is driven by **exact host equality** against
  `OAUTH_KNOWN_REDIRECT_HOSTS`, so `evilclaude.ai`, `claude.ai.evil.example`
  and an empty list all take the warning branch;
* registration **refuses** a redirect URI with no host (`https://@/cb`
  registered happily: its `netloc` is `"@"` and truthy while its `hostname` is
  empty) or one whose host cannot be converted to ASCII, and stores the
  converted form of one that can;
* a row registered *before* that rule, whose host cannot be determined,
  degrades to an explicit "could not be determined" and always warns.

The consent requests go through a real `TestClient` rather than calling
`authorize_get` directly, for the reason
`test_authorize_get_scope_preselect.py` gives: the query-parameter defaults are
part of what is under test and a direct call would hand the function `Query`
objects. The registration requests call `register_client.__wrapped__` to step
around the `3/minute` limiter, as the existing registration tests do.
"""
import asyncio
import html as html_mod
import json
import re
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.limiter import limiter
from src.oauth import routes
from src.oauth import trust

from _oauth_grant_fakes import SeqSession

VALID_PKCE_CHALLENGE = "a" * 43
WRITE_CAPABLE = "read readwrite offline_access"

# A Cyrillic "а" (U+0430) in front of "pple.com": the homograph case. Its
# A-label is the value the screen must show.
HOMOGRAPH_HOST = "аpple.com"
HOMOGRAPH_ALABEL = "xn--pple-43d.com"


# --- fakes -----------------------------------------------------------------


class _FakeClient:
    def __init__(
        self,
        redirect_uris,
        *,
        client_name="Test Client",
        scope=WRITE_CAPABLE,
        client_id="a3f19c7e5b2d4081a3f19c7e5b2d4081",
        created_at=datetime(2026, 3, 14, 9, 30, tzinfo=timezone.utc),
    ):
        self.client_id = client_id
        self.client_name = client_name
        self.scope = scope
        self.redirect_uris = list(redirect_uris)
        self.created_at = created_at


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeSession:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        return _FakeResult(self._client)


class _RegistrationRequest:
    """The minimum `register_client` reads off a request."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


# --- helpers ---------------------------------------------------------------


def _render(redirect_uri, *, known_hosts=("claude.ai", "chatgpt.com"), client=None):
    """The rendered consent screen for `redirect_uri` under `known_hosts`."""
    client = client if client is not None else _FakeClient([redirect_uri])
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)
        # `trust` and `routes` import the same `settings` singleton, so one
        # patch governs both the matcher and the handler.
        monkeypatch.setattr(
            trust.settings, "oauth_known_redirect_hosts", list(known_hosts)
        )
        monkeypatch.setattr(routes, "async_session", lambda: _FakeSession(client))

        app = FastAPI()
        app.state.limiter = limiter
        app.include_router(routes.router)

        response = TestClient(app).get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client.client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": VALID_PKCE_CHALLENGE,
                "code_challenge_method": "S256",
            },
        )
    finally:
        monkeypatch.undo()
    assert response.status_code == 200
    return response.text


def _text(html):
    """Tag-stripped, whitespace-collapsed page text, for prose assertions."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _element(html, class_attr):
    """The inner text of the single element carrying exactly `class_attr`.

    Returns `None` when no such element is rendered, which is how the branch
    assertions distinguish the badge from the warning.
    """
    match = re.search(
        rf'<div class="{re.escape(class_attr)}">(.*?)</div>', html, re.S
    )
    if match is None:
        return None
    return html_mod.unescape(" ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split()))


def _destination(html):
    """What the destination element shows — host, or the undetermined text."""
    host = _element(html, "identity-value host")
    if host is not None:
        return host
    return _element(html, "identity-value undetermined")


def _badge(html):
    return _element(html, "trust known")


def _warning(html):
    return _element(html, "trust unverified")


def _notice(html):
    return _element(html, "self-registered")


def _register(redirect_uris, **extra):
    """Call `/register` past the limiter. Returns (response body, session)."""
    monkeypatch = pytest.MonkeyPatch()
    session = SeqSession()
    try:
        monkeypatch.setattr(routes, "async_session", lambda: session)
        body = {
            "client_name": "Registrant",
            "redirect_uris": list(redirect_uris),
            "token_endpoint_auth_method": "none",
        }
        body.update(extra)
        response = asyncio.run(
            routes.register_client.__wrapped__(_RegistrationRequest(body))
        )
    finally:
        monkeypatch.undo()
    return response, session


# --- `src/oauth/trust.py`: the derivation and the match --------------------


def test_display_host_comes_from_hostname_not_netloc():
    """The whole point. `netloc` is `claude.ai@evil.example`, whose left edge
    reads as the brand; `hostname` is where the code actually goes."""
    assert (
        trust.redirect_display_host("https://claude.ai@evil.example/cb")
        == "evil.example"
    )


def test_display_host_is_lower_cased_and_drops_the_port():
    assert trust.redirect_display_host("https://EXAMPLE.Test:8443/cb") == "example.test"


def test_display_host_of_a_non_ascii_host_is_the_a_label():
    assert (
        trust.redirect_display_host(f"https://{HOMOGRAPH_HOST}/cb") == HOMOGRAPH_ALABEL
    )


def test_display_host_of_a_hostless_uri_is_the_sentinel():
    assert trust.redirect_display_host("https://@/cb") is trust.UNKNOWN_HOST


def test_known_host_matches_only_on_equality(monkeypatch):
    """A suffix test matches `evilclaude.ai`, a substring test matches
    `claude.ai.evil.example`, and `.claude.ai` hands the badge to any
    subdomain an attacker can obtain."""
    monkeypatch.setattr(trust.settings, "oauth_known_redirect_hosts", ["claude.ai"])

    assert trust.known_redirect_host("https://claude.ai/cb") is True
    assert trust.known_redirect_host("https://CLAUDE.AI/cb") is True
    for look_alike in (
        "https://evilclaude.ai/cb",
        "https://claude.ai.evil.example/cb",
        "https://sub.claude.ai/cb",
        "https://claude.aim/cb",
        "https://claude.ai@evil.example/cb",
    ):
        assert trust.known_redirect_host(look_alike) is False, look_alike


def test_an_empty_allow_list_recognises_nothing(monkeypatch):
    monkeypatch.setattr(trust.settings, "oauth_known_redirect_hosts", [])
    assert trust.known_redirect_host("https://claude.ai/cb") is False


def test_a_hostless_uri_is_never_known_even_if_empty_were_listed(monkeypatch):
    """The sentinel is `None`, so no configured entry can equal it."""
    monkeypatch.setattr(trust.settings, "oauth_known_redirect_hosts", ["", "claude.ai"])
    assert trust.known_redirect_host("https://@/cb") is False


def test_normalisation_leaves_an_ascii_uri_byte_identical():
    """Deliberate: rebuilding an authority is where an IPv6 literal loses its
    brackets and a percent-encoded userinfo loses a character."""
    for uri in (
        "https://example.test/cb",
        "https://example.test:8443/cb?x=1",
        "https://[::1]/cb",
        "https://user%40x:pw@example.test/cb",
    ):
        assert trust.normalize_redirect_uri(uri) == uri, uri


def test_normalisation_converts_a_non_ascii_host_and_keeps_the_rest():
    assert (
        trust.normalize_redirect_uri(f"https://user:pw@{HOMOGRAPH_HOST}:8443/cb?x=1")
        == f"https://user:pw@{HOMOGRAPH_ALABEL}:8443/cb?x=1"
    )


# --- the identity block ----------------------------------------------------


def test_the_destination_host_is_rendered():
    html = _render("https://example.test/cb")
    assert _destination(html) == "example.test"


def test_the_client_identifier_and_registration_date_are_rendered():
    html = _render("https://example.test/cb")
    text = _text(html)

    assert _element(html, "identity-value mono") == "a3f19c7e5b2d4081a3f19c7e5b2d4081"
    assert "2026-03-14" in text


def test_userinfo_cannot_disguise_the_destination():
    """`https://claude.ai@evil.example/cb` must read `evil.example`, and the
    familiar-looking authority must not appear as the destination."""
    html = _render("https://claude.ai@evil.example/cb")

    assert _destination(html) == "evil.example"
    assert _destination(html) != "claude.ai"
    assert _badge(html) is None
    assert "evil.example" in _warning(html)


def test_a_non_ascii_host_renders_punycode_and_is_never_decoded():
    """A decoded homograph host defeats the disclosure entirely, so the value
    is displayed exactly as its A-label."""
    html = _render(f"https://{HOMOGRAPH_HOST}/cb")

    assert _destination(html) == HOMOGRAPH_ALABEL
    # Nothing the user reads carries the decoded form. The hidden
    # `redirect_uri` field still carries the URI verbatim — it is the value
    # the form must submit for `authorize_post` to re-validate — and
    # `_text` strips attributes along with the tags.
    assert HOMOGRAPH_HOST not in _text(html)
    assert HOMOGRAPH_HOST not in (_warning(html) or "")


def test_a_hostile_client_name_is_escaped_and_stays_out_of_the_destination():
    """The name is client-chosen text in its own element; the destination is
    derived from the registered URI and is unaffected by it."""
    client = _FakeClient(
        ["https://evil.example/cb"],
        client_name='<script>alert(1)</script>claude.ai',
    )
    html = _render("https://evil.example/cb", client=client)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert _destination(html) == "evil.example"
    assert _element(html, "identity-value") == "<script>alert(1)</script>claude.ai"


# --- the unconditional notice ---------------------------------------------


def test_the_self_registration_notice_is_present_for_an_unrecognised_client():
    notice = _notice(_render("https://evil.example/cb"))
    assert "registered itself" in notice
    assert "not verified by this server" in notice


def test_the_self_registration_notice_is_present_for_a_recognised_client():
    """Suppressing it for an allow-listed destination restores the page #183
    was filed about: nothing here verifies an application."""
    html = _render("https://claude.ai/cb")

    assert _badge(html) is not None
    notice = _notice(html)
    assert "registered itself" in notice
    assert "not verified by this server" in notice


# --- the badge and the warning --------------------------------------------


def test_an_allow_listed_host_shows_the_badge_and_no_warning():
    html = _render("https://claude.ai/cb")

    badge = _badge(html)
    assert badge is not None
    assert "claude.ai" in badge
    assert _warning(html) is None


def test_the_badge_is_worded_about_the_destination_not_the_application():
    badge = _badge(_render("https://claude.ai/cb"))

    assert "destination" in badge.lower()
    assert "verified application" not in badge.lower()
    assert "does not verify the application" in badge


def test_a_non_allow_listed_host_shows_a_warning_naming_the_host():
    html = _render("https://vault-sync.example/cb")

    warning = _warning(html)
    assert warning is not None
    assert "vault-sync.example" in warning
    assert "authorization code" in warning
    assert "Deny" in warning
    assert _badge(html) is None


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://evilclaude.ai/cb",
        "https://claude.ai.evil.example/cb",
        "https://sub.claude.ai/cb",
        "https://chatgpt.com.evil.example/cb",
    ],
)
def test_a_look_alike_host_gets_the_warning_not_the_badge(redirect_uri):
    html = _render(redirect_uri)

    assert _badge(html) is None
    assert _warning(html) is not None


def test_an_empty_allow_list_warns_for_everything():
    html = _render("https://claude.ai/cb", known_hosts=())

    assert _badge(html) is None
    assert "claude.ai" in _warning(html)


# --- the pre-existing hostless row ----------------------------------------


def test_a_hostless_row_renders_undetermined_and_takes_the_warning_branch():
    """Registration now refuses these, but rows registered before it exist,
    so the display path degrades explicitly rather than showing a blank."""
    html = _render("https://@/cb", known_hosts=("claude.ai", ""))

    assert _destination(html) == "Destination could not be determined"
    assert _badge(html) is None
    warning = _warning(html)
    assert warning is not None
    assert "could not be determined" in warning
    assert "Deny" in warning
    assert "registered itself" in _notice(html)


# --- registration hardening ------------------------------------------------


def test_registration_refuses_a_hostless_redirect_uri():
    """`https://@/cb` used to register: `netloc` is `"@"` and truthy while
    `hostname` is empty, and the consent card then had no destination at all."""
    response, session = _register(["https://@/cb"])

    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "invalid_redirect_uri"
    assert session.added == []
    assert session.committed is False


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://a..b/cb",                      # an empty label
        "https://" + "a" * 70 + ".test/cb",     # a label over 63 octets
    ],
)
def test_registration_refuses_a_host_that_cannot_be_converted(redirect_uri):
    response, session = _register([redirect_uri])

    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "invalid_redirect_uri"
    assert session.added == []


def test_registration_normalises_a_non_ascii_host_to_its_a_label():
    response, session = _register([f"https://{HOMOGRAPH_HOST}/cb"])

    assert response.status_code == 201
    assert session.added, "the client row was never added"
    assert session.added[0].redirect_uris == [f"https://{HOMOGRAPH_ALABEL}/cb"]
    # RFC 7591: the response echoes the metadata that was actually registered,
    # so the client is told the form the server will compare against.
    assert json.loads(response.body)["redirect_uris"] == [
        f"https://{HOMOGRAPH_ALABEL}/cb"
    ]


def test_registration_stores_an_ascii_redirect_uri_unchanged():
    response, session = _register(["https://example.test:8443/cb?x=1"])

    assert response.status_code == 201
    assert session.added[0].redirect_uris == ["https://example.test:8443/cb?x=1"]


def test_registration_still_refuses_http_and_fragments():
    """The pre-existing rules are unchanged and are not sufficient on their
    own — the host requirement is additional, not a replacement."""
    for redirect_uri in ("http://example.test/cb", "https://example.test/cb#frag"):
        response, session = _register([redirect_uri])
        assert response.status_code == 400, redirect_uri
        assert session.added == []


def test_registration_refuses_two_spellings_that_normalise_to_one():
    response, session = _register(
        [f"https://{HOMOGRAPH_HOST}/cb", f"https://{HOMOGRAPH_ALABEL}/cb"]
    )

    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "invalid_redirect_uri"
    assert session.added == []


# --- the #63 preselect is untouched ---------------------------------------


def test_the_read_only_preselect_and_autocomplete_survive_the_identity_block():
    """#63: the checked radio is what Approve submits, and Firefox restores a
    control's dynamic checked state in preference to the markup default. This
    change adds markup above the form and must move neither."""
    html = _render("https://evil.example/cb")

    radios = [t for t in re.findall(r"<input\b[^>]*>", html) if 'name="scope"' in t]
    checked = [t for t in radios if re.search(r"\bchecked\b", re.sub(r'"[^"]*"', '""', t))]

    assert len(checked) == 1
    assert 'value="read"' in checked[0]
    assert all('autocomplete="off"' in t for t in radios)
    assert 'action="/authorize" autocomplete="off"' in html
