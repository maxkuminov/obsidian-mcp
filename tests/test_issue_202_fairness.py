"""#202 — one tenant's embed stage cannot deny another tenant's freshness.

`chunk_text` had no cap, `MAX_NOTE_BYTES` is 10 MiB (~5,120 chunks at 512
tokens), Ollama embeds them one sequential 30 s-bounded call at a time, and the
backlog SELECT has no `LIMIT`. Re-editing one huge note kept every later
tenant's new and edited notes out of `notes_metadata`, tsvector and embeddings
indefinitely, under the global `index_pass_lock`, visible only as missing
`indexer_runs` rows.

The budget is what bounds that, and four of its clauses are load-bearing rather
than decorative:

* **never mid-note** — `embed_note` refuses partial certification, so a note
  abandoned between chunks is uncertified, re-selected next tick, and
  re-performs every provider call it already made. That is #127's permanent
  burn arriving by a new route.
* **at least one note, always** — otherwise a tenant whose first note alone
  exceeds the budget advances by zero notes per pass, for ever.
* **only with more than one scope** — with one active scope there is no other
  tenant to be fair to, and budgeting there would spread a first index over
  several five-minute-spaced passes for nothing.
* **debited by chunks *submitted*, never chunks stored** — a failing provider
  stores nothing, so a budget debited by stored chunks is never debited by an
  outage and a tenant whose every note fails would burn the whole pass, every
  pass, without reaching its own bound: #202 surviving inside its own fix.

And a stop is **not** a failure: writing it into `indexer_runs.error` would fire
#201's outage signal on a healthy server.
"""
import asyncio
import inspect
import os
import tempfile
import time

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import src.services.indexer as indexer  # noqa: E402
from tests.test_issue_201_pass_record import (  # noqa: E402
    _fixture,
    _no_sweep,
    cardinality,
    embedded,
    provider_failed,
)


def _budgets(monkeypatch, *, chunks: int, seconds: int = 0):
    monkeypatch.setattr(
        indexer.settings, "embed_chunk_budget_per_user", chunks, raising=False
    )
    monkeypatch.setattr(
        indexer.settings,
        "embed_time_budget_seconds_per_user",
        seconds,
        raising=False,
    )
    monkeypatch.setattr(
        indexer.settings, "multi_user_mode", True, raising=False
    )


def _vault_of(n: int) -> dict[str, str]:
    return {f"n{i}.md": f"body {i}\n" for i in range(n)}


# ══════════════════════════════════════════════════════════════════════════
# The bound
# ══════════════════════════════════════════════════════════════════════════


def test_a_tenant_over_budget_stops_at_a_note_boundary(monkeypatch, tmp_path):
    """Ten notes, a budget of five chunks, five chunks per note.

    The first note completes, the budget is then exhausted, and the second
    note's boundary is where the pass stops — so the tenant advances and the
    next one is reached in the same pass.
    """
    _fixture(monkeypatch, tmp_path, _vault_of(10), active_scopes=2)
    _no_sweep(monkeypatch)
    _budgets(monkeypatch, chunks=5)

    calls = []

    async def _ok(_session, note, _content, **_kw):
        calls.append(note.file_path)
        return embedded(5)

    monkeypatch.setattr(indexer, "embed_note", _ok)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert calls == ["n0.md"], "the budget did not stop the tenant"
    assert result.embedded == 1
    assert result.failures == 0


def test_a_note_in_flight_is_finished_and_certified(monkeypatch, tmp_path):
    """The budget is exhausted partway through the first note's chunks.

    The note still completes and is still counted as embedded — the check runs
    between notes and never inside one, because a note abandoned mid-chunks is
    left uncertified and re-does every call it already made.
    """
    _fixture(monkeypatch, tmp_path, _vault_of(3), active_scopes=2)
    _no_sweep(monkeypatch)
    _budgets(monkeypatch, chunks=1)

    calls = []

    async def _big(_session, note, _content, **_kw):
        calls.append(note.file_path)
        return embedded(500)

    monkeypatch.setattr(indexer, "embed_note", _big)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert calls == ["n0.md"]
    assert result.embedded == 1, "the in-flight note was not certified"


def test_a_first_note_larger_than_the_whole_budget_still_embeds(
    monkeypatch, tmp_path
):
    """Without the at-least-one-note clause a small budget is a livelock."""
    _fixture(monkeypatch, tmp_path, _vault_of(4), active_scopes=2)
    _no_sweep(monkeypatch)
    _budgets(monkeypatch, chunks=1)

    calls = []

    async def _huge(_session, note, _content, **_kw):
        calls.append(note.file_path)
        return embedded(9_999)

    monkeypatch.setattr(indexer, "embed_note", _huge)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert calls == ["n0.md"]
    assert result.embedded == 1


def test_a_single_scope_is_unbudgeted(monkeypatch, tmp_path):
    """The clause that keeps the default deployment identical to today's.

    Single-user mode, and a multi-user deployment with one active user, have no
    other tenant to be fair to. Budgeting there turns a first index of a few
    thousand notes into several passes separated by five-minute sleeps and
    looks like a stall.
    """
    _fixture(monkeypatch, tmp_path, _vault_of(10), active_scopes=1)
    _no_sweep(monkeypatch)
    _budgets(monkeypatch, chunks=1)

    calls = []

    async def _ok(_session, note, _content, **_kw):
        calls.append(note.file_path)
        return embedded(500)

    monkeypatch.setattr(indexer, "embed_note", _ok)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert len(calls) == 10, "a single-scope pass was stopped by the budget"
    assert result.embedded == 10


def test_both_budgets_at_zero_disables_the_machinery(monkeypatch, tmp_path):
    _fixture(monkeypatch, tmp_path, _vault_of(6), active_scopes=5)
    _no_sweep(monkeypatch)
    _budgets(monkeypatch, chunks=0, seconds=0)

    async def _ok(*_a, **_k):
        return embedded(10_000)

    monkeypatch.setattr(indexer, "embed_note", _ok)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert result.embedded == 6


def test_a_budget_stop_is_not_an_error(monkeypatch, tmp_path):
    """A deliberate decision, the same class as a pause.

    Writing it into `error` would make a healthy server report #201's own
    outage signal. The operator-visible signal for a tenant permanently over
    budget is the dashboard's pending count, which is a property of the index
    rather than of one pass.
    """
    _fixture(monkeypatch, tmp_path, _vault_of(20), active_scopes=3)
    _no_sweep(monkeypatch)
    _budgets(monkeypatch, chunks=2)

    async def _ok(*_a, **_k):
        return embedded(2)

    monkeypatch.setattr(indexer, "embed_note", _ok)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert result.failures == 0
    assert result.first_error is None
    assert result.failure_summary is None

    stats = indexer.PassStats()
    stats.record_embedded(result)
    assert stats.error_text is None, "a budget stop reached the run row's error"


# ══════════════════════════════════════════════════════════════════════════
# The regression a stored-chunk debit would leave behind
# ══════════════════════════════════════════════════════════════════════════


def test_a_failing_provider_still_exhausts_the_chunk_budget(
    monkeypatch, tmp_path
):
    """Time budget off, chunk budget on, every provider call failing.

    A budget debited by *stored* chunks is not debited at all here — the
    failing calls store nothing — so this tenant would consume the whole pass,
    every pass, without ever reaching its bound. That is the starvation the
    budget exists to stop, reappearing inside it, and the wall clock does not
    cover the case because an operator may disable it.
    """
    _fixture(monkeypatch, tmp_path, _vault_of(10), active_scopes=2)
    _no_sweep(monkeypatch)
    _budgets(monkeypatch, chunks=6, seconds=0)

    calls = []

    async def _down(_session, note, _content, **_kw):
        calls.append(note.file_path)
        return provider_failed("Ollama: connection refused", chunks=4)

    monkeypatch.setattr(indexer, "embed_note", _down)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert len(calls) == 2, (
        "a failing provider did not debit the budget, so the tenant ran on"
    )
    assert result.failures == 2
    assert result.attempted == 2


def test_a_cardinality_mismatch_debits_the_budget(monkeypatch, tmp_path):
    """Same rule, the other failing outcome: the call sent chunks."""
    _fixture(monkeypatch, tmp_path, _vault_of(10), active_scopes=2)
    _no_sweep(monkeypatch)
    _budgets(monkeypatch, chunks=6, seconds=0)

    calls = []

    async def _short(_session, note, _content, **_kw):
        calls.append(note.file_path)
        return cardinality(requested=4, received=3)

    monkeypatch.setattr(indexer, "embed_note", _short)

    asyncio.run(indexer.embed_vault(user_id=7))
    assert len(calls) == 2


def test_a_zero_chunk_note_does_not_debit_the_budget(monkeypatch, tmp_path):
    """It made no provider call, so there is nothing to debit."""
    budget = indexer.EmbedBudget(chunk_budget=5, enforced=True)
    budget.note_finished()
    budget.debit(0)
    assert not budget.exhausted()


# ══════════════════════════════════════════════════════════════════════════
# The sweep draws on the same budget
# ══════════════════════════════════════════════════════════════════════════


def test_the_sweep_stops_at_a_note_boundary_and_keeps_its_repairs(
    monkeypatch, tmp_path
):
    """A budget-stopped sweep behaves exactly as a paused one.

    It stops between notes, already-repaired rows stay repaired, and the next
    unexhausted pass runs a fresh, idempotent sweep that completes the
    remainder.
    """
    sweep = [(f"s{i}.md", f"body {i}\n", False) for i in range(6)]
    _fixture(monkeypatch, tmp_path, {}, sweep=sweep, active_scopes=2)
    _budgets(monkeypatch, chunks=4, seconds=0)

    calls = []

    async def _ok(_session, note, _content, **_kw):
        calls.append(note.file_path)
        return embedded(4)

    monkeypatch.setattr(indexer, "embed_note", _ok)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert calls == ["s0.md"], "the sweep ignored the pass's budget"
    assert result.failures == 0, "a budget-stopped sweep recorded a failure"

    # A second pass, unexhausted, runs a fresh sweep over the remainder. The
    # per-note commits make re-visiting an already-repaired row a no-op, which
    # is why "fresh from the start" is safe.
    calls.clear()
    _fixture(monkeypatch, tmp_path, {}, sweep=sweep, active_scopes=2)
    _budgets(monkeypatch, chunks=1_000, seconds=0)
    monkeypatch.setattr(indexer, "embed_note", _ok)
    asyncio.run(indexer.embed_vault(user_id=7))
    assert len(calls) == 6


def test_the_backlog_and_the_sweep_share_one_budget(monkeypatch, tmp_path):
    """One allowance per user per pass, not one per stage.

    Two stages with a budget each would let a tenant spend twice what the
    operator configured.
    """
    sweep = [(f"s{i}.md", f"body {i}\n", False) for i in range(4)]
    _fixture(monkeypatch, tmp_path, _vault_of(2), sweep=sweep, active_scopes=2)
    _budgets(monkeypatch, chunks=3, seconds=0)

    calls = []

    async def _ok(_session, note, _content, **_kw):
        calls.append(note.file_path)
        return embedded(3)

    monkeypatch.setattr(indexer, "embed_note", _ok)

    asyncio.run(indexer.embed_vault(user_id=7))
    assert calls == ["n0.md"], (
        "the sweep started with a fresh allowance after the backlog spent it"
    )


# ══════════════════════════════════════════════════════════════════════════
# The scoping, asserted rather than asserted-in-a-comment
# ══════════════════════════════════════════════════════════════════════════


def test_the_scan_and_the_link_backfill_are_not_budgeted(monkeypatch):
    """D5b, as a fact rather than a comment.

    The fairness claim covers the **embed stage only**. `index_vault` and
    `link_backfill_pass` run before it in each user's sequence and are a single
    transaction over a walk of the vault each: stopping one part-way means
    committing a partial derive — which A.7a forbids — or discarding the pass's
    work. The residual is declared on #202 rather than half-solved, so the two
    functions must carry no budget at all.
    """
    for name in ("index_vault", "_index_vault_pinned",
                 "link_backfill_pass", "_link_backfill_pinned"):
        source = inspect.getsource(getattr(indexer, name))
        assert "EmbedBudget" not in source, f"{name} grew a budget"
        assert "budget" not in source.lower(), f"{name} grew a budget"


def test_the_budget_is_only_consulted_at_the_two_note_boundaries():
    """`exhausted()` is called beside `_is_paused()` and nowhere else.

    Asserted structurally because "never inside a note" is the clause that
    keeps the cap from recreating #127's never-finishing note, and a later
    reader adding a check inside the chunk loop would be doing exactly that.
    """
    for name in ("_embed_vault_pinned", "_reconcile_exclusions"):
        source = inspect.getsource(getattr(indexer, name))
        assert source.count("budget.exhausted()") == 1, (
            f"{name} consults the budget somewhere other than its note boundary"
        )


# ══════════════════════════════════════════════════════════════════════════
# The accumulator itself
# ══════════════════════════════════════════════════════════════════════════


def test_the_budget_never_stops_before_the_first_note():
    budget = indexer.EmbedBudget(chunk_budget=1, enforced=True)
    budget.debit(1_000)
    assert not budget.exhausted(), "a tenant was stopped before its first note"
    budget.note_finished()
    assert budget.exhausted()


def test_an_unenforced_budget_never_stops_anything():
    budget = indexer.EmbedBudget(chunk_budget=1, enforced=False)
    budget.note_finished()
    budget.debit(10_000)
    assert not budget.exhausted()


def test_the_wall_clock_budget_stops_on_its_own():
    """Either bound stops the tenant; the operator may disable either one.

    Driven by `started_at` rather than by patching the clock, because
    `time.monotonic` is the stdlib module's and patching it would leak into
    every other test in the process.
    """
    fresh = indexer.EmbedBudget(
        chunk_budget=0, time_budget=30.0, enforced=True,
        started_at=time.monotonic(),
    )
    fresh.note_finished()
    assert not fresh.exhausted()

    overrun = indexer.EmbedBudget(
        chunk_budget=0, time_budget=30.0, enforced=True,
        started_at=time.monotonic() - 31.0,
    )
    overrun.note_finished()
    assert overrun.exhausted()


def test_the_stop_is_logged_once_per_user_per_pass(caplog):
    """One WARNING, whichever stage reached the bound first."""
    budget = indexer.EmbedBudget(chunk_budget=1, enforced=True)
    with caplog.at_level("WARNING", logger="src.services.indexer"):
        budget.stop(" (user_id=7)", "the hash-mismatch backlog")
        budget.stop(" (user_id=7)", "the exclusion-reconciliation sweep")
    stops = [r for r in caplog.records if "Embed budget exhausted" in r.getMessage()]
    assert len(stops) == 1
