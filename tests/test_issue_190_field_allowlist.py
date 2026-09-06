"""#190 — the field allow-list, its three name spaces, and the AST sweep.

`extra=` is attacker-influenced: one of the ten `auth_failure` sites derives its
value from a *presented bearer token*. A formatter that serialised
`record.__dict__` would ship key prefixes, vault paths and query strings into a
shared sink the moment somebody added a field, so the policy is an allow-list
with declared types and bounds — and a sweep that fails the build when a call
site invents a name.
"""
import ast
import json
import logging
import pathlib
import re

import pytest
from starlette.requests import Request

from src.logging_setup import (
    ALLOWED_FIELDS,
    EMITTER_CONTROL,
    FORMATTER_OWNED,
    StructuredFormatter,
)
from src.services import security_events

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
CATALOGUE_DOC = ROOT / "docs" / "architecture" / "security-event-logging.md"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured():
    """Records emitted by `security_events`, with the suppressor out of the way."""
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate = logger.propagate
    level = logger.level
    logger.propagate = False
    # INFO is part of the contract, and the root's default WARNING would filter
    # it before any handler saw it.
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


def _payload(record):
    return json.loads(StructuredFormatter().format(record))


# ── Dropping ────────────────────────────────────────────────────────────────


def test_an_unknown_field_is_dropped_and_the_rest_survives(captured):
    security_events.emit(
        "auth_failure", subject="ip:198.51.100.7", reason="invalid_key", vault_path="/x"
    )
    payload = _payload(captured[0])
    assert "vault_path" not in payload
    assert payload["reason"] == "invalid_key"
    assert payload["msg"] == "auth_failure"


def test_a_field_the_event_does_not_declare_is_dropped(captured):
    # `grant_id` is allow-listed, but `auth_failure` does not declare it.
    security_events.emit(
        "auth_failure", subject="-", reason="invalid_key", grant_id="g-1"
    )
    assert "grant_id" not in _payload(captured[0])


def test_a_field_the_event_does_not_declare_raises_under_strict(captured):
    with security_events.strict_fields():
        with pytest.raises(security_events.SecurityEventFieldError):
            security_events.emit(
                "auth_failure", subject="-", reason="invalid_key", grant_id="g-1"
            )


def test_an_unregistered_event_raises_under_strict(captured):
    with security_events.strict_fields():
        with pytest.raises(security_events.SecurityEventFieldError):
            security_events.emit("not_in_the_catalogue", subject="-", reason="x")


def test_a_mistyped_value_is_dropped_and_never_converted(captured):
    security_events.emit(
        "auth_failure", subject="-", reason="invalid_key", key_id="not-an-int"
    )
    payload = _payload(captured[0])
    assert "key_id" not in payload
    assert payload["reason"] == "invalid_key"


def test_a_bool_is_not_an_int(captured):
    """`isinstance(True, int)` is True in Python; an `int` field holding `True`
    is exactly the silent type change the drop rule exists to prevent."""
    record = logging.LogRecord("probe", logging.WARNING, __file__, 1, "e", None, None)
    record.user_id = True
    assert "user_id" not in _payload(record)


def test_the_formatter_does_not_raise_on_a_mistyped_value():
    record = logging.LogRecord("probe", logging.WARNING, __file__, 1, "e", None, None)
    record.duration_ms = object()
    assert _payload(record)["msg"] == "e"


def test_a_none_value_is_absent_rather_than_null(captured):
    security_events.emit("auth_failure", subject="-", reason="invalid_key", key_id=None)
    assert "key_id" not in _payload(captured[0])


def test_strings_are_truncated_to_their_declared_bound():
    record = logging.LogRecord("probe", logging.WARNING, __file__, 1, "e", None, None)
    record.reason = "r" * 500
    record.route = "/" + "p" * 500
    payload = _payload(record)
    assert len(payload["reason"]) == ALLOWED_FIELDS["reason"].max_len
    assert len(payload["route"]) == ALLOWED_FIELDS["route"].max_len


def test_key_prefix_is_not_allow_listed():
    """Deliberate: the name invites logging a raw `omcp_` prefix of a presented
    token, and a dropped field is a safer failure than a shipped credential."""
    assert "key_prefix" not in ALLOWED_FIELDS


# ── The redaction ───────────────────────────────────────────────────────────


def test_the_token_tag_is_stable_and_shaped():
    tag = security_events.redacted_token_tag("omcp_" + "a" * 40)
    assert tag == security_events.redacted_token_tag("omcp_" + "a" * 40)
    assert re.fullmatch(r"sha:[0-9a-f]{8}", tag)


def test_no_record_contains_a_long_substring_of_the_token(captured):
    token = "omcp_" + "Zq7" * 14
    security_events.emit(
        "auth_failure",
        subject="-",
        reason="invalid_key",
        token_tag=security_events.redacted_token_tag(token),
    )
    line = StructuredFormatter().format(captured[0])
    for start in range(len(token) - 12 + 1):
        assert token[start : start + 12] not in line


def test_no_tag_is_invented_when_nothing_was_presented(captured):
    assert security_events.redacted_token_tag(None) is None
    assert security_events.redacted_token_tag("") is None
    security_events.emit(
        "auth_failure",
        subject="-",
        reason="invalid_key",
        token_tag=security_events.redacted_token_tag(None),
    )
    assert "token_tag" not in _payload(captured[0])


def test_the_auth_middleware_uses_the_one_definition():
    from src.mcp_server import auth

    assert auth._redacted_prefix("abc") == security_events.redacted_token_tag("abc")


# ── The client identity ─────────────────────────────────────────────────────


def _request(peer: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/mcp",
            "headers": headers or [],
            "client": (peer, 4444),
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
        }
    )


def test_client_ip_ignores_a_forged_forwarded_header():
    """`ProxyHeadersMiddleware` rewrites `scope["client"]` only for peers inside
    the trusted ranges. Reading the header here would accept a forgery from
    anyone, which is the precise thing that middleware exists to prevent."""
    request = _request("203.0.113.9", [(b"x-forwarded-for", b"10.0.0.1")])
    assert security_events.client_ip(request) == "203.0.113.9"


def test_client_ip_is_none_without_a_peer():
    scope = dict(_request("203.0.113.9").scope)
    scope["client"] = None
    assert security_events.client_ip(Request(scope)) is None


def test_the_subject_prefers_a_resolved_user_over_the_address():
    request = _request("203.0.113.9")
    assert security_events.subject_for(user_id=7, request=request) == "user:7"
    assert security_events.subject_for(request=request) == "ip:203.0.113.9"
    assert security_events.subject_for() == "-"


# ── The three name spaces ───────────────────────────────────────────────────


def test_the_field_names_are_disjoint_from_the_names_the_call_site_may_not_pass():
    assert ALLOWED_FIELDS.keys() & FORMATTER_OWNED == set()
    assert ALLOWED_FIELDS.keys() & EMITTER_CONTROL == set()
    # `level` is deliberately in both of the other two: it is a control keyword
    # the formatter renders from the record, and never a field (design D18).
    assert FORMATTER_OWNED & EMITTER_CONTROL == {"level"}


def test_a_formatter_owned_name_cannot_be_forged_by_a_call_site():
    record = logging.LogRecord("probe", logging.WARNING, __file__, 1, "real", None, None)
    record.ts = "1999-01-01T00:00:00.000Z"
    record.logger = "someone-elses-logger"
    record.stack = "not a traceback"
    payload = _payload(record)
    assert payload["ts"] != "1999-01-01T00:00:00.000Z"
    assert payload["logger"] == "probe"
    assert payload["msg"] == "real"
    assert "stack" not in payload


def test_passing_a_formatter_owned_name_raises_under_strict(captured):
    with security_events.strict_fields():
        with pytest.raises(security_events.SecurityEventFieldError):
            security_events.emit("auth_failure", subject="-", msg="forged")


def test_the_exception_wins_over_a_passed_error_type():
    try:
        raise ValueError("nope")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "probe", logging.ERROR, __file__, 1, "e", None, sys.exc_info()
        )
    record.error_type = "AttackerType"
    assert _payload(record)["error_type"] == "ValueError"


def test_a_passed_error_type_survives_without_exc_info():
    record = logging.LogRecord("probe", logging.WARNING, __file__, 1, "e", None, None)
    record.error_type = "OperationalError"
    assert _payload(record)["error_type"] == "OperationalError"


# ── The registry ────────────────────────────────────────────────────────────


def test_every_event_declares_only_allow_listed_fields():
    for event, fields in security_events.EVENT_FIELDS.items():
        unknown = set(fields) - ALLOWED_FIELDS.keys()
        assert not unknown, f"{event} declares {sorted(unknown)}"


def test_no_event_declares_a_formatter_owned_or_control_name():
    for event, fields in security_events.EVENT_FIELDS.items():
        assert not set(fields) & FORMATTER_OWNED, event
        assert not set(fields) & (EMITTER_CONTROL - {"level"}), event


def test_every_catalogue_row_in_the_architecture_note_has_an_entry():
    """The note's table is the registry's prose half; they may not drift."""
    text = CATALOGUE_DOC.read_text()
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", text, flags=re.M))
    assert documented, "the catalogue table was not found in the architecture note"
    missing = documented - security_events.EVENT_FIELDS.keys()
    assert not missing, f"documented but unregistered: {sorted(missing)}"
    undocumented = security_events.EVENT_FIELDS.keys() - documented
    assert not undocumented, f"registered but undocumented: {sorted(undocumented)}"


# ── The AST sweep (design D14) ──────────────────────────────────────────────
#
# A regex over `extra={...}` would miss `security_events.emit(...)` keywords
# entirely and would break on any reformatting, so the sweep parses the tree.
#
# `src/services/security_events.py` is the one exempt module: it is the emitter,
# its single `logger.log(..., extra=fields)` passes a dict this module has
# already policed at runtime, and the rest of this file is the check on it.

_SWEEP_EXEMPT = {SRC / "services" / "security_events.py"}
_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}

# Empty, and meant to stay that way. It held one dated entry: the #182
# refresh-reuse alarm logged `event`, `error` and `revoked_tokens` through
# `extra=` from `src/oauth/routes.py`, none of them allow-listed — `event`
# duplicated `msg` and `error` was the bare-string field the allow-list
# deliberately does not have. Slice B (#191) moved both call sites onto
# `security_events.emit` as `oauth_refresh_reuse_detected` and
# `oauth_refresh_reuse_revocation_failed`, so the gap is closed and the entry
# is gone. A new one is a debt, not a fix: add the field to `ALLOWED_FIELDS`.
_KNOWN_GAPS: dict[str, set[str]] = {}


def _is_logger_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
        return False
    base = func.value
    name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
    return "log" in name.lower()


def _is_emit_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "emit" and isinstance(func.value, ast.Name) and (
            func.value.id == "security_events"
        )
    return isinstance(func, ast.Name) and func.id == "emit"


def _sweep() -> list[str]:
    problems: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path in _SWEEP_EXEMPT:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            event = None
            names: list[str] = []
            if _is_logger_call(node):
                extra = next(
                    (kw.value for kw in node.keywords if kw.arg == "extra"), None
                )
                if extra is None:
                    continue
                if not isinstance(extra, ast.Dict):
                    problems.append(
                        f"{rel}:{node.lineno}: extra= is not a literal dict, so the "
                        "allow-list cannot police it"
                    )
                    continue
                for key in extra.keys:
                    if not isinstance(key, ast.Constant) or not isinstance(
                        key.value, str
                    ):
                        problems.append(
                            f"{rel}:{node.lineno}: extra= expands dynamically"
                        )
                        continue
                    names.append(key.value)
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    event = first.value
            elif _is_emit_call(node):
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    event = first.value
                for keyword in node.keywords:
                    if keyword.arg is None:
                        problems.append(
                            f"{rel}:{node.lineno}: emit(**fields) expands dynamically"
                        )
                        continue
                    if keyword.arg in EMITTER_CONTROL:
                        continue
                    names.append(keyword.arg)
            else:
                continue

            for name in names:
                if name in _KNOWN_GAPS.get(str(rel), ()):
                    continue
                if name in FORMATTER_OWNED:
                    problems.append(
                        f"{rel}:{node.lineno}: {name!r} is formatter-owned"
                    )
                elif name not in ALLOWED_FIELDS:
                    problems.append(
                        f"{rel}:{node.lineno}: {name!r} is not allow-listed"
                    )
                elif (
                    event in security_events.EVENT_FIELDS
                    and name not in security_events.EVENT_FIELDS[event]
                ):
                    problems.append(
                        f"{rel}:{node.lineno}: {event!r} does not declare {name!r}"
                    )
    return problems


def test_no_call_site_passes_a_field_the_allow_list_does_not_know():
    assert _sweep() == []


def test_the_sweep_catches_an_unknown_key(tmp_path, monkeypatch):
    offender = tmp_path / "offender.py"
    offender.write_text('logger.warning("e", extra={"vault_path": p})\n')
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    problems = _sweep()
    assert len(problems) == 1 and "not allow-listed" in problems[0]


def test_the_sweep_rejects_a_dynamic_field_set(tmp_path, monkeypatch):
    offender = tmp_path / "offender.py"
    offender.write_text(
        "logger.warning('e', extra=fields)\n"
        "logger.warning('e', extra={**base, 'reason': 'x'})\n"
        "security_events.emit('auth_failure', **fields)\n"
    )
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    problems = _sweep()
    assert len(problems) == 3
    assert any("not a literal dict" in p for p in problems)
    assert any("extra= expands dynamically" in p for p in problems)
    assert any("emit(**fields)" in p for p in problems)


def test_the_sweep_rejects_a_formatter_owned_name(tmp_path, monkeypatch):
    offender = tmp_path / "offender.py"
    offender.write_text("security_events.emit('auth_failure', stack='forged')\n")
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    problems = _sweep()
    assert len(problems) == 1 and "formatter-owned" in problems[0]


def test_the_sweep_checks_the_field_against_the_named_event(tmp_path, monkeypatch):
    offender = tmp_path / "offender.py"
    offender.write_text("security_events.emit('auth_failure', grant_id='g')\n")
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    problems = _sweep()
    assert len(problems) == 1 and "does not declare" in problems[0]


# ── The bare-logger guard (design D18, task 5.0) ────────────────────────────
#
# The sweep above polices the *fields* a call site passes. It says nothing
# about a call site that passes none — and a bare `logger.warning("...")` in a
# request-path module is the hole D18 exists to close: it reaches the sink
# whatever the suppressor says, so one caller driving one branch on demand is an
# unbounded flood channel beside the bounded one. Four modules are covered,
# because those are the four the design names as request paths where a caller
# can trigger a refusal repeatedly.
#
# `logger.info` and `logger.debug` are deliberately out of scope: they are
# below the sink's default level, they are not refusals, and pulling them in
# would make every diagnostic breadcrumb in these files a catalogue decision.

# Every module a *request* can reach, not every module in a request-shaped
# directory. Round 2 of the adversarial review found three unbounded records
# and all three arrived the same way — a sibling change added a call to a
# module this list did not name — so the unit is the call site (design D23).
# `src/auth/routes.py`, `src/control_panel/users.py` and `src/csrf.py` are
# clean today and are listed so they stay that way.
_GUARDED_MODULES = (
    "src/mcp_server/auth.py",
    "src/mcp_server/tools.py",
    "src/transfer/routes.py",
    "src/control_panel/routes.py",
    "src/control_panel/users.py",
    "src/auth/routes.py",
    "src/auth/session.py",
    "src/csrf.py",
    "src/services/transfer.py",
    "src/services/vault_overlap.py",
    "src/services/vault_fs.py",
    "src/services/vault.py",
)

_GUARDED_METHODS = {"warning", "error", "exception", "critical"}

#: `{module: {message prefix: why it is allowed to stay}}`.
#:
#: Matched on the *literal first argument* rather than a line number, so the
#: exemption survives every reformatting and cannot silently widen to cover a
#: new call that drifts onto the same line.
#:
#: Two shapes, and both are D18's own line rather than an escape from it: the
#: panel's **Danger zone** (admin-guarded, once per action, reporting the
#: operational fact the health page exists to show) and **background work**
#: (the indexer's detection pass, a descriptor close) that no request drives at
#: all. Suppressing either would hide the failure of a rare, important
#: operation rather than bound a flood. Anything a *credential* can drive
#: belongs in the catalogue instead; an entry here is a claim that no such
#: caller exists, and it has to say why.
#: The owner decision of round 3, recorded as accepted limitation **R10** and
#: used verbatim by every site it covers. These fire only when the vault's own
#: filesystem is failing *after* the bytes have landed: the tool call
#: succeeded, the record adds nothing the caller can act on, and threading an
#: event through the destructive publication path — the one path in this
#: codebase that has clobbered a note before — buys a bound that
#: `mcp-rate-limits`' write bucket already provides.
_POST_PUBLICATION = (
    "post-publication filesystem failure on a successful write; class-only, "
    "bounded by the write bucket; not routed through the suppressor to keep "
    "the destructive publication path unchanged"
)

#: The staging half of the same story: cleanup and substitution guards that run
#: after a write has finished or aborted. Same class as `_POST_PUBLICATION` and
#: the same R10 decision, but the write they follow did not necessarily
#: succeed, so the reason says what it actually is rather than borrowing text
#: that would be a claim about the wrong thing.
_STAGING_CLEANUP = (
    "staging cleanup and substitution guard, reached only after a write has "
    "already landed or already aborted; it changes no result, and detecting a "
    "substituted staging name is precisely the operational fact suppression "
    "must not withhold. Same R10 decision as the post-publication flush: "
    "bounded by the write bucket, not routed through the suppressor, because "
    "the destructive publication path stays unchanged"
)

_BARE_LOGGER_EXEMPTIONS: dict[str, dict[str, str]] = {
    "src/control_panel/routes.py": {
        "Skipping HNSW index": (
            "Admin-triggered and once per action, not caller-triggerable: it "
            "is reached only from `reset_embeddings`, which is behind the "
            "panel's admin guard, and it fires at most once per reset. It is "
            "also an operational notice the health page exists to show — "
            "semantic_search has silently fallen back to a sequential scan — "
            "and D18 keeps exactly this class (background, once-per-pass, "
            "not a refusal) on the bare logger so suppression can never hide "
            "it. There is no caller who can drive it in a loop."
        ),
        "Embedding reset aborted": (
            "`_record_embedding_fingerprint` (#201/#206), reached only from "
            "the two Danger-zone reset routes — both behind "
            "`require_admin_panel`, one behind a signed one-time token as "
            "well — and at most once per action. It is the abort notice for a "
            "**destructive** operation that has just rolled itself back, so "
            "it is the single most important line the health page's ERROR "
            "ring buffer can hold: withholding it under a flood bound would "
            "leave an administrator with a flash message and no diagnosis. "
            "`logger.exception` rather than the catalogue is also what keeps "
            "the traceback, and the traceback is the whole value here — the "
            "record has to say *why* `set_state` failed."
        ),
        "Rollback after the failed fingerprint record failed": (
            "The second-order half of the same abort, in the same helper and "
            "on the same admin-only path. If it ever fires, the reset could "
            "neither record nor undo itself, which is the one state an "
            "operator must not have to infer from silence."
        ),
    },
    "src/services/transfer.py": {
        # Round 2 exempted this as "an operational fact with no caller". That
        # was **wrong**, and round 3 said so: `_close_quietly` runs on the
        # publish path of `PUT /transfer/upload` and `import_from_url`, up to
        # three times per publication, so a caller reaches it directly. The
        # exemption stands — on the accurate reason.
        "Could not close the %s descriptor": _POST_PUBLICATION,
    },
    # R10 — the post-publication flush and the staging cleanup. Every entry
    # here is reachable from a foreground tool call (create/edit/move/delete a
    # note, write/delete a file, redeem an upload), and every one of them runs
    # when the operation has *already* landed.
    "src/services/vault_fs.py": {
        # `flush_dir_quietly` — every rename publication: move_note's
        # renameat2, the soft delete's rename into .trash, the permanent
        # unlink.
        "Could not flush the %s to durable storage": _POST_PUBLICATION,
        # `flush_publication_ancestors_quietly` — every note publish, through
        # `vault._flush_target_dirs`.
        "Completed %s but could not flush the directories above": _POST_PUBLICATION,
        # `_unlink_quietly` after a publish consumed the name.
        "Published upload but could not remove temp file": _POST_PUBLICATION,
        # The same cleanup when the write did not publish.
        "Could not remove temp file %s": _STAGING_CLEANUP,
        # `discard_staged_name`'s four guards. Each one refuses to unlink a
        # name it cannot prove is ours — the destructive write that guard
        # exists to prevent — and says so.
        "Cannot confirm what was staged under %s": _STAGING_CLEANUP,
        "Staging name": _STAGING_CLEANUP,
        "Could not confirm that staging name": _STAGING_CLEANUP,
        # Once per process, by construction: a lock-guarded flag makes this
        # announcement fire on the first exercise and never again, so no
        # caller can drive it at all.
        "VAULT_ALLOW_NAMED_STAGING_FALLBACK is set": (
            "Announced once per process — `_named_staging_exercised` is set "
            "under a lock before the call — so it is a startup-class notice "
            "about how this vault's filesystem stages writes, not a "
            "per-request record. No caller can drive it a second time."
        ),
        # The staging prune, from the maintenance driver.
        "Could not list %s for pruning": (
            "The staging prune's directory listing, run by the maintenance "
            "driver rather than by a request. Background and once per pass — "
            "the class D18 keeps on the bare logger."
        ),
    },
    "src/services/vault.py": {
        # `_flush_target_dirs`, reached when `move_note`'s link rewrites have
        # already released the root descriptor. Design D18 already named this
        # site as staying on the bare logger; R10 is why.
        "Published %s but could not flush the directories above it": _POST_PUBLICATION,
    },
    "src/services/vault_overlap.py": {
        # The detection pass. Its entry points are startup, the indexer tick,
        # an administrator saving an assignment and an administrator starting
        # a reindex — background or admin-guarded and once per action, never a
        # credential in a loop. And it is the module whose ERROR lines the
        # health page exists to surface: bounding them would hide the
        # cross-tenant misconfiguration they exist to report.
        "failed to close vault-root descriptor": (
            "Housekeeping in a `finally` after the observation is already "
            "made; the same class as the transfer descriptor close."
        ),
        "vault-root snapshot seq=": (
            "One line per out-of-order publish, from the detection pass. "
            "Sequence numbers come from the pass, not from a request, so the "
            "rate is the pass's and nothing a caller does changes it."
        ),
        "vault-root overlap detection failed and no snapshot has": (
            "The pass could not run and nothing has ever been published, so "
            "every multi-user tool call stays refused. It re-raises. This is "
            "the loudest thing the server can say about its own readiness, "
            "and withholding it under a bound would leave an operator staring "
            "at a vault that refuses everything for no stated reason."
        ),
        "vault-root overlap detection failed; retaining the snapshot": (
            "The same failure with a previous snapshot to fall back on. Once "
            "per detection pass, and the traceback is the diagnosis."
        ),
        "vault root quarantine: ": (
            "`_log_snapshot`, one ERROR per quarantined user per pass, "
            "written *for* the ops-health ring buffer. It is the operator "
            "detail that `transfer_refused`'s `owner_quarantined` reason "
            "deliberately does not carry (D23) — so suppressing it would "
            "leave the bounded record naming a condition nothing explains."
        ),
    },
}


def _bare_logger_sweep(root=None, modules=None) -> list[str]:
    """Every `logger.warning/error/exception/critical` in the guarded modules
    that is not an explicit exemption, as `path:line: message` strings."""
    root = pathlib.Path(root) if root is not None else ROOT
    modules = _GUARDED_MODULES if modules is None else modules
    problems: list[str] = []
    for rel in modules:
        path = root / rel
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in _GUARDED_METHODS:
                continue
            base = func.value
            name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
            if "log" not in name.lower():
                continue
            first = node.args[0] if node.args else None
            message = first.value if isinstance(first, ast.Constant) else None
            exempt = _BARE_LOGGER_EXEMPTIONS.get(rel, {})
            if isinstance(message, str) and any(
                message.startswith(prefix) for prefix in exempt
            ):
                continue
            problems.append(
                f"{rel}:{node.lineno}: bare logger.{func.attr}(...) — migrate it "
                "to security_events.emit or add an exemption saying why not"
            )
    return problems


def test_no_bare_refusal_logger_survives_in_a_request_path_module():
    """D18's rule, as a test rather than as a paragraph.

    Every record a caller can drive has to pass the permit, so a direct
    `logger.warning` in one of these four modules is a bypass of the bound —
    not a style question. The four `move_note` sites this caught are now
    `move_rewrite_overlap_refused`, `move_post_rename_failed` (twice) and a
    reuse of `move_rewrite_failed`.
    """
    assert _bare_logger_sweep() == []


def test_the_exemption_list_is_exactly_what_the_design_says_it_is():
    """Pinned so the list cannot grow quietly, and so a removed call cannot
    leave a stale licence behind for the next warning that lands nearby.

    Two shapes and no others (design D23): the panel's Danger zone, and
    background work no request drives. A new entry is a decision — assert it
    here, with its reason, or migrate the call.
    """
    assert set(_BARE_LOGGER_EXEMPTIONS) == {
        "src/control_panel/routes.py",
        "src/services/transfer.py",
        "src/services/vault_overlap.py",
        "src/services/vault_fs.py",
        "src/services/vault.py",
    }
    assert set(_BARE_LOGGER_EXEMPTIONS["src/control_panel/routes.py"]) == {
        "Skipping HNSW index",
        "Embedding reset aborted",
        "Rollback after the failed fingerprint record failed",
    }
    assert set(_BARE_LOGGER_EXEMPTIONS["src/services/transfer.py"]) == {
        "Could not close the %s descriptor"
    }
    assert len(_BARE_LOGGER_EXEMPTIONS["src/services/vault_overlap.py"]) == 5
    assert len(_BARE_LOGGER_EXEMPTIONS["src/services/vault_fs.py"]) == 9
    assert set(_BARE_LOGGER_EXEMPTIONS["src/services/vault.py"]) == {
        "Published %s but could not flush the directories above it"
    }


def test_the_r10_reason_is_recorded_verbatim_everywhere_it_applies():
    """R10 is an owner decision, so its wording is the record of it.

    Every post-publication site quotes the same sentence — one string, used by
    reference — so a reader who greps one of them finds the decision rather
    than a paraphrase of it, and the architecture note and design.md can be
    checked against it.
    """
    assert _POST_PUBLICATION == (
        "post-publication filesystem failure on a successful write; "
        "class-only, bounded by the write bucket; not routed through the "
        "suppressor to keep the destructive publication path unchanged"
    )
    quoting = {
        rel: sorted(k for k, v in entries.items() if v is _POST_PUBLICATION)
        for rel, entries in _BARE_LOGGER_EXEMPTIONS.items()
        if any(v is _POST_PUBLICATION for v in entries.values())
    }
    assert quoting == {
        "src/services/transfer.py": ["Could not close the %s descriptor"],
        "src/services/vault.py": [
            "Published %s but could not flush the directories above it"
        ],
        "src/services/vault_fs.py": [
            "Completed %s but could not flush the directories above",
            "Could not flush the %s to durable storage",
            "Published upload but could not remove temp file",
        ],
    }
    for source in (
        ROOT / "docs" / "architecture" / "security-event-logging.md",
        ROOT / "openspec" / "changes" / "security-event-logging" / "design.md",
    ):
        assert "R10" in source.read_text(), (
            f"{source.name} must record the accepted limitation, not just the "
            "test that enforces it"
        )


def test_every_exemption_names_a_call_that_still_exists_and_says_why():
    """An exemption for a deleted call is a licence waiting for the next
    warning that lands near it, and one without a reason is just a hole."""
    for rel, entries in _BARE_LOGGER_EXEMPTIONS.items():
        source = (ROOT / rel).read_text()
        for prefix, reason in entries.items():
            assert f'"{prefix}' in source, (
                f"{rel}: the exemption for {prefix!r} outlived the call it "
                "exempts — delete it"
            )
            assert len(reason) > 80, (
                f"{rel}: {prefix!r} is exempted without a reason"
            )


def test_the_guard_covers_every_module_a_request_can_reach():
    """The scope itself, pinned — because round 2's three findings were all
    "a sibling change added a call to a module the list did not name"."""
    for rel in _GUARDED_MODULES:
        assert (ROOT / rel).is_file(), f"{rel} is guarded but does not exist"
    for rel in (
        "src/auth/session.py",
        "src/services/transfer.py",
        "src/services/vault_overlap.py",
        "src/csrf.py",
    ):
        assert rel in _GUARDED_MODULES, f"{rel} must stay in the guard (D23)"


def test_the_guard_catches_a_new_bare_warning(tmp_path):
    module = tmp_path / "src" / "transfer"
    module.mkdir(parents=True)
    (module / "routes.py").write_text(
        'logger.warning("a caller can drive this on demand")\n'
        'logger.info("out of scope, and stays out")\n'
    )
    problems = _bare_logger_sweep(root=tmp_path, modules=("src/transfer/routes.py",))
    assert len(problems) == 1 and "bare logger.warning" in problems[0]


def test_the_guard_honours_an_exemption_prefix(tmp_path):
    module = tmp_path / "src" / "control_panel"
    module.mkdir(parents=True)
    (module / "routes.py").write_text(
        'logger.warning("Skipping HNSW index: %d exceeds the limit", dim)\n'
    )
    problems = _bare_logger_sweep(
        root=tmp_path, modules=("src/control_panel/routes.py",)
    )
    assert problems == []
