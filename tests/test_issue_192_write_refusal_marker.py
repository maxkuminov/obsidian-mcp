"""#192 — a write refused for permission is distinguishable from a write.

The defect: `_require_write` returned a string, and the `usage_logs` row written
for that call was shaped **exactly like a successful write** — same tool, same
actor, an `error`-free `params`. `/admin/usage` therefore showed a read-only
credential apparently writing, and nothing anywhere recorded the refusal.

Both halves of the fix live at `_require_write`'s single definition (design D4),
so all **nine** gated call sites inherit them without being touched. A
per-caller marker would be eight chances to forget one and a tenth tool that
silently has none — which is why the parametrisation below is the whole gated
set and not a sample of it.

Also pinned here, because they are the same property from the other side:

* `request_download` asks `_mint_preflight` for `need_write=False`, never
  reaches the gate, and is deliberately unmarked;
* the marker is **post-body** and must never enter the pre-body refusal
  predicate (design D6, residual R5);
* the row is written whether or not the *record* was; and
* the migrated admission-gate events pass the suppressor rather than reaching
  the sink through a direct `logger.warning`, so a flood is bounded and the
  withheld count is stated.
"""
import datetime as _dt
import logging
from types import SimpleNamespace

import pytest

import src.mcp_server.tools as tools
from src.auth.session import current_actor, current_user_id, current_vault_root
from src.logging_setup import build_payload
from src.mcp_server.auth import current_api_key_id, current_permission
from src.services import security_events, usage_stats

UID = 7
ACTOR = ("api_key", "nightly sync", "omcp_a1b2c3")
KEY_ID = 3


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def sink():
    """Records reaching the security-event logger, and *only* that logger.

    A second handler watches `src.mcp_server.tools`'s own module logger: the
    migrated events must pass the suppressor, and a direct `logger.warning`
    beside the bounded channel is an unbounded flood channel (design D18).
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
def read_only(tmp_path, monkeypatch):
    """A read-only credential with a real, resolvable vault root.

    The vault gate has to *pass* for these cases: `permission_denied` is a
    post-body marker precisely because `_require_write` is reached from inside a
    body that already cleared admission, and a test that refused earlier would
    be pinning a different refusal.

    Returns the list of `usage_logs` rows `_tracked` asked for.
    """
    from src.services import vault as vault_service

    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))

    written: list[dict] = []

    async def capture(tool, params, duration_ms, response_size):
        written.append({"tool": tool, "params": params, "duration_ms": duration_ms})
        return True

    monkeypatch.setattr(tools, "_log_usage", capture)

    perm = current_permission.set("read")
    uid = current_user_id.set(UID)
    root = current_vault_root.set((UID, tmp_path))
    actor = current_actor.set(ACTOR)
    key = current_api_key_id.set(KEY_ID)
    try:
        yield written
    finally:
        current_api_key_id.reset(key)
        current_actor.reset(actor)
        current_vault_root.reset(root)
        current_user_id.reset(uid)
        current_permission.reset(perm)


def _events(records, name):
    return [r for r in records if r.getMessage() == name]


#: Every call site the write gate guards, with a call that reaches it. Seven
#: tools call `_require_write` as their first statement; `request_upload` and
#: `import_from_url` reach it through `_mint_preflight(need_write=True)`.
GATED = [
    ("create_note", lambda: tools.create_note_impl("a.md", "body")),
    ("edit_note", lambda: tools.edit_note_impl("a.md", "body")),
    ("move_note", lambda: tools.move_note_impl("a.md", "b.md")),
    ("delete_note", lambda: tools.delete_note_impl("a.md")),
    ("set_frontmatter", lambda: tools.set_frontmatter_impl("a.md", {"k": "v"})),
    ("write_file", lambda: tools.write_file_impl("a.bin", "AAAA")),
    ("delete_file", lambda: tools.delete_file_impl("a.bin")),
    ("request_upload", lambda: tools.request_upload_impl("a.bin")),
    (
        "import_from_url",
        lambda: tools.import_from_url_impl("https://example.com/x", "a.bin"),
    ),
]

REFUSAL = (
    "Permission denied: this credential has read-only access. Write "
    "permission is required — a 'readwrite' API key, or an OAuth token "
    "carrying the 'readwrite' scope."
)


# ══════════════════════════════════════════════════════════════════════════
# 1. All nine call sites, marked and recorded at one definition
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("tool_name,call", GATED, ids=[n for n, _ in GATED])
async def test_every_gated_tool_marks_its_row_and_records_one_refusal(
    tool_name, call, sink, read_only
):
    with security_events.suppression_disabled():
        result = await call()

    assert result == REFUSAL, "the in-band message is the caller's contract"

    assert len(read_only) == 1
    row = read_only[0]
    assert row["tool"] == tool_name
    assert row["params"]["error"] == tools._PERMISSION_DENIED_MARKER

    records = _events(sink.records, "tool_write_refused")
    assert len(records) == 1, "exactly one record per refusal, never two"
    record = records[0]
    assert record.levelno == logging.WARNING

    payload = build_payload(record)
    assert payload["tool"] == tool_name
    assert payload["user_id"] == UID
    # The *credential*, not the peer: `_require_write` runs below
    # `ProxyHeadersMiddleware` and nothing binds the address there (residual R8).
    assert payload["actor_kind"] == ACTOR[0]
    assert payload["key_id"] == KEY_ID
    assert "client_ip" not in payload
    # **Never `actor_ref`** (design D20). For an API-key caller that value is
    # `api_keys.key_prefix` — the first twelve characters of the live key — and
    # this record goes to a shared log sink. The credential is named by row id;
    # `usage_logs` keeps the prefix, and is read behind the panel's own auth.
    assert "actor_ref" not in payload
    assert ACTOR[2] not in repr(payload)


async def test_the_refusal_message_is_unchanged(sink, read_only):
    """The reader is an agent that has to act on this string. Recording the
    refusal must not change a byte of what the caller is told."""
    with security_events.suppression_disabled():
        result = await tools.create_note_impl("a.md", "body")
    assert result == REFUSAL
    assert result.startswith("Permission denied: ")


async def test_request_download_is_neither_marked_nor_recorded(sink, read_only):
    """`_mint_preflight(need_write=False)` never reaches the gate. A read-only
    credential minting a download link is doing exactly what it may do, and a
    refusal record for it would be a false positive in the one channel an
    operator is meant to trust."""
    with security_events.suppression_disabled():
        result = await tools.request_download_impl("missing.bin")

    assert result != REFUSAL
    assert len(read_only) == 1
    assert read_only[0]["tool"] == "request_download"
    assert "error" not in read_only[0]["params"]
    assert _events(sink.records, "tool_write_refused") == []


@tools._tracked("probe_write", [])
async def probe_write() -> str:
    """A gated tool's first statement, and nothing else.

    The nine real tools carry a filesystem and a database behind the gate; what
    the control below needs is only the gate itself.
    """
    if err := tools._require_write():
        return err
    return "wrote"


async def test_a_readwrite_credential_is_neither_marked_nor_recorded(
    sink, read_only
):
    """The control. Without it a gate that refused *everything* would pass every
    case above."""
    perm = current_permission.set("readwrite")
    try:
        with security_events.suppression_disabled():
            result = await probe_write()
    finally:
        current_permission.reset(perm)

    assert result == "wrote"
    assert "error" not in read_only[0]["params"]
    assert _events(sink.records, "tool_write_refused") == []


async def test_the_probe_refuses_like_the_nine(sink, read_only):
    """And the control's own control: the probe is only evidence that the gate
    passes for a readwrite credential if it refuses for a read-only one."""
    with security_events.suppression_disabled():
        assert await probe_write() == REFUSAL
    assert read_only[0]["params"]["error"] == tools._PERMISSION_DENIED_MARKER
    assert len(_events(sink.records, "tool_write_refused")) == 1


# ══════════════════════════════════════════════════════════════════════════
# 2. The marker's classification
# ══════════════════════════════════════════════════════════════════════════


def test_the_marker_is_post_body_and_stays_out_of_the_predicate():
    """`_require_write` is called from *inside* a body that has already passed
    the vault gate, the argument screen and the quota gate — and has already
    spent its quota slot. Enumerating it as a pre-body refusal would file a call
    that ran three gates under "the body never started", and would move its rows
    from the percentiles into the refusal count.

    The accepted cost of leaving it out is residual R5: a read-only credential
    probing `create_note` dilutes that tool's percentiles. The refusal is made
    *visible* on `/admin/usage` instead.
    """
    assert tools._PERMISSION_DENIED_MARKER == "permission_denied"
    assert (
        tools._PERMISSION_DENIED_MARKER
        not in usage_stats.PRE_BODY_REFUSAL_ERROR_MARKERS
    )
    assert (
        tools._PERMISSION_DENIED_MARKER
        not in usage_stats.PRE_BODY_REFUSAL_BINDS.values()
    )
    assert tools._PERMISSION_DENIED_MARKER not in usage_stats.pre_body_refusal_sql()
    # And it does not collide with a marker on the other side of the line: two
    # branches on opposite sides of the body/no-body line may never share a
    # value (`vault_anchor_lost_at_publish` exists because that rule was broken).
    assert tools._PERMISSION_DENIED_MARKER != tools._NO_VAULT_MARKER
    assert tools._PERMISSION_DENIED_MARKER != tools._TOOL_EXCEPTION_MARKER


# ══════════════════════════════════════════════════════════════════════════
# 3. The row is never suppressed; the record is bounded and accounted
# ══════════════════════════════════════════════════════════════════════════


async def test_every_refusal_writes_its_row_even_when_records_are_withheld(
    sink, read_only
):
    """The suppressor bounds what reaches the **log sink** and nothing else. A
    refused call always writes its `usage_logs` row: the audit trail is not a
    rate-limited channel, and an operator reading `/admin/usage` after a
    credential-stuffing burst must see every call that was made."""
    calls = security_events.MAX_EVENTS_PER_WINDOW + 2
    for _ in range(calls):
        await tools.create_note_impl("a.md", "body")

    assert len(read_only) == calls
    assert all(
        row["params"]["error"] == tools._PERMISSION_DENIED_MARKER
        for row in read_only
    )

    records = _events(sink.records, "tool_write_refused")
    assert len(records) == security_events.MAX_EVENTS_PER_WINDOW

    # Nothing is dropped silently: the withheld count is stated exactly.
    security_events.flush_suppression_summaries()
    summaries = _events(sink.records, "events_suppressed")
    assert len(summaries) == 1
    payload = build_payload(summaries[0])
    assert payload["reason"] == "tool_write_refused"
    assert payload["count"] == calls - security_events.MAX_EVENTS_PER_WINDOW
    assert payload["window_seconds"] == security_events.WINDOW_SECONDS


async def test_a_no_vault_flood_is_bounded_and_passes_the_suppressor(
    sink, read_only, monkeypatch
):
    """`tool_refused_no_vault` used to be a bare `logger.warning`, which reached
    the sink whatever the suppressor said — an unbounded flood channel beside the
    bounded one (design D18, round 3's M2)."""
    root = current_vault_root.set((UID, None))
    try:
        calls = security_events.MAX_EVENTS_PER_WINDOW + 3
        for _ in range(calls):
            result = await tools.list_notes_impl()
    finally:
        current_vault_root.reset(root)

    assert "no vault is assigned" in result
    assert len(read_only) == calls, "the rows are not rate limited"
    assert all(
        row["params"]["error"] == tools._NO_VAULT_MARKER for row in read_only
    )

    records = _events(sink.records, "tool_refused_no_vault")
    assert len(records) == security_events.MAX_EVENTS_PER_WINDOW
    assert build_payload(records[0])["tool"] == "list_notes"
    assert sink.bare == [], "nothing may reach the module logger directly"

    security_events.flush_suppression_summaries()
    summary = _events(sink.records, "events_suppressed")[0]
    assert build_payload(summary)["count"] == 3


async def test_an_over_quota_flood_is_bounded_and_passes_the_suppressor(
    sink, read_only, monkeypatch
):
    day = _dt.datetime.now(_dt.timezone.utc).date()

    async def refuse(_key_id, _limit):
        return SimpleNamespace(
            admitted=False,
            day=day,
            reset_at=_dt.datetime.now(_dt.timezone.utc),
        )

    monkeypatch.setattr(tools, "_admit_quota", refuse)
    from src.mcp_server import auth as mcp_auth

    limit = mcp_auth.current_daily_request_limit.set(5)
    try:
        calls = security_events.MAX_EVENTS_PER_WINDOW + 1
        for _ in range(calls):
            await tools.list_notes_impl()
    finally:
        mcp_auth.current_daily_request_limit.reset(limit)

    assert len(read_only) == calls
    assert all(row["params"][tools._OVER_QUOTA_MARKER] is True for row in read_only)

    records = _events(sink.records, "tool_refused_over_quota")
    assert len(records) == security_events.MAX_EVENTS_PER_WINDOW
    payload = build_payload(records[0])
    assert payload["tool"] == "list_notes"
    assert payload["key_id"] == KEY_ID
    assert payload["limit"] == 5
    assert payload["day"] == day.isoformat()
    assert sink.bare == [], "nothing may reach the module logger directly"


async def test_one_subject_cannot_multiply_its_allowance_across_the_two_events(
    sink, read_only
):
    """The per-subject cap is what stops a source cycling through refusal kinds
    to buy a fresh bucket for each. The two events share one subject here — the
    resolved user — because a tool call always has one."""
    subjects = {
        security_events.subject_for(user_id=UID),
    }
    assert subjects == {f"user:{UID}"}
    for _ in range(3):
        await tools.create_note_impl("a.md", "body")
    records = _events(sink.records, "tool_write_refused")
    assert {r.user_id for r in records} == {UID}
