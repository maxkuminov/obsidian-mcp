"""One place that decides what an OAuth scope string means.

Three surfaces used to answer "does this grant write?" independently:

- ``src/oauth/routes.py`` gated the consent screen's Read+Write radio on
  ``"readwrite" in client.scope.split()`` and clamped the posted form value
  with a private ``_clamp_scope``;
- ``src/mcp_server/auth.py`` mapped a token scope to a permission with its own
  ``"readwrite" in scope_parts``;
- ``src/control_panel/routes.py`` computed ``has_write`` with a third copy, and
  ``update_oauth_token_scope`` did not consult the client's registration at
  all -- so the panel could raise a token above what its client was ever
  registered for, permanently (issue #67).

They agreed by coincidence, which is not a property anything could test. This
module is deliberately dependency-free (no ``src.config``, no ORM) so *every*
layer -- the OAuth routes, the ASGI auth middleware and the control panel --
can import it without an import cycle, which is what issue #67 warned about
when it said not to reach into ``src/oauth/routes.py`` from the panel.

Scope strings are **space-separated sets**, not enum values. ``"offline_access
readwrite"`` is a readwrite grant; string equality against ``"readwrite"``
says it is not. Membership is the only correct test, and every helper here is
built on it.
"""

# Every scope token the server understands. ``offline_access`` does not change
# vault permissions -- it only makes the already-issued refresh token explicit
# in the grant (see DEFAULT_CLIENT_SCOPE in src/oauth/routes.py).
VALID_SCOPES = frozenset({"read", "readwrite", "offline_access"})

WRITE_SCOPE = "readwrite"


def scope_set(scope: str | None) -> set[str]:
    """The set of scope tokens in a scope string. ``None`` is the empty set."""
    return set((scope or "").split())


def client_can_write(client_scope: str | None) -> bool:
    """Is this *client* registered to hold a write grant?

    The registered scope is the cap: RFC 7591 registration is a statement
    about what that software may ever hold, and no consent screen, no token
    endpoint and no panel control may raise a grant above it.
    """
    return WRITE_SCOPE in scope_set(client_scope)


def token_has_write(token_scope: str | None) -> bool:
    """Does this *token* actually carry write permission?

    Membership, not equality -- ``"offline_access readwrite"`` is a write
    grant. This is the single definition behind the panel's ``has_write``
    display flag and the middleware's ``permission`` mapping, so the two
    cannot drift apart.
    """
    return WRITE_SCOPE in scope_set(token_scope)


def clamp_scope(requested: str, registered: str) -> str:
    """Restrict a requested scope to what the client registered for.

    Both inputs are already validated scope strings. The user can only ever
    be granted the intersection of what they asked for and what the client is
    registered to hold. ``readwrite`` implies ``read``, so a client registered
    for ``readwrite`` may still be granted plain ``read``.

    The result is sorted and joined, so it is a canonical rendering of the
    granted set -- callers must never compare it with ``==`` against a
    hand-written ordering.
    """
    requested_parts = scope_set(requested)
    registered_parts = scope_set(registered)
    if WRITE_SCOPE in registered_parts:
        registered_parts.add("read")
    granted = requested_parts & registered_parts
    return " ".join(sorted(granted)) or "read"
