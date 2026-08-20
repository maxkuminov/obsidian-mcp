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
READ_SCOPE = "read"

# The scopes that actually grant access to the vault, strongest first.
# ``offline_access`` is deliberately absent: it says the grant may carry a
# refresh token, not that it may read a single note. Treating it as a vault
# scope is what let a client registered for ``offline_access`` alone end up
# with read access nobody granted.
VAULT_SCOPES = (WRITE_SCOPE, READ_SCOPE)


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


def vault_level(scope: str | None) -> str | None:
    """The vault permission a scope string carries: ``readwrite``, ``read``, or None.

    None means *no vault access at all*. That is a real state, not a
    degenerate one: a DCR client may register ``scope="offline_access"``, and
    a scope string that names only ``offline_access`` grants nothing.
    """
    parts = scope_set(scope)
    for name in VAULT_SCOPES:
        if name in parts:
            return name
    return None


def has_vault_scope(scope: str | None) -> bool:
    """Does this scope string grant *any* access to the vault?"""
    return vault_level(scope) is not None


def clamp_scope(requested: str, registered: str) -> str:
    """Restrict a requested scope to what the client registered for.

    The granted vault permission is the **weaker** of what was asked for and
    what the client is registered to hold — ``readwrite`` outranks ``read``,
    so a client registered read-only that asks for ``readwrite`` gets ``read``
    (issue #21), and a client registered ``readwrite`` that asks for ``read``
    gets ``read``. ``offline_access`` rides along only when both sides carry
    it; it is a marker, never a permission.

    **An empty result means "grant nothing", and every caller MUST refuse
    rather than write it.** This used to fall back to ``"read"`` whenever the
    intersection came out empty, which quietly conflated two different things:
    the legitimate readwrite→read downgrade above, and a client that is
    registered for *no vault scope at all*. A DCR registration of
    ``scope="offline_access"`` therefore received read access to the whole
    vault that its registration never granted. The downgrade is now expressed
    directly, so the fallback can be what it should always have been —
    nothing.

    The result is sorted and joined, so it is a canonical rendering of the
    granted set; callers must never compare it with ``==`` against a
    hand-written ordering.
    """
    requested_level = vault_level(requested)
    registered_level = vault_level(registered)
    if requested_level is None or registered_level is None:
        return ""

    # The weaker of the two. `read` beats `readwrite` here precisely because
    # "weaker" is what a clamp means.
    granted = {
        READ_SCOPE
        if READ_SCOPE in (requested_level, registered_level)
        else WRITE_SCOPE
    }
    if "offline_access" in scope_set(requested) & scope_set(registered):
        granted.add("offline_access")
    return " ".join(sorted(granted))
