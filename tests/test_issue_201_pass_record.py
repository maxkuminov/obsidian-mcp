"""#201 — the pass record tells the truth about what the embed pass did.

`embed_note` used to return a bare chunk count, and `0` meant three unrelated
things: a note that cleaned to zero chunks *and was certified*, a provider
exception it had swallowed, and a vector/chunk cardinality mismatch. The caller
ran `outcome.embedded += 1` after all three. So a total Ollama or OpenAI outage
wrote an `indexer_runs` row reading `notes_embedded = N, error = NULL` — byte
for byte the row a healthy pass writes, with a *positive* count. The pass record
built to end a wall of healthy passes reported a wall of healthy passes.

Two things are asserted here and they are separable:

* **the counting** — only the certifying outcomes reach `notes_embedded`, the
  two failing ones reach the accumulator with their own bounded description,
  and `GENERATION_MISMATCH` reaches neither;
* **the denominator** — `attempted` is incremented exactly once per note for
  which a provider call was *issued*, at that call site and nowhere else. It is
  no longer initialised from the backlog's size, so a zero-chunk note counts
  into `notes_embedded` and not into `attempted`, and a reconciliation sweep
  that decided about 16,700 rows without calling anything reports its three
  failures out of three calls rather than out of 16,700.

Fully offline: a fake session, a fake `embed_note`, a real vault on disk (the
pass pins a root and re-hashes what it reads).
"""
import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from sqlalchemy.sql.elements import TextClause  # noqa: E402
from sqlalchemy.sql.selectable import Select  # noqa: E402

import src.services.indexer as indexer  # noqa: E402
from src.services.embeddings import (  # noqa: E402
    EmbedNoteFailure,
    EmbedNoteResult,
    NoteEmbedOutcome,
)


# ── result builders ────────────────────────────────────────────────────────


def embedded(chunks: int = 1) -> EmbedNoteResult:
    return EmbedNoteResult(
        outcome=NoteEmbedOutcome.EMBEDDED,
        chunks_submitted=chunks,
        chunks_embedded=chunks,
        truncated=False,
    )


def certified_empty() -> EmbedNoteResult:
    """A note that cleaned to nothing. Certified, **no provider call.**"""
    return EmbedNoteResult(
        outcome=NoteEmbedOutcome.CERTIFIED_EMPTY,
        chunks_submitted=0,
        chunks_embedded=0,
        truncated=False,
    )


def provider_failed(message: str, *, chunks: int = 3) -> EmbedNoteResult:
    return EmbedNoteResult(
        outcome=NoteEmbedOutcome.PROVIDER_FAILED,
        chunks_submitted=chunks,
        chunks_embedded=0,
        truncated=False,
        failure=EmbedNoteFailure(
            exc_type="ConnectionError", message=message, requested=chunks
        ),
    )


def cardinality(*, requested: int = 4, received: int = 3) -> EmbedNoteResult:
    return EmbedNoteResult(
        outcome=NoteEmbedOutcome.PROVIDER_CARDINALITY_MISMATCH,
        chunks_submitted=requested,
        chunks_embedded=0,
        truncated=False,
        failure=EmbedNoteFailure.cardinality(
            requested=requested, received=received
        ),
    )


def generation_mismatch(chunks: int = 2) -> EmbedNoteResult:
    return EmbedNoteResult(
        outcome=NoteEmbedOutcome.GENERATION_MISMATCH,
        chunks_submitted=chunks,
        chunks_embedded=0,
        truncated=False,
    )


# ── the fake session ───────────────────────────────────────────────────────


class _Result:
    def __init__(self, rows=(), scalar=None, rowcount=0):
        self.rows = list(rows)
        self._scalar = scalar
        self.rowcount = rowcount

    def fetchall(self):
        return self.rows

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self.rows[0]


class _Session:
    """Just enough of a session for `_embed_vault_pinned` and its sweep.

    Dispatches on what a statement *says* rather than on the order it arrives
    in, so a new statement at the head of the pass does not silently reassign
    every canned answer.
    """

    def __init__(self, backlog=(), sweep=(), active_scopes=1):
        self.backlog = list(backlog)
        self.sweep = list(sweep)
        self.active_scopes = active_scopes
        self.statements = []
        self._backlog_served = False
        self._sweep_served = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def execute(self, statement, params=None):
        self.statements.append(statement)
        if isinstance(statement, TextClause):
            sql = statement.text
            if "to_regclass" in sql:
                # Migration 023 absent: the fingerprint reads defer to alembic,
                # so these fixtures stay about counting.
                return _Result(scalar=None)
            if "FROM users" in sql and "count(*)" in sql:
                return _Result(scalar=self.active_scopes)
            if "embedded_content_hash IS NULL" in sql:
                if self._backlog_served:
                    return _Result()
                self._backlog_served = True
                return _Result(self.backlog)
            if "IS NOT DISTINCT FROM" in sql:
                if self._sweep_served:
                    return _Result()
                self._sweep_served = True
                return _Result(self.sweep)
            return _Result()
        if isinstance(statement, Select):
            note_id = statement.compile().params.get("id_1")
            row = next(
                r
                for r in list(self.backlog) + list(self.sweep)
                if r.id == note_id
            )
            return _Result([row])
        return _Result(rowcount=1)


def _row(i, name, body, *, has_vectors=False):
    return SimpleNamespace(
        id=i,
        file_path=name,
        content_hash=indexer._content_hash(body),
        chunks_truncated=False,
        embedded_content_hash=None,
        has_vectors=has_vectors,
    )


def _fixture(monkeypatch, tmp_path, contents, *, sweep=(), active_scopes=1):
    """A real vault plus rows whose hashes match the bytes on disk.

    The pass re-hashes what it reads and refuses to certify bytes that do not
    hash to the selected row, so a fabricated hash would make every note skip
    for that reason and assert nothing about the counting.
    """
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    backlog = []
    for i, (name, body) in enumerate(contents.items(), start=1):
        (vault / name).parent.mkdir(parents=True, exist_ok=True)
        (vault / name).write_text(body, encoding="utf-8")
        backlog.append(_row(i, name, body))

    sweep_rows = []
    for j, (name, body, has_vectors) in enumerate(sweep, start=1000):
        (vault / name).parent.mkdir(parents=True, exist_ok=True)
        (vault / name).write_text(body, encoding="utf-8")
        sweep_rows.append(_row(j, name, body, has_vectors=has_vectors))

    session = _Session(backlog, sweep_rows, active_scopes=active_scopes)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(
        indexer.settings, "embedding_exclude_patterns", [], raising=False
    )
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(
        indexer, "_refuse_quarantined_pass", lambda *_a, **_k: None
    )
    return vault, session, backlog, sweep_rows


def _no_sweep(monkeypatch):
    async def _skip(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "_reconcile_exclusions", _skip)


# ══════════════════════════════════════════════════════════════════════════
# The outage the record must not hide
# ══════════════════════════════════════════════════════════════════════════


def test_a_total_outage_records_the_failure_and_no_embedded_notes(
    monkeypatch, tmp_path
):
    """Every note fails at the provider.

    The row must carry a non-null `error` naming the count, the attempted
    count, and the first failure's class and message — and `notes_embedded`
    must be zero rather than the note count the old code produced.
    """
    _fixture(monkeypatch, tmp_path, {
        "A.md": "alpha\n", "B.md": "beta\n", "C.md": "gamma\n",
    })
    _no_sweep(monkeypatch)

    async def _down(*_a, **_k):
        return provider_failed("Ollama: connection refused")

    monkeypatch.setattr(indexer, "embed_note", _down)

    result = asyncio.run(indexer.embed_vault(user_id=7))

    assert result.embedded == 0
    assert result.failures == 3
    assert result.attempted == 3
    assert result.first_error == "ConnectionError: Ollama: connection refused"
    assert result.failure_summary == (
        "embed failures: 3 of 3 — first: ConnectionError: Ollama: connection "
        "refused"
    )


def test_an_outage_row_is_distinguishable_from_an_empty_backlog(
    monkeypatch, tmp_path
):
    """The two rows an operator has to tell apart, from the other direction.

    A pass with nothing to embed writes `notes_embedded = 0, error = NULL`, and
    that is exactly what an outage used to write.
    """
    _fixture(monkeypatch, tmp_path, {})
    _no_sweep(monkeypatch)

    async def _never(*_a, **_k):  # pragma: no cover - nothing to embed
        raise AssertionError("an empty backlog called the provider")

    monkeypatch.setattr(indexer, "embed_note", _never)

    quiet = asyncio.run(indexer.embed_vault(user_id=7))
    assert (quiet.embedded, quiet.failures, quiet.attempted) == (0, 0, 0)
    assert quiet.failure_summary is None


def test_one_failing_note_among_many(monkeypatch, tmp_path):
    _fixture(monkeypatch, tmp_path, {
        "A.md": "alpha\n", "B.md": "beta\n", "C.md": "gamma\n",
    })
    _no_sweep(monkeypatch)

    async def _flaky(_session, note, _content, **_kw):
        if note.file_path == "B.md":
            return provider_failed("read timeout", chunks=2)
        return embedded(2)

    monkeypatch.setattr(indexer, "embed_note", _flaky)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert (result.embedded, result.failures, result.attempted) == (2, 1, 3)
    assert "embed failures: 1 of 3" in result.failure_summary
    assert "read timeout" in result.failure_summary


def test_a_cardinality_mismatch_is_a_failure_with_its_own_message(
    monkeypatch, tmp_path
):
    """A distinct diagnosis, the same disposition.

    The old return conflated it with a zero-chunk certification, so a provider
    silently dropping a vector read as a successfully embedded note.
    """
    _fixture(monkeypatch, tmp_path, {"A.md": "alpha\n"})
    _no_sweep(monkeypatch)

    async def _short(*_a, **_k):
        return cardinality(requested=4, received=3)

    monkeypatch.setattr(indexer, "embed_note", _short)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert (result.embedded, result.failures, result.attempted) == (0, 1, 1)
    assert result.first_error == "CardinalityMismatch: 3 vectors for 4 chunks"


def test_a_generation_mismatch_is_neither_embedded_nor_a_failure(
    monkeypatch, tmp_path
):
    """The configuration moved under the provider call.

    Nothing went wrong with the provider, so it is not a failure; nothing was
    certified, so it is not an embedded note. It **is** an attempt, because a
    call was issued — the `attempted` rule applying unchanged rather than
    gaining an exception.
    """
    _fixture(monkeypatch, tmp_path, {"A.md": "alpha\n", "B.md": "beta\n"})
    _no_sweep(monkeypatch)

    async def _moved(*_a, **_k):
        return generation_mismatch()

    monkeypatch.setattr(indexer, "embed_note", _moved)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert (result.embedded, result.failures) == (0, 0)
    assert result.attempted == 2
    assert result.failure_summary is None


# ══════════════════════════════════════════════════════════════════════════
# The denominator
# ══════════════════════════════════════════════════════════════════════════


def test_zero_chunk_notes_are_embedded_but_not_attempted(monkeypatch, tmp_path):
    """400 backlog rows of which 50 clean to nothing.

    `notes_embedded` is 400 — a zero-chunk note *is* correctly represented by
    zero vectors and is certified — and `attempted` is 350, because the other
    50 issued no provider call. Under the old shape `attempted` was
    `len(unembedded)` and would have claimed 400 calls that were never made.
    """
    contents = {f"n{i}.md": f"body {i}\n" for i in range(400)}
    _fixture(monkeypatch, tmp_path, contents)
    _no_sweep(monkeypatch)

    empty_paths = {f"n{i}.md" for i in range(50)}

    async def _mixed(_session, note, _content, **_kw):
        if note.file_path in empty_paths:
            return certified_empty()
        return embedded(2)

    monkeypatch.setattr(indexer, "embed_note", _mixed)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert result.embedded == 400
    assert result.attempted == 350
    assert result.failures == 0


def test_deliberate_decisions_move_neither_counter(monkeypatch, tmp_path):
    """An excluded note, a hash-mismatched note, and a paused pass.

    None of the three issues a provider call for that note, so none of them
    moves `attempted`; and none of them is something that went wrong, so none
    of them moves `failures`.
    """
    vault, session, backlog, _ = _fixture(monkeypatch, tmp_path, {
        "Private/A.md": "alpha\n", "B.md": "beta\n", "C.md": "gamma\n",
    })
    _no_sweep(monkeypatch)
    monkeypatch.setattr(
        indexer.settings, "embedding_exclude_patterns", ["Private/*"],
        raising=False,
    )
    # B's bytes no longer hash to its row: the scan has not caught up, so
    # nothing may be certified against it.
    backlog[1].content_hash = "a-hash-of-something-else"

    calls = []

    async def _ok(_session, note, _content, **_kw):
        calls.append(note.file_path)
        return embedded()

    monkeypatch.setattr(indexer, "embed_note", _ok)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert calls == ["C.md"]
    assert (result.embedded, result.failures, result.attempted) == (1, 0, 1)
    assert result.failure_summary is None


def test_a_paused_pass_records_neither_a_failure_nor_an_attempt(
    monkeypatch, tmp_path
):
    _fixture(monkeypatch, tmp_path, {"A.md": "alpha\n", "B.md": "beta\n"})
    _no_sweep(monkeypatch)
    monkeypatch.setattr(indexer, "_is_paused", lambda: True)

    async def _never(*_a, **_k):  # pragma: no cover - paused before the loop
        raise AssertionError("a paused pass called the provider")

    monkeypatch.setattr(indexer, "embed_note", _never)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert (result.embedded, result.failures, result.attempted) == (0, 0, 0)


# ══════════════════════════════════════════════════════════════════════════
# The chunk-cap marker (#202, D3) — set, cleared, and logged after the commit
# ══════════════════════════════════════════════════════════════════════════


def _truncated(chunks: int = 3) -> EmbedNoteResult:
    return EmbedNoteResult(
        outcome=NoteEmbedOutcome.EMBEDDED,
        chunks_submitted=chunks,
        chunks_embedded=chunks,
        truncated=True,
    )


def test_a_capped_note_is_marked_certified_and_logged_after_the_commit(
    monkeypatch, tmp_path, caplog
):
    """A declared degradation, never a skip and never a refusal.

    A capped note held uncertified would be re-selected by the backlog on every
    tick for ever and would re-perform every provider call it already made —
    #127's permanent burn arriving by a new route. So it certifies, counts as
    embedded, and says so on the row.

    And the ERROR line comes **after** the certifying transaction commits.
    Logging it first would leave a permanent entry in a bounded,
    process-lifetime buffer for a write that then rolled back on a
    `StaleCertification`, sending an operator after a note that was never
    stored that way.
    """
    _vault, session, backlog, _ = _fixture(monkeypatch, tmp_path, {
        "Huge.md": "a very long note\n",
    })
    _no_sweep(monkeypatch)

    order: list[str] = []
    real_commit = session.commit

    async def _watch_commit():
        order.append("commit")
        return await real_commit()

    session.commit = _watch_commit

    class _Handler(indexer.logging.Handler):
        def emit(self, record):
            if "MAX_CHUNKS_PER_NOTE" in record.getMessage():
                order.append("error line")

    handler = _Handler()
    indexer.logger.addHandler(handler)

    async def _capped(*_a, **_k):
        return _truncated()

    monkeypatch.setattr(indexer, "embed_note", _capped)
    try:
        result = asyncio.run(indexer.embed_vault(user_id=7))
    finally:
        indexer.logger.removeHandler(handler)

    assert result.embedded == 1, "a capped note was not certified"
    assert result.failures == 0, "a capped note was reported as a failure"
    assert order == ["commit", "error line"], (
        "the truncation was logged before the transaction that carries it "
        "committed"
    )
    # The marker is written in the certifying transaction, from the result.
    note = backlog[0]
    assert note.chunks_truncated is True


def test_the_marker_is_cleared_when_the_note_fits(monkeypatch, tmp_path):
    """`links_truncated`'s lifecycle exactly: set when it bites, cleared when
    a later embed of that note fits under the cap."""
    _vault, _session, backlog, _ = _fixture(monkeypatch, tmp_path, {
        "Shrunk.md": "now a short note\n",
    })
    _no_sweep(monkeypatch)
    backlog[0].chunks_truncated = True

    async def _fits(*_a, **_k):
        return embedded(2)

    monkeypatch.setattr(indexer, "embed_note", _fits)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert result.embedded == 1
    assert backlog[0].chunks_truncated is False


def test_a_zero_chunk_certification_clears_the_marker(monkeypatch, tmp_path):
    """`CERTIFIED_EMPTY` leaves the note with no vectors at all, so a marker
    claiming a truncated set describes something that no longer exists."""
    _vault, _session, backlog, _ = _fixture(monkeypatch, tmp_path, {
        "Empty.md": "```\nonly a fence\n```\n",
    })
    _no_sweep(monkeypatch)
    backlog[0].chunks_truncated = True

    async def _empty(*_a, **_k):
        return certified_empty()

    monkeypatch.setattr(indexer, "embed_note", _empty)

    asyncio.run(indexer.embed_vault(user_id=7))
    assert backlog[0].chunks_truncated is False


def test_a_failing_outcome_does_not_touch_the_marker(monkeypatch, tmp_path):
    """`truncated` is a fact about the note's text and is reported for every
    outcome, but only a *certifying* one licenses writing it: nothing was
    stored, so nothing on the row may claim a truncated set."""
    _vault, _session, backlog, _ = _fixture(monkeypatch, tmp_path, {
        "Huge.md": "a very long note\n",
    })
    _no_sweep(monkeypatch)

    async def _failed(*_a, **_k):
        return EmbedNoteResult(
            outcome=NoteEmbedOutcome.PROVIDER_FAILED,
            chunks_submitted=3,
            chunks_embedded=0,
            truncated=True,
            failure=EmbedNoteFailure(
                exc_type="ConnectionError", message="down", requested=3
            ),
        )

    monkeypatch.setattr(indexer, "embed_note", _failed)

    asyncio.run(indexer.embed_vault(user_id=7))
    assert backlog[0].chunks_truncated is False


# ══════════════════════════════════════════════════════════════════════════
# The reconciliation sweep reports into the same accumulator
# ══════════════════════════════════════════════════════════════════════════


def test_a_reconciliation_only_outage_marks_the_run_failed(monkeypatch, tmp_path):
    """The steady state of a fully-indexed vault.

    The backlog is empty and the sweep is the only stage making provider calls,
    so a sweep that swallowed its failures reproduced the falsely-clean row in
    the one code path a backlog-only fix does not touch. And the denominator
    must be the calls the sweep *made*, not the rows it scanned: the sweep sees
    every certification-current row in the scope, so counting those would
    render three failures out of three calls as "3 of 16,700".
    """
    # Six certification-current rows. Three are included-with-no-vectors and
    # will be re-embedded; three already agree with the configuration and are
    # decided without a call.
    sweep = [
        ("s1.md", "one\n", False),
        ("s2.md", "two\n", False),
        ("s3.md", "three\n", False),
        ("ok1.md", "four\n", True),
        ("ok2.md", "five\n", True),
        ("ok3.md", "six\n", True),
    ]
    _fixture(monkeypatch, tmp_path, {}, sweep=sweep)

    async def _down(*_a, **_k):
        return provider_failed("Ollama: connection refused", chunks=1)

    monkeypatch.setattr(indexer, "embed_note", _down)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert result.embedded == 0
    assert result.failures == 3, "the sweep's failures rode back to the pass"
    assert result.attempted == 3, (
        "attempted counted rows the sweep scanned rather than calls it made"
    )
    assert "embed failures: 3 of 3" in result.failure_summary
    assert "connection refused" in result.failure_summary


def test_a_sweep_that_repairs_records_no_failure(monkeypatch, tmp_path):
    """The control: a healthy sweep leaves `error` null."""
    sweep = [("s1.md", "one\n", False), ("ok1.md", "two\n", True)]
    _fixture(monkeypatch, tmp_path, {}, sweep=sweep)

    calls = []

    async def _ok(_session, note, _content, **_kw):
        calls.append(note.file_path)
        return embedded()

    monkeypatch.setattr(indexer, "embed_note", _ok)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert calls == ["s1.md"]
    assert (result.failures, result.attempted) == (0, 1)
    assert result.failure_summary is None
    # **And it is counted as embedded** (adversarial review). The sweep commits
    # its vectors through the same `certify_embedded` predicate the backlog
    # uses, and on a fully indexed vault it is the only stage making provider
    # calls — so leaving `embedded` at zero here made `notes_embedded`
    # under-report exactly the pass whose entire output was the sweep's.
    assert result.embedded == 1, (
        "the sweep committed vectors that the run row did not count"
    )


def test_a_sweep_that_repairs_several_notes_counts_every_one(
    monkeypatch, tmp_path
):
    """Two repairs and two rows that need none: `embedded` is 2, not 4.

    The counterpart to the failure case above — the denominator must still be
    the calls the sweep made, and now the numerator must be the notes it
    actually certified.
    """
    sweep = [
        ("s1.md", "one\n", False),
        ("s2.md", "two\n", False),
        ("ok1.md", "three\n", True),
        ("ok2.md", "four\n", True),
    ]
    _fixture(monkeypatch, tmp_path, {}, sweep=sweep)

    async def _ok(_session, note, _content, **_kw):
        return embedded(2)

    monkeypatch.setattr(indexer, "embed_note", _ok)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert (result.embedded, result.attempted, result.failures) == (2, 2, 0)


# ══════════════════════════════════════════════════════════════════════════
# The accounting boundary is the provider call, not the return
# ══════════════════════════════════════════════════════════════════════════


def _announces_then_raises(exc_factory, *, chunks: int, calls: list):
    """A fake `embed_note` shaped like the real one's dangerous path.

    The real function issues the provider call and *then* certifies, and
    `certify_embedded` raises `StaleCertification` when the row moved under it.
    Everything between the provider call and the return can therefore raise
    with the call already made and its time already spent — which is why the
    attempt and the chunk debit are announced through `on_provider_call` at
    issuance rather than reconstructed from a result the caller may never see.
    """

    async def _fake(_session, note, _content, *, on_provider_call=None, **_kw):
        calls.append(note.file_path)
        if on_provider_call is not None:
            on_provider_call(chunks)
        raise exc_factory(note.file_path)

    return _fake


def test_a_certification_that_raises_after_the_provider_call_is_attempted(
    monkeypatch, tmp_path
):
    """`StaleCertification` is not a failure, but it *is* an attempt.

    The row moved between the byte verification and the certification, so the
    vectors are discarded and the note is left for a later pass — correctly not
    a failure of this note. But the provider call was made, and a pass whose
    every note lost that race used to report `attempted = 0` while burning the
    whole stage's provider time. `3 of 0` is not a denominator.
    """
    _fixture(monkeypatch, tmp_path, {
        "A.md": "alpha\n", "B.md": "beta\n", "C.md": "gamma\n",
    })
    _no_sweep(monkeypatch)

    calls: list[str] = []
    monkeypatch.setattr(indexer, "embed_note", _announces_then_raises(
        lambda path: indexer.StaleCertification(f"{path} moved"),
        chunks=4,
        calls=calls,
    ))

    result = asyncio.run(indexer.embed_vault(user_id=7))

    assert calls == ["A.md", "B.md", "C.md"], "the backlog stopped early"
    assert result.attempted == 3, (
        "three provider calls were made and none of them was counted"
    )
    assert result.embedded == 0
    assert result.failures == 0, "a lost certification race is not a failure"


def test_a_database_error_after_the_provider_call_is_attempted_and_failed(
    monkeypatch, tmp_path
):
    """The other escaping path: counted as an attempt *and* as a failure.

    The generic handler already recorded the failure; what it could not do was
    know that a provider call had been issued, because that fact only ever
    reached it on the returned result.
    """
    _fixture(monkeypatch, tmp_path, {"A.md": "alpha\n", "B.md": "beta\n"})
    _no_sweep(monkeypatch)

    calls: list[str] = []
    monkeypatch.setattr(indexer, "embed_note", _announces_then_raises(
        lambda path: RuntimeError(f"deadlock detected writing {path}"),
        chunks=6,
        calls=calls,
    ))

    result = asyncio.run(indexer.embed_vault(user_id=7))

    assert (result.attempted, result.failures, result.embedded) == (2, 2, 0)
    assert "embed failures: 2 of 2" in result.failure_summary


def test_the_announced_call_is_not_counted_twice(monkeypatch, tmp_path):
    """The reconciling backstop is idempotent per note.

    `embed_note` announces at issuance and the loop reconciles from the result
    afterwards, so the ordinary path reports the same call from both sides. It
    must land on the counters exactly once.
    """
    _fixture(monkeypatch, tmp_path, {"A.md": "alpha\n", "B.md": "beta\n"})
    _no_sweep(monkeypatch)

    async def _both(_session, _note, _content, *, on_provider_call=None, **_kw):
        if on_provider_call is not None:
            on_provider_call(5)
        return embedded(5)

    monkeypatch.setattr(indexer, "embed_note", _both)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert result.attempted == 2, "the provider call was counted twice"
    assert result.embedded == 2


# ══════════════════════════════════════════════════════════════════════════
# The run row, and the heartbeat that must not move with it
# ══════════════════════════════════════════════════════════════════════════


def test_the_summary_reaches_the_run_row(monkeypatch, tmp_path):
    _fixture(monkeypatch, tmp_path, {"A.md": "alpha\n", "B.md": "beta\n"})
    _no_sweep(monkeypatch)

    async def _down(*_a, **_k):
        return provider_failed("Ollama: connection refused")

    monkeypatch.setattr(indexer, "embed_note", _down)
    result = asyncio.run(indexer.embed_vault(user_id=7))

    stats = indexer.PassStats()
    stats.record_embedded(result)
    assert stats.notes_embedded == 0
    assert stats.error_text is not None, "a total outage recorded a clean pass"
    assert "embed failures: 2 of 2" in stats.error_text
    assert "connection refused" in stats.error_text


def test_the_heartbeat_stays_green_through_the_outage(monkeypatch, tmp_path):
    """#160's deliberate asymmetry, restated because #201 changes the row.

    "Is this process's loop alive" and "did the work succeed" are different
    questions. A provider outage leaves the first green and the second failed,
    and collapsing them would change what the dashboard's Last run means.
    """
    _fixture(monkeypatch, tmp_path, {"A.md": "alpha\n"})
    _no_sweep(monkeypatch)

    async def _down(*_a, **_k):
        return provider_failed("Ollama: connection refused")

    async def _no_scan(*_a, **_k):
        return (0, 0)

    recorded: list = []

    async def _no_row(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "embed_note", _down)
    monkeypatch.setattr(indexer, "index_vault", _no_scan)
    monkeypatch.setattr(indexer, "_write_indexer_run", _no_row)
    monkeypatch.setattr(
        indexer, "_record_index_run", lambda ok: recorded.append(ok)
    )

    ok = asyncio.run(indexer._index_pass_once(7))
    assert ok is True, (
        "a swallowed provider failure flipped the loop-alive heartbeat"
    )


# ══════════════════════════════════════════════════════════════════════════
# The accumulator itself
# ══════════════════════════════════════════════════════════════════════════


def test_record_failure_detail_and_record_failure_share_one_counter():
    """Two entry points, one count and one summary.

    `record_failure(exc)` stays for exceptions that genuinely escape around the
    call — a database error — and `record_failure_detail` covers the provider
    failures `embed_note` swallowed. A second counter would make the run row
    report two numbers for one thing.
    """
    outcome = indexer.EmbedPassResult()
    outcome.record_attempt()
    outcome.record_attempt()
    outcome.record_failure_detail(
        EmbedNoteFailure(exc_type="TimeoutError", message="took too long")
    )
    outcome.record_failure(ValueError("a database blip"))
    assert outcome.failures == 2
    assert outcome.attempted == 2
    assert outcome.first_error == "TimeoutError: took too long"
    assert outcome.failure_summary == (
        "embed failures: 2 of 2 — first: TimeoutError: took too long"
    )


def test_a_failing_outcome_must_carry_its_failure():
    """The invariant `record_failure_detail` is driven off.

    A failing outcome with no failure record would report a provider outage as
    `"first: None"` — the exact hole #201 is.
    """
    with pytest.raises(ValueError):
        EmbedNoteResult(
            outcome=NoteEmbedOutcome.PROVIDER_FAILED,
            chunks_submitted=1,
            chunks_embedded=0,
            truncated=False,
        )
