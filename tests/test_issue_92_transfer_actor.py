"""A transfer capability carries the actor that minted it (#92, item 2).

#77 gave every MCP tool call a denormalised actor on `usage_logs`, because both
credential FKs are allowed to lose their target while the log row stays. **The
transfer routes were not covered**, and the reason is structural rather than an
oversight: a redemption request is session-less and authenticates with a
*capability*, not a credential, so `src/transfer/routes.py::_log_row` has no
request-scoped actor to read and attributed its rows by LEFT JOIN — through
`transfer_tokens.key_id`, or through `oauth_token_id` -> `oauth_clients`.

Both joins go NULL on the operator's most urgent path. Delete the OAuth client
and every `upload_file` / `download_file` line it produced renders "unknown";
the panel NULLs a key's `usage_logs.key_id` before deleting it, with the same
result. Those are the rows where bytes entered or left the vault — the ones an
operator reviewing a suspect connector opens the page to read.

The fix records the actor where the credential *is* still in hand: at mint,
from the ContextVar `APIKeyMiddleware` bound out of the credential row it had
already loaded, onto `transfer_tokens` (migration 017). `_log_row` copies it
onto the usage row at redemption. These tests pin the whole chain, plus the
three properties that make it a record rather than a lookup: it costs no query,
it is a snapshot that a later rename cannot move, and it authorises nothing.

Fully offline: no database, no network.
"""

import asyncio
import datetime
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
import src.services.transfer as transfer  # noqa: E402
from src.auth.session import (  # noqa: E402
    ACTOR_LABEL_MAX,
    ACTOR_REF_MAX,
    actor_columns,
    current_actor,
)
from src.control_panel.routes import _usage_actor  # noqa: E402
from src.models.db import APIKey, OAuthToken, TransferToken  # noqa: E402
from src.transfer.routes import _log_row  # noqa: E402


# --------------------------------------------------------------------------
# plumbing — a session that counts statements and captures the row
# --------------------------------------------------------------------------


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RecordingSession:
    """Enough of `AsyncSession` for `mint_token`, and a statement counter.

    The count is the point of one of the cases below: the label must ride along
    on reads the mint already performs, never add one.
    """

    def __init__(self, credential):
        self.credential = credential
        self.statements = []
        self.added = []
        self.commits = 0

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        # `_load_credential` is the only SELECT `mint_token` issues; the other
        # statement is the opportunistic prune, whose result is never read.
        if len(self.statements) == 1:
            return _Result(self.credential)
        return _Result()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        return None


def api_key(name="nightly sync", prefix="omcp_a1b2c3") -> APIKey:
    return APIKey(
        id=7,
        name=name,
        key_hash="x" * 64,
        key_prefix=prefix,
        permission="readwrite",
        is_active=True,
        expires_at=None,
        user_id=None,
    )


def oauth_token() -> OAuthToken:
    return OAuthToken(
        id=11,
        token_hash="y" * 64,
        token_type="access",
        client_id="client-abc",
        scope="read readwrite",
        grant_id="grant-1",
        revoked=False,
        expires_at=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=1),
        user_id=None,
    )


def mint(credential, identity, actor, *, direction="upload"):
    """Run the real `mint_token` under `actor`; return `(row, session)`."""
    session = _RecordingSession(credential)

    async def run():
        token = current_actor.set(actor)
        try:
            return await transfer.mint_token(
                session,
                direction,
                "Inbox/report.pdf",
                overwrite=False,
                identity=identity,
                vault_root="/vaults/alice",
                expected_fingerprint=None,
                expires_in=600,
            )
        finally:
            current_actor.reset(token)

    _token, row, _window = asyncio.run(run())
    assert session.commits == 1
    return row, session


KEY_IDENTITY = transfer.Identity(key_id=7, oauth_token_id=None, user_id=None)
OAUTH_IDENTITY = transfer.Identity(key_id=None, oauth_token_id=11, user_id=None)


# --------------------------------------------------------------------------
# 1. the mint records what the middleware bound
# --------------------------------------------------------------------------


def test_the_mint_records_the_api_key_actor():
    row, _session = mint(
        api_key(), KEY_IDENTITY, ("api_key", "nightly sync", "omcp_a1b2c3")
    )
    assert (row.actor_kind, row.actor_label, row.actor_ref) == (
        "api_key",
        "nightly sync",
        "omcp_a1b2c3",
    )


def test_the_mint_records_the_oauth_client_name_not_the_token():
    """The OAuth branch of the middleware gets `client_name` from the token
    lookup itself, which already `outerjoin`s `oauth_clients`. That name is
    what identifies a connector to an operator; the token id identifies
    nothing once the row is cascaded away."""
    row, _session = mint(
        oauth_token(), OAUTH_IDENTITY, ("oauth", "Claude Desktop", "client-abc")
    )
    assert (row.actor_kind, row.actor_label, row.actor_ref) == (
        "oauth",
        "Claude Desktop",
        "client-abc",
    )


def test_the_label_costs_no_additional_statement():
    """The whole design rests on the label being free.

    `APIKeyMiddleware` has already loaded the credential row, so the mint reads
    a ContextVar rather than a table. Any drift towards "look it up here" shows
    up as a statement the baseline does not have.
    """
    labelled, with_actor = mint(
        api_key(), KEY_IDENTITY, ("api_key", "nightly sync", "omcp_a1b2c3")
    )
    unlabelled, without_actor = mint(api_key(), KEY_IDENTITY, None)

    assert labelled.actor_kind == "api_key"
    assert unlabelled.actor_kind is None
    assert len(with_actor.statements) == len(without_actor.statements)
    # The pre-change baseline, spelled out: one credential SELECT
    # (`plan_mint_window`) and one opportunistic prune DELETE.
    assert len(with_actor.statements) == 2


def test_a_mint_outside_a_request_records_no_actor():
    """Single-user, sandbox, the indexer, a test — no credential to name.

    All three stay NULL and the row keeps its pre-017 shape; nothing is
    inferred from `user_id`, because two of a user's keys are different actors.
    """
    row, _session = mint(api_key(), KEY_IDENTITY, None)
    assert (row.actor_kind, row.actor_label, row.actor_ref) == (None, None, None)


def test_the_mint_and_the_tool_log_read_the_same_actor():
    """One reader, so the two writers cannot disagree.

    The columns are identically typed on both tables, so a second copy of the
    mapping is how a mint and a tool call in the *same request* start recording
    different labels — or truncating at different widths.
    """
    long_actor = ("api_key", "k" * 400, "r" * 200)
    row, _session = mint(api_key(), KEY_IDENTITY, long_actor)

    token = current_actor.set(long_actor)
    try:
        from_the_log = tools._actor_columns()
        from_the_reader = actor_columns()
    finally:
        current_actor.reset(token)

    assert from_the_log == from_the_reader
    assert {
        "actor_kind": row.actor_kind,
        "actor_label": row.actor_label,
        "actor_ref": row.actor_ref,
    } == from_the_log
    # Truncated, not dropped: an over-wide value would raise inside the writer,
    # and on the `usage_logs` path that writer swallows the error — losing the
    # whole row, which is the opposite of what these columns are for.
    assert len(row.actor_label) == ACTOR_LABEL_MAX
    assert len(row.actor_ref) == ACTOR_REF_MAX


# --------------------------------------------------------------------------
# 2. redemption copies it onto the usage row
# --------------------------------------------------------------------------


def token_row(**kwargs) -> TransferToken:
    row = TransferToken(
        public_id="pub-1",
        token_hash="z" * 64,
        direction="upload",
        state="completed",
        path="Inbox/report.pdf",
        vault_root="/vaults/alice",
        overwrite=False,
        key_id=None,
        oauth_token_id=None,
        user_id=None,
        expires_at=datetime.datetime.now(datetime.timezone.utc),
    )
    for field, value in kwargs.items():
        setattr(row, field, value)
    return row


def rendered(log, *, api_key_name=None, api_key_prefix=None, oauth_client_name=None):
    """`_usage_actor` over the panel's row shape: snapshot plus the join."""
    return _usage_actor(
        SimpleNamespace(
            actor_kind=log.actor_kind,
            actor_label=log.actor_label,
            actor_ref=log.actor_ref,
            api_key_name=api_key_name,
            api_key_prefix=api_key_prefix,
            oauth_client_name=oauth_client_name,
        )
    )


def test_the_redemption_usage_row_carries_the_recorded_actor():
    row = token_row(
        key_id=7,
        actor_kind="api_key",
        actor_label="nightly sync",
        actor_ref="omcp_a1b2c3",
    )
    log = _log_row(row, "upload_file", {"path": row.path, "size": 12}, response_size=12)

    assert log.tool == "upload_file"
    assert (log.actor_kind, log.actor_label, log.actor_ref) == (
        "api_key",
        "nightly sync",
        "omcp_a1b2c3",
    )
    # Nothing else about that function changed, and the token never appears.
    assert log.key_id == 7
    assert "token" not in log.params


def test_the_usage_row_still_names_the_api_key_after_the_panel_deletes_it():
    """`delete_key_form`'s exact sequence: NULL `usage_logs.key_id`, then
    delete the key. Before this, that erased the actor of every transfer the
    key had ever minted."""
    row = token_row(
        key_id=7,
        actor_kind="api_key",
        actor_label="nightly sync",
        actor_ref="omcp_a1b2c3",
    )
    log = _log_row(row, "upload_file", {"path": row.path})

    log.key_id = None  # the panel's UPDATE
    assert rendered(log) == ("nightly sync", "omcp_a1b2c3")


def test_the_usage_row_still_names_the_oauth_client_after_it_is_deleted():
    """The scenario in the issue: an operator suspects a connector, clicks
    Delete, then opens the Usage page to see what it did. The delete cascades
    `oauth_tokens` and SET NULLs `usage_logs.oauth_token_id`, so the join the
    page relied on is gone."""
    row = token_row(
        oauth_token_id=11,
        actor_kind="oauth",
        actor_label="Claude Desktop",
        actor_ref="client-abc",
    )
    log = _log_row(row, "download_file", {"path": row.path})

    log.oauth_token_id = None  # the cascade
    assert rendered(log) == ("Claude Desktop", "OAuth · client-abc")


def test_a_rename_between_mint_and_redemption_keeps_the_recorded_name():
    """A snapshot, never re-derived.

    The label names what the credential was called when the capability was
    minted. Re-reading it at redemption would rewrite history on every rename
    — and would fail outright in the case the scheme exists for, the credential
    deleted.
    """
    credential = api_key()
    row, _session = mint(
        credential, KEY_IDENTITY, ("api_key", "nightly sync", "omcp_a1b2c3")
    )

    credential.name = "renamed later"

    log = _log_row(row, "upload_file", {"path": row.path})
    assert log.actor_label == "nightly sync"
    assert rendered(log) == ("nightly sync", "omcp_a1b2c3")


def test_a_pre_017_token_leaves_the_usage_row_in_its_old_shape():
    """The 015 -> 017 gap, and it renders honestly.

    A token minted before 017 carries NULLs. Nothing writes an actor onto its
    usage row after the fact: the row keeps the shape it had, is attributed by
    the existing joins, and reads as an unattributable row once they resolve to
    nothing.
    """
    row = token_row(key_id=7)
    log = _log_row(row, "upload_file", {"path": row.path})

    assert (log.actor_kind, log.actor_label, log.actor_ref) == (None, None, None)
    assert rendered(log, api_key_name="nightly sync", api_key_prefix="omcp_a1b2c3") == (
        "nightly sync",
        "omcp_a1b2c3",
    )
    # And once the key is deleted, honestly unattributable rather than wrong.
    assert rendered(log) == (None, None)


# --------------------------------------------------------------------------
# 3. it authorises nothing
# --------------------------------------------------------------------------


class _NoCredentialSession:
    """Every lookup misses — the credential was deleted."""

    def __init__(self):
        self.statements = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return _Result(None)


def test_the_recorded_actor_changes_no_redemption_decision():
    """Display and audit only.

    A capability whose recorded credential has since been deleted is refused by
    the same predicates as before — `resolve_identity_ok` reads the credential
    row, and a label on the token is not one.
    """
    row = token_row(
        key_id=7,
        actor_kind="api_key",
        actor_label="nightly sync",
        actor_ref="omcp_a1b2c3",
    )

    session = _NoCredentialSession()
    assert asyncio.run(transfer.resolve_identity_ok(session, row, need_write=True)) is False


def test_the_predicate_reads_the_credential_and_not_the_label():
    """The same row, with and without a recorded actor, gets the same verdict
    from `_credential_ok` — the entry check and the pre-publication re-check
    are one function, and neither has learned about these columns."""
    credential = api_key()
    labelled = token_row(
        key_id=7, actor_kind="api_key", actor_label="nightly sync", actor_ref="omcp_x"
    )
    bare = token_row(key_id=7)

    for need_write in (False, True):
        assert transfer._credential_ok(
            credential, need_write=need_write, row=labelled
        ) is transfer._credential_ok(credential, need_write=need_write, row=bare)

    credential.is_active = False
    assert transfer._credential_ok(credential, need_write=False, row=labelled) is False


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_both_directions_record_the_actor(direction):
    """`request_upload` and `request_download` mint through the same function,
    so neither can be the one that forgets."""
    row, _session = mint(
        api_key(),
        KEY_IDENTITY,
        ("api_key", "nightly sync", "omcp_a1b2c3"),
        direction=direction,
    )
    assert row.direction == direction
    assert row.actor_label == "nightly sync"


# ── the one-credential invariant (adversarial round 1, MAJOR) ───────────────


def test_an_identity_naming_two_credentials_is_refused():
    """Nothing in a two-credential row records which of them minted it, so
    there is no correct label to pick — and 017's backfill would pick the
    API key purely because that UPDATE runs first.

    `APIKeyMiddleware` sets both ContextVars to None at the head of every
    request and fills in exactly one branch, so this is unreachable today.
    That is why it is asserted rather than assumed: the value is written
    straight onto `transfer_tokens`, whose CHECK constraint forbids the pair,
    and the attribution copied from that row is shown to an operator as an
    audit trail.
    """
    with pytest.raises(ValueError) as excinfo:
        transfer.Identity(key_id=7, oauth_token_id=11, user_id=3)
    assert "at most one credential" in str(excinfo.value)


def test_both_credentials_absent_is_still_legal():
    """The single-user and sandbox shape: no credential foreign key at all."""
    identity = transfer.Identity()
    assert identity.key_id is None and identity.oauth_token_id is None


def test_the_middleware_never_binds_both_credential_context_vars():
    """The reason the invariant is unreachable, asserted where it lives."""
    import inspect

    import src.mcp_server.auth as auth

    source = inspect.getsource(auth)
    # Both are reset to None at the head of every request, and the OAuth branch
    # explicitly clears the API-key one before setting its own.
    assert "current_api_key_id.set(None)" in source
    assert "current_oauth_token_id.set(None)" in source


def test_the_model_declares_the_one_credential_constraint():
    """`alembic check` does not compare CHECK predicates, so the model's
    declaration is what keeps a fresh `create_all` and the migration agreeing.
    """
    names = {
        c.name for c in TransferToken.__table__.constraints if hasattr(c, "sqltext")
    }
    assert "ck_transfer_tokens_one_credential" in names
