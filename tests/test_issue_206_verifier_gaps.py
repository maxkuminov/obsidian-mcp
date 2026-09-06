"""Four scenarios of `index-integrity-hardening` that shipped unasserted.

The verifier's pass over the merged change found spec scenarios with no test
behind them. They are grouped here because they share a shape rather than a
subsystem: each is a claim about a *negative* — something the code deliberately
does **not** do — and a negative is exactly what a later refactor removes
without noticing, because nothing fails.

- **A capped note is not a skip** (`index-integrity`, "A capped note is not a
  skip and does not withhold a re-derive's record"). Asserted structurally, and
  the structure is the honest form of the claim: the scan cannot make a
  chunk-capped note a skip because the scan never learns about the chunk cap.
- **The in-process reset cannot deadlock against its own pass**
  (`embedding-providers`, "The in-process reset does not deadlock against its
  own pass"). Asserted as an ordering over the handler's source: the pass lock
  is held *before* a second connection is checked out, which is the whole
  content of the claim.
- **The rebuild driver's all-or-nothing abort does not reach the link
  backfill** (`index-integrity`, "The exception does not reach the link
  backfill").
- **The single-scope keyword rebuild is private and has no production
  caller** — it writes `content_tsvector` outside the generation-lock
  interlock, so both its name and its caller set are guarded.

Fully offline.
"""
from __future__ import annotations

import ast
import inspect
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.control_panel import routes  # noqa: E402
from src.services import indexer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# (a) index-integrity — "A capped note is not a skip and does not withhold a
#     re-derive's record"
# --------------------------------------------------------------------------- #
#
# A.7a withholds a re-derive's provenance stamp when the pass appended anything
# to `skips`: the re-derive's whole claim is that every surviving row was
# written by this pass, and one skipped path falsifies it. A chunk-capped note
# must not be one of those, for the reason the *link* cap is not one either —
# the truncation is deterministic, the rows written are exactly the rows
# derived, and treating it as a skip would park a tenant holding one enormous
# note in re-derive mode for ever, with no repair that could end it.
#
# The link carve-out has a behavioural test
# (`tests/integration/test_asvs_links_truncated_pg.py`) because the *scan*
# performs link extraction and could therefore skip on it. The chunk cap is
# different in a way that makes the structural assertion the stronger one: the
# cap lives in `embed_note`, a different pass in a different transaction, and
# the scan has no access to it at all. So the claim to hold on to is not "the
# scan chose not to skip" but "the scan cannot": if a later change teaches
# `_index_vault_pinned` about chunk truncation, this fails and whoever wrote it
# has to decide, deliberately, whether the new knowledge belongs in `skips`.

#: Every name by which the chunk cap could reach the scan.
CHUNK_CAP_NAMES = (
    "chunks_truncated",
    "MAX_CHUNKS_PER_NOTE",
    "chunk_text_bounded",
    "chunk_text",
    "embed_note",
)


def _function_source(func) -> str:
    return inspect.getsource(func)


def test_the_scan_never_learns_about_the_chunk_cap():
    """`_index_vault_pinned` cannot skip on a truncation it cannot see."""
    source = _function_source(indexer._index_vault_pinned)
    found = [name for name in CHUNK_CAP_NAMES if name in source]
    assert not found, (
        "the scan now references the chunk cap "
        f"({', '.join(found)}). It owns `skips`, and anything in `skips` "
        "withholds a re-derive's provenance stamp — so if the scan is to know "
        "about chunk truncation at all, the spec scenario 'A capped note is "
        "not a skip and does not withhold a re-derive's record' has to be "
        "re-asserted behaviourally rather than by this absence."
    )


def test_no_skip_reason_anywhere_in_the_scan_names_a_truncation():
    """The other half: read the skip reasons themselves, not the function.

    `_index_vault_pinned` delegates parts of the pass to helpers that take
    `skips` as a parameter — the link rebuild is one — so the reasons it can
    accumulate are spread over more than one function. This walks every
    `skips.append(...)` in the module and asserts none of them is a truncation
    of any kind, which covers the link cap's carve-out as well.
    """
    tree = ast.parse((ROOT / "src" / "services" / "indexer.py").read_text())
    reasons: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "append"
            and isinstance(func.value, ast.Name)
            and func.value.id == "skips"
        ):
            continue
        reasons.append(ast.unparse(node.args[0]) if node.args else "")

    assert reasons, "no `skips.append` found — this test lost its subject"
    offenders = [
        reason
        for reason in reasons
        if "truncat" in reason.lower() or "capped" in reason.lower()
    ]
    assert not offenders, (
        f"a truncation was recorded as a skip: {offenders}. A declared, "
        "deterministic degradation is not a skip; it is carried on the row "
        "(`links_truncated` / `chunks_truncated`), in an ERROR line and in the "
        "tool output. Recording it here would withhold the re-derive's "
        "provenance stamp for ever."
    )


# --------------------------------------------------------------------------- #
# (b) embedding-providers — "The in-process reset does not deadlock against its
#     own pass"
# --------------------------------------------------------------------------- #
#
# The panel's Danger-zone resets hold *two* locks: `index_pass_lock`, which is
# process-local and stops this container's pass, and the advisory generation
# lock, which is cross-process. The deadlock they must not reproduce is not
# between those two — it is between the pass lock and the connection pool.
# `get_session` has already checked out one of five pooled connections; if the
# handler blocked on `index_pass_lock` while holding it, a handful of
# concurrent resets would exhaust the pool and the lock *holder* — the pass,
# which needs a connection of its own to finish — would then wait on the
# waiters for ever.
#
# `_pass_lock_without_a_connection` closes the request's session first, waits,
# and only then opens `async_session()`. The property is an **ordering**, so an
# ordering is what is asserted: a behavioural test would have to reproduce pool
# exhaustion, and one that did not would prove nothing this does not.

RESET_HANDLERS = (routes.trigger_reembed, routes.reset_embeddings)


@pytest.mark.parametrize(
    "handler", RESET_HANDLERS, ids=lambda h: h.__name__
)
def test_the_reset_holds_the_pass_lock_before_it_opens_a_connection(handler):
    source = _function_source(handler)
    lock_at = source.find("_pass_lock_without_a_connection(session)")
    fresh_at = source.find("async_session() as fresh")
    generation_at = source.find("acquire_generation_lock_unbounded(fresh)")

    assert lock_at != -1, (
        f"{handler.__name__} no longer takes the pass lock through "
        "`_pass_lock_without_a_connection`, which is the helper that ends the "
        "request's own transaction before waiting"
    )
    assert fresh_at != -1 and generation_at != -1, (
        f"{handler.__name__} no longer opens its own session and takes the "
        "generation lock in it"
    )
    assert lock_at < fresh_at < generation_at, (
        f"{handler.__name__} checks out a second connection before it holds "
        "the pass lock. That is the pool-exhaustion deadlock the helper "
        "exists to prevent: the waiters hold every connection and the lock "
        "holder needs one to finish."
    )


def test_the_pass_lock_helper_closes_the_request_session_before_waiting():
    """The helper's own half of the ordering, in the helper's own source."""
    source = _function_source(routes._pass_lock_without_a_connection)
    close_at = source.find("await session.close()")
    lock_at = source.find("async with index_pass_lock")
    assert close_at != -1 and lock_at != -1, (
        "`_pass_lock_without_a_connection` no longer closes the session and "
        "takes the pass lock"
    )
    assert close_at < lock_at, (
        "the helper waits for the pass lock while still holding the request's "
        "pooled connection — the deadlock it was written to remove"
    )


# --------------------------------------------------------------------------- #
# (c) index-integrity — "The exception does not reach the link backfill"
# --------------------------------------------------------------------------- #
#
# The fingerprint-recording rebuild is all-or-nothing: one skipped scope aborts
# the whole driver, rolls every rebuilt scope back and records nothing, because
# its fingerprint is a claim about *every* retained row. That carve-out is
# narrow and it is the exception. The link backfill keeps the ordinary
# disposition — skip the unsettled user, complete for the others — and must
# never acquire the driver's semantics by proximity.


class _Result:
    """Enough of a `Result` for the two statements past the gate."""

    def scalar(self):
        return 0

    def all(self):
        return []


class _RecordingSession:
    def __init__(self):
        self.executed = 0

    async def execute(self, *_a, **_k):
        self.executed += 1
        return _Result()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Maker:
    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self._session


@pytest.mark.asyncio
async def test_an_unsettled_user_is_skipped_and_the_settled_one_proceeds(
    monkeypatch,
):
    """One user's refused gate neither raises nor touches the other's pass."""
    permitted: dict[int, bool] = {1: False, 2: True}

    async def _gate(_session, user_id, *_a, **_k):
        return permitted[user_id]

    monkeypatch.setattr(indexer, "_ancillary_pass_is_permitted", _gate)

    seen = {}
    for uid in (1, 2):
        session = _RecordingSession()
        monkeypatch.setattr(indexer, "async_session", _Maker(session))
        stats = indexer.PassStats()
        # Returns — never raises. A `RebuildCoverageAborted` here would take
        # the whole startup sequence for every later user with it.
        result = await indexer._link_backfill_pinned(
            uid, Path("/nonexistent"), -1, stats
        )
        assert result is None
        assert stats.skipped is True
        seen[uid] = session.executed

    assert seen[1] == 0, (
        "the refused user's backfill read rows anyway — the gate is supposed "
        "to precede every statement"
    )
    assert seen[2] > 0, (
        "the settled user's backfill did not run at all, so one user's "
        "unsettled provenance stopped another user's pass"
    )


def test_the_rebuild_drivers_abort_is_not_reachable_from_the_backfill():
    """`RebuildCoverageAborted` is the driver's, and only the driver's."""
    backfill = _function_source(indexer.link_backfill_pass) + _function_source(
        indexer._link_backfill_pinned
    )
    assert "RebuildCoverageAborted" not in backfill, (
        "the link backfill now references the all-or-nothing abort. That "
        "exception exists because one stored fingerprint claims something "
        "about every retained row; the backfill claims nothing global and "
        "must keep skipping the scope and completing for the rest."
    )
    # The driver is two functions since #199 round 2 — a guarded half that
    # takes the account guard and surveys the roots, and a locked half that
    # takes the generation lock and reads — so the contrast is read off both.
    driver = _function_source(indexer.rebuild_tsvectors_all_scopes) + (
        _function_source(indexer._rebuild_all_scopes_locked)
    )
    assert "RebuildCoverageAborted" in driver, (
        "the driver stopped raising it — this test lost its contrast"
    )


# --------------------------------------------------------------------------- #
# The lock wait needs the raise because the engine caps every statement
# --------------------------------------------------------------------------- #


def test_the_engine_still_caps_every_statement():
    """The premise of `acquire_generation_lock_unbounded`, asserted.

    `pg_advisory_xact_lock` is a statement, so the connection's
    `statement_timeout` bounds the *wait* for the generation lock — which is
    why every path whose contract is "it waits for an in-flight pass" lifts it
    for the acquisition and restores it afterwards. The integration test that
    proves the raise works lowers the timeout to one second, because waiting
    out the production sixty would cost a minute per case and prove the same
    thing.

    That scale model is only faithful while production actually sets a cap. If
    `server_settings` ever stopped setting `statement_timeout`, the raise would
    become dead code protecting nothing, the integration test would still pass
    against its own lowered value, and nobody would learn that the reason for
    the helper had gone away. So the premise is pinned here, in the one place
    that reads it as a premise rather than as tuning.
    """
    source = (ROOT / "src" / "database.py").read_text()
    assert '"statement_timeout"' in source, (
        "src/database.py no longer sets a per-connection statement_timeout. "
        "If that is deliberate, `acquire_generation_lock_unbounded` no longer "
        "has anything to lift and the integration test that lowers the "
        "timeout to 1s is testing a scale model of nothing — revisit both "
        "before deleting this test."
    )
    assert '"statement_timeout": "0"' not in source, (
        "the engine's statement_timeout was set to 0 (no cap); see above"
    )


# --------------------------------------------------------------------------- #
# (5) The single-scope keyword rebuild is private and has no production caller
# --------------------------------------------------------------------------- #

#: Where a production caller could appear. Tests are excluded deliberately:
#: the single-scope rebuild keeps its `int` contract *for* them.
PRODUCTION_TREES = ("src", "scripts")

#: The single-scope keyword rebuild, named for its only legitimate caller.
SINGLE_SCOPE_REBUILD = "_rebuild_tsvectors_single_scope_for_tests"


def _calls_named(name: str) -> list[str]:
    """`file:line` for every call to `name` under the production trees."""
    hits: list[str] = []
    for tree in PRODUCTION_TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            module = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(module):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None
                )
                if called == name:
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return hits


def test_the_single_scope_rebuild_stays_private():
    """No public `rebuild_tsvectors` may reappear on the indexer.

    The name is half the guard. This function commits keyword vectors without
    taking the index generation lock and without re-reading the keyword
    fingerprint under it, which every other production writer of
    `content_tsvector` does — the incremental pass at the head of its
    transaction, the all-scopes driver before its first read. A public
    `rebuild_tsvectors` sitting beside `rebuild_tsvectors_all_scopes` reads
    like the cheap per-user version of it, and picking the wrong one writes a
    row outside the interlock.
    """
    assert hasattr(indexer, SINGLE_SCOPE_REBUILD), (
        f"{SINGLE_SCOPE_REBUILD} is gone; if the single-scope rebuild was "
        "removed outright, remove these two tests with it"
    )
    assert not hasattr(indexer, "rebuild_tsvectors"), (
        "a public `rebuild_tsvectors` is back on the indexer. It writes "
        "`content_tsvector` outside the generation-lock interlock, and beside "
        "`rebuild_tsvectors_all_scopes` it reads like the per-user version of "
        "the operational entry point."
    )


def test_the_single_scope_rebuild_has_no_production_caller():
    """A caller would need the generation lock, and would not have it.

    The single-scope rebuild survives for the tests that hold its `int`
    contract — atomicity, the certified UPDATE predicate, the provenance gate.
    Every *production* writer of `content_tsvector` takes the index generation
    lock at the head of its transaction and re-reads the keyword fingerprint
    under it. This one does neither, because nothing in `src/` or `scripts/`
    calls it.

    Giving it a caller is therefore not a small change: it introduces a
    keyword-vector writer outside the interlock, and a keyword vector is
    rewritten only when a note's content hash changes, so a row written under
    a superseded `FTS_CONFIGS` would keep that vector indefinitely behind a
    fingerprint claiming otherwise — a keyword hit on a note that does not
    contain the word, acted on by an agent unseen. Whoever adds the caller must
    take the lock first, and must delete this test on purpose rather than
    discover the requirement afterwards.
    """
    callers = _calls_named(SINGLE_SCOPE_REBUILD) + _calls_named(
        "rebuild_tsvectors"
    )
    assert callers == [], (
        "the single-scope keyword rebuild gained a production caller at "
        f"{', '.join(callers)}. Take `acquire_generation_lock` (or the "
        "unbounded form) at the head of that transaction and re-read the "
        "keyword fingerprint under it before removing this test — see "
        "docs/architecture/indexing-and-embeddings.md, 'The index generation "
        "lock'. `rebuild_tsvectors_all_scopes` already does both."
    )
