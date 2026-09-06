"""#201 — `embed_note` answers with a typed outcome, not with `0` three times.

The return used to be a chunk count, and `0` meant three unrelated things: a
note that cleaned to zero chunks *and was certified*, a provider exception the
function swallowed, and a vector/chunk cardinality mismatch. `embed_vault` ran
`outcome.embedded += 1` after all three, so a total Ollama or OpenAI outage
wrote an `indexer_runs` row reading `notes_embedded = N, error = NULL` — byte
for byte the record a healthy pass writes, with a *positive* count. The
consumer of this index is an agent, so the operator's only chance of seeing an
outage is that row.

This file pins the five outcomes apart, the bounded failure detail the pass's
record is built from, the two chunk counts, and the generation interlock (#206
/ D7c) that sits between the provider call and the certification.

Fully offline: the session and the provider are both faked.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest
from sqlalchemy import Delete, Update
from sqlalchemy.sql.elements import TextClause

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.config import MAX_EMBED_FAILURE_MESSAGE_CHARS  # noqa: E402
from src.services import embeddings  # noqa: E402
from src.services.embeddings import (  # noqa: E402
    EmbedNoteResult,
    NoteEmbedOutcome,
    StaleCertification,
)
from src.services.index_state import embedding_fingerprint  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _StateResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        # `state_table_exists`'s `to_regclass('indexer_state')`. The fakes
        # answer it separately from the fingerprint read, because the two are
        # different questions with different dispositions: "has 023 run" and
        # "what does it say".
        return self._value


class _RowcountResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _Session:
    """An `AsyncSession` stand-in that records the writes `embed_note` makes.

    `fingerprint` is what the `indexer_state` read answers with; a test can
    change it from inside the fake provider, which is exactly the interleaving
    D7c exists for — a one-off `make reset-embeddings` container committing a
    new fingerprint while this process's provider call is in flight.
    """

    def __init__(
        self, *, fingerprint=None, certify_rowcount=1, state_table=True
    ):
        self.fingerprint = fingerprint
        self.certify_rowcount = certify_rowcount
        #: Whether the state table exists. `False` models that table alone, where
        #: a `SELECT` against `indexer_state` would raise `UndefinedTableError`
        #: and abort the transaction — which is why the guard asks
        #: `to_regclass` first.
        self.state_table = state_table
        self.state_table_probes = 0
        self.statements: list[str] = []
        self.certified: list[str] = []
        self.vector_deletes = 0
        self.added: list = []
        self.flushed = 0
        self.lock_taken = False
        self.fingerprint_reads = 0

    async def execute(self, clause, params=None):
        self.statements.append(str(clause))
        if isinstance(clause, TextClause):
            if "pg_advisory_xact_lock" in clause.text:
                self.lock_taken = True
                return None
            # Ordered before the fingerprint branch: both statements name
            # `indexer_state`, and only this one answers when the table is
            # absent.
            if "to_regclass" in clause.text:
                self.state_table_probes += 1
                return _StateResult("indexer_state" if self.state_table else None)
            if "indexer_state" in clause.text:
                if not self.state_table:
                    raise AssertionError(
                        "a fingerprint SELECT was issued against a database "
                        "where `indexer_state` does not exist — that is the "
                        "`UndefinedTableError` that aborts the whole "
                        "transaction, after the provider call"
                    )
                self.fingerprint_reads += 1
                return _StateResult(self.fingerprint)
            return None
        if isinstance(clause, Update):
            values = dict(clause._values or {})
            self.certified.append(
                str(next(iter(values.values())).effective_value)
            )
            return _RowcountResult(self.certify_rowcount)
        if isinstance(clause, Delete):
            self.vector_deletes += 1
            return None
        return None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1

    def expire(self, _obj, _attrs=None):
        pass


class _Note:
    def __init__(self):
        self.id = 7
        self.file_path = "Projects/Big.md"
        self.content_hash = "h-new"
        self.embedded_content_hash = "h-old"


def _other_fingerprint() -> str:
    """A parseable, this-`v` fingerprint that names a different model."""
    parsed = json.loads(embedding_fingerprint())
    parsed["model"] = parsed["model"] + "-v2"
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


@pytest.fixture
def two_chunks(monkeypatch):
    """A chunk size small enough that ordinary test prose yields >1 chunk."""
    monkeypatch.setattr(embeddings.settings, "chunk_size", 1)
    monkeypatch.setattr(embeddings.settings, "chunk_overlap", 0)


CONTENT = "first chunk here second chunk here third chunk here"


# --------------------------------------------------------------------------- #
# The five outcomes
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_provider_raise_is_its_own_outcome_and_writes_nothing(
    monkeypatch, two_chunks
):
    session, note = _Session(), _Note()

    async def boom(_chunks):
        raise TimeoutError("x" * 5_000)

    monkeypatch.setattr(embeddings, "get_embeddings_batch", boom)

    result = await embeddings.embed_note(
        session, note, CONTENT,
        certified_hash="h-new", certified_path="Projects/Big.md",
    )

    assert result.outcome is NoteEmbedOutcome.PROVIDER_FAILED
    assert result.failure.exc_type == "TimeoutError"
    # Truncated **at capture**, not where the run row is written: the run row's
    # 4,000-character budget is shared with the pass's stage labels, and one
    # provider traceback can evict them on its own.
    assert len(result.failure.message) == MAX_EMBED_FAILURE_MESSAGE_CHARS
    assert result.failure.message == "x" * MAX_EMBED_FAILURE_MESSAGE_CHARS
    assert result.failure.requested == result.chunks_submitted > 1
    assert result.failure.received is None
    # Submitted debits the budget; embedded feeds `total_chunks`. A failure
    # submitted its chunks and stored none.
    assert result.chunks_embedded == 0
    # Nothing was certified, nothing deleted, nothing added (#11).
    assert session.certified == []
    assert session.vector_deletes == 0
    assert session.added == []
    assert note.embedded_content_hash == "h-old"


@pytest.mark.asyncio
async def test_cardinality_mismatch_is_its_own_outcome_and_writes_nothing(
    monkeypatch, two_chunks
):
    session, note = _Session(), _Note()

    async def short(chunks):
        return [[0.0, 1.0]] * (len(chunks) - 1)

    monkeypatch.setattr(embeddings, "get_embeddings_batch", short)

    result = await embeddings.embed_note(
        session, note, CONTENT,
        certified_hash="h-new", certified_path="Projects/Big.md",
    )

    assert result.outcome is NoteEmbedOutcome.PROVIDER_CARDINALITY_MISMATCH
    assert result.failure.exc_type == "CardinalityMismatch"
    assert result.failure.requested == result.chunks_submitted
    assert result.failure.received == result.chunks_submitted - 1
    assert str(result.failure.received) in result.failure.message
    assert str(result.failure.requested) in result.failure.message
    assert result.chunks_embedded == 0
    assert session.certified == []
    assert session.vector_deletes == 0
    assert session.added == []
    assert note.embedded_content_hash == "h-old"


@pytest.mark.asyncio
async def test_zero_chunks_is_certified_and_is_not_an_attempt(monkeypatch):
    session, note = _Session(), _Note()

    async def never(_chunks):  # pragma: no cover - reaching it fails the test
        raise AssertionError("no provider call may be made for an empty note")

    monkeypatch.setattr(embeddings, "get_embeddings_batch", never)

    result = await embeddings.embed_note(
        session, note, "   \n\t  \n",
        certified_hash="h-new", certified_path="Projects/Big.md",
    )

    assert result.outcome is NoteEmbedOutcome.CERTIFIED_EMPTY
    # Zero submitted is what makes this not an attempt: the pass's denominator
    # counts notes for which a provider call was issued, and none was.
    assert result.chunks_submitted == 0
    assert result.chunks_embedded == 0
    assert result.failure is None
    assert result.truncated is False
    # Certified with zero vectors, which is the correct representation of it.
    assert session.certified == ["h-new"]
    assert session.vector_deletes == 1
    assert session.added == []


@pytest.mark.asyncio
async def test_success_reports_both_counts_equal(monkeypatch, two_chunks):
    session, note = _Session(), _Note()

    async def ok(chunks):
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    result = await embeddings.embed_note(
        session, note, CONTENT,
        certified_hash="h-new", certified_path="Projects/Big.md",
    )

    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert result.chunks_submitted == result.chunks_embedded == len(session.added)
    assert result.chunks_embedded > 1
    assert result.failure is None
    assert session.certified == ["h-new"]
    assert session.vector_deletes == 1


# --------------------------------------------------------------------------- #
# The generation interlock (#206 / D7c) — the fifth outcome
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_fingerprint_that_moves_during_the_provider_call_refuses(
    monkeypatch, two_chunks
):
    """The exact interleaving the lock exists for.

    `make reset-embeddings` runs as a one-off container on purpose (#142), so
    it can commit a wipe and a new fingerprint while a *previous* container's
    provider call is in flight. Without the re-read under the lock that
    container certifies previous-model vectors under the new fingerprint —
    permanently, with every later startup silent, because the stored value
    already matches.
    """
    session, note = _Session(fingerprint=embedding_fingerprint()), _Note()

    async def reset_lands_mid_call(chunks):
        session.fingerprint = _other_fingerprint()
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", reset_lands_mid_call)

    result = await embeddings.embed_note(
        session, note, CONTENT,
        certified_hash="h-new", certified_path="Projects/Big.md",
    )

    assert result.outcome is NoteEmbedOutcome.GENERATION_MISMATCH
    # An attempt — a provider call *was* issued — but not a failure and not an
    # embedded note, so it carries no `EmbedNoteFailure`.
    assert result.chunks_submitted > 1
    assert result.chunks_embedded == 0
    assert result.failure is None
    # Nothing certified, nothing inserted, nothing deleted.
    assert session.certified == []
    assert session.vector_deletes == 0
    assert session.added == []
    assert note.embedded_content_hash == "h-old"
    # And the refusal happened under the lock, after the provider answered.
    assert session.lock_taken is True
    assert session.fingerprint_reads == 1


@pytest.mark.asyncio
async def test_the_lock_is_taken_after_the_provider_call_never_before(
    monkeypatch, two_chunks
):
    """No lock of any kind may be held across a network round trip."""
    session, note = _Session(fingerprint=embedding_fingerprint()), _Note()
    lock_state_during_call = []

    async def ok(chunks):
        lock_state_during_call.append(session.lock_taken)
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    await embeddings.embed_note(
        session, note, CONTENT,
        certified_hash="h-new", certified_path="Projects/Big.md",
    )

    assert lock_state_during_call == [False]
    # …and before the certification, which is this transaction's first row
    # lock: advisory-before-any-row-lock and after-the-provider-call agree.
    lock_at = next(
        i for i, s in enumerate(session.statements) if "pg_advisory_xact_lock" in s
    )
    certify_at = next(
        i for i, s in enumerate(session.statements) if "UPDATE notes_metadata" in s
    )
    assert lock_at < certify_at


@pytest.mark.asyncio
async def test_an_absent_fingerprint_is_not_a_mismatch(monkeypatch, two_chunks):
    """Absent claims nothing about the stored rows, so nothing contradicts it.

    Adoption belongs to startup and to the maintenance workflows; this path
    must never write one, which is what stops a refusal from clearing itself.
    """
    session, note = _Session(fingerprint=None), _Note()

    async def ok(chunks):
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    result = await embeddings.embed_note(
        session, note, CONTENT,
        certified_hash="h-new", certified_path="Projects/Big.md",
    )

    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert session.certified == ["h-new"]
    # Nothing wrote the fingerprint back.
    assert not any("INSERT INTO indexer_state" in s for s in session.statements)


@pytest.mark.asyncio
async def test_an_unreadable_fingerprint_refuses_and_is_not_overwritten(
    monkeypatch, two_chunks
):
    """A value this build cannot interpret is one it cannot certify against —
    `clean_at_version`'s "an unknown stamped version counts as differs", in a
    new place. Overwriting it would turn an unreadable claim into a confident
    false one."""
    session, note = _Session(fingerprint="{not json at all"), _Note()

    async def ok(chunks):
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    result = await embeddings.embed_note(
        session, note, CONTENT,
        certified_hash="h-new", certified_path="Projects/Big.md",
    )

    assert result.outcome is NoteEmbedOutcome.GENERATION_MISMATCH
    assert session.certified == []
    assert session.added == []
    assert not any("INSERT INTO indexer_state" in s for s in session.statements)


@pytest.mark.asyncio
async def test_a_matching_fingerprint_certifies_normally(monkeypatch, two_chunks):
    session, note = _Session(fingerprint=embedding_fingerprint()), _Note()

    async def ok(chunks):
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    result = await embeddings.embed_note(
        session, note, CONTENT,
        certified_hash="h-new", certified_path="Projects/Big.md",
    )

    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert session.certified == ["h-new"]


@pytest.mark.asyncio
async def test_a_stale_certification_still_raises_through_the_new_return(
    monkeypatch, two_chunks
):
    """The generation lock is a *second* refusal beside `StaleCertification`,
    not a replacement for it: the row can still move under a pass whose
    configuration never changed."""
    session = _Session(fingerprint=embedding_fingerprint(), certify_rowcount=0)
    note = _Note()

    async def ok(chunks):
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    with pytest.raises(StaleCertification):
        await embeddings.embed_note(
            session, note, CONTENT,
            certified_hash="h-new", certified_path="Projects/Big.md",
        )

    assert session.vector_deletes == 0
    assert session.added == []


# --------------------------------------------------------------------------- #
# An absent state table does not abort the certification transaction
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_embed_path_proceeds_when_indexer_state_is_absent(
    monkeypatch, two_chunks
):
    """An absent state table does not trigger a failing fingerprint SELECT.

    This is a unit test of the guard, using an otherwise usable session double;
    it does not prove that a full revision-022 schema can run the embed pass.
    """
    session = _Session(state_table=False)
    note = _Note()

    async def ok(chunks):
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    result = await embeddings.embed_note(
        session, note, CONTENT,
        certified_hash="h-new", certified_path="Projects/Big.md",
    )

    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert session.added, "nothing was stored with the state table absent"
    assert session.state_table_probes == 1, (
        "the guard must ask `to_regclass` exactly once per note"
    )
    assert session.fingerprint_reads == 0, (
        "a fingerprint SELECT was issued against a table that does not exist"
    )
    assert session.lock_taken is False, (
        "there is nothing to serialise against when nothing is claimed"
    )


@pytest.mark.asyncio
async def test_a_migrated_database_still_takes_the_lock_and_reads(
    monkeypatch, two_chunks
):
    """The contrast, so the guard cannot swallow the interlock wholesale."""
    session = _Session(fingerprint=embedding_fingerprint())
    note = _Note()

    async def ok(chunks):
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    result = await embeddings.embed_note(
        session, note, CONTENT,
        certified_hash="h-new", certified_path="Projects/Big.md",
    )

    assert result.outcome is NoteEmbedOutcome.EMBEDDED
    assert session.state_table_probes == 1
    assert session.fingerprint_reads == 1
    assert session.lock_taken is True


# --------------------------------------------------------------------------- #
# The provider call is announced at issuance, not at the return
# --------------------------------------------------------------------------- #
#
# `on_provider_call` is the pass's accounting hook, and the whole reason it
# exists is that a note can reach the provider and never produce a result: the
# certification raises when the row moved, a database error can escape anywhere
# below, and a cancelled pass abandons the await outright. Each of those spent
# the provider's time, and the pass has to know.
#
# These drive the **real** `embed_note` and the **real** `certify_embedded` —
# `certify_rowcount=0` is what makes the row look moved — so moving the
# invocation below the await, or into the success path, fails them.


def _order_recorder():
    """A callback and a shared order list, so "before" is read off the order."""
    order: list = []

    def announce(chunks_submitted: int) -> None:
        order.append(("announced", chunks_submitted))

    return announce, order


@pytest.mark.asyncio
async def test_the_call_is_announced_before_a_certification_that_raises(
    monkeypatch, two_chunks
):
    """Codex's case: the row moved, so nothing returns — and the call counted.

    `StaleCertification` is not a failure of this note (the vectors describe
    content the row no longer claims, so they are discarded and a later pass
    embeds it as it then stands), but it *is* an attempt, and its chunks were
    submitted. Reconstructing that from the return counts nothing at all here.
    """
    session = _Session(
        fingerprint=embedding_fingerprint(), certify_rowcount=0
    )
    note = _Note()
    announce, order = _order_recorder()

    async def ok(chunks):
        order.append(("provider", len(chunks)))
        return [[0.0, 1.0] for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", ok)

    with pytest.raises(StaleCertification):
        await embeddings.embed_note(
            session, note, CONTENT,
            certified_hash="h-new", certified_path="Projects/Big.md",
            on_provider_call=announce,
        )

    assert [step for step, _ in order] == ["announced", "provider"], (
        "the call was announced after the await, so a path that never returns "
        f"never announces it: {order}"
    )
    announced = [n for step, n in order if step == "announced"]
    submitted = [n for step, n in order if step == "provider"]
    assert len(announced) == 1, "the call was announced more than once"
    assert announced == submitted, (
        "the announced count is not the number of chunks actually submitted"
    )
    # Nothing was written, exactly as before.
    assert session.vector_deletes == 0
    assert session.added == []


@pytest.mark.asyncio
async def test_an_abandoned_provider_call_is_still_announced(
    monkeypatch, two_chunks
):
    """The case that pins "before the await" rather than "before the return".

    A pass cancelled at shutdown, or a provider call killed by a timeout,
    raises a `BaseException` that `embed_note`'s `except Exception` does not
    catch — so there is no result, no swallowed failure, and no second chance
    to notice. The chunks were still handed over. Announcing anywhere below the
    await loses this one.
    """
    session = _Session(fingerprint=embedding_fingerprint())
    note = _Note()
    announce, order = _order_recorder()

    async def abandoned(_chunks):
        raise asyncio.CancelledError()

    monkeypatch.setattr(embeddings, "get_embeddings_batch", abandoned)

    with pytest.raises(asyncio.CancelledError):
        await embeddings.embed_note(
            session, note, CONTENT,
            certified_hash="h-new", certified_path="Projects/Big.md",
            on_provider_call=announce,
        )

    assert [step for step, _ in order] == ["announced"], (
        "a provider call this pass paid for was never announced"
    )


@pytest.mark.asyncio
async def test_a_zero_chunk_note_announces_nothing(monkeypatch):
    """No call, no announcement — the rule stated once, not enumerated.

    A note that cleans to zero chunks is certified with no vectors and makes no
    provider call, so it counts into `notes_embedded` and **not** into
    `attempted`. If it announced, a vault of empty notes would report a
    denominator of provider calls that were never made.
    """
    session = _Session(fingerprint=embedding_fingerprint())
    note = _Note()
    announce, order = _order_recorder()

    async def never(_chunks):  # pragma: no cover - asserted by not being called
        raise AssertionError("a zero-chunk note called the provider")

    monkeypatch.setattr(embeddings, "get_embeddings_batch", never)

    result = await embeddings.embed_note(
        session, note, "```\ncode only\n```\n",
        certified_hash="h-new", certified_path="Projects/Big.md",
        on_provider_call=announce,
    )

    assert result.outcome is NoteEmbedOutcome.CERTIFIED_EMPTY
    assert result.chunks_submitted == 0
    assert order == [], "a note that issued no provider call announced one"


# --------------------------------------------------------------------------- #
# The return type is not a number, deliberately
# --------------------------------------------------------------------------- #
def test_the_result_is_not_an_integer_and_offers_no_shim():
    """A first draft of the design claimed `total_chunks += result` would keep
    working through an `__int__`. It would not — `int.__iadd__` falls back to
    `int.__add__(result)`, which returns `NotImplemented`, and there is no
    `__radd__` — so every caller must read the field it means. Pinned here so
    a later "convenience" cannot quietly re-fuse five outcomes into one
    number."""
    result = EmbedNoteResult(
        outcome=NoteEmbedOutcome.EMBEDDED,
        chunks_submitted=3,
        chunks_embedded=3,
        truncated=False,
    )

    with pytest.raises(TypeError):
        int(result)

    total = 0
    with pytest.raises(TypeError):
        total += result

    assert not hasattr(EmbedNoteResult, "__radd__")
    assert not hasattr(EmbedNoteResult, "__int__")
    # The fields a caller reads instead.
    assert result.chunks_embedded == 3 and result.chunks_submitted == 3


def test_a_failing_outcome_must_carry_its_failure():
    """`record_failure_detail` is driven entirely off `failure`, so a failing
    outcome without one would report a provider outage as `"first: None"` —
    the hole #201 is."""
    with pytest.raises(ValueError):
        EmbedNoteResult(
            outcome=NoteEmbedOutcome.PROVIDER_FAILED,
            chunks_submitted=3,
            chunks_embedded=0,
            truncated=False,
        )
    with pytest.raises(ValueError):
        EmbedNoteResult(
            outcome=NoteEmbedOutcome.EMBEDDED,
            chunks_submitted=3,
            chunks_embedded=3,
            truncated=False,
            failure=embeddings.EmbedNoteFailure.cardinality(requested=3, received=2),
        )


def test_the_result_is_frozen():
    result = EmbedNoteResult(
        outcome=NoteEmbedOutcome.EMBEDDED,
        chunks_submitted=1,
        chunks_embedded=1,
        truncated=False,
    )
    with pytest.raises(Exception):
        result.chunks_embedded = 99
