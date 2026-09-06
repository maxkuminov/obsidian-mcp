"""The transfer redemption gate refuses a capability whose owner is
quarantined (#199, slice 5).

A capability is a **delayed** write — or a delayed read — into a vault root:
authorised at mint, redeemed later on the public `/transfer/*` routes. Those
routes carry no OAuth chain and never call `vault._vault_root`, so the
admission gate that refuses every MCP tool for a quarantined caller does not
reach them. And every predicate the gate already runs *agrees*: the token pins
`vault_root`, the owner's `users.vault_path` is unchanged byte for byte, the
owner is still active, the credential is still live. The assignment did not
change; the directory underneath it did.

So without this check, refusing all 25 tools would still leave the cross-tenant
write reachable through the one path designed to outlive the session that
created it — which is the ranked failure for this server (a destructive write
that clobbers a note, delivered to an agent, with no human seeing the query).

Where it goes is not incidental. The gate already re-reads the owner row and
already fails closed on an inactive owner and a reassigned root; the quarantine
is one more condition at a point whose refusal semantics, error surface and
locking are established. Three sites, because the gate has three:
`resolve_root_ok` (the unlocked entry check, both directions), `locked_rows_ok`
(the locked publish path, re-run against rows held `FOR UPDATE`), and
`_identity_publish_ok` (the same guarantee for `import_from_url`, which has no
capability but holds a stream open for up to 30 s).

**The response is unchanged.** `_not_found()` is byte-identical for every
cause, which is the anti-oracle the whole public surface rests on; only the
server-side record says which condition refused.
"""
import asyncio
import datetime
import errno as errno_module

import pydantic_settings
import pytest

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from src.config import settings
    from src.models.db import APIKey
    from src.services import transfer, vault_overlap
    from src.transfer import routes as transfer_routes
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


OWNER_ID = 7
OTHER_ID = 8
ROOT = "/vaults/bob"


# --- Fakes -----------------------------------------------------------------


class _OwnerRow:
    def __init__(self, vault_path=ROOT, is_active=True):
        self.vault_path = vault_path
        self.is_active = is_active


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _OwnerSession:
    """Answers the one `SELECT users.vault_path, users.is_active` the unlocked
    root check issues, and counts it — so a test can assert the refusal came
    before the query rather than from it."""

    def __init__(self, row=None):
        self._row = row if row is not None else _OwnerRow()
        self.queries = 0

    async def execute(self, stmt, *_a, **_k):
        self.queries += 1
        return _Result(self._row)


class _TokenRow:
    def __init__(self, *, user_id=OWNER_ID, vault_root=ROOT, direction="upload"):
        self.user_id = user_id
        self.vault_root = vault_root
        self.direction = direction
        self.key_id = 1
        self.oauth_token_id = None
        self.path = "inbox/report.pdf"
        self.state = "pending"
        self.id = 42


def _Cred(user_id=OWNER_ID):
    """A live read-write `APIKey`. `_credential_ok` is an `isinstance` ladder,
    so the real ORM class is what the predicate accepts."""
    cred = APIKey(
        key_hash="h",
        name="k",
        permission="readwrite",
        is_active=True,
        user_id=user_id,
    )
    cred.expires_at = None
    return cred


class _User:
    def __init__(self, vault_path=ROOT, is_active=True):
        self.vault_path = vault_path
        self.is_active = is_active


def _entry(user_id=OWNER_ID, reason=None):
    return vault_overlap.QuarantineEntry(
        user_id=user_id,
        username="bob",
        assignment=ROOT,
        reason=reason
        or vault_overlap.Overlap(
            OTHER_ID, "carol", "/vaults", vault_overlap.RELATION_CONTAINED_BY
        ),
        detected_at=datetime.datetime(2026, 9, 5, 9, 0, tzinfo=datetime.timezone.utc),
    )


@pytest.fixture(autouse=True)
def _multi_user(monkeypatch):
    monkeypatch.setattr(settings, "multi_user_mode", True)


def _quarantine(reason=None):
    vault_overlap.publish_synthetic_snapshot([_entry(reason=reason)])


# --- `owner_quarantined` itself ---------------------------------------------


def test_an_owner_the_snapshot_names_is_quarantined():
    _quarantine()
    assert transfer.owner_quarantined(OWNER_ID) is True


def test_an_unrelated_owner_is_not():
    _quarantine()
    assert transfer.owner_quarantined(OTHER_ID + 1) is False


def test_an_unexaminable_root_quarantines_too():
    """Same refusal for the same reason: the root's status could not be
    established, and "we could not look" is not evidence of safety."""
    _quarantine(vault_overlap.RootUnexaminable(errno_module.ENOENT))
    assert transfer.owner_quarantined(OWNER_ID) is True


def test_the_never_published_state_refuses(unpublished_vault_root_snapshot):
    """A redemption served before the first detection is served against roots
    nothing has checked — the same window `_vault_root` refuses in."""
    assert vault_overlap.published_snapshot() is None
    assert transfer.owner_quarantined(OWNER_ID) is True


def test_single_user_mode_is_untouched(monkeypatch, unpublished_vault_root_snapshot):
    """One root, no second assignment, nothing to detect. The readiness state
    must not turn single-user redemption off — the quarantine test simply does
    not apply there."""
    monkeypatch.setattr(settings, "multi_user_mode", False)
    assert transfer.owner_quarantined(None) is False
    assert transfer.owner_quarantined(OWNER_ID) is False


def test_an_ownerless_row_in_multi_user_mode_is_left_to_its_own_refusal():
    """There is no user to name. `_ownerless_in_multi_user` already refuses
    that row in all three predicates, and this must not take that refusal's
    place — two checks answering for one condition is how the two drift."""
    vault_overlap.publish_synthetic_snapshot()
    assert transfer.owner_quarantined(None) is False


def test_the_snapshot_is_read_without_a_query_or_a_syscall():
    """Refuse-only, one attribute read plus a dict lookup. The public routes
    are the hot path this must not put work on."""
    _quarantine()
    session = _OwnerSession()
    assert asyncio.run(transfer.resolve_root_ok(session, _TokenRow())) is False
    assert session.queries == 0, "the refusal cost a database round trip"


# --- The unlocked root check: both directions -------------------------------


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_the_unlocked_root_check_refuses_both_directions(direction):
    """Upload and download redeem through the same predicate, so nothing is
    published and nothing is streamed."""
    _quarantine()
    row = _TokenRow(direction=direction)
    assert asyncio.run(transfer.resolve_root_ok(_OwnerSession(), row)) is False


def test_the_pinned_root_still_matches_which_is_the_whole_problem():
    """The token's `vault_root` equals the owner's current assignment, the
    owner is active, and the existing checks therefore all pass. Without the
    snapshot test this redemption proceeds into a directory the server has just
    determined is shared with another tenant."""
    vault_overlap.publish_synthetic_snapshot()
    row = _TokenRow()
    session = _OwnerSession(_OwnerRow(vault_path=ROOT, is_active=True))
    assert asyncio.run(transfer.resolve_root_ok(session, row)) is True

    _quarantine()
    assert asyncio.run(transfer.resolve_root_ok(_OwnerSession(), row)) is False


def test_an_unrelated_owners_capability_proceeds():
    _quarantine()
    row = _TokenRow(user_id=OTHER_ID + 1)
    session = _OwnerSession(_OwnerRow(vault_path=ROOT, is_active=True))
    assert asyncio.run(transfer.resolve_root_ok(session, row)) is True


def test_a_corrected_condition_restores_redemption():
    """The quarantine is derived at every entry point, never persisted, so a
    later snapshot that no longer names the owner is the whole of the
    correction — no row to clear, no cache to bust."""
    _quarantine()
    row = _TokenRow()
    assert asyncio.run(transfer.resolve_root_ok(_OwnerSession(), row)) is False

    vault_overlap.publish_synthetic_snapshot()  # a later, clean detection
    session = _OwnerSession(_OwnerRow(vault_path=ROOT, is_active=True))
    assert asyncio.run(transfer.resolve_root_ok(session, row)) is True


# --- The locked publish path -------------------------------------------------


def _locked(**overrides):
    return transfer.LockedRows(
        token=overrides.get("token", _TokenRow()),
        credential=overrides.get("credential", _Cred()),
        user=overrides.get("user", _User()),
    )


def test_the_locked_path_refuses_before_anything_is_published():
    """The entry check runs before the body is read; a quarantine published
    while the bytes were streaming is caught only here — and here is still
    before the link or rename, so nothing lands in the vault."""
    vault_overlap.publish_synthetic_snapshot()
    assert transfer.locked_rows_ok(_locked(), need_write=True) is True

    _quarantine()
    assert transfer.locked_rows_ok(_locked(), need_write=True) is False


def test_the_locked_path_refuses_an_unexaminable_owner_too():
    _quarantine(vault_overlap.RootUnexaminable(errno_module.ETIMEDOUT))
    assert transfer.locked_rows_ok(_locked(), need_write=True) is False


def test_the_locked_path_leaves_an_unrelated_owner_alone():
    _quarantine()
    locked = _locked(token=_TokenRow(user_id=OTHER_ID + 1))
    locked.credential.user_id = OTHER_ID + 1
    assert transfer.locked_rows_ok(locked, need_write=True) is True


# --- The identity publish gate (`import_from_url`) ---------------------------


class _IdentitySession:
    def __init__(self, cred, user):
        self._cred = cred
        self._user = user
        self.user_queries = 0

    async def execute(self, stmt, *_a, **_k):
        sql = str(stmt)
        if sql.startswith("SELECT users."):
            self.user_queries += 1

            class _R:
                def scalar_one_or_none(_s):
                    return None

            return _R()

        outer = self

        class _R:
            def scalar_one_or_none(_s):
                return outer._cred

        return _R()


def test_the_identity_publish_gate_refuses_a_quarantined_caller(monkeypatch):
    """`import_from_url` holds a network stream open for up to 30 s — long
    enough for a detection to publish a quarantine while the bytes arrive. The
    tool admission gate refuses the *call*; this refuses the *publish*."""
    _quarantine()
    identity = transfer.Identity(key_id=1, user_id=OWNER_ID)
    session = _IdentitySession(_Cred(), _User())
    ok = asyncio.run(
        transfer._identity_publish_ok(
            session, identity, vault_root=ROOT, need_write=True
        )
    )
    assert ok is False
    assert session.user_queries == 0, "refused before the locked owner re-read"


# --- End to end through the routes' own predicate ladder --------------------


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_load_valid_refuses_a_quarantined_owner(monkeypatch, direction):
    """The route helper every bearer-protected endpoint funnels through. A
    refusal here is the uniform 404 — nothing published, nothing streamed — and
    the reason exists only for the server-side record."""
    _quarantine()
    row = _TokenRow(direction=direction)

    async def _lookup(session, token, *, direction):
        return row

    async def _identity_ok(session, r, *, need_write):
        return True

    monkeypatch.setattr(transfer, "lookup_token", _lookup)
    monkeypatch.setattr(transfer, "resolve_identity_ok", _identity_ok)

    outcome = asyncio.run(
        transfer_routes._load_valid(_OwnerSession(), "tok", direction=direction)
    )
    assert transfer_routes._refused(outcome), (
        "a quarantined owner's capability was accepted for redemption"
    )
    assert isinstance(outcome, transfer.TransferRefusal)
    assert outcome.row is row


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_load_valid_still_accepts_an_unaffected_owner(monkeypatch, direction):
    vault_overlap.publish_synthetic_snapshot()
    row = _TokenRow(direction=direction)

    async def _lookup(session, token, *, direction):
        return row

    async def _identity_ok(session, r, *, need_write):
        return True

    monkeypatch.setattr(transfer, "lookup_token", _lookup)
    monkeypatch.setattr(transfer, "resolve_identity_ok", _identity_ok)
    monkeypatch.setattr(transfer_routes, "_path_ok", lambda root, path: True)

    outcome = asyncio.run(
        transfer_routes._load_valid(
            _OwnerSession(_OwnerRow(vault_path=ROOT, is_active=True)),
            "tok",
            direction=direction,
        )
    )
    assert outcome is row
