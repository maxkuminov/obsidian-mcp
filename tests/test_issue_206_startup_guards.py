"""The two startup fingerprint guards and the reset that clears them (#206).

`note_embeddings` records nothing about the model that produced it and
`content_tsvector` records nothing about `FTS_CONFIGS`, so a same-dimension
model swap or a stemmer change is invisible to every existing check: the first
mixes two vector spaces in one column permanently, and the second makes a
`simple` query for `run` match a note that only ever contained `running`. Both
are answers an agent acts on without a human seeing the query, so both guards
fail closed.

What these cases pin is the *disposition table*, because every entry in it is a
decision that could plausibly have gone the other way:

- absent is **adopted**, not refused — refusing would take every existing
  deployment down on upgrade over a configuration nobody changed;
- unreadable is **refused and never overwritten** — overwriting converts a
  claim this build cannot read into a confident false one;
- a refusal **writes nothing**, so it cannot clear itself and the only exits
  are the repair command or restoring the configuration;
- the table being absent **defers to alembic** rather than deciding;
- and the keyword side's disposition is byte-for-byte the embedding side's,
  because the draft that let it warn and serve was wrong.

The reset cases pin the other half: the lock is the first statement of the
transaction, the fingerprint is recorded in the same transaction as the wipe,
and a failed record takes the whole reset with it.
"""
import json

import pytest

from src import main as main_module
from src.config import MAX_CHUNKS_PER_NOTE
from src.services import index_state
from src.services.index_state import (
    KEY_EMBEDDING_FINGERPRINT,
    KEY_FTS_FINGERPRINT,
    embedding_fingerprint,
    fts_fingerprint,
)

import scripts.reset_embeddings as reset_module


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value

    def first(self):
        return None if self._value is None else (self._value,)


class _FakeSession:
    """An `indexer_state` key/value store with real transaction semantics.

    Writes land in a pending buffer and reach `store` only on `commit()`, so a
    case can tell "wrote nothing" from "wrote and rolled back" — which is the
    whole difference between a guard that can clear its own refusal and one
    that cannot, and between a reset that recorded its fingerprint and one that
    did not.
    """

    def __init__(self, store, *, table_exists=True, fail_on_insert=False):
        self.store = store
        self.table_exists = table_exists
        self.fail_on_insert = fail_on_insert
        self.statements: list[str] = []
        self.pending: dict[str, str] = {}
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        if "to_regclass" in sql:
            return _FakeResult("indexer_state" if self.table_exists else None)
        if sql.startswith("SELECT value FROM indexer_state"):
            key = params["key"]
            return _FakeResult(self.pending.get(key, self.store.get(key)))
        if "INSERT INTO indexer_state" in sql:
            if self.fail_on_insert:
                raise RuntimeError("indexer_state is not writable")
            self.pending[params["key"]] = params["value"]
            return _FakeResult(None)
        return _FakeResult(None)

    async def commit(self):
        self.store.update(self.pending)
        self.pending = {}
        self.commits += 1

    async def rollback(self):
        self.pending = {}
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _patch_sessions(monkeypatch, module, store, **kwargs):
    """Hand `module` a fresh session per `async_session()`, sharing `store`."""
    made: list[_FakeSession] = []

    def _factory():
        session = _FakeSession(store, **kwargs)
        made.append(session)
        return session

    monkeypatch.setattr(module, "async_session", _factory)
    return made


@pytest.fixture
def ollama(monkeypatch):
    """A known-good configuration, so every case varies exactly one thing."""
    settings = main_module.settings
    monkeypatch.setattr(settings, "mcp_sandbox_mode", False)
    monkeypatch.setattr(settings, "embedding_provider", "ollama")
    monkeypatch.setattr(settings, "embedding_model", "bge-m3")
    monkeypatch.setattr(settings, "openai_embedding_model", "text-embedding-3-small")
    monkeypatch.setattr(settings, "embedding_dimensions", 1024)
    monkeypatch.setattr(settings, "chunk_size", 512)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    monkeypatch.setattr(settings, "fts_configs", ["english"])
    return settings


# --------------------------------------------------------------------------
# the embedding guard refuses on every field that changes what a vector is
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute,value,field",
    [
        ("embedding_model", "some-other-1024-dim-model", "model"),
        ("chunk_size", 1024, "chunk_size"),
        ("chunk_overlap", 64, "chunk_overlap"),
        ("embedding_provider", "openai", "provider"),
    ],
)
@pytest.mark.asyncio
async def test_a_changed_generating_setting_refuses_startup(
    ollama, monkeypatch, caplog, attribute, value, field
):
    """The model case is the one the dimension guard cannot see: bge-m3 and
    another 1024-dim model produce the same column width and non-comparable
    vectors, so nothing but this fingerprint notices."""
    stored = embedding_fingerprint()
    store = {KEY_EMBEDDING_FINGERPRINT: stored}
    monkeypatch.setattr(ollama, attribute, value)
    current = embedding_fingerprint()
    _patch_sessions(monkeypatch, main_module, store)

    with caplog.at_level("CRITICAL"):
        with pytest.raises(SystemExit) as exc:
            await main_module._check_embedding_fingerprint()

    assert exc.value.code == 1
    assert stored in caplog.text
    assert current in caplog.text
    assert field in caplog.text
    assert "make reset-embeddings" in caplog.text
    # A refusal decides; it does not repair.
    assert store == {KEY_EMBEDDING_FINGERPRINT: stored}


@pytest.mark.asyncio
async def test_a_chunk_cap_change_refuses_startup(ollama, monkeypatch, caplog):
    """The cap is fingerprinted because it changes what a note's stored vector
    set *is*, and because nothing would ever re-select a note left incomplete
    against a raised cap — its `embedded_content_hash` still matches."""
    stored = embedding_fingerprint()
    store = {KEY_EMBEDDING_FINGERPRINT: stored}
    monkeypatch.setattr(index_state, "MAX_CHUNKS_PER_NOTE", MAX_CHUNKS_PER_NOTE * 2)
    _patch_sessions(monkeypatch, main_module, store)

    with caplog.at_level("CRITICAL"):
        with pytest.raises(SystemExit) as exc:
            await main_module._check_embedding_fingerprint()

    assert exc.value.code == 1
    assert "max_chunks_per_note" in caplog.text
    assert "make reset-embeddings" in caplog.text


@pytest.mark.asyncio
async def test_a_matching_fingerprint_is_silent(ollama, monkeypatch, caplog):
    store = {KEY_EMBEDDING_FINGERPRINT: embedding_fingerprint()}
    sessions = _patch_sessions(monkeypatch, main_module, store)

    with caplog.at_level("WARNING"):
        await main_module._check_embedding_fingerprint()

    assert caplog.records == []
    assert sessions[0].commits == 0


# --------------------------------------------------------------------------
# the keyword guard has the *same* disposition, deliberately
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_fts_membership_change_refuses_startup(ollama, monkeypatch, caplog):
    """The draft that warned and served here was wrong. Under `english`,
    `running` is stored as the lexeme `run`; a `simple` query for `run` then
    matches a note that does not contain the word — a false positive an agent
    acts on, not a recall shortfall."""
    stored = fts_fingerprint()
    store = {KEY_FTS_FINGERPRINT: stored}
    monkeypatch.setattr(ollama, "fts_configs", ["simple"])
    current = fts_fingerprint()
    _patch_sessions(monkeypatch, main_module, store)

    with caplog.at_level("CRITICAL"):
        with pytest.raises(SystemExit) as exc:
            await main_module._check_fts_fingerprint()

    assert exc.value.code == 1
    assert stored in caplog.text
    assert current in caplog.text
    assert "configs" in caplog.text
    assert "make rebuild-tsvectors" in caplog.text
    assert store == {KEY_FTS_FINGERPRINT: stored}


@pytest.mark.asyncio
async def test_reordering_the_fts_configs_does_not_refuse(ollama, monkeypatch, caplog):
    """A note is indexed under every config and a query matches if any hits;
    both operators are order-insensitive over lexeme sets, so refusing on a
    reordering would be a false alarm."""
    monkeypatch.setattr(ollama, "fts_configs", ["english", "norwegian"])
    store = {KEY_FTS_FINGERPRINT: fts_fingerprint()}
    monkeypatch.setattr(ollama, "fts_configs", ["norwegian", "english"])
    _patch_sessions(monkeypatch, main_module, store)

    with caplog.at_level("WARNING"):
        await main_module._check_fts_fingerprint()

    assert caplog.records == []


@pytest.mark.asyncio
async def test_both_guards_refuse_identically(ollama, monkeypatch):
    """No WARNING-vs-ERROR split remains between the two kinds."""
    embedding_store = {KEY_EMBEDDING_FINGERPRINT: embedding_fingerprint()}
    fts_store = {KEY_FTS_FINGERPRINT: fts_fingerprint()}
    monkeypatch.setattr(ollama, "embedding_model", "another-model")
    monkeypatch.setattr(ollama, "fts_configs", ["simple"])

    _patch_sessions(monkeypatch, main_module, embedding_store)
    with pytest.raises(SystemExit) as embedding_exit:
        await main_module._check_embedding_fingerprint()

    _patch_sessions(monkeypatch, main_module, fts_store)
    with pytest.raises(SystemExit) as fts_exit:
        await main_module._check_fts_fingerprint()

    assert embedding_exit.value.code == fts_exit.value.code == 1


# --------------------------------------------------------------------------
# absent is adopted; unreadable is refused and never rewritten
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_absent_fingerprint_is_adopted_then_silent(
    ollama, monkeypatch, caplog
):
    """Refusing on absence would take every existing deployment down on upgrade
    over a configuration nobody changed. The warning is what records that the
    adopted value was *assumed*, not verified."""
    store: dict[str, str] = {}
    _patch_sessions(monkeypatch, main_module, store)

    with caplog.at_level("WARNING"):
        await main_module._check_embedding_fingerprint()

    assert store == {KEY_EMBEDDING_FINGERPRINT: embedding_fingerprint()}
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert "assumed" in caplog.text and "not verified" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING"):
        await main_module._check_embedding_fingerprint()
    assert caplog.records == []


@pytest.mark.parametrize(
    "stored",
    [
        "not json at all",
        "[1, 2, 3]",
        json.dumps({"v": 99, "provider": "ollama"}),
        json.dumps({"provider": "ollama"}),
    ],
)
@pytest.mark.asyncio
async def test_an_unreadable_fingerprint_refuses_and_is_left_alone(
    ollama, monkeypatch, caplog, stored
):
    """A value this build cannot read must not be overwritten with a confident
    one — that would convert an unreadable claim into a false one. Same rule as
    an extraction version whose frozen cleaner this build does not have."""
    store = {KEY_EMBEDDING_FINGERPRINT: stored}
    sessions = _patch_sessions(monkeypatch, main_module, store)

    with caplog.at_level("CRITICAL"):
        with pytest.raises(SystemExit) as exc:
            await main_module._check_embedding_fingerprint()

    assert exc.value.code == 1
    assert "could not be interpreted" in caplog.text
    assert store == {KEY_EMBEDDING_FINGERPRINT: stored}
    assert not any(
        "INSERT INTO indexer_state" in sql for sql in sessions[0].statements
    )


@pytest.mark.asyncio
async def test_a_refusal_writes_nothing_and_repeats(ollama, monkeypatch):
    """A guard that can clear its own refusal is not a guard: only the
    maintenance workflows write a fingerprint after the initial adoption."""
    stored = embedding_fingerprint()
    store = {KEY_EMBEDDING_FINGERPRINT: stored}
    monkeypatch.setattr(ollama, "embedding_model", "another-model")
    sessions = _patch_sessions(monkeypatch, main_module, store)

    for _ in range(2):
        with pytest.raises(SystemExit) as exc:
            await main_module._check_embedding_fingerprint()
        assert exc.value.code == 1

    assert store == {KEY_EMBEDDING_FINGERPRINT: stored}
    assert all(session.commits == 0 for session in sessions)


@pytest.mark.asyncio
async def test_restoring_the_configuration_clears_the_refusal(ollama, monkeypatch):
    """The second exit, and what keeps a refusal from being an outage: a
    configuration edit is always reversible."""
    store = {KEY_EMBEDDING_FINGERPRINT: embedding_fingerprint()}
    monkeypatch.setattr(ollama, "embedding_model", "another-model")
    _patch_sessions(monkeypatch, main_module, store)
    with pytest.raises(SystemExit):
        await main_module._check_embedding_fingerprint()

    monkeypatch.setattr(ollama, "embedding_model", "bge-m3")
    _patch_sessions(monkeypatch, main_module, store)
    await main_module._check_embedding_fingerprint()


@pytest.mark.asyncio
async def test_an_unmigrated_state_store_defers(ollama, monkeypatch, caplog):
    """`_check_embedding_dim`'s stance when its column is absent: return, and
    let alembic own the table. Asking with `to_regclass` rather than a SELECT
    is what makes that possible — a SELECT against a missing relation aborts
    the transaction."""
    store: dict[str, str] = {}
    sessions = _patch_sessions(monkeypatch, main_module, store, table_exists=False)

    with caplog.at_level("WARNING"):
        await main_module._check_embedding_fingerprint()
        await main_module._check_fts_fingerprint()

    assert caplog.records == []
    assert store == {}
    for session in sessions:
        assert session.statements == ["SELECT to_regclass('indexer_state')"]


# --------------------------------------------------------------------------
# where they sit in lifespan
# --------------------------------------------------------------------------


class _FakeSessionManager:
    def run(self):
        manager = self

        class _CM:
            async def __aenter__(self):
                return manager

            async def __aexit__(self, *args):
                return None

        return _CM()


class _FakeMcp:
    session_manager = _FakeSessionManager()


async def _noop():
    return None


def _stub_earlier_guards(monkeypatch):
    monkeypatch.setattr(main_module, "_check_openat2_support", lambda: None)
    monkeypatch.setattr(main_module, "_check_mount_identity_support", lambda: None)
    monkeypatch.setattr(main_module, "_check_embedding_dim", _noop)
    monkeypatch.setattr(main_module, "_check_pgvector_version", _noop)
    monkeypatch.setattr(main_module, "mcp", _FakeMcp())


@pytest.mark.asyncio
async def test_a_misspelled_config_fails_on_its_own_message(ollama, monkeypatch):
    """The fingerprint comparison runs *after* `_validate_fts_configs`, so a
    name the database does not have fails with the message that lists the
    installed configurations rather than as an opaque fingerprint diff. The
    fingerprint refusal is only ever reached by a name that exists."""
    _stub_earlier_guards(monkeypatch)
    reached: list[str] = []

    async def _unknown_config():
        raise SystemExit(
            "FTS_CONFIGS contains unknown text-search config(s): ['engilsh']. "
            "Available: ['english', 'simple']"
        )

    async def _fingerprint():
        reached.append("fingerprint")

    monkeypatch.setattr(main_module, "_validate_fts_configs", _unknown_config)
    monkeypatch.setattr(main_module, "_check_embedding_fingerprint", _fingerprint)
    monkeypatch.setattr(main_module, "_check_fts_fingerprint", _fingerprint)

    with pytest.raises(SystemExit) as exc:
        async with main_module.lifespan(object()):
            pass

    assert "unknown text-search config" in str(exc.value)
    assert "Available:" in str(exc.value)
    assert reached == []


@pytest.mark.asyncio
async def test_sandbox_mode_neither_reads_nor_writes_a_fingerprint(
    ollama, monkeypatch
):
    """Both guards sit below the sandbox short-circuit, so `MCP_SANDBOX_MODE`
    skips them with every other guard — it has no database to read a
    fingerprint from and nothing it could verify."""
    monkeypatch.setattr(ollama, "mcp_sandbox_mode", True)
    _stub_earlier_guards(monkeypatch)

    async def _forbidden():
        raise AssertionError("a fingerprint guard ran in sandbox mode")

    def _forbidden_session():
        raise AssertionError("sandbox mode opened a database session")

    monkeypatch.setattr(main_module, "_check_embedding_fingerprint", _forbidden)
    monkeypatch.setattr(main_module, "_check_fts_fingerprint", _forbidden)
    monkeypatch.setattr(main_module, "async_session", _forbidden_session)

    async with main_module.lifespan(object()):
        pass


# --------------------------------------------------------------------------
# the reset writes the fingerprint the next startup verifies against
# --------------------------------------------------------------------------


class _FakeEngine:
    def __init__(self):
        self.disposed = 0

    async def dispose(self):
        self.disposed += 1


def _patch_reset(monkeypatch, store, **kwargs):
    sessions = _patch_sessions(monkeypatch, reset_module, store, **kwargs)
    monkeypatch.setattr(reset_module, "engine", _FakeEngine())
    return sessions


@pytest.mark.asyncio
async def test_the_reset_takes_the_generation_lock_first(ollama, monkeypatch):
    """Design D7c's ordering rule: the advisory lock is acquired before any row
    or table lock the transaction takes — one direction everywhere, so it
    cannot close a cycle with the locks the pass and the panel contend for.
    `DROP INDEX` takes an ACCESS EXCLUSIVE lock, so "first statement" is not a
    stylistic preference here."""
    store: dict[str, str] = {}
    sessions = _patch_reset(monkeypatch, store)

    await reset_module.reset()

    statements = sessions[0].statements
    assert statements[0] == "SELECT pg_advisory_xact_lock(:key)"
    assert "DROP INDEX" in statements[1] or "statement_timeout" in statements[1]
    lock_at = statements.index("SELECT pg_advisory_xact_lock(:key)")
    drop_at = next(i for i, sql in enumerate(statements) if "DROP INDEX" in sql)
    assert lock_at < drop_at


@pytest.mark.asyncio
async def test_the_reset_records_the_new_fingerprint_in_its_transaction(
    ollama, monkeypatch
):
    """The reset is one of the only writers of the embedding fingerprint, and
    that is what closes the loop: a configuration change refuses startup, the
    repair records the new configuration, and the next startup is silent
    because the rows really were produced under it."""
    stored_before = embedding_fingerprint()
    store = {KEY_EMBEDDING_FINGERPRINT: stored_before}
    monkeypatch.setattr(ollama, "embedding_model", "another-1024-dim-model")
    expected = embedding_fingerprint()
    sessions = _patch_reset(monkeypatch, store)

    await reset_module.reset()

    assert store == {KEY_EMBEDDING_FINGERPRINT: expected}
    session = sessions[0]
    assert session.commits == 1
    # In the *same* transaction as the wipe it describes.
    insert_at = next(
        i for i, sql in enumerate(session.statements) if "INSERT INTO indexer_state" in sql
    )
    wipe_at = next(
        i
        for i, sql in enumerate(session.statements)
        if "SET embedded_content_hash = NULL" in sql
    )
    assert wipe_at < insert_at

    # And the guard that refused before the reset is silent after it.
    _patch_sessions(monkeypatch, main_module, store)
    await main_module._check_embedding_fingerprint()


@pytest.mark.asyncio
async def test_a_failed_fingerprint_record_rolls_the_reset_back(ollama, monkeypatch):
    """Design D7d. "Recording never fails the operation" governs
    instrumentation — the run history, the rotation cursor — where a lost write
    costs an operator a view. A fingerprint is the claim a later startup
    refuses on: a reset that wiped the column and swallowed a failed record
    would leave a stored value naming the previous configuration over rows
    about to be built under the new one."""
    stored_before = embedding_fingerprint()
    store = {KEY_EMBEDDING_FINGERPRINT: stored_before}
    monkeypatch.setattr(ollama, "embedding_model", "another-1024-dim-model")
    sessions = _patch_reset(monkeypatch, store, fail_on_insert=True)

    with pytest.raises(RuntimeError) as exc:
        await reset_module.reset()

    assert "rolled back" in str(exc.value)
    assert store == {KEY_EMBEDDING_FINGERPRINT: stored_before}
    assert sessions[0].commits == 0
    assert sessions[0].rollbacks == 1


@pytest.mark.parametrize(
    "dimensions,expect_index",
    [(1024, True), (2000, True), (3072, False)],
)
@pytest.mark.asyncio
async def test_the_hnsw_index_is_created_only_below_the_limit(
    ollama, monkeypatch, capsys, dimensions, expect_index
):
    """pgvector refuses to build an HNSW index above 2000 dimensions, and this
    script used to issue the CREATE unconditionally — so a deployment
    configured above the limit got a wiped column and an aborted transaction.
    The panel's reset path already applies this condition; both now agree."""
    monkeypatch.setattr(ollama, "embedding_dimensions", dimensions)
    store: dict[str, str] = {}
    sessions = _patch_reset(monkeypatch, store)

    await reset_module.reset()

    created = any("CREATE INDEX" in sql for sql in sessions[0].statements)
    assert created is expect_index
    assert sessions[0].commits == 1
    assert store == {KEY_EMBEDDING_FINGERPRINT: embedding_fingerprint()}
    if not expect_index:
        assert "Skipping HNSW index" in capsys.readouterr().out
