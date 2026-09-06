"""Redirect-destination identity for the OAuth consent screen (#183).

`/register` is unauthenticated (RFC 7591), so **every** client on this server
registered itself and every string it supplied — its name above all — is
attacker-chosen text. The one value on the consent screen the client cannot
choose the *meaning* of is the host the authorization code is delivered to,
because `authorize_post` re-validates the submitted `redirect_uri` against the
client's registered list before minting anything. This module is the single
definition of two things derived from that URI:

* `redirect_display_host` — what the consent screen shows as the destination;
* `known_redirect_host` — whether the operator has listed that destination.

Three rules live here so no caller can get them subtly wrong:

**`hostname`, never `netloc`.** `urlparse("https://claude.ai@evil.example/cb")`
has a `netloc` of `"claude.ai@evil.example"`, whose left edge reads as the
brand and whose right edge is where the code actually goes. `.hostname` strips
the userinfo and the port and answers `"evil.example"`. Rendering `netloc`
would hand an attacker the whole disclosure for the cost of one `@`.

**ASCII, never decoded.** A host is displayed in its IDNA A-label
(`xn--…`) form. A homograph host rendered decoded — `аpple.com` with a
Cyrillic `а` — is indistinguishable from the real one on screen, which defeats
the disclosure the same way `netloc` does. So the display path *converts to*
punycode and never converts back.

**Equality, never containment.** `endswith("claude.ai")` matches
`evilclaude.ai`; `"claude.ai" in host` matches `claude.ai.evil.example`; even
`endswith(".claude.ai")` hands the badge to any subdomain an attacker can get
and neither real connector needs one. The match is `==` against a lower-cased
configured entry, and `src/config.py` refuses an entry containing `*`, `/`,
`@` or internal whitespace so a pattern is a container that will not start
rather than a badge that never appears.

`UNKNOWN_HOST` (a plain `None`) is the sentinel for "this URI has no host we
can name". Rows registered before `_valid_redirect_uri` required a resolvable
ASCII host may still carry one, so the sentinel is a real runtime state, not a
defensive impossibility: the template renders it as an explicit "could not be
determined" and `known_redirect_host` answers `False` for it under **every**
configuration, including one that somehow listed the empty string.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from src.config import settings

# The sentinel `redirect_display_host` returns when no host can be named.
# `None` rather than a string, so a template's `{% if redirect_host %}` and a
# caller's `is None` agree, and so no configured allow-list entry can ever
# equal it.
UNKNOWN_HOST = None


def to_ascii_host(host: str | None) -> str | None:
    """A host's lower-cased IDNA A-label, or `None` if it has none.

    Uses the standard library's `idna` codec rather than a dependency: it
    passes an already-ASCII label through unchanged (so `xn--…`, an IPv6
    literal's inner text and a plain `example.com` survive byte-for-byte), and
    raises `UnicodeError` on an empty label, an over-long label, or a label
    that cannot be encoded at all. Any raise is a `None` — "we cannot name
    this host" — never an exception reaching a request handler, because both
    callers are display or validation paths that must degrade rather than 500.
    """
    if not host:
        return None
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    return ascii_host.lower() or None


def redirect_display_host(redirect_uri: str) -> str | None:
    """The host of `redirect_uri`, lower-cased ASCII — or `UNKNOWN_HOST`.

    This is the value the consent screen shows as "Destination". It is the
    host component alone: no scheme, no path, no port, and above all no
    userinfo (see the module docstring). A non-ASCII host is converted to its
    punycode form here and is never converted back.
    """
    try:
        host = urlparse(redirect_uri).hostname
    except (ValueError, AttributeError):
        return UNKNOWN_HOST
    return to_ascii_host(host) or UNKNOWN_HOST


def known_redirect_host(redirect_uri: str) -> bool:
    """True when the URI's host **equals** a configured known-connector host.

    Case-insensitive equality against `settings.oauth_known_redirect_hosts`
    (already lower-cased and pattern-refused at settings construction). Not a
    suffix, prefix, substring or wildcard test — see the module docstring for
    the three ways each of those grants the badge to an attacker.

    An empty configured list means nothing is known, which is the safe
    direction: clearing the setting produces warnings, never badges. So does
    an unnameable host, which returns `UNKNOWN_HOST` above and equals no entry.
    """
    host = redirect_display_host(redirect_uri)
    if host is UNKNOWN_HOST:
        return False
    return any(host == entry.lower() for entry in settings.oauth_known_redirect_hosts)


def normalize_redirect_uri(redirect_uri: str) -> str | None:
    """`redirect_uri` with its host as an A-label, or `None` if it has no host.

    The form registration stores. Normalising here rather than at display time
    is what stops one host being stored in two spellings that render alike and
    compare differently — a Unicode row and its punycode twin would show the
    same badge-bearing text while only one of them could ever match the
    allow-list.

    An **already-ASCII** host is left strictly alone: the URI is returned
    unchanged, byte for byte. That is deliberate rather than lazy. Rebuilding
    an authority is where an IPv6 literal loses its brackets and a userinfo
    string loses a percent-encoded character, and the overwhelming majority of
    registrations are already ASCII, so the reassembly below runs only on the
    narrow non-ASCII case — which cannot be an IPv6 literal.
    """
    try:
        parsed = urlparse(redirect_uri)
        host = parsed.hostname
        port = parsed.port  # raises ValueError on a malformed port
    except (ValueError, AttributeError):
        return None

    ascii_host = to_ascii_host(host)
    if ascii_host is None:
        return None
    if host.isascii():
        return redirect_uri

    netloc = ""
    if parsed.username:
        netloc += parsed.username
        if parsed.password:
            netloc += f":{parsed.password}"
        netloc += "@"
    netloc += ascii_host
    if port is not None:
        netloc += f":{port}"
    return urlunparse(parsed._replace(netloc=netloc))
