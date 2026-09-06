"""#199 — the tool-facing vault-root quarantine refusals, and their markers.

Two tenants whose vault roots name overlapping directories are not a *partial*
leak: they are one tenant, in both directions, for every tool the server has.
The database-backed tools answer from `notes_metadata` / `note_embeddings` and
never touch the disk, so rows a previous pass filed under the outer tenant stay
queryable; and the write tools resolve beneath a root that physically contains
the inner tenant's files, with `RESOLVE_BENEATH` agreeing they are contained.
Refusing to *index* the pair reaches neither. The admission gate is the only
control that is total over both, which is why the refusal lands in `_tracked`
rather than in any tool.

What is pinned here:

* `_vault_admission_error` catches `VaultRootOverlap`, `VaultRootUnexaminable`
  and `VaultRootNotReady` **ahead** of the generic `RuntimeError` branch — they
  are `RuntimeError` subclasses, so an ordering slip would silently file every
  quarantine under `no_vault_assigned`;
* a database-backed tool, a graph tool, a write tool and the three
  capability-minting transfer tools are each refused for each of the three
  reasons, before any body runs;
* each reason carries its **own** pre-body marker, distinct from each other and
  from `no_vault_assigned`, and every one of them is enumerated by the shared
  pre-body refusal predicate (a marker the predicate does not enumerate is
  wrong in both directions at once: its `duration_ms` is folded into the tool's
  latency percentiles as though the body had executed, and the refusal itself
  is never counted);
* the caller-facing wording names **no other user, no other vault path and no
  note path**, for any reason — the caller is a tenant's agent, and a refusal
  that said whose root it collided with would be the leak in miniature;
* the refusal passes the security-event suppressor rather than reaching the
  module logger directly; and
* a caller the snapshot does not name is admitted, single-user mode never
  consults the snapshot at all, and the refusal opens no database session — so
  nothing it could delete is even reachable.

Hermetic. That the predicate actually partitions `usage_logs` rows that way on
PostgreSQL is `tests/integration/test_issue_160_performance_pg.py`'s job; what
is pinned below is the enumeration and the bind set, which is where a new
marker goes missing.
"""
import datetime as _dt
import errno
import logging
import re
from types import SimpleNamespace

import pytest

import src.mcp_server.tools as tools
import src.services.vault as vault
import src.services.vault_overlap as vault_overlap
from src.auth.session import current_actor, current_user_id, current_vault_root
from src.logging_setup import build_payload
from src.services import security_events, usage_stats

UID = 11
ACTOR = ("api_key", "nightly sync", "omcp_a1b2c3")

#: The peer's identifiers, which the operator surfaces name and the agent-facing
#: refusal must not. Chosen to be unmistakable in a substring search.
PEER_UID = 12
PEER_USERNAME = "bob-the-other-tenant"
PEER_ASSIGNMENT = "/vaults/bob-private-root"
SUBJECT_ASSIGNMENT = "/vaults/alice"
NOTE_PATH = "Projects/Alpha.md"


# ── The three reasons, and how to put the process into each ─────────────────

#: `(reason label, marker, caller-facing message)` per quarantine reason. The
#: labels match the `reason` field on the security event.
#:
#: The message is read from `_QUARANTINE_REFUSALS` rather than from
#: `src/services/vault.py` directly, because since #194 what the caller
#: receives is that wording **plus** the one machine-readable sentinel line
#: every refusal raised inside `_tracked` ends with. The prose is unchanged —
#: `test_the_wording_is_additive_to_the_vault_module` below pins that — and the
#: line is appended.
REASONS = [
    (
        "overlap",
        tools._VAULT_ROOT_OVERLAP_MARKER,
        tools._QUARANTINE_REFUSALS[vault.VaultRootOverlap][0],
    ),
    (
        "root_unexaminable",
        tools._VAULT_ROOT_UNEXAMINABLE_MARKER,
        tools._QUARANTINE_REFUSALS[vault.VaultRootUnexaminable][0],
    ),
    (
        "snapshot_not_ready",
        tools._VAULT_ROOT_NOT_READY_MARKER,
        tools._QUARANTINE_REFUSALS[vault.VaultRootNotReady][0],
    ),
]


def test_the_wording_is_additive_to_the_vault_module():
    """The sentinel line is appended; the prose is byte-identical (#194).

    The three messages an agent reads are the three `src/services/vault.py`
    authored, plus one final line — so every assertion written against that
    wording, here and everywhere else, still holds.
    """
    for prose, (message, _marker, _reason) in zip(
        (
            vault.VAULT_ROOT_OVERLAP_ERROR,
            vault.VAULT_ROOT_UNEXAMINABLE_ERROR,
            vault.VAULT_ROOT_NOT_READY_ERROR,
        ),
        (
            tools._QUARANTINE_REFUSALS[vault.VaultRootOverlap],
            tools._QUARANTINE_REFUSALS[vault.VaultRootUnexaminable],
            tools._QUARANTINE_REFUSALS[vault.VaultRootNotReady],
        ),
    ):
        assert message.startswith(prose)
        assert message.splitlines()[-1].startswith("MCP-REFUSAL ")
REASON_IDS = [reason for reason, _, _ in REASONS]


def _quarantine(reason: str) -> None:
    """Put this process into the state that produces `reason` at the gate.

    `snapshot_not_ready` is the *absence* of a publication, not an entry in one:
    the suite's autouse fixture publishes an empty snapshot before every test
    precisely so the readiness refusal does not turn every multi-user test into
    a vault-unavailable failure, so this reason has to clear it.
    """
    if reason == "snapshot_not_ready":
        vault_overlap.reset_snapshot_state()
        return
    if reason == "overlap":
        why = vault_overlap.Overlap(
            peer_user_id=PEER_UID,
            peer_username=PEER_USERNAME,
            peer_assignment=PEER_ASSIGNMENT,
            relation="contains",
        )
    else:
        why = vault_overlap.RootUnexaminable(cause=errno.ENOENT)
    vault_overlap.publish_synthetic_snapshot(
        [
            vault_overlap.QuarantineEntry(
                user_id=UID,
                username="alice",
                assignment=SUBJECT_ASSIGNMENT,
                reason=why,
                detected_at=_dt.datetime.now(_dt.timezone.utc),
            )
        ]
    )


# ── Fixtures ────────────────────────────────────────────────────────────────


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def sink():
    """Records reaching the security-event logger, and the module logger.

    The second handler is the point of the first: a refusal that reached
    `tools.logger` directly would be an unbounded flood channel beside the
    bounded one, which is exactly what `tool_refused_no_vault` used to be.
    """
    handler = _Capture()
    bare = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    tools.logger.addHandler(bare)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.strict_fields():
            yield SimpleNamespace(records=handler.records, bare=bare.records)
    finally:
        logger.removeHandler(handler)
        tools.logger.removeHandler(bare)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


@pytest.fixture
def quarantined(tmp_path, monkeypatch):
    """A caller whose credential and assignment are otherwise perfectly good.

    The vault root is bound to a real directory and the cache is warm, so the
    *only* reason any of these calls is refused is the quarantine. A fixture
    that left the root unresolvable would pin `no_vault_assigned` and prove
    nothing about #199.

    `async_session` is replaced with one that raises: the gate must open no
    database session, and a refusal that opened one could in principle be a
    refusal that deleted something. Returns the captured `usage_logs` rows.
    """
    from src.services import vault as vault_service

    monkeypatch.setattr(vault_service, "_user_vault_cache", {UID: tmp_path})

    def _no_session():
        raise AssertionError("the admission gate opened a database session")

    monkeypatch.setattr(tools, "async_session", _no_session)

    written: list[dict] = []

    async def capture(tool, params, duration_ms, response_size):
        written.append({"tool": tool, "params": params, "size": response_size})
        return True

    monkeypatch.setattr(tools, "_log_usage", capture)

    uid = current_user_id.set(UID)
    root = current_vault_root.set((UID, tmp_path))
    actor = current_actor.set(ACTOR)
    try:
        yield written
    finally:
        current_actor.reset(actor)
        current_vault_root.reset(root)
        current_user_id.reset(uid)


#: One tool from each consuming surface, plus the three that mint a transfer
#: capability. The database-backed and graph tools are here because refusing to
#: *index* a quarantined pair never touches them; the write tool because the
#: cross-tenant destructive write is the failure this product ranks highest;
#: the minting tools because a capability pins its vault root at mint time and
#: would outlive the quarantine that should have stopped it.
SURFACES = [
    ("list_notes", "database-backed", lambda: tools.list_notes_impl()),
    ("keyword_search", "database-backed", lambda: tools.search_notes_impl("salary")),
    ("get_backlinks", "graph", lambda: tools.get_backlinks_impl(NOTE_PATH)),
    ("edit_note", "write", lambda: tools.edit_note_impl(NOTE_PATH, "body")),
    ("request_upload", "mint", lambda: tools.request_upload_impl(NOTE_PATH)),
    ("request_download", "mint", lambda: tools.request_download_impl(NOTE_PATH)),
    (
        "import_from_url",
        "mint",
        lambda: tools.import_from_url_impl("https://example.com/a.md", NOTE_PATH),
    ),
]
SURFACE_IDS = [f"{name}-{kind}" for name, kind, _ in SURFACES]


# ── (a) every surface refuses, for every reason ─────────────────────────────


@pytest.mark.parametrize("surface", SURFACES, ids=SURFACE_IDS)
@pytest.mark.parametrize("reason", REASONS, ids=REASON_IDS)
async def test_every_surface_is_refused_for_every_reason(quarantined, surface, reason):
    tool_name, _kind, call = surface
    label, marker, message = reason
    _quarantine(label)

    result = await call()

    assert result == message, f"{tool_name} under {label}: {result[:200]!r}"
    assert len(quarantined) == 1, f"{tool_name} refusal was not logged"
    row = quarantined[0]
    assert row["tool"] == tool_name
    assert row["params"]["error"] == marker


@pytest.mark.parametrize("reason", REASONS, ids=REASON_IDS)
async def test_the_write_tool_is_refused_before_a_path_is_resolved(
    quarantined, tmp_path, reason, monkeypatch
):
    """`edit_note` must not reach the disk at all.

    The whole reason the quarantine is a *tool* refusal and not merely an index
    refusal is that the write path never consults the indexer: the inner
    tenant's files really are beneath the outer tenant's root, and every
    beneath-root check agrees they are contained.
    """
    label, _marker, message = reason
    _quarantine(label)

    def _no_open(*args, **kwargs):
        raise AssertionError("the refused write tool touched the filesystem")

    monkeypatch.setattr(tools, "open_mutable", _no_open)
    monkeypatch.setattr(tools, "validate_visible_path", _no_open)

    assert await tools.edit_note_impl(NOTE_PATH, "clobber") == message


# ── (b) the wording names no other tenant ───────────────────────────────────


@pytest.mark.parametrize("reason", REASONS, ids=REASON_IDS)
def test_the_message_names_no_other_tenant(reason):
    """Owner decision 9: the tool-facing refusal names no other user or path.

    The snapshot carries the peer's username and canonical assignment — the
    panel, the log line and the `indexer_runs` row render them — and none of it
    may reach the agent on the other side of the collision.
    """
    _label, _marker, message = reason
    for forbidden in (
        PEER_USERNAME,
        PEER_ASSIGNMENT,
        SUBJECT_ASSIGNMENT,
        NOTE_PATH,
        "/vaults/",
        str(PEER_UID),
    ):
        assert forbidden not in message, f"{forbidden!r} leaked into {message!r}"
    # Nor a bare absolute path of any shape.
    assert not re.search(r"(?<![\w/])/[A-Za-z0-9_.-]+/", message), message


@pytest.mark.parametrize("reason", REASONS, ids=REASON_IDS)
async def test_the_refusal_the_caller_receives_names_no_other_tenant(
    quarantined, reason
):
    """The same property asserted through the wire, not only over the constant.

    The message is read from `_QUARANTINE_REFUSALS` rather than from the
    exception instance, so a future `raise VaultRootOverlap(f"...{peer}")` for
    a richer operator log cannot push a peer's name out to an agent. This is
    the test that would catch it if the table were bypassed.
    """
    label, _marker, _message = reason
    _quarantine(label)
    result = await tools.get_backlinks_impl(NOTE_PATH)
    for forbidden in (PEER_USERNAME, PEER_ASSIGNMENT, NOTE_PATH):
        assert forbidden not in result


async def test_the_message_is_read_from_the_table_not_the_exception(
    quarantined, monkeypatch
):
    """A raiser that puts the peer in the exception must not reach the agent."""

    def _leaky(_uid=None):
        raise vault.VaultRootOverlap(
            f"root {SUBJECT_ASSIGNMENT} overlaps {PEER_USERNAME}'s "
            f"{PEER_ASSIGNMENT}"
        )

    monkeypatch.setattr(tools, "_vault_root", _leaky)
    result = await tools.list_notes_impl()

    assert result == tools._QUARANTINE_REFUSALS[vault.VaultRootOverlap][0]
    assert result.startswith(vault.VAULT_ROOT_OVERLAP_ERROR)
    assert PEER_USERNAME not in result
    assert PEER_ASSIGNMENT not in result


# ── (c) the markers ─────────────────────────────────────────────────────────


def test_the_four_pre_body_gate_markers_are_distinct():
    """An operator acts differently on each: an assignment corrected, a mount
    restored, a detection that is failing, an administrator who unassigned a
    user. Sharing a value collapses two of those investigations into one."""
    assert (
        len(
            {
                tools._NO_VAULT_MARKER,
                tools._VAULT_ROOT_OVERLAP_MARKER,
                tools._VAULT_ROOT_UNEXAMINABLE_MARKER,
                tools._VAULT_ROOT_NOT_READY_MARKER,
            }
        )
        == 4
    )


async def test_the_three_reasons_write_three_different_rows(quarantined):
    """The spec scenario: one call refused for an overlap and another for a root
    that could not be examined carry different markers, and both differ from the
    no-assignment marker."""
    seen = []
    for label, marker, _message in REASONS:
        _quarantine(label)
        await tools.list_notes_impl()
        seen.append(quarantined[-1]["params"]["error"])
        assert seen[-1] == marker
    assert len(set(seen)) == 3
    assert tools._NO_VAULT_MARKER not in seen


def test_the_quarantine_types_are_caught_ahead_of_the_generic_branch():
    """Structural, because the ordering is the whole of the classification.

    All three subclass `RuntimeError` — deliberately, so every existing
    `except RuntimeError` around the gate keeps failing closed — which means a
    generic branch placed first would swallow them and file every quarantine
    under `no_vault_assigned` with nothing visibly broken.
    """
    import inspect

    source = inspect.getsource(tools._vault_admission_error)
    typed = source.index("VaultRootOverlap")
    generic = source.index("except RuntimeError")
    assert typed < generic, "the generic RuntimeError branch shadows the typed ones"
    for name in ("VaultRootUnexaminable", "VaultRootNotReady"):
        assert source.index(name) < generic


# ── (d) the pre-body predicate ──────────────────────────────────────────────


def test_every_new_marker_is_enumerated_by_the_predicate():
    """A pre-body marker the predicate does not enumerate is wrong in both
    directions at once: the refusal's near-zero `duration_ms` is folded into the
    tool's latency percentiles as though the body had executed, and the refusal
    is never counted. A gate that starts refusing a whole tenant is exactly the
    traffic an operator opens `/admin/performance` to understand."""
    for marker in (
        tools._VAULT_ROOT_OVERLAP_MARKER,
        tools._VAULT_ROOT_UNEXAMINABLE_MARKER,
        tools._VAULT_ROOT_NOT_READY_MARKER,
    ):
        assert marker in usage_stats.PRE_BODY_REFUSAL_ERROR_MARKERS
        assert marker in usage_stats.PRE_BODY_REFUSAL_BINDS.values()


def test_the_enumeration_and_the_bind_set_agree_in_both_directions():
    """The spec scenario, and the test that fails when a marker is added to one
    list and not the other. The binds are derived from the enumeration today —
    which is the right shape — so this pins that they stay derived rather than
    becoming two hand-maintained lists."""
    assert set(usage_stats.PRE_BODY_REFUSAL_BINDS.values()) == set(
        usage_stats.PRE_BODY_REFUSAL_ERROR_MARKERS
    )
    assert len(usage_stats.PRE_BODY_REFUSAL_BINDS) == len(
        usage_stats.PRE_BODY_REFUSAL_ERROR_MARKERS
    )


def test_every_new_marker_is_bound_into_the_sql_fragment():
    """The values travel as bind parameters, never as interpolated literals, and
    the fragment the aggregates and the refusal count share is one expression:
    excluded from the percentiles and counted as a refusal are the same fact."""
    fragment = usage_stats.pre_body_refusal_sql()
    for name, value in usage_stats.PRE_BODY_REFUSAL_BINDS.items():
        assert f":{name}" in fragment
        assert value not in fragment
    assert usage_stats.executed_sql() == f"(NOT {fragment})"


def test_the_marker_mirror_has_not_drifted_from_the_writer():
    """`usage_stats` mirrors the marker strings rather than importing them —
    `tools.py` imports `usage_stats` for `OVER_QUOTA_PARAM`, and an import the
    other way closes the cycle. A mirror needs a pin."""
    assert usage_stats.VAULT_ROOT_OVERLAP_MARKER == tools._VAULT_ROOT_OVERLAP_MARKER
    assert (
        usage_stats.VAULT_ROOT_UNEXAMINABLE_MARKER
        == tools._VAULT_ROOT_UNEXAMINABLE_MARKER
    )
    assert usage_stats.VAULT_ROOT_NOT_READY_MARKER == tools._VAULT_ROOT_NOT_READY_MARKER


# ── (e) the security event ──────────────────────────────────────────────────


@pytest.mark.parametrize("reason", REASONS, ids=REASON_IDS)
async def test_the_refusal_is_emitted_through_the_suppressor(
    quarantined, sink, reason
):
    """Same permit discipline as `tool_refused_no_vault`.

    That event used to be a bare `logger.warning`, which reached the sink
    whatever the suppressor said — an unbounded flood channel beside the bounded
    one. A quarantine refuses *every* call from an account until an operator
    acts, so it is the flood shape by construction.
    """
    label, _marker, _message = reason
    _quarantine(label)

    calls = security_events.MAX_EVENTS_PER_WINDOW + 2
    for _ in range(calls):
        await tools.list_notes_impl()

    records = [
        r
        for r in sink.records
        if r.getMessage() == "tool_refused_vault_quarantined"
    ]
    assert len(records) == security_events.MAX_EVENTS_PER_WINDOW
    payload = build_payload(records[0])
    assert payload["tool"] == "list_notes"
    assert payload["user_id"] == UID
    assert payload["reason"] == label
    assert sink.bare == [], "nothing may reach the module logger directly"

    # The rows are not rate limited — the log sink is.
    assert len(quarantined) == calls

    security_events.flush_suppression_summaries()
    summary = [r for r in sink.records if r.getMessage() == "events_suppressed"][0]
    assert build_payload(summary)["count"] == 2


@pytest.mark.parametrize("reason", REASONS, ids=REASON_IDS)
async def test_the_event_names_no_other_tenant(quarantined, sink, reason):
    """`reason` is a closed vocabulary, not a peer's name or a path."""
    label, _marker, _message = reason
    _quarantine(label)
    await tools.list_notes_impl()

    payload = build_payload(
        [r for r in sink.records if r.getMessage() == "tool_refused_vault_quarantined"][0]
    )
    rendered = repr(payload)
    assert PEER_USERNAME not in rendered
    assert PEER_ASSIGNMENT not in rendered
    assert SUBJECT_ASSIGNMENT not in rendered


async def test_a_quarantine_is_not_recorded_as_a_missing_assignment(
    quarantined, sink
):
    """The no-assignment event says an administrator unassigned the user. This
    account is assigned, active and indexed; recording it under that event would
    send an operator to the users page to fix something that is already right."""
    _quarantine("overlap")
    await tools.list_notes_impl()

    assert [r for r in sink.records if r.getMessage() == "tool_refused_no_vault"] == []


# ── (f) what the quarantine must NOT change ─────────────────────────────────


def test_a_caller_the_snapshot_does_not_name_is_admitted(tmp_path, monkeypatch):
    """Unrelated tenants are untouched: the guard fails closed for the pair, not
    for the deployment."""
    from src.services import vault as vault_service

    monkeypatch.setattr(vault_service, "_user_vault_cache", {UID: tmp_path})
    _quarantine("overlap")  # names UID, not the caller below

    other = 99
    uid = current_user_id.set(other)
    root = current_vault_root.set((other, tmp_path))
    try:
        assert tools._vault_admission_error() is None
    finally:
        current_vault_root.reset(root)
        current_user_id.reset(uid)


def test_single_user_mode_never_consults_the_snapshot(tmp_path, monkeypatch):
    """`user_id is None` has one root and no second assignment, so there is
    nothing to detect and nothing to be ready for — including in the
    never-published state, which would otherwise refuse the whole server."""
    from src.services import vault as vault_service

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(vault_service.settings, "multi_user_mode", False)
    vault_overlap.reset_snapshot_state()

    uid = current_user_id.set(None)
    try:
        assert tools._vault_admission_error() is None
    finally:
        current_user_id.reset(uid)


@pytest.mark.parametrize("reason", REASONS, ids=REASON_IDS)
async def test_the_refusal_opens_no_database_session(quarantined, reason):
    """So there is nothing it could have deleted.

    The index rows are deliberately preserved — a corrected assignment must not
    cost a full re-embed, and a blanket delete would be a second, unreviewed
    deletion path over index contents. The `quarantined` fixture makes
    `tools.async_session` raise, so this is enforced rather than asserted after
    the fact.
    """
    label, _marker, message = reason
    _quarantine(label)
    assert await tools.list_notes_impl() == message


def test_a_plain_string_refusal_still_files_as_no_vault_assigned():
    """The historical default. `_tracked` reads the marker off the refusal, and
    a refusal with no marker is the no-assignment one — which is what keeps a
    monkeypatched gate returning a bare message behaving as it always did."""
    assert getattr(tools._NO_VAULT_MESSAGE, "marker", tools._NO_VAULT_MARKER) == (
        tools._NO_VAULT_MARKER
    )
    marked = tools._MarkedRefusal("refused", tools._VAULT_ROOT_OVERLAP_MARKER)
    assert marked == "refused"
    assert isinstance(marked, str)
    assert marked.marker == tools._VAULT_ROOT_OVERLAP_MARKER
