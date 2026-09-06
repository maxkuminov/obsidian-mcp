import asyncio
import base64
import binascii
import copy
import errno
import inspect
import json
import logging
import math
import mimetypes
import os
import posixpath
import re
import stat
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import urlsplit

import pydantic_core
from mcp.server.fastmcp import Image
from pydantic import BaseModel
from sqlalchemy import text

from src.auth.session import (
    ACTOR_LABEL_MAX as _ACTOR_LABEL_MAX,
    ACTOR_REF_MAX as _ACTOR_REF_MAX,
    actor_columns,
    current_principal,
    current_user_id,
)
from src.config import (
    MAX_CHUNKS_PER_NOTE,
    MAX_LINKS_PER_NOTE,
    MAX_MOVE_REWRITE_BYTES,
    MAX_NOTE_BYTES,
    MAX_SEARCH_QUERY_CHARS,
    max_move_rewrite_sources,
    settings,
)
from src.database import async_session
from src.mcp_server.auth import (
    current_api_key_id,
    current_daily_request_limit,
    current_oauth_token_id,
    current_permission,
)
from src.mcp_server.read_result import (
    ReadNoteResult,
    apply_metadata_budget,
    build_outline,
    frontmatter_view,
    screen_unrenderable,
)
from src.models.db import UsageLog
from src.services import rate_limits, refusals, security_events, timing
from src.services.embeddings import semantic_search
from src.services.filters import apply_note_filters
from src.services.quotas import admit as _admit_quota, quota_refusal_message
from src.services.search import full_text_search
from src.services.usage_stats import OVER_QUOTA_PARAM
from src.services import transfer, vault_fs
from src.services.vault import (
    MAX_PATH_CHARS,
    VAULT_ROOT_NOT_READY_ERROR,
    VAULT_ROOT_OVERLAP_ERROR,
    VAULT_ROOT_UNEXAMINABLE_ERROR,
    UnconfirmedPublication,
    VaultAnchorUnavailable,
    VaultAssignmentChanged,
    VaultConfirmationUnavailable,
    VaultRootMismatch,
    VaultRootNotReady,
    VaultRootOverlap,
    VaultRootUnexaminable,
    _vault_root,
    classify_bytes,
    confirmed_publication,
    content_hash_for_bytes,
    extract_section_parts,
    is_canonical_content_hash,
    is_encodable,
    is_hidden_path,
    list_dir,
    move_file_no_clobber,
    open_mutable,
    outline_sections,
    read_bytes,
    read_bytes_at,
    read_file,
    soft_delete_target,
    unlink_at,
    validate_visible_path,
    write_bytes_at,
    write_file_at,
)

logger = logging.getLogger(__name__)

#: The tool whose call is in flight on this task, or `None` outside one.
#:
#: `_tracked` owns the lifecycle — set at the top of the wrapper, reset in the
#: same `finally` that clears the timing holder — so a helper reached from deep
#: inside a tool body can name the tool without every caller growing a
#: parameter. That is what lets `_require_write` record its refusal at the one
#: definition all nine gated call sites reach (#192): a per-caller `tool=` is
#: eight chances to forget one and a tenth tool that silently has none.
#:
#: Deliberately *not* the timing holder: `_tracked` merges that holder into
#: `usage_logs.params` verbatim, so a tool name parked there would become a
#: `params` key, and the reserved-key rule in
#: `docs/architecture/usage-attribution.md` exists to keep that surface
#: enumerated. A `ContextVar` is per-task, and each MCP tool call runs in its
#: own task, so concurrent calls cannot see each other's name.
_current_tool_name: ContextVar[str | None] = ContextVar(
    "_current_tool_name", default=None
)


def _security_subject() -> str:
    """The suppression subject for a tool-surface record.

    The resolved user when the credential named one, otherwise `-`. Never the
    client address: `_tracked` runs below `ProxyHeadersMiddleware` and nothing
    binds the request there (residual R8), so these records identify the
    *credential*, not the peer.
    """
    return security_events.subject_for(user_id=current_user_id.get())


_VAULT_GUIDE_PRIMER = (Path(__file__).parent / "vault_guide_primer.md").read_text(
    encoding="utf-8"
)

_NO_CLAUDE_MD_MESSAGE = (
    "# Vault-Specific Conventions\n"
    "\n"
    "No `CLAUDE.md` found at the vault root. To teach the agent about your\n"
    "folder structure, file-naming conventions, tag taxonomy, required\n"
    "frontmatter fields, or task-management syntax, create a `CLAUDE.md`\n"
    "file at the root of your vault. The agent will pick it up automatically\n"
    "on the next call.\n"
    "\n"
    "Suggested sections:\n"
    "\n"
    "- **Folder structure** — what lives where, and where new notes belong.\n"
    "- **Naming conventions** — how filenames are formatted.\n"
    "- **Frontmatter** — required and conventional YAML fields.\n"
    "- **Tag taxonomy** — top-level tags and their meaning.\n"
    "- **Task syntax** — any GTD/Dataview/checklist conventions in use.\n"
)


def _actor_columns() -> dict:
    """The denormalised actor for this request's `usage_logs` row, or `{}`.

    A thin delegation to `src.auth.session.actor_columns`, which lives beside
    the ContextVar it reads because the transfer mint records the same triple
    on `transfer_tokens` (issue #92). One reader is the point: the columns are
    identically typed on both tables, and a second copy of the mapping is how
    the mint and the tool-call log start truncating differently. The widths are
    re-exported above under their old names so nothing that reads
    `tools._ACTOR_LABEL_MAX` has to learn where they moved.
    """
    return actor_columns()


# PostgreSQL SQLSTATE for foreign_key_violation — the only insert failure
# `_log_usage` can recover from, and the only one it tries to.
_FK_VIOLATION_SQLSTATE = "23503"

# `usage_logs.user_id`'s constraint, from migration 009. The violated FK is
# resolved by name so that a dangling *credential* does not also cost the row
# its owner: the panel scopes a non-admin's usage page by `user_id`, so
# clearing it needlessly would hide the row from the one person entitled to
# see it.
_USER_FK_CONSTRAINT = "fk_usage_logs_user_id"


# The failure arrives wrapped twice. SQLAlchemy raises `IntegrityError`, whose
# `.orig` is the *dialect's* DBAPI-shaped `IntegrityError`, whose `__cause__` is
# asyncpg's own `ForeignKeyViolationError`. Measured on the deployed stack:
# `sqlstate` is present on the middle layer, `constraint_name` only on the
# innermost one. Both are walked rather than assumed, because which layer
# carries what is a driver detail we do not control.
_FK_CONSTRAINT_IN_MESSAGE = re.compile(r'foreign key constraint "([^"]+)"')


def _error_chain(exc: Exception):
    seen = []
    node = getattr(exc, "orig", None)
    while node is not None and node not in seen:
        seen.append(node)
        node = getattr(node, "__cause__", None)
    return seen


def _is_fk_violation(exc: Exception) -> bool:
    for node in _error_chain(exc):
        if getattr(node, "sqlstate", None) == _FK_VIOLATION_SQLSTATE:
            return True
        if getattr(node, "pgcode", None) == _FK_VIOLATION_SQLSTATE:
            return True
        # asyncpg raises `ForeignKeyViolationError`. A driver that carries no
        # SQLSTATE at all is still recognisable by class name.
        if "ForeignKeyViolation" in type(node).__name__:
            return True
    return False


def _fk_constraint_name(exc: Exception) -> str | None:
    chain = _error_chain(exc)
    for node in chain:
        name = getattr(node, "constraint_name", None)
        if name:
            return name
    # Last resort: the layers still carry the message, which names it.
    for node in (*chain, exc):
        match = _FK_CONSTRAINT_IN_MESSAGE.search(str(node))
        if match:
            return match.group(1)
    return None


def _violated_user_fk(exc: Exception) -> bool:
    """Was `usage_logs.user_id` the FK that failed?

    Unresolvable — no constraint name anywhere in the chain — counts as yes.
    Clearing a column that did not have to be cleared costs the row its
    per-user scoping; *not* clearing the one that did costs the row entirely.
    """
    name = _fk_constraint_name(exc)
    if not name:
        return True
    return name == _USER_FK_CONSTRAINT or "user_id" in name


async def _insert_usage(values: dict) -> None:
    async with async_session() as session:
        try:
            session.add(UsageLog(**values))
            await session.commit()
        except Exception:
            # Discard the failed transaction before the session goes back to
            # the pool. `async_session()`'s exit would do it too; doing it here
            # keeps the retry in `_log_usage` independent of that detail.
            await session.rollback()
            raise


async def _log_usage(
    tool: str, params: dict, duration_ms: int, response_size: int
) -> bool:
    """Write one `usage_logs` row for the request in flight.

    The values are read from the request-scoped context variables here and
    nowhere else; `write_usage_row` below owns the insert and its recovery, so
    a caller that already *holds* the attribution — the refusal coalescer's
    deferred flush, which by definition has no request context left — can land
    a row through exactly the same path.
    """
    return await write_usage_row(
        dict(
            key_id=current_api_key_id.get(),
            oauth_token_id=current_oauth_token_id.get(),
            user_id=current_user_id.get(),
            tool=tool,
            params=params,
            duration_ms=duration_ms,
            response_size=response_size,
            **_actor_columns(),
        )
    )


async def write_usage_row(values: dict) -> bool:
    """Insert one prepared `usage_logs` row, and do not lose it to a dangling
    credential.

    A tool call can outlive its own credential: an operator revokes and deletes
    a key, or deletes an OAuth client, while a slow call is still running. The
    insert then names a row that no longer exists, PostgreSQL raises
    `foreign_key_violation`, and a blanket `except` drops the whole audit line
    — precisely the call an operator investigating that credential most wants
    to see, and precisely the one whose durable attribution `actor_*` already
    carries (issue #77). Denormalising the label is pointless if the row it
    rides on is the thing that gets discarded.

    So an FK violation is retried **once**, with the credential FKs cleared and
    the actor columns kept. That is the same end state the panel's own key
    delete produces (`UPDATE usage_logs SET key_id = NULL`, then the delete),
    so it is a shape the reader already handles. `user_id` is dropped only when
    it is the constraint that failed — see `_violated_user_fk`.

    The broad `except` stays as the last resort: usage logging must never fail
    a tool call that has already done its work.

    **It returns whether the row landed** (#193). Swallowing every failure is
    right for the success path — a call that has already written to disk must
    not be failed by its own bookkeeping — but it left the one caller that has
    to *report* the audit unable to know: `_tracked`'s exception handler says
    `tool_usage_log_failed` when this answers `False`, so an operator reading
    the log can tell "the tool failed and here is the row" from "the tool failed
    and the row is missing". Every existing caller ignores the value and is
    unchanged.
    """
    tool = values.get("tool")
    subject = security_events.subject_for(user_id=values.get("user_id"))
    try:
        await _insert_usage(values)
        return True
    except Exception as e:
        if not _is_fk_violation(e):
            # `error_type` and not the exception's text: a failed insert's
            # message quotes the statement *and its bound parameters*, which
            # here are `usage_logs.params` — truncated note content and vault
            # paths. The allow-list has no field one could ride in, and a
            # traceback would have carried the lot (design D2, D18).
            security_events.emit(
                "usage_log_failed",
                subject=subject,
                tool=tool,
                reason="initial",
                error_type=type(e).__name__,
            )
            return False
        retry = dict(values, key_id=None, oauth_token_id=None)
        if _violated_user_fk(e):
            retry["user_id"] = None
        security_events.emit(
            "usage_log_credential_gone",
            subject=subject,
            tool=tool,
            cleared_user_id=retry["user_id"] is None,
        )

    try:
        await _insert_usage(retry)
        return True
    except Exception as e:
        security_events.emit(
            "usage_log_failed",
            subject=subject,
            tool=tool,
            reason="after_clearing_fks",
            error_type=type(e).__name__,
        )
        return False


_MAX_PARAM_LEN = 200  # truncate long string params (e.g. note content)
_MAX_QUERY_RESULTS = 500
_MAX_SEMANTIC_RESULTS = 50


def _clamp_limit(limit: int, maximum: int = _MAX_QUERY_RESULTS) -> int:
    """Keep authenticated callers from creating unbounded DB/response work."""
    return max(1, min(limit, maximum))


def _truncate_params(params: dict) -> dict:
    return {
        k: (v[:_MAX_PARAM_LEN] + "…" if isinstance(v, str) and len(v) > _MAX_PARAM_LEN else v)
        for k, v in params.items()
    }


#: The prose, unchanged since #66. Every `in` / `startswith` assertion written
#: against it still holds, because the sentinel line below is **appended**.
_NO_VAULT_PROSE = (
    "Error: no vault is assigned to this account, so no vault tool can run. "
    "Ask an administrator to assign a vault path to your user in the control "
    "panel."
)

#: What the caller actually receives: the prose plus the one machine-readable
#: line every refusal raised inside `_tracked` now ends with. **No
#: `retry_after_seconds`** — an unassigned vault is a fact about the account
#: that no amount of waiting changes, and a number there would invite a loop
#: that cannot end.
_NO_VAULT_MESSAGE = refusals.render(
    _NO_VAULT_PROSE, refusals.Refusal(code=refusals.NO_VAULT_ASSIGNED)
)

# Marker written into `usage_logs.params` for a call refused by the gate. It
# carries no new information — the user id and tool name are already columns.
_NO_VAULT_MARKER = "no_vault_assigned"

# The three vault-root **quarantine** markers (#199), one per reason the
# admission gate can refuse for once a quarantine snapshot is in play. All
# three are *pre-body* — the gate writes them before any tool body runs — so
# all three are enumerated by `src/services/usage_stats.py`'s pre-body refusal
# predicate alongside `_NO_VAULT_MARKER` and `_UNENCODABLE_ARG_MARKER`. A
# marker the predicate does not enumerate is wrong in both directions at once:
# its `duration_ms` is folded into the tool's latency percentiles as though the
# body had executed, and the refusal itself is never counted.
#
# Three values and not one, because each says what the other two do not, and an
# operator acts differently on each:
#
# * `vault_root_overlap` — this account's configured root collides with another
#   active account's (same directory, or one nested inside the other). The fix
#   is an **assignment or a mount corrected**, and there is a second account
#   involved. Reusing `_NO_VAULT_MARKER` here would tell an operator that an
#   administrator unassigned a user whose users page plainly shows one.
_VAULT_ROOT_OVERLAP_MARKER = "vault_root_overlap"

# * `vault_root_unexaminable` — this account's root could not be opened, so no
#   overlap could be ruled out. The fix is a **mount restored**, and no second
#   account exists. Recording it as an overlap would send an operator looking
#   for a peer that was never observed.
_VAULT_ROOT_UNEXAMINABLE_MARKER = "vault_root_unexaminable"

# * `vault_root_not_ready` — no snapshot has been published in this process
#   yet. Nothing about *this* account is wrong: it is the startup window or a
#   detector that is failing. The lifespan publishes synchronously before the
#   app serves, so a burst of this value in the usage log is the signal that
#   detection is not completing — a fact neither of the other two markers can
#   carry.
_VAULT_ROOT_NOT_READY_MARKER = "vault_root_not_ready"

# Marker for a *publication* refused because the assignment changed while the
# call was in flight (#88). Deliberately distinct from `_NO_VAULT_MARKER`: the
# admission gate's refusal means "this credential never had a vault this call",
# and this one means "it had one, and an administrator moved it underneath a
# call that was already running". An operator reading `/admin/usage` after a
# reassignment has to be able to tell those apart.
# Marker for a call refused because one of its arguments carries an unpaired
# surrogate (#149). Distinct from every vault marker on purpose: it says the
# credential was fine and the *request* was not, which is the difference between
# an operator investigating a permission problem and one investigating a client
# that is emitting bad JSON escapes.
_UNENCODABLE_ARG_MARKER = "argument_not_encodable"

_VAULT_REASSIGNED_MARKER = "vault_assignment_changed"

# Marker for a publication stopped because the assignment could not be **read**
# (#88). A third distinct value on purpose: `_NO_VAULT_MARKER` says the
# credential had no vault this call, `_VAULT_REASSIGNED_MARKER` says an
# administrator moved it, and this one says the server could not tell — a
# database outage. Recording an outage under the reassignment marker would put
# an administrator's name on an infrastructure incident in the audit trail.
_CONFIRMATION_UNAVAILABLE_MARKER = "vault_confirmation_unavailable"

# Marker for a publication stopped because the **bound root itself** could not
# be resolved at publish time (`VaultAnchorUnavailable`). It used to be logged
# under `_NO_VAULT_MARKER`, and that was a classification error with a
# consequence: `_NO_VAULT_MARKER` is a *pre-body* marker — the admission gate
# writes it before any tool body runs, and `src/services/usage_stats.py`
# enumerates it as one of the exact values meaning "this row's body never
# executed". This branch is the opposite. It is reached from inside a mutating
# tool that has already resolved a root, read the note, computed the write and
# reached its publication step, so the row is one of the most expensive the
# server logs. Filed under the pre-body marker it was silently dropped from
# every latency percentile — the #88 race is the reachable path, and the one
# view built to find slow write paths would have hidden precisely it.
#
# Distinct from `_CONFIRMATION_UNAVAILABLE_MARKER` too: that one says the
# *assignment query* failed (a database outage), this one says the anchor for
# an assignment we could read could not be named.
_ANCHOR_LOST_AT_PUBLISH_MARKER = "vault_anchor_lost_at_publish"

# Markers for `find_related`'s two operational failures (#161). Both are
# **post-body**: the body ran, resolved the vault, and queried the database —
# so, like every other post-body marker, they must never be added to
# `src/services/usage_stats.py`'s pre-body refusal predicate, which would drop
# these calls out of the latency percentiles.
#
# They exist because those two branches used to return a plain string with no
# marker at all, which made them indistinguishable *in the log* from a search
# that ran and found nothing. `/admin/search-analytics` reads
# `result_count == 0` as "the vault was asked for something it does not hold" —
# the signature question the page exists to answer — and a source note that is
# missing or not yet embedded is not that. It is a call that never got as far
# as looking, and counting it as a zero-result would put the operator's own
# typos and the indexer's backlog into the list of gaps in the vault's memory.
#
# Two values and not one, for the reason the classification rule gives: they
# are different facts with different fixes. `related_source_not_found` means
# the caller named a note that does not exist (or is not theirs);
# `related_source_not_embedded` means the note exists and the embed pass has
# not reached it, which resolves itself and is a fact about the indexer.
_RELATED_SOURCE_NOT_FOUND_MARKER = "related_source_not_found"
_RELATED_SOURCE_NOT_EMBEDDED_MARKER = "related_source_not_embedded"


# ── The vector tools' declared degradations (#200, #202) ────────────────────
#
# Both vector paths used to render the stored `chunk_text` with no
# `embedded_content_hash` predicate, so during a provider outage an agent was
# handed **superseded note text** as a current result with nothing marking it.
# Every other field on that row — path, title, tags — had been refreshed by the
# scan; only the chunk text was out of date, and only the chunk text is
# quotable as the note's content.
#
# The render is `get_links_impl`'s idiom, and the three properties are the same
# three: the count is on the header **always, including zero** (an absent token
# is not evidence of absence — a caller cannot otherwise tell "nothing here is
# stale" from a build that does not report staleness); the per-row marker is
# rendered only when it is *true* (`stale: false` on fifteen of fifteen rows is
# noise, not information, and the header count is the always-present signal);
# and a trailing bold block states the consequence in words rather than leaving
# the caller to infer it from a flag.
_STALE_PREVIEW_NOTICE = (
    "(preview withheld — this note changed after it was embedded, so the "
    "stored excerpt is superseded text. Call `read_note` for the current "
    "content.)"
)


def _degradation_suffix(stale: bool, truncated: bool) -> str:
    """The per-row markers, in `get_links`'s `— key: true` shape.

    Only the true ones are rendered. A row can carry both: a capped note whose
    head was embedded before its last edit is stale *and* truncated, and those
    are independent facts with different remedies — `read_note` repairs the
    first for this call, and nothing repairs the second short of the note
    getting shorter.
    """
    parts = []
    if stale:
        parts.append(" — stale: true")
    if truncated:
        parts.append(" — embedding_truncated: true")
    return "".join(parts)


def _stale_source_line(path: str) -> str:
    """`find_related`'s one extra fact, which no per-row marker can express.

    The query vector is the mean of the *source's* stored chunk vectors, so a
    stale source means every neighbour answers a question about content that
    note no longer has. Distinct from `related_source_not_embedded`, which
    keeps its own message: a source with no vectors at all is a different fact
    with a different fix, and collapsing them would send a caller to the wrong
    remedy.
    """
    # Worded so it reads correctly on **both** return paths, because it is
    # emitted on both: "these neighbours were computed from…" is nonsense above
    # an empty list, and the empty list is where this line does the most work.
    return (
        f"**`{path}` changed after it was embedded**, so this search ran "
        "against the vector of its previous content and answers a superseded "
        "question. The next embed pass repairs this on its own."
    )


def _degradation_footer(stale_count: int, truncated_count: int) -> list[str]:
    """The trailing block, one paragraph per degradation that occurred.

    Says what was withheld and what is still trustworthy, because the row
    itself is not being retracted: a stale note is still *found*, still ranked
    and still named, and its path and title were refreshed by the scan. That is
    the whole trade — a silently wrong answer becomes a visibly degraded one
    whose remedy is one call away.
    """
    out: list[str] = []
    if stale_count == 1:
        out.append(
            "\n**One of these notes changed after it was embedded.** Its "
            "preview is withheld and its ranking reflects its previous "
            "content; its path and title are current. Read it with `read_note`."
        )
    elif stale_count:
        out.append(
            f"\n**{stale_count} of these notes changed after they were "
            "embedded.** Their previews are withheld and their ranking "
            "reflects their previous content; their paths and titles are "
            "current. Read them with `read_note`."
        )
    if truncated_count == 1:
        out.append(
            "\n**One of these notes was embedded only up to the first "
            f"{MAX_CHUNKS_PER_NOTE} chunks.** A match against it is a match "
            "against its head — its tail is not reachable by semantic search "
            "at all, though `keyword_search` still covers the whole note."
        )
    elif truncated_count:
        out.append(
            f"\n**{truncated_count} of these notes were embedded only up to "
            f"the first {MAX_CHUNKS_PER_NOTE} chunks.** A match against one of "
            "them is a match against its head — its tail is not reachable by "
            "semantic search at all, though `keyword_search` still covers the "
            "whole note."
        )
    return out

# The write gate's marker (#192). `_require_write` records it at its single
# definition, so all nine gated call sites inherit it without being touched —
# `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`,
# `write_file`, `delete_file`, and `request_upload` / `import_from_url` through
# `_mint_preflight(need_write=True)`. `request_download` asks for
# `need_write=False` and is deliberately unmarked.
#
# **Post-body**, by the classification rule above, and deliberately so (design
# D6): `_require_write` is called from *inside* a tool body that has already
# passed the vault gate, the argument screen and the quota gate — and has
# already spent its quota slot. So it must never be added to
# `src/services/usage_stats.py`'s pre-body refusal predicate. The accepted cost
# is stated where it can be seen: a read-only credential probing `create_note`
# contributes near-zero rows to that tool's percentiles (residual R5). Moving
# the gate up into `_tracked` would fix that and change quota accounting and
# refusal ordering for nine tools, which is a different change.
#
# Before this marker existed the row was shaped *exactly* like a successful
# write, so `/admin/usage` showed a read-only credential apparently writing.
_PERMISSION_DENIED_MARKER = "permission_denied"

# The marker for a tool body that raised (#193). Also **post-body** by
# definition — the body is what raised — so it too stays out of the pre-body
# predicate: a tool that fails after eight seconds of I/O is the slowest path
# there is, and the one view built to find slow paths must see it.
#
# It wins over any post-body marker the body recorded before it raised: a
# `find_related` that recorded `related_source_not_found` and *then* raised is
# logged as `tool_exception`, because the exception is the outcome. It travels
# with the reserved `params` key `error_type` (the exception's class name),
# which no reader casts — see docs/architecture/usage-attribution.md.
_TOOL_EXCEPTION_MARKER = "tool_exception"

# The quota gate's marker (#162), and the one marker on this list that is not a
# `params.error` string: it is the JSON **boolean** `over_quota: true`.
#
# It is *imported* from `src/services/usage_stats.py` rather than declared here
# and mirrored, which is the opposite of what `_NO_VAULT_MARKER` and
# `_UNENCODABLE_ARG_MARKER` do — and deliberately. Those two are mirrored
# because the import can only run one way without closing a cycle, and this is
# the direction it runs: the quota gate imports the reader's constant, so the
# writer and every consumer of the pre-body refusal predicate cannot disagree
# about the key's name at all. `usage_stats` enumerated it ahead of this gate
# shipping precisely so the two would land as one contract.
#
# **Pre-body**, by the classification rule above: the increment happens after
# credential resolution and argument screening and *before* the tool body, so a
# refused call did no work and belongs out of the latency percentiles. The
# value must stay a real JSON boolean — `/admin/performance` casts it with an
# unguarded `(params->>'over_quota')::boolean`, and a row carrying the string
# `"yes"` there takes the page down with a 500 for every user until it ages out
# (see docs/architecture/usage-attribution.md, "the casts are unguarded").
_OVER_QUOTA_MARKER = OVER_QUOTA_PARAM

# ── The rate controls' three markers (#188, #194) ───────────────────────────
#
# Split by what an operator asks, which is the classification rule applied
# rather than three names for one event: "is one agent too fast?", "did a
# caller send something too big?" and "did the provider refuse what we sent?"
# are answerable from the marker alone, without parsing a scope string.

# **Pre-body.** Either token bucket; `rate_limit_scope` says which. The buckets
# are the *first* gates in the decorator, so a row carrying this marker did
# nothing at all — no vault resolution, no argument walk, no quota statement,
# no body — and it belongs out of the latency percentiles, which is what the
# pre-body predicate in `src/services/usage_stats.py` is for.
_RATE_LIMITED_MARKER = "rate_limited"

# **Pre-body.** The declarative argument length cap, refused beside the
# unencodable-argument screen and therefore before the embedding call, the
# `tsquery` parse and the quota statement.
_ARGUMENT_TOO_LONG_MARKER = "argument_too_long"

# **Post-body, and deliberately not in the pre-body predicate.** The embedding
# provider rejected the input against *its own* token limit, which can only be
# learned by asking it: the body ran, resolved a vault and made a network round
# trip before this branch could be reached. Enumerating it as a refusal would
# drop the slowest kind of call there is out of the percentiles — the same
# classification error `vault_anchor_lost_at_publish` exists to record.
#
# The caller-facing *code* for this branch is `argument_too_long`, so the agent
# sees one actionable failure mode for "the query was too large" whichever
# limit applied. The marker and the code answering different questions is
# permitted, and here it is deliberate.
_PROVIDER_INPUT_REJECTED_MARKER = "provider_input_rejected"

# `params` keys the rate controls add. Neither is one of the three reserved
# keys the unguarded casts on `/admin/performance` read (`embed_ms`, `db_ms`,
# `over_quota`).
#
# `rate_limit_scope` is a **string** and no reader casts it — it exists so a
# write-bucket refusal is never attributed to the general one.
_RATE_LIMIT_SCOPE_PARAM = "rate_limit_scope"
# `suppressed` is an **integer** and is read with a *guarded* cast: a row
# stands for `1 + suppressed` refusals, so a reader that summed rows would
# undercount every coalesced window.
_SUPPRESSED_PARAM = rate_limits.SUPPRESSED_PARAM

#: Which setting an over-long argument names in its refusal. Keyed by argument
#: name rather than by the cap's value, because two caps may one day share a
#: number and an operator needs the name of the thing they would change.
_CHAR_CAP_SETTING_NAMES = {"query": "MAX_SEARCH_QUERY_CHARS"}


class _TrashUnusable(Exception):
    """The trash probe failed for a reason that is not `UnsupportedFilesystem`.

    The probe runs inside the confirmed publish step (it creates `.trash`, so a
    refused delete must not reach it), and that step is a synchronous callback
    which can only report by raising. This type carries the message the tool
    used to return directly, so the wording an operator sees is unchanged.
    """


async def _confirmed_publication(uid: int | None, publish):
    """Confirm the vault assignment and publish, in one uninterrupted step.

    The one entry point every mutating tool uses for *each* publishing
    operation it performs (#88). `publish` is a **synchronous** callable taking
    the `RootConfirmation` and performing exactly that one publication with it;
    `vault.confirmed_publication` awaits the confirming read and then calls it
    before returning control to the event loop, so no caller-visible `await`
    can sit between the confirmation and the write. Returns
    `(None, publish's result)` when the assignment is unchanged, or
    `(the tool-error string, None)` when it changed.

    The refusal is recorded through the request-scoped params holder `_tracked`
    already merges, so it reaches `usage_logs.params` as one `error` field and
    nothing else. `_tracked` remains the only thing that calls `begin()` /
    `clear()`.

    **Only the confirming read's own failures are caught here.** Whatever
    `publish` raises propagates untouched to the tool's own handlers — that is
    what makes "a refused confirmation" and "a failed write" distinguishable by
    type rather than by string matching, and it is why
    `VaultAnchorUnavailable` is a named subclass rather than a bare
    `RuntimeError` (which a publish body may legitimately raise).

    **A confirmation outage is deliberately left to propagate.** Before the
    first publication of a call it is fail-closed either way — nothing is
    confirmed, so nothing publishes — and the two alternatives are worse:
    swallowing it would report a write that did not happen as an ordinary
    refusal, and recording it under the reassignment marker would put a claim
    in the audit trail that no administrator made. The one caller with
    something partial to report catches it explicitly (`move_note`, after the
    move has already stood). Single-user mode issues no query at all, so a
    database blip cannot reach a single-user note write through this path.
    """
    try:
        return None, await confirmed_publication(uid, publish)
    except VaultAssignmentChanged as exc:
        timing.record("error", _VAULT_REASSIGNED_MARKER)
        return str(exc), None
    except VaultAnchorUnavailable as exc:
        # The bound root itself could not be resolved — a cold cache, or an
        # ownerless credential in multi-user mode. The admission gate refuses
        # both before the body runs, so this is rare; the #88 race reaches it
        # when the assignment is torn out underneath a call that had already
        # passed admission. A mutation whose anchor cannot be named must not
        # publish either.
        #
        # **Its own marker, not the admission gate's.** This row's body ran —
        # it resolved a root, read the note and computed the write before
        # arriving here — and `_NO_VAULT_MARKER` is enumerated by
        # `src/services/usage_stats.py` as a value meaning "the body never
        # started". Sharing it made the most expensive refusal in the server
        # invisible to the latency view.
        timing.record("error", _ANCHOR_LOST_AT_PUBLISH_MARKER)
        return str(exc), None


#: Exception type -> `(caller-facing message, usage_logs marker, event reason)`
#: for the three quarantine refusals `_vault_root` can raise.
#:
#: **The message is taken from this table, not from the exception instance.**
#: `str(exc)` would be the same string today, and the day somebody raises one
#: of these with a path or a peer's name in it — debugging, a richer operator
#: log — that string would go straight out to the tenant's agent. Reading the
#: wording from a fixed table makes "the refusal names no other user, no other
#: vault path and no note path" a property of this module rather than a
#: property of every future `raise` site.
#: **The prose is unchanged and the sentinel line is appended**, like every
#: other pre-body refusal. Each carries its own `code` rather than folding onto
#: `no_vault_assigned`, for the reason the markers are three and not one: an
#: agent that can tell "this account has no vault" from "the server will not
#: serve the vault it has" can say something useful to its operator. None
#: carries a `retry_after_seconds` — no interval this module could name is
#: honest for a misconfigured mount or an unpublished snapshot, and a wrong one
#: would be a retry loop nobody scheduled.
_QUARANTINE_REFUSALS: dict[type, tuple[str, str, str]] = {
    VaultRootOverlap: (
        refusals.render(
            VAULT_ROOT_OVERLAP_ERROR,
            refusals.Refusal(code=refusals.VAULT_ROOT_OVERLAP),
        ),
        _VAULT_ROOT_OVERLAP_MARKER,
        "overlap",
    ),
    VaultRootUnexaminable: (
        refusals.render(
            VAULT_ROOT_UNEXAMINABLE_ERROR,
            refusals.Refusal(code=refusals.VAULT_ROOT_UNEXAMINABLE),
        ),
        _VAULT_ROOT_UNEXAMINABLE_MARKER,
        "root_unexaminable",
    ),
    VaultRootNotReady: (
        refusals.render(
            VAULT_ROOT_NOT_READY_ERROR,
            refusals.Refusal(code=refusals.VAULT_ROOT_NOT_READY),
        ),
        _VAULT_ROOT_NOT_READY_MARKER,
        "snapshot_not_ready",
    ),
}


class _MarkedRefusal(str):
    """A pre-body refusal message that carries its own `usage_logs` marker.

    A `str` subclass on purpose. Every existing consumer of a refusal treats it
    as the message it is — `refusal_result` maps it onto a structured tool's
    shape, `_log_usage` measures it as the response, the caller reads it — and
    a subclass keeps all of that unchanged, including the tests that
    monkeypatch `_vault_admission_error` with a plain string. What it adds is
    the one thing `_tracked` cannot otherwise know: **which** pre-body marker
    this refusal is. Before the quarantine reasons there was one refusal here
    and the decorator could hard-code `_NO_VAULT_MARKER`; there are four now,
    and deriving the marker from the message text would tie the audit
    vocabulary to prose that gets reworded.

    A refusal that arrives without a marker (a plain `str`) is the
    no-assignment one, which is what `_tracked`'s `getattr` default says.
    """

    __slots__ = ("marker",)

    def __new__(cls, message: str, marker: str) -> "_MarkedRefusal":
        refusal = super().__new__(cls, message)
        refusal.marker = marker
        return refusal


def _vault_admission_error() -> str | None:
    """Refuse the call when the caller has no resolvable vault root.

    Unassigning `users.vault_path` used to revoke only the *disk*-touching
    tools: `semantic_search`, `keyword_search`, `list_notes`, `get_recent` and
    every graph tool are served entirely from `notes_metadata` /
    `note_embeddings`, never call `_vault_root`, and those rows are never
    deleted (the indexer skips users with a NULL `vault_path`, so nothing ever
    prunes them). The result was an indefinite, fully queryable mirror of the
    content the user last held — file paths, titles, tags, frontmatter and
    chunk excerpts — reachable with an unchanged API key, while the panel told
    the operator "vault tools error" (issue #66).

    So the gate lives here, in the decorator every tool shares, rather than in
    the individual tools: resolve the root **once, before the body runs**, and
    fail the whole call if it cannot be resolved. Every `_tracked` tool reads
    or writes vault content or vault metadata, so there is nothing to exempt —
    `get_vault_guide` reads the vault's `CLAUDE.md`, `check_upload` reports a
    vault path. Fail closed and keep the list at zero.

    Single-user mode is untouched: `current_user_id` is None there (the
    sentinel's `id` is None and sandbox mode never sets it), and `_vault_root`
    answers from `settings.vault_path` without consulting the cache.

    Preserving the index rows is deliberate — a reassignment of the same path
    should not have to re-embed the vault from scratch.
    """
    uid = current_user_id.get()
    try:
        _vault_root(uid)
    except (VaultRootOverlap, VaultRootUnexaminable, VaultRootNotReady) as exc:
        # The vault-root quarantine (#199), ahead of the generic branch below
        # because these three are `RuntimeError` subclasses and would otherwise
        # be swallowed by it and filed under `no_vault_assigned`. They are
        # ordered first for that reason alone; the generic branch is unchanged.
        #
        # The wording comes from `src/services/vault.py` and **names no other
        # user, no other vault path and no note path, for any reason**. The
        # caller is a tenant's agent, and the whole point of the quarantine is
        # that two tenants were about to be able to see each other; a refusal
        # that says whose root it collided with would be the leak in miniature.
        # The panel, the log line and the `indexer_runs` row are where the
        # affected accounts, reasons and roots are named.
        message, marker, reason = _QUARANTINE_REFUSALS[type(exc)]
        security_events.emit(
            "tool_refused_vault_quarantined",
            subject=_security_subject(),
            user_id=uid,
            tool=_current_tool_name.get(),
            reason=reason,
        )
        return _MarkedRefusal(message, marker)
    except RuntimeError:
        # Unassigned, deactivated, or a cold cache. All three mean the same
        # thing to the caller and all three must refuse: a cold cache is not
        # permission to serve stale rows.
        security_events.emit(
            "tool_refused_no_vault",
            subject=_security_subject(),
            user_id=uid,
            tool=_current_tool_name.get(),
        )
        return _NO_VAULT_MESSAGE
    return None


async def _quota_admission_error() -> str | None:
    """Consume one quota slot, or refuse the call. `None` means "carry on".

    **Who this applies to.** Only a caller authenticated with an API key that
    has a non-null `daily_request_limit`. Both facts are already bound to the
    request by `APIKeyMiddleware` from the key row it loaded, so the common
    case — every key today, and all OAuth traffic — is two ContextVar reads and
    **no database statement at all**. That is the whole of "keys with a null
    limit behave exactly as today": there is no query to regress on, not a query
    whose result happens to be permissive.

    OAuth is exempt in v1 because panel OAuth is the operator, and an operator
    locked out of the panel by a ceiling they set on themselves cannot raise it.

    **Where it sits.** After the vault admission gate and the unencodable
    argument screen, before the tool body — so a call refused for having no
    vault, or for carrying an unpaired surrogate, consumes nothing. It is the
    *last* pre-body gate on purpose: a slot spent on a call that was never going
    to run is a slot an operator cannot account for.

    **What it costs when it does run.** One statement, committed in its own
    transaction, on its own pooled connection, released before the body starts
    (see `src/services/quotas.py`). The body never waits on it and never
    inherits it.
    """
    limit = current_daily_request_limit.get()
    if limit is None:
        return None
    key_id = current_api_key_id.get()
    if key_id is None:
        # A limit with no key is not a state the middleware produces — it binds
        # both together — but it is the state a direct in-process caller or a
        # half-set test fixture reaches, and counting against a key id of None
        # would violate the counter's own NOT NULL. Exempt rather than crash.
        return None
    decision = await _admit_quota(key_id, limit)
    if decision.admitted:
        return None
    security_events.emit(
        "tool_refused_over_quota",
        subject=_security_subject(),
        key_id=key_id,
        limit=limit,
        day=decision.day.isoformat(),
        user_id=current_user_id.get(),
        tool=_current_tool_name.get(),
    )
    # The reset comes from the decision, not from a fresh clock read. A refusal
    # that straddles UTC midnight would otherwise name the day-after-next's
    # midnight and tell an obedient agent to back off for nearly two days when
    # its quota was about to reset in milliseconds — a self-inflicted outage
    # produced entirely by reading the clock twice.
    #
    # `refusals.render` is idempotent, so this is the *fallback* rendering: the
    # sentinel line belongs on the message either way, and the moment
    # `quota_refusal_message` renders it from the admission's own recorded
    # decision instant this call sees a message that already carries one and
    # returns it untouched. Until then the interval is derived here, and the
    # `max(1, …)` is what keeps the UTC-midnight case honest — a decision whose
    # reset has already passed quotes one second, never a negative number and
    # never the day-after-next's midnight.
    prose = quota_refusal_message(limit, decision.reset_at)
    decided_at = getattr(decision, "decided_at", None) or datetime.now(timezone.utc)
    retry_after = max(
        1, math.ceil((decision.reset_at - decided_at).total_seconds())
    )
    return refusals.render(
        prose,
        refusals.Refusal(
            code=refusals.OVER_QUOTA,
            scope="api_key",
            limit=limit,
            limit_unit=refusals.CALLS_PER_DAY,
            retry_after_seconds=retry_after,
        ),
    )


def _response_size(result) -> int:
    """How many characters this result costs the caller's context.

    A tool that declares a structured output type does not put `str(result)` on
    the wire: FastMCP renders the text block with
    `pydantic_core.to_json(result, fallback=str, indent=2)` and sends
    `structuredContent` beside it. `len(str(model))` would record a pydantic
    `repr` — a number that tracks nothing an operator can act on. So models are
    measured as the JSON the client actually receives; everything else keeps
    the accounting it always had.
    """
    if isinstance(result, BaseModel):
        try:
            return len(pydantic_core.to_json(result, fallback=str, indent=2).decode())
        except Exception as exc:  # pragma: no cover — accounting never fails a call
            # `error_type` and no traceback: the failure is a serialisation of
            # the *result*, so its message and frames quote note content. The
            # allow-list has no field that could carry it (design D2).
            security_events.emit(
                "tool_result_measure_failed",
                subject=_security_subject(),
                tool=_current_tool_name.get(),
                error_type=type(exc).__name__,
            )
    return len(str(result))


def _first_unencodable_argument(bound) -> str | None:
    """The name of the first argument carrying an unpaired surrogate, or None.

    A JSON-RPC argument can hold `"\\ud800"`: a valid JSON string, a valid
    Python `str`, and not a Unicode scalar value — so it cannot be encoded as
    UTF-8. Nothing good happens downstream. It cannot name a file. It cannot be
    written to one. And when a tool quotes it back in an error message — which
    every not-found and every ambiguity listing does — the MCP layer's own
    serialization raises, turning a careful in-band error into a *protocol*
    error that the agent cannot act on and the audit trail does not explain.

    So the screen sits in the decorator every tool shares, above the individual
    error paths, for the same reason #66's vault gate does: auditing each
    interpolation one at a time is how the #149 forgery class stayed open for
    two audit rounds. `read_note`'s `path` and `section` were closed
    individually first; this closes `edit_note(section=…)`, `edit_note(find=…)`,
    `operation=`, `set_frontmatter(updates=…)` and everything added later,
    including arguments no error message quotes today but might tomorrow.
    """

    def offends(root) -> bool:
        # Iterative, with an explicit stack and **no depth limit**. The first
        # version was recursive and stopped at six levels, which is not a bound
        # — it is a hole with a number on it: `{"a": [[[[[[["\ud800"]]]]]]]}`
        # walked straight past it and the value was written to the note. Depth
        # is the only axis that needs a loop; total size is already bounded by
        # the transport's request-body limit, so a document that reaches this
        # function is finite and the walk terminates.
        #
        # Containers already visited are skipped, which makes a self-referential
        # argument terminate as well. An argument cannot be recursive over the
        # wire (JSON has no aliases), but this function is called on whatever a
        # caller passes an impl directly, in tests and in future in-process
        # callers.
        stack = [root]
        seen: set[int] = set()
        while stack:
            value = stack.pop()
            if isinstance(value, str):
                if not is_encodable(value):
                    return True
                continue
            if isinstance(value, dict):
                if id(value) in seen:
                    continue
                seen.add(id(value))
                stack.extend(value.keys())
                stack.extend(value.values())
                continue
            if isinstance(value, (list, tuple, set, frozenset)):
                if id(value) in seen:
                    continue
                seen.add(id(value))
                stack.extend(value)
        return False

    for name, value in bound.arguments.items():
        if offends(value):
            return name
    return None


def _unencodable_argument_error(name: str) -> str:
    """The refusal wording. Deliberately does not repeat the value.

    Quoting it back is the whole problem: the argument is precisely the one
    that cannot be encoded into the response carrying the complaint.
    """
    return refusals.render(
        f"Argument '{name}' is not valid UTF-8: it contains an unpaired "
        "surrogate code point. Re-send it as UTF-8 text.",
        # No retry interval: the argument's bytes are the problem, and they do
        # not improve while the caller waits.
        refusals.Refusal(code=refusals.ARGUMENT_NOT_ENCODABLE),
    )


async def _record_tool_failure(
    tool_name: str, exc: Exception, duration_ms: int, logged: dict
) -> None:
    """Record a tool body that raised, then let `_tracked` re-raise it (#193).

    `_tracked`'s wrapper used to have a `try`/`finally` and no `except`, so an
    exception from a tool body skipped `_log_usage` entirely — zero usage rows,
    no logger call, and nothing for the health page's ERROR ring buffer. A
    write tool that failed halfway left no trace at all.

    The order is deliberate. The record goes first because it is the cheapest
    step and the one the health page depends on; the row second, because it is
    the one that can fail; the re-raise last, and by the caller, so the SDK
    still produces its error result and the traceback is unchanged.

    **The audit write is best-effort, and this is the one place in the codebase
    that catches `BaseException`** — scoped to a single bookkeeping `await`. If
    the task is cancelled while the row is being written, the `CancelledError`
    is recorded as `tool_usage_log_failed` and **superseded**: the caller
    re-raises the tool's original exception instead. The coroutine still unwinds
    immediately, so cancellation achieves its purpose; what changes is only the
    exception type the awaiter observes on a call that had *already* failed. The
    accepted consequence, written down rather than glossed: a `wait_for` around
    such a call reports the tool's failure rather than the timeout. The
    alternative — letting the cancellation win — loses the tool exception
    entirely, which is the record this whole change exists to produce (D11).
    """
    actor = _actor_columns()
    security_events.emit(
        "tool_exception",
        level=logging.ERROR,
        subject=_security_subject(),
        exc_info=exc,
        tool=tool_name,
        error_type=type(exc).__name__,
        user_id=current_user_id.get(),
        actor_kind=actor.get("actor_kind"),
        # **Never `actor_ref`.** For an API-key caller that column holds
        # `api_keys.key_prefix`, which is the first twelve characters of the
        # live key — a credential substring, in a record that also carries a
        # traceback. `usage_logs` keeps it (that is the #77 attribution design,
        # and those rows are read behind the panel's own auth); a security
        # event, which goes to a shared log sink, identifies the credential by
        # its row id instead. The ids answer the same operator question — which
        # credential did this — without being any part of the secret.
        key_id=current_api_key_id.get(),
        oauth_token_id=current_oauth_token_id.get(),
        duration_ms=duration_ms,
    )
    try:
        written = await _log_usage(tool_name, logged, duration_ms, 0)
    except BaseException as audit_exc:
        security_events.emit(
            "tool_usage_log_failed",
            subject=_security_subject(),
            tool=tool_name,
            error_type=type(audit_exc).__name__,
        )
        return
    if not written:
        # No `error_type`: `_log_usage` swallowed the failure by design and
        # already emitted `usage_log_failed` carrying the class. What is worth
        # saying here is the thing only this caller knows — that the tool
        # exception's own audit row is missing.
        security_events.emit(
            "tool_usage_log_failed",
            subject=_security_subject(),
            tool=tool_name,
        )


def _bucket_admission(write_class: bool) -> tuple[str, int] | None:
    """`(scope, retry_after_seconds)` for the bucket that refused, or `None`.

    Two buckets, because velocity and destruction are different questions. The
    general one bounds the rate at which *any* work is created; the write one
    halves that for the eight tools that change vault bytes. Neither bounds
    totality — 120 deletes a minute empties this vault in about twenty minutes
    — which is what the daily quota is for, and only for the credentials it
    reaches.

    The general bucket is charged first and its token is **spent** even when
    the write bucket then refuses: a token refills, and the refusal itself is
    work the server performed.

    A caller with no principal — sandbox mode, or a direct in-process caller —
    reads `None` and is exempt from both, the same shape as the quota gate's
    "a limit with no key is exempt rather than a crash".
    """
    principal = current_principal.get()
    admitted, retry_after = rate_limits.take(principal, refusals.SCOPE_PRINCIPAL)
    if not admitted:
        return refusals.SCOPE_PRINCIPAL, retry_after
    if write_class:
        admitted, retry_after = rate_limits.take(
            principal, refusals.SCOPE_PRINCIPAL_WRITE
        )
        if not admitted:
            return refusals.SCOPE_PRINCIPAL_WRITE, retry_after
    return None


def _rate_limited_message(scope: str, retry_after: int) -> str:
    """The refusal a rate-limited caller receives, prose plus sentinel line.

    It says what was *not* consumed, because the reader is an agent deciding
    what to do next: a refused call ran no body and spent no daily quota slot,
    so retrying after the quoted interval is exactly as good as the original
    call would have been.
    """
    limit = rate_limits.bucket_limit(scope)
    which = "write" if scope == refusals.SCOPE_PRINCIPAL_WRITE else "general"
    prose = (
        f"Error: this credential exceeded its {which} rate limit of {limit} "
        f"calls per minute, so the call was refused before it ran. Nothing "
        f"was read, written, or counted against the daily quota. Retry in "
        f"{retry_after} seconds, or slow the calling loop down."
    )
    return refusals.render(
        prose,
        refusals.Refusal(
            code=refusals.RATE_LIMITED,
            scope=scope,
            limit=limit,
            limit_unit=refusals.CALLS_PER_MINUTE,
            retry_after_seconds=retry_after,
        ),
    )


def _rate_refusal_template(tool_name: str, params: dict, scope: str) -> dict:
    """The complete row a coalescing window will write, captured when it opens.

    Everything the row needs, resolved **now**: the owner, both credential ids,
    the denormalised actor triple, the tool, the marker, the scope and the
    bounded arguments. That is what lets a deferred flush read no
    request-scoped context variable and depend on no live credential — by the
    time the tick or the shutdown flush runs, the request is long gone and the
    key may have been deleted, and `write_usage_row`'s existing 23503 recovery
    (clear the foreign keys, keep the `actor_*` columns) is what lands the row
    anyway.

    `duration_ms` and `response_size` are zero because they are meaningless for
    a call that never ran; the pre-body predicate keeps such rows out of the
    latency aggregates regardless.
    """
    return dict(
        key_id=current_api_key_id.get(),
        oauth_token_id=current_oauth_token_id.get(),
        user_id=current_user_id.get(),
        tool=tool_name,
        params={
            **params,
            "error": _RATE_LIMITED_MARKER,
            _RATE_LIMIT_SCOPE_PARAM: scope,
        },
        duration_ms=0,
        response_size=0,
        **_actor_columns(),
    )


def _argument_too_long_error(name: str, length: int, cap: int) -> str:
    """The over-long-argument refusal. **Never echoes the argument.**

    Quoting it back would be the #149 discipline broken by the very screen that
    exists to enforce it — and an 8 KB argument quoted into a tool result is 8
    KB of the caller's context spent on repeating what it just sent. The
    setting is named so an operator reading the agent's transcript knows what
    they would change.
    """
    setting = _CHAR_CAP_SETTING_NAMES.get(name)
    named = f" ({setting})" if setting else ""
    prose = (
        f"Error: argument '{name}' is {length} characters, above the limit of "
        f"{cap}{named}. The call was refused before it ran and the argument is "
        "not echoed back. Send a shorter one."
    )
    return refusals.render(
        prose,
        # No retry interval: re-sending the same argument fails identically,
        # however long the caller waits. What fixes it is a shorter argument.
        refusals.Refusal(
            code=refusals.ARGUMENT_TOO_LONG,
            scope="argument",
            limit=cap,
            limit_unit=refusals.CHARACTERS,
        ),
    )


def _first_over_long_argument(bound, caps: dict[str, int]) -> tuple[str, int] | None:
    """`(name, length)` of the first argument over its declared cap, or None."""
    for name, cap in caps.items():
        value = bound.arguments.get(name)
        if isinstance(value, str) and len(value) > cap:
            return name, len(value)
    return None


def _tracked(
    tool_name: str,
    param_keys: list[str],
    transforms: dict | None = None,
    refusal_result=None,
    write_class: bool = False,
    arg_char_caps: dict[str, int] | None = None,
):
    """Decorator that times the call and logs it to usage_logs.

    `transforms` maps a parameter name to a function applied before the value
    is logged. It exists for `import_from_url`, whose `url` must be reduced to
    its host: the whole point of the allow-list is that a capability or a URL
    carrying one never reaches `usage_logs`, and "just don't log the URL" would
    lose the one field that makes an import auditable.

    `refusal_result` maps the admission gate's refusal *message* onto the shape
    this particular tool returns. It exists for `read_note`, which declares a
    structured output type (#149): a bare string from a tool FastMCP validates
    against an output schema is not an in-band error, it is a protocol error —
    the agent sees a transport failure instead of "you have no vault". Tools
    that return strings leave it None and get the message unchanged. It maps
    *every* pre-body refusal, not just the vault one — the unencodable-argument
    screen and the quota gate (#162) reach the same branch, so a structured
    tool refuses over quota in its own shape rather than by breaking the wire
    format.

    `write_class` marks a tool that changes vault bytes, so it must pass the
    per-principal **write** bucket in addition to the general one. Eight tools
    carry it — the five note tools, `write_file`, `delete_file` and
    `import_from_url` — because those are the calls that amplify into the next
    indexer pass. `PUT /transfer/upload` writes vault bytes without being a
    tool call at all and consumes the same bucket at redemption, in
    `src/transfer/routes.py`; bounding only the tools would leave the write
    rate escapable by minting capabilities and redeeming them.

    `arg_char_caps` maps an argument name to the longest string this tool will
    accept for it, refused **pre-body** — before the embedding call, the
    `tsquery` parse, any search statement and the quota gate. Declarative and
    beside the unencodable-argument screen on purpose: a generic argument
    screen already lives there, and auditing each interpolation one at a time
    is how the #149 class stayed open for two audit rounds.

    **The gate order is L2 → L3 → L4 → L5 → L6 → body** — general bucket,
    write bucket, vault admission, the two argument screens, daily quota, and
    only then the tool. The buckets are first because they are the only gate
    that is pure arithmetic; the quota stays **last** because it is the only
    gate that consumes something durable, and nothing durable may be spent by a
    call that does not run.
    """
    transforms = transforms or {}
    arg_char_caps = arg_char_caps or {}

    def decorator(fn):
        sig = inspect.signature(fn)

        @wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            # The decorator owns the per-phase timing holder: a fresh dict per
            # call, reset in `finally`. That is what makes cross-call
            # attribution impossible — an early return or an exception leaves
            # measured phases at their measured value, and the *next* call in
            # the same task starts from empty rather than inheriting them.
            token = timing.begin()
            # The tool's name, for the helpers reached from inside its body
            # that must name it without growing a parameter — `_require_write`
            # above all (#192). Same lifecycle as the timing holder, reset in
            # the same `finally`.
            name_token = _current_tool_name.set(tool_name)

            def named_params() -> dict:
                """The row's named arguments, truncated. No outcome markers.

                Resolved by NAME via the wrapped signature so that a non-logged
                positional argument between logged ones cannot shift the
                mapping (positional zipping silently mislabelled params once).
                `transforms` reduces a value before it is logged (e.g. an
                import URL to its host) so no capability leaks.

                Both outcome paths build the row from this, so the failed call
                and the successful one carry the same arguments.
                """
                try:
                    bound = sig.bind(*args, **kwargs)
                    bound.apply_defaults()
                    params = {
                        key: transforms.get(key, lambda v: v)(bound.arguments[key])
                        for key in param_keys
                        if key in bound.arguments
                    }
                except TypeError:
                    params = {}
                return _truncate_params(params)

            # Whether this call writes its own `usage_logs` row. False for
            # exactly one case: a rate refusal folded into an already-open
            # coalescing window, where the whole point is that it issues no
            # statement of any kind. Its count lands on the next row that key
            # writes, or on the tick / shutdown flush.
            log_row = True
            try:
                refusal = None
                extra: dict = {}

                # ── L2 and L3: the two per-principal token buckets, the first
                # gates in the decorator (design D3).
                #
                # First because they are the only gate that is pure arithmetic
                # — one dictionary lookup and some floats, no statement, no
                # session checkout, no lock and no `await` between the read and
                # the write — so the flood we most want to shed is shed before
                # anything touches a cache, an argument tree or the database.
                #
                # Above the vault gate, deliberately: a rate-refused call never
                # resolves a vault root, and it reveals nothing about the vault
                # by not doing so, because its content depends only on the
                # caller's own request rate.
                #
                # A token is not a quota slot. A token refills, so spending one
                # on a call a later gate refuses is correct — the refusal is
                # itself work. The daily quota keeps its #162 position as the
                # last pre-body gate, so a call refused here consumes no daily
                # slot.
                bucket = _bucket_admission(write_class)
                if bucket is not None:
                    scope, retry_after = bucket
                    refusal = _rate_limited_message(scope, retry_after)
                    security_events.emit(
                        "tool_refused_rate_limited",
                        subject=_security_subject(),
                        tool=tool_name,
                        reason=scope,
                        limit=rate_limits.bucket_limit(scope),
                        user_id=current_user_id.get(),
                        key_id=current_api_key_id.get(),
                        oauth_token_id=current_oauth_token_id.get(),
                    )
                    # Recorded through the coalescer, because a rate refusal
                    # arrives at the caller's *arrival* rate — precisely the
                    # rate nothing else bounds — so an uncoalesced row would
                    # make "generate database writes" the cheapest thing an
                    # agent could do. The template is built only when a window
                    # opens or rolls over.
                    def refusal_template():
                        # Guarded for the telemetry tail's reason: a
                        # `transforms` entry that raises on the value it was
                        # given must not turn a *refusal* into an exception.
                        # A row with no arguments is a worse row; a refusal
                        # that became a traceback is a worse bug.
                        try:
                            params = named_params()
                        except Exception:  # noqa: BLE001 - never fail a refusal
                            params = {}
                        return _rate_refusal_template(tool_name, params, scope)

                    planned = rate_limits.record_rate_refusal(
                        current_principal.get(),
                        tool_name,
                        _RATE_LIMITED_MARKER,
                        scope,
                        refusal_template,
                    )
                    # **The coalescer owns every `rate_limited` row**, this one
                    # included, and it writes them all from the template it
                    # captured. Two reasons, and the second is the one that was
                    # a bug: one code path builds every such row, immediate and
                    # deferred alike, so the deferred one is not the only path
                    # anybody has checked — and the write is *acknowledged*, so
                    # a row that does not land puts its whole weight
                    # (`1 + suppressed`) back into the window instead of
                    # vanishing between an advanced counter and a failed
                    # insert. The decorator's own tail therefore writes nothing
                    # here.
                    log_row = False
                    if planned is not None:
                        await rate_limits.write_planned_row(planned)

                if refusal is None:
                    # L4 — admission gate: a caller with no resolvable vault
                    # root never reaches the tool body, including the DB-only
                    # ones. The refusal is still logged, like any other tool
                    # error.
                    refusal = _vault_admission_error()
                    # **The refusal names its own marker.** The gate has four
                    # reasons now — no assignment, and the three vault-root
                    # quarantine reasons (#199) — and each is a different fact
                    # an operator acts on differently, so each gets its own
                    # value in `usage_logs.params["error"]`. A plain `str` is
                    # the no-assignment refusal, which keeps the historical
                    # default and keeps a monkeypatched gate returning a bare
                    # message working.
                    extra = (
                        {"error": getattr(refusal, "marker", _NO_VAULT_MARKER)}
                        if refusal is not None
                        else {}
                    )
                screened = None
                if refusal is None:
                    # L5a — second admission gate, same altitude: an argument
                    # that cannot be encoded as UTF-8 never reaches a tool
                    # body, where quoting it back into an error message would
                    # make the MCP layer's serialization raise.
                    try:
                        screened = sig.bind(*args, **kwargs)
                        screened.apply_defaults()
                    except TypeError:
                        screened = None
                    offender = (
                        None if screened is None
                        else _first_unencodable_argument(screened)
                    )
                    if offender is not None:
                        refusal = _unencodable_argument_error(offender)
                        extra = {"error": _UNENCODABLE_ARG_MARKER}
                if refusal is None and arg_char_caps and screened is not None:
                    # L5b — the declarative argument length cap, beside the
                    # screen above and for the same reason: before the tool
                    # body, so before the embedding-provider call, before the
                    # `tsquery` parse, before any search or quota statement,
                    # and before the value is interpolated into a
                    # server-authored string.
                    #
                    # **Not coalesced**, unlike the rate refusals: this gate
                    # sits *below* the general bucket, so a principal can
                    # produce at most `MCP_RATE_LIMIT_PER_MINUTE` of these a
                    # minute — the same bound an admitted call's row already
                    # has. A second mechanism would buy nothing.
                    over_long = _first_over_long_argument(screened, arg_char_caps)
                    if over_long is not None:
                        name, length = over_long
                        refusal = _argument_too_long_error(
                            name, length, arg_char_caps[name]
                        )
                        extra = {"error": _ARGUMENT_TOO_LONG_MARKER}
                if refusal is None:
                    # L6 — the last admission gate (#162), deliberately after
                    # every other one: a call refused for having no vault or for
                    # an unencodable argument must consume no quota, because
                    # its body was never going to run. This one *does* consume
                    # — the increment commits before the body starts, so an
                    # admitted call that later raises has still spent its slot,
                    # and a refusal never increments at all.
                    #
                    # Only an API-key caller with a non-null limit reaches a
                    # database statement here; for everyone else this is two
                    # ContextVar reads.
                    over_quota = await _quota_admission_error()
                    if over_quota is not None:
                        refusal = over_quota
                        # The one marker in `params` that is a JSON boolean
                        # rather than an `error` string. `usage_stats` reads it
                        # as `(params->>'over_quota')::boolean` with no guard,
                        # so `True` is the only value that may ever be written.
                        extra = {_OVER_QUOTA_MARKER: True}
                if refusal is not None:
                    result = refusal if refusal_result is None else refusal_result(refusal)
                else:
                    # **The only guarded expression** (design D5). The handler
                    # wraps the body call and nothing else: not the three
                    # admission gates above it — a database fault inside the
                    # quota gate is not a tool failure, and `quotas.admit`
                    # already logs and re-raises it deliberately — and not the
                    # telemetry below it, because reporting a completed
                    # `edit_note` as `tool_exception` because `_log_usage`
                    # failed *after* the bytes landed is precisely the silently
                    # wrong record this change exists to prevent.
                    #
                    # `Exception`, never `BaseException`: `asyncio.CancelledError`
                    # is a `BaseException` in 3.8+, and a client disconnect or a
                    # shutdown must not be recorded as a tool failure or write a
                    # row.
                    try:
                        result = await fn(*args, **kwargs)
                    except Exception as exc:
                        # Guarded: a `transforms` entry that raises on a value
                        # the body already choked on must not become the
                        # exception the caller sees. A row with no arguments is
                        # a worse row; a masked failure is a worse bug.
                        try:
                            failed = named_params()
                            failed.update(timing.current() or {})
                        except Exception:  # noqa: BLE001 - never mask `exc`
                            failed = {}
                        # Merged over the top: a body that recorded
                        # `related_source_not_found` and *then* raised is
                        # logged as `tool_exception`, because the exception is
                        # the outcome.
                        failed["error"] = _TOOL_EXCEPTION_MARKER
                        failed["error_type"] = type(exc).__name__
                        await _record_tool_failure(
                            tool_name,
                            exc,
                            int((time.monotonic() - start) * 1000),
                            failed,
                        )
                        # Bare, so the SDK still produces its error result over
                        # the *original* traceback. It re-raises `exc` even when
                        # the audit above swallowed a cancellation of its own.
                        raise
                # From here the body has COMPLETED. Nothing below may write
                # `_TOOL_EXCEPTION_MARKER` — and nothing below may fail the
                # call either. Keeping the tail outside the exception
                # classifier stops a failed audit being *reported* as a tool
                # failure (design D5); it did not stop a failed audit
                # **being** one. `named_params()` runs the tools' `transforms`,
                # `_response_size` serialises the result, and `_log_usage`
                # takes a pooled connection: any of the three can raise after
                # `edit_note` has already written the bytes, and the caller
                # then sees an error for a write that stood. So the tail is
                # best-effort and response-neutral — the completed result is
                # returned either way, and the bookkeeping failure is recorded
                # as itself.
                #
                # `Exception`, not `BaseException`: a cancellation here is a
                # client that went away or a shutdown, and it must keep
                # unwinding rather than be swallowed into a returned result.
                try:
                    duration_ms = int((time.monotonic() - start) * 1000)
                    logged = named_params()
                    logged.update(extra)
                    # Whatever the service measured. Absent for tools that
                    # measure nothing, so `params` keeps its current shape.
                    logged.update(timing.current() or {})
                    if log_row:
                        await _log_usage(
                            tool_name, logged, duration_ms, _response_size(result)
                        )
                except Exception as tail_exc:  # noqa: BLE001 - see above
                    # Class only, and structurally distinct from
                    # `tool_exception`: an operator filtering for failed tool
                    # calls must not find a call that succeeded. The exception's
                    # own text is withheld because a `transforms` or
                    # serialisation failure quotes the arguments or the result,
                    # which are note content and vault paths (design D2).
                    security_events.emit(
                        "tool_telemetry_failed",
                        subject=_security_subject(),
                        tool=tool_name,
                        error_type=type(tail_exc).__name__,
                    )
                return result
            finally:
                timing.clear(token)
                _current_tool_name.reset(name_token)

        # Structural marker. `tests/test_issue_66_*` asserts that every tool
        # registered on the MCP server delegates to something carrying it, so
        # "the admission gate is inherited by construction" is checked rather
        # than asserted.
        wrapper.__tracked_tool__ = tool_name
        return wrapper
    return decorator


# The tool this impl backs is registered as `keyword_search` (server.py takes
# the function name), so that is what `usage_logs.tool` must record — the old
# "search_notes" named a tool no client was ever offered, which made the audit
# trail unsearchable in both directions (#78).
@_tracked(
    "keyword_search",
    ["query", "folder", "limit", "tags", "frontmatter"],
    arg_char_caps={"query": MAX_SEARCH_QUERY_CHARS},
)
async def search_notes_impl(
    query: str,
    folder: str | None = None,
    limit: int = 20,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Full-text keyword search across vault notes."""
    limit = _clamp_limit(limit)
    uid = current_user_id.get()
    async with async_session() as session:
        results = await full_text_search(
            session,
            query,
            folder=folder,
            limit=limit,
            tags=tags,
            frontmatter=frontmatter,
            user_id=uid,
        )
    if not results:
        return f"No results for '{query}'"
    lines = [f"Found {len(results)} results for '{query}':\n"]
    for r in results:
        tags_str = f" [{', '.join(r['tags'])}]" if r.get("tags") else ""
        lines.append(f"- **{r['title']}** (`{r['path']}`){tags_str} — rank: {r['rank']:.3f}")
    return "\n".join(lines)


def _window(body: str, offset: int, limit: int) -> tuple[str, int | None]:
    """Slice `body` to a window. Returns `(chunk, next_offset)`.

    `next_offset` is None when the window reached the end of `body`.
    """
    start = max(0, offset)
    chunk = body[start:start + limit]
    end = start + len(chunk)
    return chunk, (end if end < len(body) else None)


# How much of a caller-supplied section selector may be quoted back inside a
# server-authored notice or error. The selector is an argument, not note text,
# but it is unbounded, and the response requirement is that error and notice
# strings interpolate only bounded values (#149).
_NOTICE_SELECTOR_MAX = 256


def _bounded(text: str, limit: int) -> str:
    """Cut `text` to `limit` characters. No marker — see below.

    **Only ever applied to server-authored prose** (`error`, `notice`, and the
    caller-supplied selector they quote). Note-controlled *fields* are never cut
    at all: they are dropped whole, because a shortened or marked value inside
    one is indistinguishable from note content, which is the forgery class #149
    exists to end. A marker is left off here too, for the same reason — the
    selector a message quotes is caller text sitting inside server text.
    """
    return text if len(text) <= limit else text[:limit]


_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _scalar_safe(text: str) -> str:
    """Server-authored prose, guaranteed UTF-8-encodable.

    The one place a replacement character is acceptable. `error` and `notice`
    are server prose, not note-controlled fields, so substituting U+FFFD for an
    unpaired surrogate cannot be mistaken for content — and the alternative is
    `pydantic_core` refusing to serialize the response at all, which turns an
    in-band error into a protocol error. Every known route to an unencodable
    interpolation is already closed upstream (the path and the selector are
    refused at admission, in both tools); this catches the next one.

    It substitutes explicitly rather than via `encode(errors="replace")`, which
    despite the name emits ASCII `?` on the encode side — indistinguishable from
    a question mark the caller actually sent, in a message a human is meant to
    read.
    """
    if is_encodable(text):
        return text
    return _SURROGATE_RE.sub("\ufffd", text)


def _origin_label(section: str | None) -> str:
    """What a notice or error calls the thing being read."""
    if section is None:
        return "note"
    return f"section '{_bounded(section, _NOTICE_SELECTOR_MAX)}'"


def _read_notice(
    path: str,
    section: str | None,
    offset: int,
    shown_to: int,
    total: int,
    next_offset: int | None,
    outline,
) -> str:
    """The server-authored prose beside a truncated read's truncation fields.

    Truncation itself is *data* now (`truncated`, `offset`, `next_offset`,
    `total_chars`, and the outline object); this string is the guidance an
    agent acts on, and it is the single producer of the narrowing guidance the
    note-read spec requires to name only registered tools. Every value it
    interpolates is bounded: `path` by `MAX_PATH_CHARS` at admission, the
    selector by `_NOTICE_SELECTOR_MAX`, the rest are integers.
    """
    origin = _origin_label(section)
    parts = [
        f"Truncated: showing characters {offset:,}–{shown_to:,} of {total:,} "
        f"for this {origin}."
    ]
    if next_offset is not None:
        call = f'read_note(path="{path}"'
        if section is not None:
            call += f', section="{_bounded(section, _NOTICE_SELECTOR_MAX)}"'
        call += f", offset={next_offset})"
        parts.append(f"Continue with `{call}`.")

    if section is None:
        if outline is not None and outline.entries:
            parts.append(
                "This note's sections are listed in the outline field, each with "
                "the ordinal that addresses it. Read one directly with "
                f'`read_note(path="{path}", section="<heading>")`, or by ordinal '
                '(`section="#7"`). A bare `#N` always selects by position, so it '
                "stays reliable when titles repeat."
            )
            if outline.truncated:
                listed = len(outline.entries)
                parts.append(
                    f"The outline lists {listed:,} of {listed + (outline.omitted or 0):,} "
                    "sections; the ones it left out are counted in its own "
                    "omission fields rather than left silent."
                )
        elif outline is not None:
            parts.append(
                "This note has sections, but not one outline entry fits the "
                "response budget, so the outline carries only its truncation "
                "marker."
            )
        parts.append(
            "You can also narrow the request with `keyword_search` instead of "
            "reading the whole note."
        )
    return "\n\n".join(parts)


def _read_note_refusal(message: str) -> ReadNoteResult:
    """The admission gate's refusal, in the shape this tool declares.

    `read_note` is the one tool with an MCP output schema, so the gate cannot
    hand back a bare string: FastMCP would fail output validation and the agent
    would see a protocol error where the contract promises an in-band one. The
    path is deliberately NOT echoed — the gate runs before any validation, so
    the argument is unbounded, and this response's whole discipline is that
    nothing unbounded is interpolated.
    """
    return ReadNoteResult(error=message)


@_tracked(
    "read_note",
    ["path", "section", "offset", "limit"],
    refusal_result=_read_note_refusal,
)
async def read_note_impl(
    path: str,
    section: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> ReadNoteResult:
    """Read a note by its vault-relative path, capped to a context-safe size.

    Returns a **structured result**, not a rendered string: metadata and note
    content sit in separate fields, so nothing a note contains can change which
    field anything else appears in (#149 — the previous envelope was forgeable
    by a note's own frontmatter, twice reproduced).

    - `content` — whole-note reads: the body with a valid frontmatter block
      stripped, which is what `edit_note(path, content)` full replacement
      accepts, but **only** when the read is complete and unwindowed
      (`offset=0`, `truncated` false). Section reads: the section's **body
      only**, byte-exact input for `edit_note(path, content, section=…)`.
    - `heading` — section reads: the matched heading line, no terminator. It is
      NOT part of `content`, and a section write must not be sent it.
    - `frontmatter_yaml` — the block's YAML source, authoritative, and
      LF-normalized rather than byte-exact (a CRLF block's terminators come
      back as LF, the same declared residual `content` carries; `edit_note`
      still reattaches the original block byte-identically). `frontmatter` is a
      best-effort JSON view beside it and can be absent — with the reason in
      `metadata_omissions` — because dates, non-string keys, recursive aliases
      and unpaired-surrogate escapes have no faithful JSON form. Mutate
      frontmatter through `set_frontmatter` or the raw block, never by
      round-tripping the view.
    - `metadata_omissions` — every metadata field this response dropped, and
      why. Nothing is signalled inside a note-controlled field.
    - `content_hash` — the **whole file's** digest,
      `sha256:<64 lowercase hex>`, from the same read that built this response
      (never a second one, which could describe different bytes). It is the
      whole file's for a section read and a truncated read alike, so the token
      means one thing wherever it came from, and it is **not** a hash of
      `content`: a note with frontmatter or CRLF terminators has a digest the
      returned text cannot reproduce. Hand it to a write tool as
      `expected_hash` to bind the write to the bytes this read saw.

    Byte-identity on a round trip holds for LF-bodied notes; terminators inside
    the selected body come back as LF because this path normalises and the
    write path rewrites raw bytes.

    There is no textual procedure for recovering content from this response,
    and none is needed: the field is the recovery.
    """
    uid = current_user_id.get()
    # `limit` lowers the CONTENT window (and, as it always has, the outline's
    # own budget). The metadata budget is the server cap: `limit` is documented
    # as bounding what content comes back, and letting `limit=1` silently drop
    # a note's title and tags would make a cheap probe useless.
    metadata_budget = settings.max_read_response_chars
    cap = settings.max_read_response_chars

    def _fail(message: str) -> ReadNoteResult:
        # `_scalar_safe` is the last line of defence, not the first: the path
        # and the selector are both refused at admission when they carry an
        # unpaired surrogate, precisely so no error message has to quote one.
        # It stays because an error string that cannot be serialized is a
        # protocol error, and this function is the only place every one of them
        # passes through.
        result = ReadNoteResult(error=_scalar_safe(_bounded(message, metadata_budget)))
        # An over-long or unencodable path is refused before it is echoed
        # anywhere; every other error names the path the caller asked for,
        # exactly.
        if len(path) <= MAX_PATH_CHARS and is_encodable(path):
            result.path = path
        return result

    # Error precedence, in this order and no other: path resolution first, then
    # parameter validation, then section resolution. Exactly one error comes
    # back, and no content-bearing field rides with it.
    try:
        note = read_file(path, user_id=uid)
    except FileNotFoundError:
        return _fail(f"Note not found: {path}")
    except ValueError as e:
        return _fail(str(e))

    if limit is not None:
        if limit < 1:
            return _fail(f"read_note: limit must be >= 1 (got {limit}).")
        cap = min(limit, cap)
    if offset < 0:
        return _fail(f"read_note: offset must be >= 0 (got {offset}).")
    content = note["content"]
    heading: str | None = None
    body = content
    if section is not None:
        parts, err = extract_section_parts(content, section)
        if err is not None:
            return _fail(err)
        heading, body = parts

    total = len(body)
    chunk, next_offset = _window(body, offset, cap)
    if not chunk and offset > 0:
        # Empty content at offset 0 is a successful read of an empty section;
        # only a *continuation* offset can run off the end.
        if offset == total:
            return _fail(
                f"read_note: offset {offset:,} is exactly the end of "
                f"{_origin_label(section)} in {path} ({total:,} chars) — the "
                "whole selection has been read, there is nothing further."
            )
        return _fail(
            f"read_note: offset {offset:,} is past the end of "
            f"{_origin_label(section)} in {path} ({total:,} chars)."
        )

    truncated = offset > 0 or next_offset is not None
    view, view_omission, view_coercions = frontmatter_view(note["frontmatter"])
    result = ReadNoteResult(
        path=note["path"],
        # From `read_file`'s single read of the note's bytes — the same bytes
        # every field beside it was derived from. A second open here could
        # hash a different file than the one this response describes, which is
        # worse than no hash at all (#149's D3, #205's D6). Set for every
        # successful read: whole-note, section, windowed and truncated alike,
        # and always the whole file's.
        content_hash=note["content_hash"],
        title=note["title"],
        tags=list(note["tags"]) or None,
        frontmatter_yaml=note["frontmatter_yaml"],
        frontmatter=view,
        heading=heading,
        content=chunk,
        truncated=truncated,
        offset=offset,
        next_offset=next_offset,
        total_chars=total,
    )
    if truncated:
        # The outline is a whole-note affordance: a caller who named a section
        # has already chosen, and does not need the others listed.
        if section is None:
            result.outline = build_outline(content, cap)
        # `_scalar_safe` on the notice as well as the error. Nothing can reach
        # it today — the path and the selector it interpolates are both refused
        # at admission — but "cannot happen today" is the reasoning that kept
        # this class open for two audit rounds, and the guard is one call.
        result.notice = _scalar_safe(_read_notice(
            note["path"], section, offset, min(offset, total) + len(chunk),
            total, next_offset, result.outline,
        ))
    # Values that would not render are reported BEFORE the budget runs, so
    # `metadata_omissions` reads in decision order and the budget never spends
    # room on a field that is about to be dropped anyway.
    carried = [] if view_omission is None else [view_omission]
    carried += screen_unrenderable(result, note["lossy_metadata"])
    # Coercions go in beside the omissions and are filtered against what
    # survives the budget: a retained-but-altered value is a different fact
    # from a dropped one, and the two lists never carry each other's entries
    # (#154, design D10).
    apply_metadata_budget(result, carried, metadata_budget, view_coercions)
    return result



@_tracked("list_notes", ["folder", "limit", "tags", "frontmatter"])
async def list_notes_impl(
    folder: str = "",
    limit: int = 50,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """List notes in a vault folder, sourced from the index."""
    from sqlalchemy import select
    from src.models.db import NoteMetadata

    limit = _clamp_limit(limit)
    uid = current_user_id.get()
    async with async_session() as session:
        stmt = select(NoteMetadata).order_by(NoteMetadata.modified_at.desc())
        stmt = apply_note_filters(
            stmt, folder=folder or None, tags=tags, frontmatter=frontmatter, user_id=uid
        )
        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        notes = result.scalars().all()

    if not notes:
        return f"No markdown files in '{folder or '/'}'"

    lines = [f"Found {len(notes)} notes in '{folder or '/'}':\n"]
    for n in notes:
        if n.modified_at:
            mod = n.modified_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        else:
            mod = "unknown"
        size = n.file_size or 0
        lines.append(f"- `{n.file_path}` ({size:,}B, modified {mod})")
    return "\n".join(lines)


@_tracked("get_tags", ["limit"])
async def get_tags_impl(limit: int = 50) -> str:
    """List all tags with counts."""
    from sqlalchemy import func, select
    from src.models.db import NoteMetadata

    limit = _clamp_limit(limit)
    uid = current_user_id.get()
    async with async_session() as session:
        tag_query = select(
            func.unnest(NoteMetadata.tags).label("tag"),
            func.count().label("count"),
        )
        # Total mapping: an ownerless call counts the NULL-owned rows' tags
        # and nobody else's. A named user's private tag vocabulary is exactly
        # the kind of thing this used to hand over wholesale (#127).
        tag_query = tag_query.where(_note_owner_predicate(uid))
        result = await session.execute(
            tag_query.group_by("tag")
            .order_by(func.count().desc())
            .limit(limit)
        )
        rows = result.fetchall()

    if not rows:
        return "No tags found"

    lines = [f"Top {len(rows)} tags:\n"]
    for row in rows:
        lines.append(f"- #{row.tag} ({row.count})")
    return "\n".join(lines)


@_tracked("get_recent", ["limit", "folder", "tags", "frontmatter"])
async def get_recent_impl(
    limit: int = 20,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Recently modified notes."""
    from sqlalchemy import select
    from src.models.db import NoteMetadata

    limit = _clamp_limit(limit)
    uid = current_user_id.get()
    async with async_session() as session:
        query = select(NoteMetadata).order_by(NoteMetadata.modified_at.desc())
        query = apply_note_filters(
            query, folder=folder, tags=tags, frontmatter=frontmatter, user_id=uid
        )
        query = query.limit(limit)
        result = await session.execute(query)
        notes = result.scalars().all()

    if not notes:
        return "No recent notes found"

    lines = [f"Last {len(notes)} modified notes:\n"]
    for n in notes:
        mod = n.modified_at.strftime("%Y-%m-%d %H:%M") if n.modified_at else "unknown"
        tags_str = f" [{', '.join(n.tags)}]" if n.tags else ""
        lines.append(f"- `{n.file_path}` — {n.title}{tags_str} (modified {mod})")
    return "\n".join(lines)


@_tracked(
    "semantic_search",
    ["query", "limit", "folder", "tags", "frontmatter"],
    arg_char_caps={"query": MAX_SEARCH_QUERY_CHARS},
)
async def semantic_search_impl(
    query: str,
    limit: int = 15,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Vector similarity search over the configured embedding provider's vectors.

    Reports `stale: true` for a note whose stored vectors predate its indexed
    content, and **withholds that row's preview** rather than quoting text the
    note no longer has (#200); `embedding_truncated: true` for a note the
    indexer capped at `MAX_CHUNKS_PER_NOTE` chunks (#202). Both counts are on
    the header line whether or not they are zero, for the reason
    `get_links`'s `truncated` is: an absent token is not evidence of absence.
    """
    limit = _clamp_limit(limit, _MAX_SEMANTIC_RESULTS)
    uid = current_user_id.get()
    try:
        async with async_session() as session:
            results = await semantic_search(
                session,
                query,
                limit=limit,
                folder=folder,
                tags=tags,
                frontmatter=frontmatter,
                user_id=uid,
            )
    except refusals.ProviderInputTooLarge as exc:
        # The provider refused the input against **its own** token limit,
        # which `MAX_SEARCH_QUERY_CHARS` cannot promise: 8,192 characters of a
        # densely-tokenizing script still exceed it. The caller sees the same
        # `argument_too_long` code it would have seen from the character cap,
        # carrying the provider's stated reason, so there is one actionable
        # failure mode for "the query was too large" rather than a raw
        # provider error.
        #
        # **The marker is different, and post-body.** This branch is reached
        # only after the body ran, resolved a vault and made a network round
        # trip, so `provider_input_rejected` stays out of the pre-body refusal
        # predicate: enumerating it would drop a real provider round trip out
        # of the latency percentiles. The caller-facing code and the
        # operator-facing marker answer different questions and are permitted
        # to differ — here they do.
        timing.record("error", _PROVIDER_INPUT_REJECTED_MARKER)
        return refusals.render(
            "Error: the embedding provider refused this query as too large "
            f"for its own input limit: {exc.reason} The query is under "
            f"{MAX_SEARCH_QUERY_CHARS} characters, but a character cap cannot "
            "promise a token limit. Send a shorter query.",
            refusals.Refusal(
                code=refusals.ARGUMENT_TOO_LONG,
                scope="provider",
                # **No `limit` and no `limit_unit`.** The limit that actually
                # fired is the provider's, it is a *token* limit, and this
                # server does not know its value — the provider states it in
                # prose or not at all. Quoting `MAX_SEARCH_QUERY_CHARS` here
                # (the previous shape) told a parsing agent that a
                # 8,192-character bound had been exceeded by a query that was
                # under it, which is a machine-readable falsehood: an agent
                # trimming to that number would be refused again, identically.
                # The prose still explains *why* the character cap did not
                # catch it; the payload asserts nothing it cannot support.
                limit=None,
                limit_unit=None,
            ),
        )
    if not results:
        return f"No semantic results for '{query}' (embeddings may still be building)"
    stale_count = sum(1 for r in results if r["stale"])
    truncated_count = sum(1 for r in results if r["embedding_truncated"])
    lines = [
        f"Found {len(results)} semantic matches for '{query}' — "
        f"{stale_count} stale, {truncated_count} truncated:\n"
    ]
    for r in results:
        tags_str = f" [{', '.join(r['tags'])}]" if r.get("tags") else ""
        lines.append(
            f"- **{r['title']}** (`{r['path']}`){tags_str} — "
            f"similarity: {r['similarity']:.3f}"
            f"{_degradation_suffix(r['stale'], r['embedding_truncated'])}"
        )
        # `chunk` is `None` for a stale row — the service withholds it there,
        # so no caller can obtain the superseded text at all. The substitute is
        # a notice and **never the note's current leading text**: that is a
        # different span from the one that matched, presented where the
        # matching span goes, which is a fabricated excerpt and worse than
        # none.
        if r["chunk"] is None:
            lines.append(f"  > {_STALE_PREVIEW_NOTICE}")
        else:
            lines.append(f"  > {r['chunk'][:200]}...")
    lines.extend(_degradation_footer(stale_count, truncated_count))
    return "\n".join(lines)


@_tracked("get_vault_guide", [])
async def get_vault_guide_impl() -> str:
    """Return the Obsidian primer plus any vault-specific conventions from CLAUDE.md."""
    uid = current_user_id.get()
    try:
        note = read_file("CLAUDE.md", user_id=uid)
        vault_section = (
            "# Vault-Specific Conventions\n"
            "\n"
            f"{note['content']}"
        )
    except FileNotFoundError:
        vault_section = _NO_CLAUDE_MD_MESSAGE
    except ValueError as e:
        vault_section = f"# Vault-Specific Conventions\n\n{e}"
    return f"{_VAULT_GUIDE_PRIMER}\n\n---\n\n{vault_section}"


# ══════════════════════════════════════════════════════════════════════════
# The write precondition (#205)
# ══════════════════════════════════════════════════════════════════════════
#
# **Two windows, and they are a pair.** `_atomic_write_at(expected=…)` re-reads
# the destination through the same parent descriptor immediately before the
# publishing rename, so it closes *this call's read → this call's rename*. It
# structurally cannot see a change that landed between the caller's own
# `read_note` and its `edit_note`, because the bytes it compares are the ones
# this call read. `expected_hash` closes that other window — *the caller's read
# → this call's read* — and neither subsumes the other. Both stay. What is left
# after both is the rename syscall itself, against a writer who can already
# write the destination directory: the residual `vault-tools.md` has declared
# since #59, narrowed here rather than widened.
#
# **Two helpers, not one, and the split is the design.** The syntax check is a
# pure function of the argument, so it runs at a tool's *entry* — before path
# resolution, before the leaf check, before any read — which is what makes a
# malformed hash outrank "not found", a symlinked leaf and the size cap. A
# caller told "not found" for a call whose argument was never valid fixes the
# wrong thing. The rest of the ladder needs the file, so it lives in the
# second helper and runs where the tool has read the incumbent.
#
# The ladder, in order, first match wins:
#
#   1. syntax          — not `sha256:<64 lowercase hex>`  → malformed_precondition
#   2. no incumbent    — nothing at this path to bind      → no_incumbent
#   3. unavailable     — incumbent over the tool's cap     → precondition_unavailable
#   4. required        — no hash while the deployment      → precondition_required
#                        requires one
#   5. comparison      — digest differs                    → stale_precondition
#   6. publication     — the in-call compare (elsewhere)   → concurrent_write
#
# Ordering is observable and therefore normative: the comparison runs *before*
# mode dispatch, before the result size cap, before a `dry_run` diff and before
# any no-op or defect determination, because a diff or a "no changes" answer
# computed against a base the caller does not hold is a wrong answer, not a
# cheap one.

#: The one sentence that says where a usable hash comes from — carried by
#: every refusal a read can resolve, and by `malformed_precondition`, which
#: must name where a *valid* hash comes from as well as the form it takes.
#: `no_incumbent` and `precondition_unavailable` deliberately omit it: no
#: re-read resolves either, and pointing at one would send a caller after a
#: hash that changes nothing.
_PRECONDITION_READ_HINT = (
    "`read_note` returns a note's hash as `content_hash`; "
    "`read_file(hash_only=true)` returns a raw file's."
)

#: What a caller may send. Stated in one place so every refusal spells it the
#: same way.
_PRECONDITION_CANONICAL_FORM = "sha256:<64 lowercase hex>"


def _precondition_path(path: str | None) -> str | None:
    """`path` if it can be quoted into a refusal, else `None`.

    The same bound every other path-bearing message obeys: `MAX_PATH_CHARS`
    and UTF-8-encodability. A path that fails either is refused at admission
    long before a write tool runs, so this is the belt to that braces — an
    unencodable path in a sentinel line would be a serialization failure in the
    one message whose job is to be parseable.
    """
    if path is None:
        return None
    if len(path) <= MAX_PATH_CHARS and is_encodable(path):
        return path
    return None


def _precondition_refusal(
    prose: str,
    code: str,
    *,
    path: str | None = None,
    current_hash: str | None = None,
    cap_name: str | None = None,
    cap_bytes: int | None = None,
    nothing_written: bool | None = True,
) -> str:
    """`prose` with the typed sentinel line appended.

    Prose an agent has to pattern-match is not a contract, which is the whole
    reason these refusals are typed; the prose half stays because a human reads
    it too. No refusal here carries note content — no excerpt, no diff, no
    length — and none carries a retry delay: no interval makes a stale hash
    match, a malformed one canonical, or a file smaller than a cap.
    """
    return refusals.render(
        prose,
        refusals.Refusal(
            code=code,
            path=_precondition_path(path),
            current_hash=current_hash,
            cap_name=cap_name,
            cap_bytes=cap_bytes,
            nothing_written=nothing_written,
        ),
    )


def _precondition_syntax_error(
    tool: str, expected_hash: str | None, *, path: str | None = None
) -> str | None:
    """`malformed_precondition` when `expected_hash` is not canonical.

    **A pure function of the argument.** It needs no path, no descriptor and no
    filesystem, and every guarded tool calls it at its entry — ahead of
    `open_mutable`, `_leaf_state_error` and every read — so that a caller who
    sent the wrong *kind* of value learns that, rather than learning something
    about a file its argument never validly named. `path` is quoted into the
    prose when the tool has one to quote; it is not needed to decide.

    The caller's value is deliberately **not** echoed: it is unbounded input,
    and the refusal names the accepted form instead, which is the actionable
    half.
    """
    if expected_hash is None or is_canonical_content_hash(expected_hash):
        return None
    where = f" for {path}" if _precondition_path(path) else ""
    return _precondition_refusal(
        f"{tool}: expected_hash{where} is not a content hash this server can "
        f"compare. The one accepted form is `{_PRECONDITION_CANONICAL_FORM}` — "
        "the exact value a read returns, prefix included. It was not compared "
        "against the file and nothing was written. "
        f"{_PRECONDITION_READ_HINT} (Note: `notes_metadata`'s own content hash "
        "is a different digest of different bytes and is not accepted here.)",
        refusals.MALFORMED_PRECONDITION,
        path=path,
    )


def _precondition_error(
    tool: str,
    path: str,
    incumbent: bytes | None,
    expected_hash: str | None,
    *,
    cap_name: str | None = None,
    cap_bytes: int | None = None,
    no_incumbent: bool = False,
    over_cap: bool = False,
    enforceable: bool = True,
) -> str | None:
    """The rest of the ladder. `None` means "the write may proceed".

    Arguments:
        tool: the tool's name, for the prose.
        path: the vault-relative path the refusal is about.
        incumbent: the bytes this call read for the file, or `None` when it
            read none (which must be explained by `no_incumbent` or
            `over_cap`).
        expected_hash: exactly what the caller supplied, `None` when it
            supplied nothing.
        cap_name / cap_bytes: the cap that governs this tool's reads. Required
            when `over_cap` is set — a cap without its name is a number no
            operator can act on.
        no_incumbent: this tool or mode can never bind incumbent bytes
            (`create_note`, `write_file(overwrite=False)`) or the path holds no
            file. Such calls are also **exempt from required mode**: requiring
            a hash where none can exist would make creation impossible.
        over_cap: the incumbent exists but is larger than `cap_bytes`, so no
            comparison is possible.
        enforceable: whether `WRITE_PRECONDITION_REQUIRED` applies to this
            call at all.
    """
    syntax = _precondition_syntax_error(tool, expected_hash, path=path)
    if syntax is not None:
        return syntax

    if no_incumbent:
        if expected_hash is None:
            # Exempt from required mode by construction: there is nothing here
            # a precondition could bind.
            return None
        return _precondition_refusal(
            f"{tool}: there are no existing bytes at {path} for expected_hash "
            "to bind — nothing was overwritten, so there was nothing to "
            "guard. Nothing was written. Call again without expected_hash if "
            "you meant to create this file.",
            refusals.NO_INCUMBENT,
            path=path,
        )

    required = enforceable and settings.write_precondition_required

    if over_cap:
        # "I could not check" is not "I checked and it differs", and it is not
        # "you forgot to send one" either. Under required mode this outranks
        # `precondition_required`, because telling such a caller to supply a
        # hash sends it after one it can never obtain.
        if expected_hash is None and not required:
            # The compatibility rule: an unguarded call on an over-cap file
            # behaves exactly as it does today and simply reports no hash.
            # Nothing that works now may stop working because a file is too
            # large to hash.
            return None
        return _precondition_refusal(
            f"{tool}: {path} is larger than {cap_name} ({cap_bytes:,} bytes), "
            "the most this tool may read, so its current bytes cannot be "
            "hashed and no precondition can be checked. Nothing was written. "
            f"Raising {cap_name} is an operator action; to fetch this file's "
            "bytes, use the transfer download route (`request_download`).",
            refusals.PRECONDITION_UNAVAILABLE,
            path=path,
            cap_name=cap_name,
            cap_bytes=cap_bytes,
        )

    if incumbent is None:
        # A wiring error, not a caller error: a tool that read nothing must say
        # why with `no_incumbent` or `over_cap`. Failing loudly here is the
        # only way a green-but-unwired guard is caught before it silently
        # admits every write.
        raise ValueError(
            f"{tool}: _precondition_error was given no incumbent bytes and "
            "neither no_incumbent nor over_cap — the guard would admit every "
            "write."
        )

    current_hash = content_hash_for_bytes(incumbent)

    if expected_hash is None:
        if not required:
            return None
        return _precondition_refusal(
            f"{tool}: this deployment requires every write to name the bytes "
            "it is replacing (WRITE_PRECONDITION_REQUIRED). Resend with "
            f"expected_hash. Nothing was written. {path} currently hashes to "
            f"{current_hash}, which you may send as expected_hash if nothing "
            "changes in between.",
            refusals.PRECONDITION_REQUIRED,
            path=path,
            current_hash=current_hash,
        )

    if expected_hash == current_hash:
        return None

    return _precondition_refusal(
        f"{tool}: {path} has changed since the hash you supplied was taken, so "
        "this write was refused rather than applied to bytes you have not "
        f"seen. Nothing was written. Its current content_hash is {current_hash}"
        " — re-read the file, recompute the write from its current bytes, and "
        "resend; the hash named here may be sent as expected_hash if nothing "
        f"else changes in between. {_PRECONDITION_READ_HINT}",
        refusals.STALE_PRECONDITION,
        path=path,
        current_hash=current_hash,
    )


def _concurrent_write_refusal(
    prose: str, path: str | None = None, *, nothing_written: bool | None = True
) -> str:
    """The **in-call** conflict, typed, with its prose untouched.

    `_atomic_write_at` refuses with `File changed while editing: <name>` and
    that wording stays exactly as it is — every existing `in` / `startswith`
    assertion still holds — with the sentinel appended, which is the same
    additive rule the pre-body refusals follow. The two windows then differ by
    **code** rather than by prose shape: `stale_precondition` means "the file
    moved before you called, and here is the new hash"; `concurrent_write`
    means "the file moved during my call, so no hash from before it can be
    valid — re-read and retry".

    `nothing_written` is a parameter because a post-rename failure is a
    *partial success*: `move_note`'s rename has committed, and a caller told
    "nothing was written" would go looking for a note that has already
    relocated.
    """
    return _precondition_refusal(
        prose, refusals.CONCURRENT_WRITE, path=path, nothing_written=nothing_written
    )


def _note_precondition_cap() -> tuple[str, int]:
    """The cap a *note* tool may read, by name and value."""
    return "MAX_NOTE_BYTES", MAX_NOTE_BYTES


def _file_precondition_cap() -> tuple[str, int]:
    """The cap a *raw-file* tool may read, by name and value."""
    return "MAX_FILE_READ_BYTES", settings.max_file_read_bytes


def _read_incumbent(target, path: str, cap_bytes: int) -> tuple[bytes | None, bool]:
    """The incumbent bytes at `target`, bounded. Returns `(bytes, over_cap)`.

    `(None, False)` means there is no file there; `(None, True)` means there is
    one and it is over `cap_bytes`.

    **The size is established from the descriptor the tool already holds and
    the read runs through that same descriptor** — `read_bytes_at` opens the
    leaf `O_NOFOLLOW` under the pinned parent, `fstat`s it and then reads it —
    so the bytes measured are the bytes hashed and no second pathname is
    resolved. The bound is the one that already governs that tool's content
    (`MAX_NOTE_BYTES` for a note tool, `MAX_FILE_READ_BYTES` for a raw-file
    tool): this capability adds no unbounded read anywhere.

    A tool whose path guard is not `open_mutable` — `delete_file` walks
    `vault_fs`'s beneath-root lookup — performs its own equally anchored
    bounded read and passes the result to `_precondition_error` directly.
    """
    try:
        return read_bytes_at(target, max_bytes=cap_bytes, label=path), False
    except FileNotFoundError:
        return None, False
    except ValueError:
        # `read_bytes_at` raises `ValueError` for an over-cap file and for a
        # non-regular one. The latter is already refused by `_leaf_state_error`
        # ahead of every guarded write, so what reaches here is the size — and
        # reporting "I could not hash it" for a leaf that changed shape under
        # us is the honest answer either way, since no comparison happened.
        return None, True


def _require_write() -> str | None:
    """Return an error message if the current credential lacks write permission.

    Credential-neutral on purpose: `current_permission` is set from an API
    key's `permission` *or* from an OAuth token's scope (`src/mcp_server/auth.py`),
    so naming "a readwrite API key" told an OAuth caller to go get a kind of
    credential it does not use and cannot mint.

    **The refusal is recorded here and only here** (#192, design D4). Both the
    `usage_logs` marker and the log record are written at this single
    definition, so all nine gated call sites inherit them without being
    touched; a per-caller marker would be eight chances to forget one and a
    tenth tool that silently has none. `request_download` asks
    `_mint_preflight` for `need_write=False`, never reaches this function, and
    is deliberately unaffected.

    Until this existed, the row written for a refused write was shaped
    *exactly* like a successful one, so `/admin/usage` showed a read-only
    credential apparently writing.

    `timing.record` is a no-op outside a tracked call
    (`src/services/timing.py`), so a call from a test or a future non-tool path
    records nothing and cannot raise; `_tracked` merges the holder into
    `params`. The tool name comes from the tracked context for the same reason
    the marker does — no caller signature changes — and is simply absent when
    there is no tracked call to name.
    """
    if current_permission.get() == "readwrite":
        return None
    timing.record("error", _PERMISSION_DENIED_MARKER)
    uid = current_user_id.get()
    actor = _actor_columns()
    security_events.emit(
        "tool_write_refused",
        subject=_security_subject(),
        tool=_current_tool_name.get(),
        user_id=uid,
        actor_kind=actor.get("actor_kind"),
        # Never `actor_ref` — see `_record_tool_failure`. `key_id` and
        # `oauth_token_id` were already here; the prefix added nothing but risk.
        key_id=current_api_key_id.get(),
        oauth_token_id=current_oauth_token_id.get(),
    )
    return (
        "Permission denied: this credential has read-only access. Write "
        "permission is required — a 'readwrite' API key, or an OAuth token "
        "carrying the 'readwrite' scope."
    )


def _leaf_state_error(target, path: str, *, missing: str | None = None) -> str | None:
    """Refuse a leaf that is a link, not a regular file, or (optionally) absent.

    `open_mutable` already refused a symlinked leaf; this re-checks through the
    parent descriptor immediately before the tool acts, so a leaf swapped for a
    link in the interval is *named* as such. Reporting it as a missing note
    would be worse than unhelpful: the obvious next move an agent makes is to
    create it, which writes through the link.

    `missing` is the message for an absent leaf. Omitting it means absence is
    fine — that is the creating tools (`create_note`, `write_file`), which need
    the link and non-regular refusals but must still write when nothing is
    there. Returning `None` therefore means "an ordinary regular file, or
    nothing", which is also what makes this usable as the `or`-fallback on a
    no-clobber `FileExistsError`: a link that appeared is named, a real file
    keeps the tool's own "already exists" wording.
    """
    info = target.lstat()
    if info is None:
        return missing
    if stat.S_ISLNK(info.st_mode):
        return (
            f"{path} became a symbolic link after it was validated — mutating "
            "tools act only on the named file. Nothing was changed."
        )
    if not stat.S_ISREG(info.st_mode):
        return f"{path} is not a regular file. Nothing was changed."
    return None


def _pin_source_inode(target) -> tuple[int, int] | None:
    """`(dev, ino)` of the source, pinned through an `O_PATH|O_NOFOLLOW` fd.

    Taken *before* the rename. `O_PATH` opens the directory entry without
    opening the file — it works for a symlink or a directory as happily as for
    a regular file and never has side effects — which is what makes it usable
    as a witness here: whatever is at the source when the rename runs is what
    moves, so the only way to talk about "the thing we moved" afterwards is to
    have identified it beforehand.

    `None` when the source cannot be pinned; the caller then treats the
    post-rename state as unverifiable rather than guessing.
    """
    parent_fd = target.parent_fd
    if parent_fd is None:
        return None
    flags = os.O_PATH | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(target.name, flags, dir_fd=parent_fd)
    except OSError:
        return None
    try:
        info = os.stat(fd)
        return (info.st_dev, info.st_ino)
    except OSError:
        return None
    finally:
        os.close(fd)


def _verify_the_moved_inode(
    src_target,
    dst_target,
    moved: tuple[int, int] | None,
    from_path: str,
    to_rel: str,
    *,
    permit=None,
) -> str | None:
    """Check that what arrived at the destination is our source, and a file.

    `renameat2` relocates whichever inode sits at the source when it runs —
    the property that stops a file which replaced the source from being
    destroyed — so the regular-file check made before the preflight does not
    bind the commit. Three outcomes, and the distinction between the last two
    is the point:

    * the destination is **our** inode and a regular file → the move stands;
    * the destination is **our** inode but a directory or a symlink → somebody
      swapped the source after the check and we relocated that instead. Roll
      back with a second `RENAME_NOREPLACE` (the shape
      `vault_fs._refuse_a_moved_directory` uses, so the rollback can never
      clobber whatever now holds the source name) and refuse;
    * the destination is **not** our inode, or cannot be identified → something
      landed there after our rename. Do **not** roll back: moving it away would
      relocate a third party's file on the strength of a name. Report where
      things are and let the caller look.

    Returns an error message, or `None` when the move stands. Never touches the
    database — the caller returns first — and never raises: every failure has
    to become an explicit result, because by this point the file has already
    been published somewhere and a traceback would leave the caller with no
    idea where.
    """
    try:
        info = dst_target.lstat()
    except OSError as exc:
        return (
            f"Move published but unverifiable: {from_path} was moved to "
            f"{to_rel} and the result could not be inspected ({exc}). Nothing "
            "was reindexed; check both paths before retrying."
        )
    if info is None:
        return (
            f"Move published but {to_rel} is already gone: something removed "
            f"or replaced it immediately after {from_path} was moved there. "
            "Nothing was reindexed; check both paths before retrying."
        )

    arrived = (info.st_dev, info.st_ino)
    if moved is not None and arrived != moved:
        return (
            f"Move published but {to_rel} is not the file that was moved: "
            "something else took that name immediately afterwards. Nothing was "
            "reindexed and nothing was moved back — check both paths before "
            "retrying."
        )
    if moved is None:
        return (
            f"Move published but unverifiable: {from_path} could not be "
            f"identified before it was moved to {to_rel}. Nothing was "
            "reindexed; check both paths before retrying."
        )
    if stat.S_ISREG(info.st_mode):
        return None

    kind = (
        "a directory"
        if stat.S_ISDIR(info.st_mode)
        else "a symbolic link"
        if stat.S_ISLNK(info.st_mode)
        else "not a regular file"
    )
    try:
        # The rollback is authorised by the permit the forward move returned —
        # a licence to undo exactly that move and nothing else (#88). It is not
        # a second confirmation: it undoes the very publication the
        # confirmation covered, synchronously, inside that same window.
        move_file_no_clobber(dst_target, src_target, permit=permit)
    except Exception as exc:
        return (
            f"Move refused: {from_path} was replaced by {kind} after it was "
            f"checked, and it could not be moved back ({exc}). It is now at "
            f"{to_rel} — restore it from there. Nothing was reindexed."
        )
    return (
        f"Move refused: {from_path} was replaced by {kind} after it was "
        "checked. It was moved back and nothing was reindexed."
    )


def _note_size_error_for(size: int) -> str | None:
    """`_note_size_error` for a caller that already knows the encoded length.

    Split out so a path that must encode the content anyway (the `move_note`
    preflight, which also sums the encoded lengths) can reuse that one encode
    instead of paying for a second one inside the check.
    """
    if size > MAX_NOTE_BYTES:
        return f"Content too large ({size} bytes, max {MAX_NOTE_BYTES})"
    return None


def _note_size_error(content: str) -> str | None:
    """Refuse a note write whose *result* would exceed `MAX_NOTE_BYTES`.

    Every note write tool applies this to the content it is about to write, so
    a supported write is always decided here — with an actionable message —
    rather than by the MCP transport's body limit, which sits well above it.
    """
    return _note_size_error_for(len(content.encode("utf-8")))


def _frontmatter_defect_error(tool: str, path: str, diagnosis) -> str:
    """Refuse a write over a defective frontmatter block, naming the defect.

    The two callers refuse for different immediate reasons and share this text
    because the caller's next move is the same in both: `set_frontmatter`
    would otherwise prepend a *second* block above the broken one and report
    success, and a section write would otherwise resolve headings over raw
    bytes where a YAML `#` comment is selectable and a replacement can delete
    the closing fence.

    The repair named is `edit_note(replace_frontmatter=True)` — the one mode
    that replaces the block wholesale — because nothing else in the tool
    surface can rewrite a block that does not parse.
    """
    return (
        f"{tool}: {path} has a malformed frontmatter block — "
        f"{diagnosis.message}. Nothing was written. Read the note, then repair "
        "the whole file with `edit_note(path, content=<complete note text>, "
        "replace_frontmatter=True)`, which replaces the frontmatter block "
        "along with the body."
    )


def _note_unmatched_openers(scan_text: str, diagnosis=None):
    """Unmatched indented fence openers in `scan_text`, positioned on the file.

    `scan_text` is what the write path resolves over — the frontmatter-stripped
    body when the note carries a valid block — so the recognizer runs in `BODY`
    context (it must never re-partition a body whose own first line is `---`)
    and the reported position is then re-based onto the whole file, which is
    the only coordinate the caller can act on.
    """
    from src.services.links import BODY, unmatched_indented_openers

    openers = unmatched_indented_openers(scan_text, context=BODY)
    if not openers or diagnosis is None or not getattr(diagnosis, "valid", False):
        return openers
    block = diagnosis.block
    return tuple(
        o.shifted(chars=len(block), lines=len(_LINE_BREAK_SPLIT_RE.findall(block)))
        for o in openers
    )


_LINE_BREAK_SPLIT_RE = re.compile(r"\r\n|\n|\r")


def _scan_rewrite_source(content: str):
    """The one fence scan a `move_note` rewrite source gets.

    `FULL_NOTE`, unlike the section-write guard: a rewrite is spliced into the
    source's raw bytes, so the frontmatter block is part of the scanned text
    and must be held out of fence recognition rather than stripped first.

    Returned whole rather than reduced to its unmatched openers, because the
    caller needs the masked text too and the recognizer's contract is that the
    frontmatter partition runs **at most once per note**.
    """
    from src.services.links import FULL_NOTE, scan_fences

    return scan_fences(content, context=FULL_NOTE)




def _unmatched_fence_error(path: str, scan_text: str, diagnosis=None) -> str | None:
    """Refuse a section write over an undecidable fence, naming the opener.

    `None` when the note has none, so the caller can use it as a guard. The
    refusal is deliberately asymmetric with reads, exactly as the defective-
    frontmatter refusal is: reads destroy nothing, so `read_note(section=…)`
    and the truncation outline keep working under the not-a-fence reading,
    and the guarantee on such a note is the refusal rather than the round trip.
    """
    openers = _note_unmatched_openers(scan_text, diagnosis)
    if not openers:
        return None
    where = "; ".join(o.describe() for o in openers)
    return (
        f"edit_note: {path} contains an indented fence opener that nothing "
        f"below it closes — {where}. Nothing was written. A fence indented by "
        "one to three spaces may be inside a list item, whose code block ends "
        "where the item does; this server does not parse container blocks, so "
        "it cannot tell whether the text below the opener is code or content, "
        "and a section write there would either split the block or replace "
        "real content. Close the fence (or unindent it to column zero), then "
        "reissue the section write. `read_note(path, section=...)` still "
        "works, and a whole-note `edit_note` without `section=` is unaffected."
    )


# ────────────────────────────────────────────────────────────────────────────
# Note-write preconditions (#205)
#
# The caller-visible half of the pair `vault-tools.md` documents: `expected=`
# closes *this call's* read→rename window, `expected_hash` closes the caller's
# read→this call's read window. Neither subsumes the other, so both run.
# ────────────────────────────────────────────────────────────────────────────


#: `_atomic_write_at`'s in-call conflict, by its opening words. The prose is
#: unchanged by this change (#205 D3) — every existing `in`/`startswith`
#: assertion still holds — so recognising it is a prefix test, and the typed
#: `concurrent_write` sentinel is appended to whatever it said.
_IN_CALL_CONFLICT_PREFIX = "File changed while editing:"


def _is_in_call_conflict(exc: BaseException) -> bool:
    """Is `exc` the publish-time comparison refusing, rather than an I/O error?

    The two are told apart because they mean different things to an agent:
    a conflict says "re-read and retry", an `OSError` says "this write did not
    happen for a reason retrying will not fix". `_atomic_write_at` raises the
    conflict as a `RuntimeError` carrying `File changed while editing: <name>`.
    """
    return isinstance(exc, RuntimeError) and str(exc).startswith(
        _IN_CALL_CONFLICT_PREFIX
    )


def _published_hash_clause(data: bytes) -> str:
    """The `content_hash` a successful note write ends its result with (D9).

    The value describes **the bytes this call published**, not whatever is on
    disk by the time the caller reads the message; the docstrings say so. It is
    appended rather than interpolated so the existing result prose — which
    plenty of callers and tests match with `in` / `startswith` — is unchanged
    ahead of it.
    """
    return f" — content_hash: {content_hash_for_bytes(data)}"


def _hash_unavailable_clause(path: str) -> str:
    """Why a successful write reported no hash: the file is over the note cap.

    A call that has already succeeded must not be failed merely because its
    result is too large to hash (design D4, L16), so the hash is omitted and
    the reason named.
    """
    cap_name, cap_bytes = _note_precondition_cap()
    return (
        f"content_hash not reported: {path} is larger than {cap_name} "
        f"({cap_bytes:,} bytes), the most this tool may read back"
    )


def _move_precondition_error(
    from_path: str,
    incumbent: bytes | None,
    expected_hash: str | None,
    *,
    over_cap: bool = False,
) -> str | None:
    """`_precondition_error` for `move_note`, with the binding scope spelled out.

    A `move_note` precondition binds **`from_path`'s own bytes and nothing
    else** (design D8, L3). The backlink sources a `rewrite_links=True` move
    rewrites are unbound because the caller never read them, and a precondition
    that covered one of N files while implying all of them would be worse than
    none — so the refusal says it, rather than leaving an agent to infer the
    scope from a tool name.

    The sentence goes into the **prose** half: the sentinel is a final,
    line-initial single line by contract, so it stays last.
    """
    cap_name, cap_bytes = _note_precondition_cap()
    err = _precondition_error(
        "move_note",
        from_path,
        incumbent,
        expected_hash,
        cap_name=cap_name,
        cap_bytes=cap_bytes,
        over_cap=over_cap,
    )
    if err is None:
        return None
    scope = (
        " (A move_note precondition binds this note's own bytes only: the "
        "backlink sources a rewrite_links=True move would rewrite are not "
        "bound, because you never read them.)"
    )
    prose, newline, line = err.rpartition("\n")
    if not newline or not refusals.has_sentinel(err):
        # Defensive: the renderer always appends the sentinel on its own final
        # line, and if that ever stops being true the scope sentence still gets
        # said rather than being silently dropped into the machine-readable
        # half.
        return f"{err}{scope}"
    return f"{prose}{scope}\n{line}"


@_tracked("create_note", ["path", "expected_hash"], write_class=True)
async def create_note_impl(
    path: str, content: str, expected_hash: str | None = None
) -> str:
    """Create a new note in the vault.

    Accepts `expected_hash` and can never honour it: there are no incumbent
    bytes at a path this tool is willing to write, so a supplied hash is
    `no_incumbent`, answered **before any filesystem work**. The argument is in
    the signature deliberately — a signature that rejected it would answer with
    a protocol-level argument error instead of the typed refusal #205 promises
    — and a malformed hash still outranks it, so a caller that sent the wrong
    *kind* of value learns that first. Exempt from `WRITE_PRECONDITION_REQUIRED`
    for the same reason: requiring a hash where none can exist would make
    creation impossible.

    Confirms the caller's vault assignment immediately before it
    publishes (#88). That narrows the window in which an administrator's
    reassignment can be missed to staging, the durability flush and one
    publishing call — it does not close it, the same optimistic guarantee
    the system declares for `edit_note(expected=…)`.
    """
    if err := _require_write():
        return err
    if not path.endswith(".md"):
        path += ".md"
    # The tool's entry, ahead of `open_mutable` and every read: a malformed
    # hash is a pure function of the argument and must win over "not found"
    # and over a symlinked leaf. `.md` is appended first because it is a
    # string normalisation, not a resolution, and both refusals should name
    # the same path.
    if err := _precondition_syntax_error("create_note", expected_hash, path=path):
        return err
    # Still before any filesystem work. `enforceable=False` states the required
    # -mode exemption at the call site rather than leaving it to be inferred.
    if err := _precondition_error(
        "create_note",
        path,
        None,
        expected_hash,
        no_incumbent=True,
        enforceable=False,
    ):
        return err
    uid = current_user_id.get()
    # Validate before the size check: a caller naming an alias should learn
    # that the path is a symlink, not that its content is too big — the second
    # message sends it off to trim content that was never the problem.
    try:
        target = open_mutable(path, user_id=uid)
    except ValueError as e:
        return str(e)
    with target:
        if err := _note_size_error(content):
            return err
        # #88, immediately before the publication and after every refusal that
        # writes nothing: the assignment this request was admitted for must
        # still be the caller's. The confirming read and the write happen in
        # one uninterrupted step — no `await` of this tool's can sit between
        # them. A symlinked leaf is still named as one: the no-clobber `link`
        # refuses it and `_leaf_state_error` says so.
        try:
            err, _ = await _confirmed_publication(
                uid,
                lambda c: write_file_at(
                    target, content, overwrite=False, confirmation=c
                ),
            )
            if err:
                return err
            # D9: the bytes this call published, which `_atomic_write_at`
            # encoded exactly this way.
            return f"Created note: {path}" + _published_hash_clause(
                content.encode("utf-8")
            )
        except FileExistsError:
            # No-clobber `link` refuses a plain file, a directory *and* a
            # symlink identically, so the bare "already exists" would hide a
            # leaf that turned into a link after validation.
            return _leaf_state_error(target, path) or (
                f"Note already exists: {path}. Use edit_note to modify it."
            )
        except (ValueError, vault_fs.VaultFSError) as e:
            return str(e)
        except OSError as e:
            return f"Failed to write {path}: {e}"


# Row-level excerpt bound for the graph tools. `link_text` is note text the
# indexer stored verbatim — a wikilink alias is caller-controlled and can be
# thousands of characters — and a tool result is model input, so a row is
# bounded the same way the response as a whole is. Newlines collapse because a
# multi-line alias would otherwise break the bullet list it sits in.
_LINK_EXCERPT_CHARS = 120


def _link_excerpt(link_text: str | None) -> str:
    """`link_text` flattened to one line and clipped, with an ellipsis when
    it was actually clipped (silent truncation reads as the note's real
    text)."""
    flat = (link_text or "").replace("\n", " ")
    if len(flat) <= _LINK_EXCERPT_CHARS:
        return flat
    return flat[:_LINK_EXCERPT_CHARS] + "…"


@_tracked("get_backlinks", ["path", "limit"])
async def get_backlinks_impl(path: str, limit: int = 50) -> str:
    """Notes that link TO `path` (resolved links only)."""
    from sqlalchemy import and_, select
    from src.models.db import NoteLink, NoteMetadata

    uid = current_user_id.get()
    limit = max(1, min(limit, 500))
    async with async_session() as session:
        target_stmt = select(NoteMetadata).where(
            NoteMetadata.file_path == path, _note_owner_predicate(uid)
        )
        target = (await session.execute(target_stmt)).scalar_one_or_none()
        if target is None:
            return f"Note not found: {path}"

        SourceMeta = NoteMetadata
        # The owner predicate rides the JOIN: a link row whose *source* is
        # another owner's note resolves to nothing here, so neither its title
        # nor its path can be printed. `note_links` carries no `user_id` of its
        # own, which is why the scope has to be expressed through the endpoint
        # rows (#127, D1).
        stmt = (
            select(
                SourceMeta.file_path,
                SourceMeta.title,
                NoteLink.link_text,
                NoteLink.position,
                NoteLink.kind,
            )
            .join(
                SourceMeta,
                and_(
                    NoteLink.source_note_id == SourceMeta.id,
                    _owner_predicate_for(SourceMeta, uid),
                ),
            )
            .where(NoteLink.target_note_id == target.id)
            .order_by(SourceMeta.file_path, NoteLink.position)
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()

    if not rows:
        return f"No backlinks to `{path}`"
    lines = [f"Found {len(rows)} backlinks to `{path}`:\n"]
    for r in rows:
        excerpt = (r.link_text or "").replace("\n", " ")[:120]
        lines.append(
            f"- **{r.title}** (`{r.file_path}`) — {r.kind} `{excerpt}` @ pos {r.position}"
        )
    return "\n".join(lines)


@_tracked("get_links", ["path", "limit"])
async def get_links_impl(path: str, limit: int = 100) -> str:
    """Outgoing links from `path` — both resolved and dangling.

    Reports `truncated: true` when the indexer capped this note's link
    extraction at `MAX_LINKS_PER_NOTE`, so a capped set is never read as a
    complete one (#203).

    **Bounded like every other list tool.** `get_backlinks` and
    `get_neighborhood` have always clamped; this one selected every row a note
    had, which for a note the indexer capped is up to `MAX_LINKS_PER_NOTE`
    (10,000) rows rendered into one tool result — the answer an agent least
    wants and the payload the read caps exist to prevent.

    **The default is below the hard cap on purpose.** A default equal to the
    cap makes the over-limit notice's "raise `limit`" advice unactionable —
    the caller is already at the ceiling — so the notice would be telling an
    agent to retry a call that cannot return anything new. 100 leaves that
    advice a real move, and the notice says plainly that rows past 500 are
    not reachable through this tool at all.
    """
    from sqlalchemy import and_, func, or_, select
    from sqlalchemy.orm import aliased
    from src.models.db import NoteLink, NoteMetadata

    uid = current_user_id.get()
    limit = max(1, min(limit, 500))
    async with async_session() as session:
        src_stmt = select(NoteMetadata).where(
            NoteMetadata.file_path == path, _note_owner_predicate(uid)
        )
        source = (await session.execute(src_stmt)).scalar_one_or_none()
        if source is None:
            return f"Note not found: {path}"

        TargetMeta = aliased(NoteMetadata)
        # The owner predicate is part of the outer join's ON clause, not a
        # WHERE on the joined row: as a WHERE it would discard every dangling
        # link (`target_note_id IS NULL` joins to a NULL row), which this tool
        # exists to report. In the ON clause a cross-owner target simply fails
        # to resolve, and the link is reported by the `target_path` string
        # stored on the caller's *own* note — nothing of the other owner's row
        # is read (#127, D1).
        resolved_id = TargetMeta.id.label("resolved_id")
        stmt = (
            select(
                NoteLink.kind,
                NoteLink.link_text,
                NoteLink.position,
                NoteLink.target_path,
                resolved_id,
                TargetMeta.file_path,
                TargetMeta.title,
            )
            .outerjoin(
                TargetMeta,
                and_(
                    NoteLink.target_note_id == TargetMeta.id,
                    _owner_predicate_for(TargetMeta, uid),
                ),
            )
            .where(
                NoteLink.source_note_id == source.id,
                # Three cases, not two. A row with no `target_note_id` is
                # genuinely dangling and is reported — that is why the owner
                # predicate is in the ON clause above and not here. A row that
                # resolved inside the owned set is reported. A row that names a
                # `target_note_id` outside it is **omitted**: it is not
                # dangling, and reporting it would print the other owner's path
                # (a state the per-user link resolution does not produce, so
                # this is the corrupted/adversarial path).
                or_(
                    NoteLink.target_note_id.is_(None),
                    TargetMeta.id.isnot(None),
                ),
            )
            .order_by(NoteLink.position)
            # One over the limit, so "there are more" is read off the result
            # rather than guessed from a full page.
            .limit(limit + 1)
        )
        rows = (await session.execute(stmt)).all()
        over_limit = len(rows) > limit
        if over_limit:
            rows = rows[:limit]

        # **How many rows the pass actually persisted for this note**, counted
        # only when a notice is going to quote it. `len(rows)` is not that
        # number: the scoped join above omits any row that resolved to a note
        # outside the owned set, so a notice built from it would tell the
        # caller "the N above are the first in document order" about an N that
        # is neither the page size nor the persisted total.
        persisted: int | None = None
        if over_limit or source.links_truncated:
            persisted = await session.scalar(
                select(func.count())
                .select_from(NoteLink)
                .where(NoteLink.source_note_id == source.id)
            )

    # Read off the note row the pass wrote it on, never inferred from the row
    # count: a capped set is exactly `MAX_LINKS_PER_NOTE` rows and looks like
    # any other complete set from here. An agent that read a truncated set as
    # complete would act on a graph answer that is silently wrong, which is
    # what the column exists to prevent (#203).
    truncated = "true" if source.links_truncated else "false"

    if not rows:
        return f"`{path}` has no outgoing links — truncated: {truncated}"
    # Classified by what the *scoped* join resolved, never by the raw
    # `note_links.target_note_id`: that column can name a row outside the
    # owned set, and calling such a link "resolved" would print a `None` title
    # and path for it.
    resolved = [r for r in rows if r.resolved_id is not None]
    dangling = [r for r in rows if r.resolved_id is None]
    lines = [
        f"`{path}` — {len(resolved)} resolved, {len(dangling)} dangling — "
        f"truncated: {truncated}:\n"
    ]
    if resolved:
        lines.append("**Resolved:**")
        for r in resolved:
            lines.append(
                f"- {r.kind} → **{r.title}** (`{r.file_path}`) — "
                f"`{_link_excerpt(r.link_text)}`"
            )
    if dangling:
        lines.append("\n**Dangling:**")
        for r in dangling:
            lines.append(
                f"- {r.kind} → `{r.target_path}` — `{_link_excerpt(r.link_text)}`"
            )
    if over_limit:
        # "Raise `limit`" only while raising it can do something. At the hard
        # cap it cannot, and the remainder is stated as unreachable rather
        # than dressed up as a paging step the caller could take.
        advice = "Raise `limit` (hard cap 500) to see more. " if limit < 500 else ""
        lines.append(
            f"\n… showing {len(rows)} of {persisted:,} link rows persisted for "
            f"this note (limit={limit}, hard cap 500), in document order. "
            f"{advice}Rows past the first 500 are NOT reachable through this "
            "tool — read the note itself if you need them."
        )
    if source.links_truncated:
        lines.append(
            f"\n**This note's link extraction was capped at "
            f"{MAX_LINKS_PER_NOTE} links. The {persisted:,} rows persisted for "
            "it are the first in document order and the set is INCOMPLETE** — "
            "do not treat it as the note's full outgoing-link set."
        )
    return "\n".join(lines)


@_tracked("get_neighborhood", ["path", "depth", "limit"])
async def get_neighborhood_impl(path: str, depth: int = 1, limit: int = 50) -> str:
    """BFS over the resolved-link graph treating links as undirected."""
    from sqlalchemy import and_, or_, select
    from sqlalchemy.orm import aliased
    from src.models.db import NoteLink, NoteMetadata

    uid = current_user_id.get()
    depth = max(1, min(depth, 5))
    limit = max(1, min(limit, 200))

    async with async_session() as session:
        src_stmt = select(NoteMetadata).where(
            NoteMetadata.file_path == path, _note_owner_predicate(uid)
        )
        source = (await session.execute(src_stmt)).scalar_one_or_none()
        if source is None:
            return f"Note not found: {path}"

        # BFS state.
        seen: dict[int, dict] = {source.id: {"distance": 0, "via": None}}
        frontier: list[int] = [source.id]
        truncated = False

        for d in range(1, depth + 1):
            if not frontier:
                break
            # **Both endpoints must be inside the owned set**, and that has
            # to happen here rather than when the metadata is hydrated below.
            # A cross-owner edge admitted into the traversal changes what the
            # answer *is*, not merely what is printed: it occupies a slot in
            # `seen` (so it counts against `limit` and can truncate the walk
            # early), and it can act as a bridge — a foreign note linking two
            # owned notes would make them distance-2 neighbours through a row
            # the caller cannot see. Dropping it at hydration time is too late
            # for both (#127, D1).
            SrcMeta = aliased(NoteMetadata)
            TgtMeta = aliased(NoteMetadata)
            stmt = (
                select(
                    NoteLink.source_note_id,
                    NoteLink.target_note_id,
                )
                .join(
                    SrcMeta,
                    and_(
                        NoteLink.source_note_id == SrcMeta.id,
                        _owner_predicate_for(SrcMeta, uid),
                    ),
                )
                .join(
                    TgtMeta,
                    and_(
                        NoteLink.target_note_id == TgtMeta.id,
                        _owner_predicate_for(TgtMeta, uid),
                    ),
                )
                .where(
                    or_(
                        NoteLink.source_note_id.in_(frontier),
                        NoteLink.target_note_id.in_(frontier),
                    ),
                    NoteLink.target_note_id.isnot(None),
                )
            )
            edges = (await session.execute(stmt)).all()
            next_frontier: list[int] = []
            for src_id, tgt_id in edges:
                # Walk both directions.
                for from_id, to_id in ((src_id, tgt_id), (tgt_id, src_id)):
                    if from_id in seen and to_id not in seen:
                        seen[to_id] = {"distance": d, "via": from_id}
                        next_frontier.append(to_id)
                        if len(seen) - 1 >= limit:
                            truncated = True
                            break
                if truncated:
                    break
            frontier = next_frontier
            if truncated:
                break

        # Hydrate metadata for everything except the source. The BFS edges are
        # already closed over the owned set by the joins above; this repeats
        # the predicate as defence in depth, on the same total mapping.
        ids = [nid for nid in seen if nid != source.id]
        if not ids:
            return f"`{path}` has no resolved-link neighbors"
        meta_stmt = select(NoteMetadata).where(
            NoteMetadata.id.in_(ids), _note_owner_predicate(uid)
        )
        meta_rows = (await session.execute(meta_stmt)).scalars().all()
        meta_by_id = {m.id: m for m in meta_rows}
        # Drop any ids that the user_id filter excluded (shouldn't happen
        # under normal operation but keeps the output consistent).
        ids = [i for i in ids if i in meta_by_id]
        if not ids:
            return f"`{path}` has no resolved-link neighbors"
        # We also need `via` paths — fetch those.
        via_ids = {seen[nid]["via"] for nid in ids if seen[nid]["via"] is not None}
        via_paths = {source.id: source.file_path}
        if via_ids - {source.id}:
            via_stmt = select(NoteMetadata.id, NoteMetadata.file_path).where(
                NoteMetadata.id.in_(via_ids), _note_owner_predicate(uid)
            )
            via_rows = (await session.execute(via_stmt)).all()
            for vid, vpath in via_rows:
                via_paths[vid] = vpath

    ordered = sorted(ids, key=lambda nid: (seen[nid]["distance"], meta_by_id[nid].file_path))
    lines = [
        f"Neighborhood of `{path}` (depth ≤ {depth}, {len(ordered)} notes"
        + (", truncated" if truncated else "") + "):\n"
    ]
    for nid in ordered:
        m = meta_by_id[nid]
        info = seen[nid]
        via_path = via_paths.get(info["via"], "?")
        tags_str = f" [{', '.join(m.tags)}]" if m.tags else ""
        lines.append(
            f"- d={info['distance']} **{m.title}** (`{m.file_path}`){tags_str} via `{via_path}`"
        )
    return "\n".join(lines)


def find_related_stmt(source_id: int, avg_embedding: list[float], user_id: int | None,
                      limit: int):
    """The vector statement `find_related` runs, and its overfetch.

    Factored out of `find_related_impl` so the recall benchmark in
    `tests/integration/test_search_recall.py` can EXPLAIN and re-run *this*
    statement rather than a hand-copied lookalike — a benchmark that measures a
    query production does not issue measures nothing.

    The three metadata columns the degradation markers read (#200, #202) are
    **projected, never filtered on**: they are scalar columns of a table the
    statement already joins, so they change no plan and the benchmark's EXPLAIN
    assertions hold unchanged. A staleness predicate was rejected outright —
    see `semantic_search`.
    """
    from sqlalchemy import select
    from src.models.db import NoteEmbedding, NoteMetadata

    # Pull more than `limit` so we can dedupe by note. Same overfetch as
    # semantic_search so both vector paths share one recall contract.
    overfetch = max(limit * 5, 50)
    distance = NoteEmbedding.embedding.cosine_distance(avg_embedding)
    stmt = (
        select(
            NoteEmbedding.note_id,
            NoteEmbedding.chunk_text,
            NoteMetadata.file_path,
            NoteMetadata.title,
            NoteMetadata.tags,
            NoteMetadata.content_hash,
            NoteMetadata.embedded_content_hash,
            NoteMetadata.chunks_truncated,
            distance.label("distance"),
        )
        .join(NoteMetadata, NoteEmbedding.note_id == NoteMetadata.id)
        .where(NoteEmbedding.note_id != source_id)
    )
    # Total mapping, as everywhere else on the read path (#127, D1): `None`
    # scopes to the NULL-owned slice rather than to everything. This is also
    # why the caller's zero-row exact fallback is unconditional — there is no
    # unfiltered form of this statement left (D1a).
    stmt = stmt.where(_note_owner_predicate(user_id))
    return stmt.order_by(distance).limit(overfetch)


@_tracked("find_related", ["path", "limit"])
async def find_related_impl(path: str, limit: int = 10) -> str:
    """Semantic neighbors via averaged chunk embeddings.

    Carries `semantic_search`'s per-row `stale` / `embedding_truncated`
    markers, and one more that only this tool can have: **the source note can
    itself be stale**, in which case the averaged query vector describes the
    source's *previous* content and every neighbour answers a superseded
    question. No per-row flag can express that, so it is stated once, on every
    return path where the source row was loaded — the empty one included.
    """
    import numpy as np
    from sqlalchemy import select
    from src.models.db import NoteEmbedding, NoteMetadata

    uid = current_user_id.get()
    limit = max(1, min(limit, 50))

    # The grouping key for this tool's analytics, recorded before anything can
    # return: every row this call writes — the two failures below included —
    # has to name the source it was asked about, or the failures cannot be
    # attributed to a note. The named `path` param is truncated at 200
    # characters and would collapse distinct long paths onto one row.
    timing.record_source_path(path)

    async with async_session() as session:
        # `db_ms` covers every database phase of this tool, the source-chunk
        # fetch included — it is accumulated, so the early returns below still
        # report the work they actually did.
        db_start = time.monotonic()
        src_stmt = select(NoteMetadata).where(
            NoteMetadata.file_path == path, _note_owner_predicate(uid)
        )
        source = (await session.execute(src_stmt)).scalar_one_or_none()
        if source is None:
            timing.add_ms("db_ms", time.monotonic() - db_start)
            # Marked, and marked *distinctly*: this call never reached a
            # vector query, so it is an operational failure and not a
            # zero-result. `result_count` is still recorded so the key's type
            # is uniform across every row this tool writes — the analytics
            # page excludes the row on the marker, not on a missing key.
            timing.record("error", _RELATED_SOURCE_NOT_FOUND_MARKER)
            timing.record_results(())
            return f"Note not found: {path}"

        # `IS DISTINCT FROM`, so a NULL `embedded_content_hash` is stale rather
        # than NULL-propagating into "fresh" — Python's `!=` against `None` is
        # already that operator. Computed here, from the row every return path
        # below has in hand, so no path can quietly omit it.
        source_stale = source.embedded_content_hash != source.content_hash

        chunks = (await session.execute(
            select(NoteEmbedding.embedding).where(NoteEmbedding.note_id == source.id)
        )).scalars().all()
        timing.add_ms("db_ms", time.monotonic() - db_start)
        if not chunks:
            # The other operational failure: the note is there, the embed pass
            # has not reached it. A fact about the indexer, not about the
            # vault's contents, so it carries its own marker and stays out of
            # the zero-result view.
            timing.record("error", _RELATED_SOURCE_NOT_EMBEDDED_MARKER)
            timing.record_results(())
            return (
                f"`{path}` has not been embedded yet — "
                "the indexer is still catching up. Try again in a few minutes."
            )

        # The query vector: the mean of this note's own chunk vectors. NumPy is
        # still the right tool here (pgvector returns plain lists); what moved
        # to the database is the *scoring*, below.
        avg_list = np.mean(
            [np.asarray(c, dtype=float) for c in chunks], axis=0
        ).tolist()

        vector_start = time.monotonic()
        # Same HNSW tuning as semantic_search — see embeddings.py for the
        # full rationale, including why iterative_scan is what keeps a
        # filtered vector query from silently coming back empty. This query is
        # always filtered (`note_id != source.id`, plus the user scope), so it
        # is exposed to exactly the same post-filter candidate loss.
        await session.execute(text("SET LOCAL hnsw.ef_search = 80"))
        await session.execute(text("SET LOCAL random_page_cost = 1.1"))
        await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))

        stmt = find_related_stmt(source.id, avg_list, uid, limit)
        rows = (await session.execute(stmt)).all()

        # Zero-row exact fallback, as in semantic_search: an empty result from
        # an approximate filtered scan is ambiguous, so re-run the identical
        # statement as an exact sequential scan before believing it. It is
        # unconditional here and there (#127, D1a) — the owner predicate is
        # itself a filter, so there is no unfiltered form of this query.
        exact_fallback = False
        if not rows:
            # Transaction-scoped, like every other SET LOCAL here: it applies
            # to the re-run below and dies with this transaction. The session
            # closes immediately after the re-sort, so nothing else in this
            # call can inherit the exact plan — do not append further
            # statements to this block without re-reading that.
            await session.execute(text("SET LOCAL enable_indexscan = off"))
            rows = (await session.execute(stmt)).all()
            exact_fallback = True
        timing.record("exact_fallback", exact_fallback)
        timing.add_ms("db_ms", time.monotonic() - vector_start)

        # `relaxed_order` does not promise a globally sorted stream; re-sort
        # before dedupe so the presented order is monotone in distance.
        rows = sorted(rows, key=lambda r: r.distance)

    if not rows:
        # A **true** zero-result: the source exists, it is embedded, the exact
        # fallback has already re-run the query, and the vault holds nothing
        # near it. No marker — this is precisely the row the zero-result view
        # is built to count.
        timing.record_results(())
        # **The empty result is where the stale-source line matters most.** A
        # bare "no related notes" from a stale source is the reading a caller
        # acts on — that the note has no neighbours — when the truth is that
        # the vector searched with describes content the note no longer has.
        # A first draft put this line only above a non-empty list, which loses
        # it exactly where it explains the most.
        empty = [f"No related notes for `{path}`"]
        if source_stale:
            empty.append(_stale_source_line(path))
        return "\n".join(empty)

    # Dedupe by note_id, keeping the nearest chunk — ranked by the *same*
    # cosine distance the database ordered by, never by a distance recomputed
    # here. pgvector compares float32 vectors; NumPy would recompute in
    # float64 from the round-tripped values and order near-ties differently,
    # so a recomputed ranking could invert two rows relative to the ORDER BY
    # that selected them (and relative to the recall baseline). `similarity`
    # is the cosine similarity that distance encodes: `1 - distance`.
    best: dict[int, dict] = {}
    for r in rows:
        dist = float(r.distance)
        prev = best.get(r.note_id)
        if prev is None or dist < prev["distance"]:
            stale = r.embedded_content_hash != r.content_hash
            best[r.note_id] = {
                "path": r.file_path,
                "title": r.title,
                "tags": r.tags,
                "distance": dist,
                # Withheld for a stale row, exactly as `semantic_search` does
                # it: the chunk is the one field that is a verbatim quotation
                # of the note's text and the one a caller reproduces in an
                # answer. Both vector tools must behave the same way, or the
                # remedy depends on which tool the caller happened to reach
                # for.
                "chunk": None if stale else r.chunk_text,
                "stale": stale,
                # From the durable column, never inferred from the number of
                # chunk rows: a capped note holds exactly the cap.
                "embedding_truncated": bool(r.chunks_truncated),
            }

    ranked = sorted(best.values(), key=lambda x: x["distance"])[:limit]
    # After the dedupe and the truncation to `limit`: the telemetry names what
    # the caller was handed, not what the overfetch scanned.
    timing.record_results(r["path"] for r in ranked)
    stale_count = sum(1 for r in ranked if r["stale"])
    truncated_count = sum(1 for r in ranked if r["embedding_truncated"])
    lines = [
        f"Top {len(ranked)} related notes for `{path}` — "
        f"{stale_count} stale, {truncated_count} truncated:\n"
    ]
    if source_stale:
        lines.append(_stale_source_line(path) + "\n")
    for r in ranked:
        tags_str = f" [{', '.join(r['tags'])}]" if r["tags"] else ""
        lines.append(
            f"- **{r['title']}** (`{r['path']}`){tags_str} — "
            f"sim: {1 - r['distance']:.3f}"
            f"{_degradation_suffix(r['stale'], r['embedding_truncated'])}"
        )
        if r["chunk"] is None:
            lines.append(f"  > {_STALE_PREVIEW_NOTICE}")
        else:
            snippet = r["chunk"].replace("\n", " ")[:200]
            lines.append(f"  > {snippet}…")
    lines.extend(_degradation_footer(stale_count, truncated_count))
    return "\n".join(lines)


@_tracked("find_orphans", ["folder", "limit"])
async def find_orphans_impl(folder: str | None = None, limit: int = 50) -> str:
    """Notes with zero incoming AND zero outgoing resolved links."""
    from sqlalchemy import and_, or_, select, union
    from src.models.db import NoteLink, NoteMetadata

    uid = current_user_id.get()
    limit = max(1, min(limit, 500))

    async with async_session() as session:
        # The "connected" subquery collects every NoteLink endpoint id, and it
        # is closed over the owned set before it does so. `note_links` carries
        # no `user_id`, and the outer filter on `notes_metadata` is not enough:
        # a *foreign* note linking to an owned one puts the owned id into
        # `targets` and silently strips its orphan status — an edge the caller
        # cannot see deciding an answer about a note they own. So an edge
        # counts only when its source is owned **and** its target is either
        # owned or genuinely dangling (#127, D1).
        #
        # Keeping dangling edges in is today's behaviour, deliberately: a note
        # whose only link points at nothing is not an orphan here, and that is
        # unrelated to ownership.
        owned_ids = select(NoteMetadata.id).where(_note_owner_predicate(uid))
        edge_within_owned_set = and_(
            NoteLink.source_note_id.in_(owned_ids),
            or_(
                NoteLink.target_note_id.is_(None),
                NoteLink.target_note_id.in_(owned_ids),
            ),
        )
        sources = select(NoteLink.source_note_id.label("nid")).where(
            NoteLink.source_note_id.isnot(None), edge_within_owned_set
        )
        targets = select(NoteLink.target_note_id.label("nid")).where(
            NoteLink.target_note_id.isnot(None), edge_within_owned_set
        )
        connected = union(sources, targets).subquery()
        stmt = select(NoteMetadata).where(NoteMetadata.id.notin_(select(connected.c.nid)))
        stmt = apply_note_filters(stmt, folder=folder, user_id=uid)
        stmt = stmt.order_by(NoteMetadata.modified_at.desc().nullslast()).limit(limit)
        notes = (await session.execute(stmt)).scalars().all()

    if not notes:
        scope = f" in `{folder}`" if folder else ""
        return f"No orphan notes{scope}"
    lines = [f"Found {len(notes)} orphan notes:\n"]
    for n in notes:
        mod = n.modified_at.strftime("%Y-%m-%d") if n.modified_at else "unknown"
        tags_str = f" [{', '.join(n.tags)}]" if n.tags else ""
        lines.append(f"- `{n.file_path}` — {n.title}{tags_str} (modified {mod})")
    return "\n".join(lines)


@_tracked(
    "edit_note",
    # `replace_frontmatter` is logged because it is the destructive-intent flag
    # on this tool: it is the difference between a write that preserved the
    # note's frontmatter and one that replaced it wholesale, and an operator
    # reading `usage_logs` after a block went missing needs to see which was
    # asked for. `content` stays out, as it always has.
    # `expected_hash` is logged for every tool that accepts it (design D11):
    # it is a digest, not a secret, and an operator reading `usage_logs` after
    # a lost update needs to see which writes were guarded and against which
    # base.
    [
        "path", "append", "operation", "find", "section", "replace_all",
        "dry_run", "replace_frontmatter", "expected_hash",
    ],
    write_class=True,
)
async def edit_note_impl(
    path: str,
    content: str,
    append: bool = False,
    operation: str | None = None,
    find: str | None = None,
    section: str | None = None,
    replace_all: bool = False,
    dry_run: bool = False,
    replace_frontmatter: bool = False,
    expected_hash: str | None = None,
) -> str:
    """Edit an existing note in the vault.

    **Full replacement preserves an existing valid frontmatter block by
    default.** `content` is the note's new *body*; a valid line-1 `---` block
    is kept byte-identical ahead of it (with one `\\n` inserted when the block
    ends at EOF without a newline and `content` is non-empty). No property of
    `content`'s shape changes that — a leading `---`, or a complete
    mapping-shaped fenced block, is body. Pass `replace_frontmatter=True` to
    overwrite the whole file, frontmatter included; that is the escape hatch
    for replacing, dropping or repairing a block, and the only way to fix a
    malformed one. A note with no valid block (absent or malformed) is
    replaced wholesale by default, since there is nothing valid to preserve.

    **The round-trip guarantee covers a complete, unwindowed whole-note read
    only** — `read_note(path)` with no `section`, `offset=0` and `truncated`
    false in the response. Feed that response's `content` field back through
    default full replacement and the note is unchanged. A truncated read must
    be completed (page with `offset`) before it is written back. To change the
    frontmatter itself use `set_frontmatter` or edit the raw block with
    `find=`; the response's `frontmatter` JSON view is a lossy convenience and
    is never valid input to a write.

    **Section mode: what `content` replaces.** In section mode `content` is the
    section's **body**: the text beginning on the line immediately after the
    matched heading line, running to the next heading of equal-or-shallower
    depth or to end of note. The heading line itself is never removed or
    rewritten. A section write replaces that body **whole**, so anything
    `content` does not resend is **deleted** — a blank line, and a **fenced
    code block sitting directly under the heading**, included; there is no
    third region between the heading line and the body that survives a write.
    A blank line wanted between the heading and its content therefore belongs
    in `content`. `read_note(path, section=...)` is the matching read: its
    response carries the heading line in the `heading` field and the body in
    the `content` field, and this tool takes exactly that `content` — pass the
    field through unchanged; there is nothing to split off it and nothing to
    strip from it. (There used to be: the response was one rendered string and
    the docstrings had to describe recovering the body from it. Every such
    procedure was forgeable by a note's own frontmatter into a write that
    clobbered the section, which is why the response is fields now — #149,
    `docs/architecture/vault-tools.md`.) Byte-identity holds for notes
    whose body newlines are LF; every non-LF terminator inside the *selected
    body* comes back as LF, whether the note uses one dialect throughout or
    mixes them, because the read path normalises and this tool writes raw
    bytes. Terminators outside the selected body are untouched.

    Section mode resolves and replaces over the frontmatter-stripped body, so
    a YAML `#` comment is never selectable and never counted by an ordinal,
    and the block is reattached byte-identically. Over a *defective* block
    (unclosed fence, YAML error, non-mapping) a section write is refused by
    name; reads are deliberately asymmetric there — `read_note` still extracts
    from such a note, because a read destroys nothing.

    **A section write is also refused, by name and without writing, on a note
    containing a fence opener indented by one to three spaces that nothing
    below it closes.** Such an opener may sit inside a list item, whose code
    block ends where the item does; this server does not parse container
    blocks, so it cannot tell code from content there and will not guess.
    Reads stay asymmetric for the same reason as above: `read_note(section=…)`
    and the truncation outline keep resolving on such a note under the
    not-a-fence reading. Selector parity between the two tools is therefore a
    claim about **how a selector resolves on a write this tool admits**, not a
    promise that every readable section is writable.

    Every mode that writes confirms the caller's vault assignment immediately
    before it publishes (#88). `dry_run` publishes nothing and takes no
    confirmation — and must never be the reason a later mode skips one. The
    confirmation narrows the window to staging, the durability flush and one
    publishing call; it does not close it, at the same optimistic level as this
    tool's own `expected=` conflict check.

    **`expected_hash` binds the whole file, in every mode.** It is the
    caller-visible half of the pair: `expected=` below closes this call's
    read→rename window, `expected_hash` closes the caller's read→this call's
    read window (#205 D2). It is compared **immediately after the in-call read
    and before mode dispatch, the size cap, the `dry_run` diff and every no-op
    branch** — a diff or a "no changes" answer computed against a base the
    caller does not hold is a wrong answer, not a cheap one. A section write is
    bound to the *whole file's* hash because `#N` ordinals are positional, so a
    body-only digest could certify an unchanged body while an insertion above
    it changed which section the selector names (D5); the price is that an
    unrelated edit elsewhere refuses the safest mode, which is why the argument
    is optional.
    """
    if err := _require_write():
        return err

    # The tool's entry: ahead of `open_mutable`, `_leaf_state_error` and every
    # read, so a malformed hash outranks not-found, a symlinked leaf and the
    # size cap. A caller told "not found" for a call whose argument was never
    # valid fixes the wrong thing.
    if err := _precondition_syntax_error("edit_note", expected_hash, path=path):
        return err

    if operation is not None:
        operation = operation.lower()
        if operation not in {"append", "replace"}:
            return (
                'edit_note: operation must be "append" or "replace" '
                f'(got {operation!r}).'
            )
        if operation == "append":
            append = True

    selected = []
    if append:
        selected.append("append=True")
    if find is not None:
        selected.append("find=...")
    if section is not None:
        selected.append("section=...")
    if operation == "replace" and selected:
        selected.append('operation="replace"')
    if replace_frontmatter and selected:
        # `replace_frontmatter` selects wholesale full replacement, so it is
        # meaningless in the other three modes. Ignoring it there would be the
        # worse failure: a caller who passed it with `section=` believes the
        # block was replaced, and it was not.
        selected.append("replace_frontmatter=True")
    if len(selected) > 1:
        message = (
            "edit_note: choose at most one of append, find, section "
            f"(got {', '.join(selected)})."
        )
        if replace_frontmatter:
            message += (
                " replace_frontmatter applies to full replacement only — it "
                "is what makes a full replacement overwrite the frontmatter "
                "block instead of preserving it."
            )
        return message

    uid = current_user_id.get()
    from src.services.vault import parse_frontmatter_diagnose, replace_section

    try:
        # Resolved and *opened* before the read, so every mode — `dry_run`
        # included — refuses an alias rather than diffing (and then reporting
        # on) a note the caller did not name. Everything below acts on this
        # target's parent descriptor: no pathname is resolved again, so neither
        # an ancestor repointed nor the parent directory renamed between the
        # read and the write can redirect the write.
        target = open_mutable(path, user_id=uid)
    except ValueError as e:
        return str(e)
    with target:
        if err := _leaf_state_error(
            target,
            path,
            missing=f"Note not found: {path}. Use create_note to create it.",
        ):
            return err

        cap_name, cap_bytes = _note_precondition_cap()
        try:
            # The bounded incumbent read this tool has always performed: one
            # `fstat` and one read through the descriptor already open above,
            # bounded by this tool's own content cap, so the bytes measured are
            # the bytes hashed and no second pathname is resolved.
            existing_bytes = read_bytes_at(target, max_bytes=cap_bytes, label=path)
        except ValueError as e:
            # `read_bytes_at` raises `ValueError` only for an over-cap file
            # here — a non-regular leaf was refused by `_leaf_state_error`
            # above. A guarded call learns *why the guard could not run*
            # (`precondition_unavailable`, naming the cap) rather than being
            # told the file differs; an unguarded call with required mode off
            # keeps today's message, which is the compatibility rule.
            if err := _precondition_error(
                "edit_note",
                path,
                None,
                expected_hash,
                cap_name=cap_name,
                cap_bytes=cap_bytes,
                over_cap=True,
            ):
                return err
            return f"Failed to read {path}: {e}"
        except Exception as e:
            # Includes OSError: an ELOOP from the `O_NOFOLLOW` read means the leaf
            # became a symlink after validation. That is a refusal, not a crash.
            return f"Failed to read {path}: {e}"

        # Immediately after the read and before everything else this tool does:
        # mode dispatch, the size cap, the `dry_run` diff and every no-op or
        # defect branch. Ordering here is observable and therefore normative.
        if err := _precondition_error(
            "edit_note",
            path,
            existing_bytes,
            expected_hash,
            cap_name=cap_name,
            cap_bytes=cap_bytes,
        ):
            return err

        try:
            existing = existing_bytes.decode("utf-8")
        except Exception as e:
            return f"Failed to read {path}: {e}"

        new_content: str | None = None
        success_message: str = f"Updated note: {path}"

        if section is not None:
            # D5. A valid block is held out of the scan entirely, so heading
            # resolution, the replacement and the not-found/ambiguity listings
            # all run over exactly the text `read_note` scans — restoring the
            # selector parity the spec already promises — and the block is
            # reattached byte-identically. A *defective* block is refused
            # rather than scanned: over raw bytes a YAML `#` comment inside a
            # broken block is selectable as a heading, and the replacement span
            # can swallow the closing fence.
            _, stripped_body, diagnosis = parse_frontmatter_diagnose(existing)
            if diagnosis.defect is not None:
                return _frontmatter_defect_error("edit_note", path, diagnosis)
            scan_text = stripped_body if diagnosis.valid else existing
            # An indented fence opener with no closer below it is the one
            # shape the flat grammar cannot decide: under CommonMark the block
            # may end at an enclosing list item's end, so any flat reading
            # either splits a code block or extends a section over real
            # content. Reads keep working under the not-a-fence reading;
            # writes are refused by name, exactly as a defective frontmatter
            # block is (#150).
            if err := _unmatched_fence_error(path, scan_text, diagnosis):
                return err
            new_body, err = replace_section(scan_text, section, content)
            if err is not None:
                return err
            new_content = diagnosis.block + new_body
        elif find is not None:
            if find == "":
                return (
                    "edit_note: find must be a non-empty string. "
                    "An empty find would match every position and corrupt the note."
                )
            count = existing.count(find)
            if count == 0:
                preview = existing[:500]
                return (
                    f"Find text not found in {path}. "
                    f"First 500 chars of note:\n---\n{preview}\n---"
                )
            if count > 1 and not replace_all:
                return (
                    f"Find text matches {count} locations in {path}. "
                    "Provide more surrounding context to match a unique section, "
                    "or set replace_all=True."
                )
            if replace_all:
                new_content = existing.replace(find, content)
                success_message = (
                    f"Replaced {count} occurrence(s) in {path}"
                )
            else:
                new_content = existing.replace(find, content, 1)
        elif append:
            new_content = existing + "\n" + content
        elif replace_frontmatter:
            # Today's behaviour, now opt-in: the whole file, frontmatter
            # included, becomes exactly `content`. Also the escape hatch for
            # dropping a block or repairing a defective one.
            new_content = content
        else:
            # D1/D2. `content` is always the new *body*; an existing valid
            # block is preserved byte-identically ahead of it. `content` is
            # NEVER classified — a body whose first line is a thematic break
            # `---`, or which itself opens with a complete mapping-shaped
            # fenced block (exactly what `read_note` returns for such a note),
            # is body. Three audit rounds established that destructive intent
            # cannot be inferred from content shape, so it is asked for
            # explicitly instead.
            _, _, diagnosis = parse_frontmatter_diagnose(existing)
            if not diagnosis.valid:
                # Nothing valid to preserve (absent or defective) — wholesale,
                # which is what keeps the repair path open without the flag.
                new_content = content
            else:
                block = diagnosis.block
                # A metadata-only note whose closing fence sits at EOF without
                # a terminator. Exactly one `\n` goes in, and only when there is
                # a body to separate — otherwise `---Body` would corrupt the
                # fence, or an empty `content` would gain a stray line.
                #
                # The test is "ends in a line terminator", not "ends in `\n`":
                # a lone-CR note's block ends `\r`, which already terminates
                # the fence line, and adding `\n` there would rewrite that
                # terminator to CRLF — the block would no longer be the
                # byte-identical slice this branch promises.
                separator = (
                    "\n" if content and not block.endswith(("\n", "\r")) else ""
                )
                new_content = block + separator + content

        # Bound the *result*, before the diff and before the atomic write, so an
        # over-cap edit is refused by the tool in every mode (including dry_run)
        # and nothing is written. Must stay ahead of the `expected=` write below,
        # which is what detects a concurrent read-modify-write conflict.
        if err := _note_size_error(new_content):
            return err

        if dry_run:
            if new_content == existing:
                return f"No changes for {path}"
            import difflib
            diff = "".join(difflib.unified_diff(
                existing.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=path,
                tofile=path,
                lineterm="",
            ))
            return diff or f"No changes for {path}"

        # #88. Placed after the `dry_run` return above on purpose: a dry run
        # publishes nothing and so needs no confirmation, but every mode that
        # does publish takes its own — the dry run must never become the reason
        # a later write skipped one.
        try:
            err, _ = await _confirmed_publication(
                uid,
                lambda c: write_file_at(
                    target, new_content, expected=existing_bytes, confirmation=c
                ),
            )
            if err:
                return err
        except (ValueError, RuntimeError, vault_fs.VaultFSError) as e:
            if _is_in_call_conflict(e):
                # The *other* window, typed. Prose byte-unchanged, sentinel
                # appended, so an agent tells this from `stale_precondition`
                # by code: that one means "the file moved before you called,
                # and here is the new hash", this one means "the file moved
                # during my call, so no hash from before it can be valid".
                return _concurrent_write_refusal(str(e), path)
            return str(e)
        except OSError as e:
            return f"Failed to write {path}: {e}"
        return success_message + _published_hash_clause(
            new_content.encode("utf-8")
        )


# ────────────────────────────────────────────────────────────────────────────
# move_note
# ────────────────────────────────────────────────────────────────────────────


# The rewrite grammar. It MUST apply the same closed-class rules as the
# extraction grammar in `src/services/links.py` (see the long comment there):
# wikilink target/anchor/alias exclude `[` and `]`, markdown link text
# excludes `[`, the markdown href is length-bounded to 2,048 rather than
# bracket-free, and every quantifier is possessive. A class that can swallow
# the rest of a line before the tail fails makes `move_note(rewrite_links=…)`
# a quadratic stall for every other tenant, exactly as it did in extraction.
#
# Two divergences from `_MDLINK_RE` are PRE-EXISTING and deliberately left
# alone here — fixing either changes what `move_note` rewrites, which is its
# own change with its own audit. Both are recorded as known gaps in
# `tests/test_asvs_link_grammar.py`:
#   1. no CommonMark `<href>` alternative, so `[t](<a.md>)` is extracted but
#      never rewritten;
#   2. the anchor class is `[^)]` (crosses newlines) where extraction's is
#      `[^)\n]`.
#
# The markdown half is no longer a regex at all. `{1,2048}?` is linear but
# with a 2,048× constant — 4.7 s per 512 KiB of `[a](`, ≈ 90 s for a 10 MiB
# note — and `asyncio.to_thread` cannot shorten it, because CPython holds the
# GIL for the whole of one `re` step and a scan that matches nothing is a
# single step. `scan_md_links` is a hand-written linear scanner with EXACTLY
# these semantics; the two divergences above are its two keyword arguments,
# and the retired pattern is kept as a differential oracle in
# `tests/test_asvs_mdlink_scanner.py`. See the long comment in
# `src/services/links.py`.
_WIKILINK_REWRITE_RE = re.compile(
    r"(?P<embed>!)?\[\[(?P<target>[^\[\]\|#\n]++)"
    r"(?P<rest>(?:#[^\[\]\|\n]*+)?(?:\|[^\[\]\n]*+)?)\]\]"
)
# The rewrite grammar's markdown scan, as keyword arguments: no `<href>`
# alternative (divergence 1), anchor crosses newlines (divergence 2).
MDLINK_REWRITE_FLAGS = {"angle": False, "anchor_crosses_newlines": True}


class MoveRewriteCapExceeded(Exception):
    """One `move_note` source carries more rewrites than the per-note cap.

    The same bound the indexer applies to extraction (`MAX_LINKS_PER_NOTE`),
    applied to the *write* side, and for the same reason: a 10 MiB note of
    `[[Old]] ` holds ~1.7 million rewritable links, and rewriting them all
    would build a note whose `note_links` set the indexer then truncates at
    10,000 — the graph would assert a link set the vault bytes contradict.
    Raised out of the pure rewrite function and turned into an in-band
    refusal by `_move_note_locked`, which aborts the move **before any
    mutation**, exactly as the `MAX_NOTE_BYTES` and `MAX_MOVE_REWRITE_BYTES`
    preflight refusals do.
    """

    def __init__(self, source_path: str, count: int, cap: int):
        self.source_path = source_path
        self.count = count
        self.cap = cap
        super().__init__(
            f"{source_path} holds {count} links to rewrite, more than "
            f"MAX_LINKS_PER_NOTE={cap}"
        )


class MoveRewriteOverlap(Exception):
    """Two of one source's rewrites claim overlapping spans of the note.

    Sibling of `MoveRewriteCapExceeded`, with the same disposition: raised out
    of the pure rewrite function, turned into a whole-move refusal by
    `_move_note_locked` **before any mutation**.

    The spans come from two scans that are each non-overlapping, and they were
    believed unable to overlap *each other*. They can: a markdown link's
    ANCHOR class is `[^)]`, so `[x](Old.md#anchor[[Old]])` is one markdown
    link whose anchor contains a whole wikilink, and when both halves resolve
    to the moved note both are planned. There is no correct way to apply two
    replacements to one region — the retired reverse splice applied the inner
    one first and then used the outer one's now-stale `end`, deleting bytes
    *outside* the link (`…[[Old]])TAIL` came back as `…[[Old]])IL`) and
    reporting two successful rewrites — so this is refused rather than
    resolved. The agent renames the anchor, or moves without `rewrite_links`.
    """

    def __init__(
        self,
        source_path: str | None,
        first: tuple[int, int],
        second: tuple[int, int],
    ):
        self.source_path = source_path
        self.first = first
        self.second = second
        where = source_path or "the rewrite input"
        super().__init__(
            f"{where} holds two overlapping link rewrites: "
            f"{first[0]}–{first[1]} and {second[0]}–{second[1]}"
        )


def _splice_rewrites(
    content: str,
    rewrites: list[tuple[int, int, str]],
    source_path: str | None = None,
) -> str:
    """Apply `(start, end, replacement)` spans to `content` in one pass.

    **Linear, and byte-for-byte what the retired reverse-splice produced**
    for every plan it now accepts (overlapping ones are refused, below).
    The old shape was `out = out[:start] + repl + out[end:]` per rewrite over
    a descending sort — a fresh copy of the whole note per link, so a note of
    `[[Old]] ` cost 0.07 / 0.18 / 0.72 / 5.3 s at 64 / 128 / 256 / 512 KiB
    (clean O(n²), ~35 minutes at `MAX_NOTE_BYTES`) while holding the
    process-wide `_MOVE_REWRITE_LOCK` — one caller's move stalling every
    other tenant's, which is the exact shape of the extraction stall this
    change exists to close.

    The spans come from two non-overlapping scans (`_WIKILINK_REWRITE_RE`
    leftmost-non-overlapping, `scan_md_links` resuming at each match's end),
    and the cursor walk is equivalent to any per-span splice only while they
    do not overlap **each other**. They can: a markdown link's anchor class is
    `[^)]`, so a wikilink can sit inside a markdown link's anchor and both
    halves can resolve to the moved note.

    That is a **refusal**, not a fallback. This function used to hand such a
    plan to a reverse splice, on the theory that the retired implementation
    defined the answer. It does not define a correct one: applying the inner
    replacement first changes the string's length, so the outer one then
    splices at a stale `end` and deletes bytes *outside* the link, while the
    tool reports both rewrites as successes — a silent destructive write, the
    exact class this whole area exists to prevent. There is no right way to
    apply two replacements to one region, so an overlap raises
    `MoveRewriteOverlap` and `_move_note_locked` aborts the move before
    anything is renamed or written.
    """
    if not rewrites:
        return content
    ordered = sorted(rewrites, key=lambda r: r[0])
    cursor = 0
    previous = (0, 0)
    parts: list[str] = []
    for start, end, replacement in ordered:
        if start < cursor:
            raise MoveRewriteOverlap(source_path, previous, (start, end))
        parts.append(content[cursor:start])
        parts.append(replacement)
        cursor = end
        previous = (start, end)
    parts.append(content[cursor:])
    return "".join(parts)


def _rewrite_links_in_text(
    content: str,
    from_rel: str,
    to_rel: str,
    source_path: str,
    pre_move_index: dict,
    output_source_path: str | None = None,
    fence_scan=None,
) -> tuple[str, int]:
    """Rewrite any wikilink/embed/markdown-link in `content` whose pre-move
    resolution would have pointed at `from_rel`, so it now refers to `to_rel`.

    Preserves alias (`|...`) and anchor (`#...`) parts. For wikilinks, a bare
    target stays bare (uses the new stem), while a path-style target is
    rewritten to the full new path-style form (preserving any trailing `.md`).
    Markdown links always get the new full path. Code blocks are skipped.

    `fence_scan` is the caller's already-computed `FULL_NOTE` scan of this
    exact `content` — the preflight has one, and passing it keeps the
    frontmatter partition to one run per note. Omitted, this scans for itself.
    """
    from src.services.links import (
        FULL_NOTE,
        apply_fence_mask,
        resolve_target,
        scan_fences,
        scan_md_links,
    )

    paths = pre_move_index.get("paths", {})
    from_id = paths.get(from_rel)
    if from_id is None:
        return content, 0

    to_stem = PurePosixPath(to_rel).stem
    to_no_md = to_rel[:-3] if to_rel.endswith(".md") else to_rel

    # `FULL_NOTE`: `content` is the source note's raw bytes, frontmatter block
    # included, and the rewrite is spliced back into those same bytes.
    if fence_scan is None:
        fence_scan = scan_fences(content, context=FULL_NOTE)
    masked = apply_fence_mask(content, fence_scan)
    rewrites: list[tuple[int, int, str]] = []

    def _kept(start: int, masked_slice: str) -> str:
        """The bytes at a match's span, taken from `content`, not `masked`.

        The recognizers below run over `masked` — links inside code must not
        be rewritten, and that is the whole reason the mask exists. But every
        byte this function *writes back* has to come from the unmasked
        `content`: masking is a same-length substitution, so a span found in
        `masked` indexes the identical region of `content`, and splicing the
        masked slice instead replaced any inline code inside a link's alias,
        anchor or text with spaces — a silent destructive write on every
        `move_note(rewrite_links=True)` over a source like
        ``See [the `foo` option](Old.md)`` (#211).
        """
        return content[start:start + len(masked_slice)]

    def _unmasked_target(start: int, masked_slice: str) -> bool:
        """Is the part that DECIDES the rewrite free of masked bytes?

        The other half of #211. Slicing the written-back bytes out of
        `content` fixes what a rewrite publishes; it does not fix *which*
        links are rewritten, and that is decided from the masked target or
        href. The mask's filler is spaces, so ``[[`x`Old]]`` reaches
        `resolve_target` as `"   Old"`, strips to `Old`, and a move of
        `Old.md` rewrote a link that names a *different* note — publishing
        `[[New]]` over a link the author never pointed at the moved note.
        A candidate whose deciding span differs between `masked` and
        `content` is therefore skipped outright: this server cannot know what
        note the masked bytes were naming, and the safe answer on a
        destructive path is to leave the link exactly as written.
        """
        return content[start:start + len(masked_slice)] == masked_slice

    for m in _WIKILINK_REWRITE_RE.finditer(masked):
        target_raw = m.group("target")
        if not _unmasked_target(m.start("target"), target_raw):
            continue
        target = target_raw.strip()
        if not target:
            continue
        if resolve_target(target, source_path, pre_move_index) != from_id:
            continue
        target_no_md = target[:-3] if target.endswith(".md") else target
        is_path_style = "/" in target_no_md or target.endswith(".md")
        if is_path_style:
            new_target = to_no_md + (".md" if target.endswith(".md") else "")
        else:
            new_target = to_stem
        embed_prefix = "!" if m.group("embed") else ""
        rest = _kept(m.start("rest"), m.group("rest") or "")
        rewrites.append((m.start(), m.end(), f"{embed_prefix}[[{new_target}{rest}]]"))

    for link in scan_md_links(masked, **MDLINK_REWRITE_FLAGS):
        if not _unmasked_target(link.href_start, link.href):
            continue
        href = link.href.strip()
        if not href:
            continue
        target_for_resolve = href[:-3] if href.endswith(".md") else href
        if resolve_target(target_for_resolve, source_path, pre_move_index) != from_id:
            continue
        anchor = _kept(link.anchor_start, link.anchor)
        text = _kept(link.text_start, link.text)
        # Resolve against the original source location, but generate the new
        # href relative to where that source lives after the move. These differ
        # for a moved note rewriting its own Markdown self-link.
        output_path = output_source_path or source_path
        source_dir = PurePosixPath(output_path).parent.as_posix()
        relative_target = posixpath.relpath(to_rel, source_dir)
        rewrites.append((
            link.start,
            link.end,
            f"[{text}]({relative_target}{anchor})",
        ))

    if not rewrites:
        return content, 0
    # Bounded per source by the same cap the indexer applies to extraction.
    # Raised, not truncated: a partial rewrite would leave some links pointing
    # at a path the move is about to vacate, reported as a success.
    if len(rewrites) > MAX_LINKS_PER_NOTE:
        raise MoveRewriteCapExceeded(source_path, len(rewrites), MAX_LINKS_PER_NOTE)
    return _splice_rewrites(content, rewrites, source_path), len(rewrites)


def _rewrite_failure_warning(
    failed_sources: list[str],
    *,
    stopped: str | None = None,
    admitted_root: str | None = None,
) -> str | None:
    """Describe backlink rewrites that did not happen after the move committed.

    `stopped` switches the wording to one of the two #88 partial outcomes, and
    `admitted_root` names the root the move completed in:

    - `"reassigned"` — an administrator changed the vault assignment part way
      through, the confirming read said so, and the rewrites were stopped.
    - `"unavailable"` — the confirming read **failed**, so this server cannot
      say what the assignment is. Named as an outage rather than as a
      reassignment: reporting a database failure as an administrator's action
      states something nobody did, and an agent that relays it to a human has
      then relayed a fiction.

    Both reuse this one idiom rather than inventing a second reporting
    mechanism — they are new *reasons*, not a new mechanism — and it is what
    stops a half-rewritten link graph being reported as a clean move, which is
    precisely the "graph asserting a link the vault bytes do not contain" the
    preflight exists to prevent.
    """
    if not failed_sources:
        return None
    preview = ", ".join(failed_sources[:3])
    if len(failed_sources) > 3:
        preview += f", and {len(failed_sources) - 3} more"
    where = f" ({admitted_root})" if admitted_root else ""
    if stopped == "reassigned":
        return (
            "partial success: the note was moved in the vault this call was "
            f"admitted for{where}, but the vault assignment changed while the "
            "call was in flight, so the link rewrites were stopped. "
            f"{len(failed_sources)} note(s) still link to the old path and "
            f"were left unrewritten: {preview}"
        )
    if stopped == "unavailable":
        return (
            "partial success: the note was moved in the vault this call was "
            f"admitted for{where}, but the vault assignment could not be "
            "re-read before the link rewrites — the database was unreachable. "
            "This is a confirmation outage, not a reassignment: nobody is "
            "known to have changed the assignment. The rewrites were stopped, "
            f"so {len(failed_sources)} note(s) still link to the old path and "
            f"were left unrewritten: {preview}"
        )
    return (
        "partial success: note moved, but link rewrites failed in "
        f"{len(failed_sources)} note(s): {preview}"
    )


def _owner_predicate_for(entity, uid: int | None):
    """The ownership predicate for one `notes_metadata` entity or alias.

    Takes the entity so a graph query can carry the predicate in a JOIN's ON
    clause — for an outer join that is the difference between "this edge
    resolves to nothing I own" and "drop the row entirely", and dangling links
    must still be reported (#127, D1).
    """
    return entity.user_id.is_(None) if uid is None else entity.user_id == uid


def _note_owner_predicate(uid: int | None):
    """Return the exact NoteMetadata ownership predicate for a vault context.

    Total over `uid`: `None` is `IS NULL`, never "no predicate". Read paths use
    the same mapping as the write paths — see `apply_note_filters`.
    """
    from src.models.db import NoteMetadata

    return _owner_predicate_for(NoteMetadata, uid)


async def _stale_extraction_error(session, uid) -> str | None:
    """Refuse a rewrite-enabled move while this owner's re-derivation is open.

    `None` when every one of the caller's rows is stamped with the current
    grammar, so the caller can use it as a guard.

    Owner-scoped, and that is load-bearing on a multi-user server: another
    user's unfinished pass says nothing about whether *this* caller's
    `note_links` rows can be trusted, and refusing on it would wedge every
    account behind one idle vault.

    One indexed row, `LIMIT 1` — the predicate is over
    `(user_id, extraction_version)` on a table already indexed by `user_id`, so
    it costs one row read on the overwhelmingly common path where nothing is
    stale.
    """
    from sqlalchemy import select

    from src.models.db import NoteMetadata
    from src.services.indexer import CURRENT_EXTRACTION_VERSION

    stale = (
        await session.execute(
            select(NoteMetadata.file_path)
            .where(_note_owner_predicate(uid))
            .where(NoteMetadata.extraction_version != CURRENT_EXTRACTION_VERSION)
            .limit(1)
        )
    ).all()
    if not stale:
        return None
    return (
        "Move aborted: this vault's index is still being re-derived after a "
        "note-parsing change, so the link graph is not yet a trustworthy list "
        "of the notes that link here — a link the previous parser read as code "
        "has no row yet, and rewriting would silently leave it pointing at the "
        f"old path (first note still pending: {stale[0].file_path}). Nothing was "
        "moved, rewritten or reindexed. The re-derivation runs automatically "
        "on the indexer's next pass (within about five minutes); retry after "
        "that, or move now with rewrite_links=False and update the links "
        "yourself."
    )


def _ensure_move_source_in_index(index: dict, from_rel: str) -> None:
    """Make stale/missing metadata unable to suppress moved-note self rewrites."""
    if from_rel in index["paths"]:
        return
    synthetic_id = -1
    index["paths"][from_rel] = synthetic_id
    stem = PurePosixPath(from_rel).stem
    index["stems"].setdefault(stem, []).append((from_rel, synthetic_id))


@_tracked(
    "move_note",
    ["from_path", "to_path", "rewrite_links", "expected_hash"],
    write_class=True,
)
async def move_note_impl(
    from_path: str,
    to_path: str,
    rewrite_links: bool = False,
    expected_hash: str | None = None,
) -> str:
    """Move (rename or relocate) a note inside the vault.

    **`rewrite_links=True` preflights every source it would rewrite — the
    moved note's own body included — and refuses the whole move, before the
    rename, if any of them contains a fence opener indented by one to three
    spaces that nothing below it closes.** The refusal names each such source
    and where its opener sits. Rewriting mutates note text, and a link under
    an unmatched indented opener may be inside a list item's code block, which
    this server does not parse; it will not silently rewrite text whose
    code-or-content status it had to guess. `rewrite_links=False` is
    unaffected and remains the way to move such a note.

    Confirms the caller's vault assignment immediately before the `renameat2`
    that commits the move **and again before every link rewrite** (#88): the
    metadata transaction between them is an `await` of unbounded duration, so
    one confirmation cannot cover both. A rewrite refused after the move has
    committed — whether because the assignment changed or because the
    confirming read itself failed — stops the loop and is reported as a partial
    outcome naming which of the two it was; the move is not rolled back, and
    the metadata rows keep describing where the note now is. Each publication
    therefore carries its own narrowed window rather than one for the whole
    call; the tool has several such windows and can be refused part way
    through.

    **`expected_hash` binds `from_path`'s own bytes and nothing else** (design
    D8): it is compared in the preflight, before the rename and before any
    rewrite, and the backlink sources a `rewrite_links=True` move would rewrite
    are unbound because the caller never read them. Success reports the hash of
    the bytes **actually published at the destination** — see the matrix in
    `_move_note_locked`.
    """
    if err := _require_write():
        return err

    # The tool's entry: ahead of the descriptor gate, `open_mutable` and every
    # read, so a malformed hash outranks a missing source and a symlinked leaf
    # at either endpoint.
    if err := _precondition_syntax_error("move_note", expected_hash, path=from_path):
        return err

    uid = current_user_id.get()
    # A move that rewrites links pins one descriptor per planned rewrite for
    # the whole preflight-plus-rewrite span. Two such moves running at once can
    # jointly exhaust the process table even though each is inside its own
    # budget, so they are serialised — the bound has to hold for the process,
    # not per call. Moves without rewrites pin two descriptors and are not
    # serialised.
    async with _move_rewrite_gate(rewrite_links):
        return await _move_note_locked(
            from_path, to_path, rewrite_links, uid, expected_hash
        )


# Process-wide, so the descriptor budget is a bound on the *process* and not
# merely on each call: two moves that are each inside their own budget can
# still exhaust the table between them. Held for the whole preflight-plus-
# rewrite span, which is exactly the span descriptors are pinned for.
_MOVE_REWRITE_LOCK = asyncio.Lock()


@asynccontextmanager
async def _move_rewrite_gate(rewrite_links: bool):
    """Serialise moves that rewrite links; let plain moves through."""
    if not rewrite_links:
        yield
        return
    async with _MOVE_REWRITE_LOCK:
        yield


async def _move_note_locked(
    from_path: str,
    to_path: str,
    rewrite_links: bool,
    uid: int | None,
    expected_hash: str | None = None,
) -> str:
    """`move_note`'s body, under the descriptor gate. See `move_note_impl`.

    **What the success result's `content_hash` is** (design D4). A
    `rewrite_links=True` move publishes twice — the `renameat2`, then the moved
    note's own body rewrite — and the second can fail after the first has
    committed, so the reported value is always the hash of the bytes *actually
    published*, never of bytes this call intended to publish:

    - plain move (`rewrite_links=False`) → the moved bytes, read back through
      the destination's own verified descriptor;
    - the moved note's own rewrite published → the post-rewrite bytes;
    - that rewrite failed **without observing a change** (I/O, a stopped loop
      under a reassignment or a confirmation outage) → the rename's bytes,
      which are what is on disk;
    - that rewrite lost the in-call conflict (`concurrent_write`) → **no hash
      at all**: the destination holds a third writer's bytes this call never
      read, and naming the rename's hash would hand back a token that binds
      nothing;
    - a **backlink source's** rewrite failing changes none of the above.

    A post-rename failure is a **partial success**, never a whole-call refusal,
    and never carries `nothing_written: true`: the move happened, and a caller
    told otherwise goes looking for a note that has already relocated.
    """
    from sqlalchemy import select, update
    from src.models.db import NoteLink, NoteMetadata
    from src.services.links import build_vault_index

    # Every target below is resolved and opened exactly once and closed in the
    # `finally`. The descriptors are what the move and the rewrites act on, so
    # renaming a parent directory mid-call cannot redirect either. Both
    # endpoints are acquired inside the same guard: a non-`ValueError` failure
    # opening the destination would otherwise strand the source's descriptors.
    targets: list = []
    shared_root_fd: int | None = None
    try:
        try:
            src_target = open_mutable(from_path, user_id=uid)
            targets.append(src_target)
            dst_target = open_mutable(to_path, user_id=uid)
            targets.append(dst_target)
        except ValueError as e:
            return str(e)
        except OSError as e:
            return f"Could not open {from_path} or {to_path}: {e}"
        if err := _leaf_state_error(
            src_target,
            from_path,
            missing=f"Source note not found: {from_path}",
        ):
            return err

        # The precondition, before the rename and before any rewrite — and
        # before the index queries and the preflight too, so a refused move
        # costs the database nothing. It binds `from_path`'s own bytes only,
        # and the refusal says so. Read only when a hash was supplied or the
        # deployment requires one: an unguarded move reads nothing here that it
        # does not read today. (Under required mode the read is what tells an
        # over-cap source from a missing hash, which the ladder ranks in that
        # order.)
        if expected_hash is not None or settings.write_precondition_required:
            _, note_cap_bytes = _note_precondition_cap()
            try:
                incumbent, over_cap = _read_incumbent(
                    src_target, from_path, note_cap_bytes
                )
            except OSError as e:
                return f"Failed to read {from_path}: {e}"
            if incumbent is None and not over_cap:
                return f"Source note not found: {from_path}"
            if err := _move_precondition_error(
                from_path, incumbent, expected_hash, over_cap=over_cap
            ):
                return err

        # Both targets carry `resolved_parent / name`, so `rel` is the path the
        # indexer stores for a note reached through a symlinked folder — the DB
        # rows below, and the backlink lookup keyed on `from_rel`, line up with it.
        from_rel = src_target.rel
        to_rel = dst_target.rel

        pre_move_index: dict | None = None
        rewrite_sources: list[str] = [from_rel] if rewrite_links else []
        if rewrite_links:
            async with async_session() as session:
                rows_stmt = select(NoteMetadata.file_path, NoteMetadata.id).where(
                    _note_owner_predicate(uid)
                )
                rows = (await session.execute(rows_stmt)).all()
                pre_move_index = build_vault_index([(r.file_path, r.id) for r in rows])
                _ensure_move_source_in_index(pre_move_index, from_rel)
                target_id = pre_move_index["paths"].get(from_rel)
                if target_id is not None:
                    src_q = (
                        select(NoteMetadata.file_path)
                        .join(NoteLink, NoteLink.source_note_id == NoteMetadata.id)
                        .where(NoteLink.target_note_id == target_id)
                        .distinct()
                    )
                    src_q = src_q.where(_note_owner_predicate(uid))
                    src_rows = (await session.execute(src_q)).all()
                    rewrite_sources.extend(r.file_path for r in src_rows)
                    rewrite_sources = list(dict.fromkeys(rewrite_sources))

                # **That inventory is `note_links`, so it is only as good as
                # the grammar that built it (#150).** While any row in this
                # caller's scope still carries a stale extraction marker, some
                # of those rows came out of the previous fence grammar — and a
                # link the old grammar masked as code but the new one reads as
                # prose has NO row at all, so it is not in `rewrite_sources`
                # and never will be on this call. The move would then succeed,
                # report success, and silently strand that link: exactly the
                # class of failure the rest of this preflight exists to
                # prevent. Checked here, still before the rename, and it clears
                # itself once the re-derivation pass finishes.
                if err := await _stale_extraction_error(session, uid):
                    return err

        # ── Phase 1: preflight ──────────────────────────────────────────────────
        # Compute every rewritten body *before* anything is mutated. If one would
        # exceed the note cap the whole move aborts: the alternative (move, update
        # note_links, then skip the over-cap source) leaves the graph asserting a
        # link the vault bytes do not contain, and an agent acting on that graph
        # never sees the discrepancy.
        #
        # Memory: `read_bytes_at` bounds each source at MAX_NOTE_BYTES, but the number
        # of sources is unbounded — a target with hundreds of near-cap backlinks
        # would buffer gigabytes before a single byte is mutated. So the originals
        # and the rewrites are summed as they accumulate and the move aborts (still
        # before any mutation) once that total would exceed MAX_MOVE_REWRITE_BYTES.
        planned_rewrites: list[tuple[str, object, bytes, str, int]] = []
        rewrite_bytes_held = 0
        failed_rewrite_sources: list[str] = []
        # Sources the flat fence grammar cannot decide. Collected across the
        # whole preflight and refused as a set *before* phase 2, so the move
        # is never published half-decided; see `_unmatched_fence_error`.
        undecidable_sources: list[tuple[str, tuple]] = []
        # One vault-root descriptor for the whole rewrite phase, shared by every
        # planned rewrite (`MutableTarget.share_root`). It is a `dup` of a root
        # the kernel has already proved, never a fresh open of the root
        # *pathname* — re-resolving the name is the substitution surface #59
        # exists to close, and a `dup` resolves nothing.
        #
        # Each target still needs *a* root after its publish: since the chain
        # rule (#97) a successful write flushes every directory above its
        # parent, up to the root, and a target with no root descriptor cannot
        # look those up. Releasing the root — the previous shape — silently
        # reduced every backlink rewrite to a leaf-parent flush.

        def drop(candidate) -> None:
            """Close a per-source target we are not going to keep.

            A backlink source only has to stay open if its rewrite is actually
            *planned*: the point of holding it is that the phase-1 read and the
            phase-3 write go through one descriptor. Every other exit — not a
            file, nothing to rewrite, a failed read — has no phase 3, so
            holding the descriptor would pin one fd per source for the whole
            call and a hub note with hundreds of backlinks would exhaust the
            table. `targets` keeps the entry as a backstop; `close()` is
            idempotent, and the two move endpoints are never dropped here
            because the move itself still needs them.
            """
            if candidate is None or candidate is src_target or candidate is dst_target:
                return
            candidate.close()

        if rewrite_links and pre_move_index is not None:
            try:
                shared_root_fd = os.dup(src_target.root_fd)
            except OSError as e:
                return (
                    "Move aborted: ran out of file descriptors before the link "
                    f"rewrites could be planned ({e}). Nothing was moved, "
                    "rewritten or reindexed."
                )
            for original_src_path in rewrite_sources:
                # A moved note may link to itself: it is still at its old path now,
                # so read it there, but emit link targets relative to where it is
                # about to land — and write it at its new location.
                moved_note = original_src_path == from_rel
                out_path = to_rel if moved_note else original_src_path
                read_target = None
                try:
                    # Each source is resolved and opened once here and mutated
                    # through that descriptor in phase 3. Re-passing the string to
                    # `write_file` would resolve it a second time, after the move,
                    # so an ancestor repointed — or the parent renamed — in between
                    # would send the rewritten body somewhere the preflight never
                    # checked.
                    if moved_note:
                        read_target, write_target = src_target, dst_target
                    else:
                        read_target = open_mutable(original_src_path, user_id=uid)
                        targets.append(read_target)
                        write_target = read_target
                    if not read_target.is_file():
                        drop(read_target)
                        continue
                    if not moved_note:
                        # Pinned from here until phase 3 writes it. The number of
                        # backlink sources is unbounded, so this target hands
                        # back its *own* root and borrows the phase's shared one:
                        # one fd per source plus one for the phase, rather than
                        # two per source. `share_root` verifies the shared
                        # descriptor names the same inode this target's parent
                        # was proved beneath before it swaps — a mismatch means
                        # the vault root was repointed mid-call and is refused
                        # below, while nothing has been mutated yet.
                        read_target.share_root(shared_root_fd)
                    original_bytes = read_bytes_at(
                        read_target, max_bytes=MAX_NOTE_BYTES, label=original_src_path
                    )
                    content = original_bytes.decode("utf-8")
                    # Every SELECTED source is inspected, whether or not it
                    # turns out to carry a link to rewrite: the refusal is
                    # about text this call would mutate, and the decision has
                    # to be made before phase 2 publishes the rename. The scan
                    # is `FULL_NOTE` because a rewrite splices back into these
                    # raw bytes, frontmatter block included — and it is taken
                    # ONCE and handed on to the rewriter, because the
                    # recognizer's contract is that the frontmatter partition
                    # runs at most once per note.
                    #
                    # Off the loop for the same reason as the rewrite below
                    # (#180): the scan is linear but over up to
                    # `MAX_NOTE_BYTES` of text, seconds of solid CPU on a
                    # near-cap note, and it runs once per backlink source
                    # while the process-wide `_MOVE_REWRITE_LOCK` is held.
                    # Dispatching only the rewrite left the larger half of the
                    # per-source work on the loop.
                    fence_scan = await asyncio.to_thread(_scan_rewrite_source, content)
                    if fence_scan.unmatched_indented_openers:
                        undecidable_sources.append(
                            (original_src_path, fence_scan.unmatched_indented_openers)
                        )
                    # Off the loop (#180). The rewrite is a pure function of a
                    # string, and a hub note's backlink sources are read one
                    # after another — linear work on a near-cap note is still
                    # dead air for every other tenant if it runs here.
                    new_content, n = await asyncio.to_thread(
                        _rewrite_links_in_text,
                        content,
                        from_rel,
                        to_rel,
                        original_src_path,
                        pre_move_index,
                        output_source_path=out_path,
                        fence_scan=fence_scan,
                    )
                except OSError as e:
                    if getattr(e, "errno", None) in (errno.EMFILE, errno.ENFILE):
                        # Not a per-source failure. Running out of descriptors
                        # says the *plan* is too big for this process, and
                        # carrying on would move the note and silently drop
                        # every remaining rewrite — while the exhaustion takes
                        # concurrent requests down too. Abort before any
                        # mutation, which is still free at this point.
                        drop(read_target)
                        return (
                            "Move aborted: ran out of file descriptors while "
                            f"planning the link rewrites ({e}). Nothing was "
                            "moved, rewritten or reindexed. Move without "
                            "rewrite_links and update links in batches, or "
                            "raise the process's RLIMIT_NOFILE."
                        )
                    # `move_rewrite_failed` rather than a bare
                    # `logger.warning`: a caller can drive this branch on
                    # demand, and a direct logger call is an unbounded flood
                    # channel beside the bounded one (design D18). The path it
                    # used to interpolate has no allow-listed field to ride in
                    # and `emit`'s message is the event name, so the note is
                    # named where the caller and the operator both still see
                    # it: in `failed_rewrite_sources` on the tool's own reply,
                    # and in the move's `params` on `/admin/usage`.
                    security_events.emit(
                        "move_rewrite_failed",
                        subject=_security_subject(),
                        tool=_current_tool_name.get(),
                        error_type=type(e).__name__,
                    )
                    failed_rewrite_sources.append(original_src_path)
                    drop(read_target)
                    continue
                except VaultRootMismatch as e:
                    # Not a per-source failure: the vault root itself moved, so
                    # every remaining target is suspect and the endpoints we
                    # already validated may no longer describe the vault the
                    # caller meant. Abort while that is still free — phase 2 has
                    # not run, so nothing has been mutated.
                    drop(read_target)
                    return (
                        f"Move aborted: {e} Nothing was moved, rewritten or "
                        "reindexed."
                    )
                except MoveRewriteCapExceeded as e:
                    # Not a per-source failure either: rewriting all but this
                    # source would move the note and leave this one pointing
                    # at the vacated path, reported as success. Same
                    # disposition as the `MAX_NOTE_BYTES` and
                    # `MAX_MOVE_REWRITE_BYTES` refusals above — abort before
                    # phase 2, while that is still free.
                    drop(read_target)
                    return (
                        f"Move aborted: rewriting links in {e.source_path} would "
                        f"change {e.count} links, more than the per-note limit "
                        f"(MAX_LINKS_PER_NOTE={e.cap}). Nothing was moved, "
                        "rewritten or reindexed. Move without rewrite_links and "
                        "update that note's links in batches instead."
                    )
                except MoveRewriteOverlap as e:
                    # Same disposition, and for a stronger reason: this source
                    # has no correct rewritten form at all. One link nests
                    # inside another — a wikilink inside a markdown link's
                    # anchor — and both name the note being moved, so applying
                    # either replacement destroys the other's span. Refused
                    # here, before phase 2, rather than published as two
                    # "successful" rewrites over mangled bytes (#211).
                    # Migrated off the bare logger for the reason the
                    # three siblings above it were (design D18): a caller
                    # drives this branch on demand, by moving a note some
                    # other note links to twice over, so a direct
                    # `logger.warning` is an unbounded flood channel beside
                    # the bounded one. Its own event rather than
                    # `move_rewrite_failed`, because the dispositions differ:
                    # this one *aborts the whole move* before any mutation,
                    # while `move_rewrite_failed` skips one source and carries
                    # on. The path has no allow-listed field to ride in and is
                    # named where both the caller and the operator still see
                    # it — in the aborting reply below, and in the move's own
                    # `params` on `/admin/usage`.
                    security_events.emit(
                        "move_rewrite_overlap_refused",
                        subject=_security_subject(),
                        tool=_current_tool_name.get(),
                        error_type=type(e).__name__,
                    )
                    drop(read_target)
                    return (
                        f"Move aborted: {original_src_path} holds a link to "
                        f"{from_rel} nested inside another link to it, and "
                        "rewriting either would corrupt the other. Nothing was "
                        "moved, rewritten or reindexed. Move without "
                        "rewrite_links, or rewrite that note's nested link by "
                        "hand first."
                    )
                except Exception as e:
                    security_events.emit(
                        "move_rewrite_failed",
                        subject=_security_subject(),
                        tool=_current_tool_name.get(),
                        error_type=type(e).__name__,
                    )
                    failed_rewrite_sources.append(original_src_path)
                    drop(read_target)
                    continue
                if n == 0:
                    drop(read_target)
                    continue
                # A rewrite can only grow a note (the new path is usually longer
                # than the old one), so it is a note write like any other and gets
                # the same cap — enforced here, where refusing costs nothing. The
                # one encode also feeds the aggregate below.
                new_size = len(new_content.encode("utf-8"))
                if err := _note_size_error_for(new_size):
                    return (
                        f"Move aborted: rewriting links in {original_src_path} would "
                        f"exceed the note size limit ({err}). Nothing was moved, "
                        "rewritten or reindexed."
                    )
                rewrite_bytes_held += len(original_bytes) + new_size
                if rewrite_bytes_held > MAX_MOVE_REWRITE_BYTES:
                    return (
                        f"Move aborted: rewriting links across "
                        f"{len(planned_rewrites) + 1} notes would need "
                        f"{rewrite_bytes_held} bytes in memory (limit "
                        f"{MAX_MOVE_REWRITE_BYTES} bytes, "
                        f"{MAX_MOVE_REWRITE_BYTES // (1024 * 1024)} MiB). Nothing "
                        "was moved, rewritten or reindexed. Move without "
                        "rewrite_links and update links in batches instead."
                    )
                # Descriptors, for the same reason as the bytes above: each
                # planned rewrite pins one open parent fd from here until its
                # phase-3 write. Unbounded, one hub note's move exhausts the
                # *process* table and breaks every concurrent request — so the
                # move aborts here, before any mutation, rather than hitting
                # EMFILE half way through the rewrites.
                # `max_move_rewrite_sources()` already charges the phase's one
                # shared root, so this compares planned rewrites against what is
                # left for their parent descriptors.
                fd_budget = max_move_rewrite_sources()
                if len(planned_rewrites) + 1 > fd_budget:
                    return (
                        f"Move aborted: rewriting links across more than "
                        f"{fd_budget} notes would hold more open file "
                        "descriptors than this process can spare. Nothing was "
                        "moved, rewritten or reindexed. Move without "
                        "rewrite_links and update links in batches instead."
                    )
                planned_rewrites.append(
                    (out_path, write_target, original_bytes, new_content, n)
                )

        if undecidable_sources:
            # Refused as a set, and before the rename: a link inside an actual
            # list-contained unterminated fence is code, and rewriting it
            # would silently mutate code the flat grammar only guessed at.
            # `rewrite_links=False` never reaches here, and is unaffected.
            where = "; ".join(
                f"{src} — {', '.join(o.describe() for o in openers)}"
                for src, openers in undecidable_sources
            )
            return (
                "Move aborted: rewriting links would touch "
                f"{len(undecidable_sources)} note(s) containing an indented "
                f"fence opener that nothing below them closes — {where}. "
                "Nothing was moved, rewritten or reindexed. A fence indented "
                "by one to three spaces may be inside a list item, whose code "
                "block ends where the item does; this server does not parse "
                "container blocks, so it cannot tell whether a link below the "
                "opener is code or content. Close the fences (or unindent "
                "them to column zero), or move with rewrite_links=False and "
                "update the links yourself."
            )

        # ── Phase 2: commit ─────────────────────────────────────────────────────
        # #88, after the whole preflight so a refusal here still aborts before
        # any mutation, and immediately before the `renameat2` that commits the
        # move. The confirmation, the rename **and** the inode verification —
        # including the rollback that verification may have to perform — are
        # one synchronous step, so no `await` sits anywhere inside the window
        # the confirmation covers. The rollback is authorised by the
        # `MovePermit` the forward move returns, not by a second confirmation
        # and not by a spare copy of the first: it undoes the very publication
        # that confirmation covered, and it can undo nothing else.
        def _commit_the_move(confirmation):
            # Identify the source *before* the rename: `renameat2` moves
            # whichever inode is there when it runs, so this is the only chance
            # to know what we actually moved.
            moved_inode = _pin_source_inode(src_target)
            permit = move_file_no_clobber(
                src_target, dst_target, confirmation=confirmation
            )
            # What actually arrived at the destination: our inode, and a
            # regular file? Anything else is refused, and only our own inode is
            # ever moved back — see `_verify_the_moved_inode`.
            return _verify_the_moved_inode(
                src_target, dst_target, moved_inode, from_path, to_rel,
                permit=permit,
            )

        try:
            err, verify_error = await _confirmed_publication(uid, _commit_the_move)
        except FileExistsError:
            return _leaf_state_error(dst_target, to_path) or (
                f"Destination already exists: {to_path}"
            )
        except FileNotFoundError:
            return f"Source note not found: {from_path}"
        except (ValueError, vault_fs.VaultFSError) as e:
            return f"Move failed: {e}"
        except OSError as e:
            return f"Move failed: {e}"
        if err:
            return err
        if verify_error:
            return verify_error
        # The root the move completed in, for the partial-outcome report. It
        # is the target's own canonical assignment string, which is exactly
        # what the confirmation was checked against — no second source of
        # truth, and nothing retained from the confirmation object.
        admitted_root = src_target.assignment

        # ── The title is derived from the path, so a move changes it ──────
        # `notes_metadata.title` falls back to the filename stem when the
        # frontmatter sets none, so a row that keeps its old title after
        # `Alpha.md → Beta.md` reports `Alpha` in `list_notes`, `get_recent`,
        # every graph tool and both searches — for ever, because the content
        # hash is unchanged and the scan therefore never revisits the row
        # (#127, D3).
        #
        # It is derived from the **file**, through the same helper and the same
        # parser the indexer uses, so the result is byte-identical to what a
        # fresh index would write — every falsy-title case (`false`, `0`, `[]`,
        # `{}`, `""`) falls back to the new stem, which a SQL `CASE` over the
        # stored JSONB got wrong. Reading the file also means frontmatter added
        # or removed since the last pass decides, rather than a stale copy.
        #
        # The read happens *after* `_verify_the_moved_inode`, through the
        # destination target's own descriptor: the inode there has been proved
        # to be the one we moved, and the descriptor resolves no pathname.
        from src.services.indexer import _note_title  # local: avoids a cycle
        from src.services.vault import parse_frontmatter

        dest_name = PurePosixPath(to_rel).name
        moved_title: str | None = None
        # The bytes the **rename** published, captured from the same read the
        # title derivation already performs (D9: one read, and the hash it
        # yields describes exactly the bytes that read saw). `None` when the
        # destination could not be read back within `MAX_NOTE_BYTES` — the
        # move still stands and the result then says why it names no hash.
        renamed_bytes: bytes | None = None
        try:
            renamed_bytes = read_bytes_at(
                dst_target, max_bytes=MAX_NOTE_BYTES, label=to_rel
            )
            moved_frontmatter, _ = parse_frontmatter(renamed_bytes.decode("utf-8"))
            moved_title = _note_title(moved_frontmatter, dest_name)
        except Exception as exc:
            # Best-effort fallback, declared: the file vanished, is no longer
            # decodable, or its frontmatter no longer parses. Derive from the
            # row's stored sanitized frontmatter instead — possibly stale, and
            # self-healing at the note's next content change. Never a reason to
            # fail a move that has already stood.
            # Post-rename, so `reason` separates it from the other
            # best-effort failure below: both happen after the move has stood
            # and neither fails the call, and an operator reading a burst of
            # them needs to know whether the *file* or the *database* is the
            # one misbehaving. The two paths are already in the reply and in
            # the move's `params`; no structured field carries one (D16).
            security_events.emit(
                "move_post_rename_failed",
                subject=_security_subject(),
                tool=_current_tool_name.get(),
                reason="title_read_failed",
                error_type=type(exc).__name__,
            )

        db_failed = False
        try:
            async with async_session() as session:
                # **A path change invalidates the embedding certification.**
                # `embedded_content_hash` records that the row's *current
                # content* has been dealt with, and one of the things that
                # decides how it was dealt with is the path: `embed_vault`'s
                # exclusion branch matches `embedding_exclude_patterns` against
                # `file_path`. A move changes that answer while changing no
                # content, so carrying the stamp across it freezes the old
                # decision forever — the pass selects on
                # `embedded_content_hash != content_hash`, which is now false.
                #
                # Both directions are wrong and both are permanent. A note
                # moved *out* of an excluded folder keeps a stamp written by
                # the branch that deleted its vectors, so it is included, has
                # none, is never selected again, and is silently absent from
                # `semantic_search`. A note moved *into* one keeps its vectors
                # and stays searchable although it is now excluded.
                #
                # NULL is the conservative repair and needs no knowledge of the
                # exclusion configuration here: it means "re-evaluate at the
                # next pass". A note whose content did not really change is
                # re-embedded only because the hash check selects it, and the
                # exclusion decision is re-taken against the path it now has.
                title = moved_title
                if title is None:
                    # The fallback needs the row, so it is resolved inside the
                    # same transaction as the UPDATE.
                    row = (await session.execute(
                        select(NoteMetadata.frontmatter).where(
                            NoteMetadata.file_path == from_rel,
                            _note_owner_predicate(uid),
                        )
                    )).scalar_one_or_none()
                    title = _note_title(row or {}, dest_name)

                nm_update = (
                    update(NoteMetadata)
                    .where(
                        NoteMetadata.file_path == from_rel,
                        _note_owner_predicate(uid),
                    )
                    .values(
                        file_path=to_rel,
                        title=title,
                        embedded_content_hash=None,
                    )
                )
                await session.execute(nm_update)

                # Scope the NoteLink.target_path update to this user's link rows
                # by joining through their source notes. In single-user mode the
                # subquery selects every notes_metadata row (user_id IS NULL) so
                # the legacy behavior is preserved.
                user_note_ids = select(NoteMetadata.id).where(
                    _note_owner_predicate(uid)
                )
                link_update = (
                    update(NoteLink)
                    .where(
                        NoteLink.target_path == from_rel,
                        NoteLink.source_note_id.in_(user_note_ids),
                    )
                    .values(target_path=to_rel)
                )
                await session.execute(link_update)
                await session.commit()
        except Exception as e:
            security_events.emit(
                "move_post_rename_failed",
                subject=_security_subject(),
                tool=_current_tool_name.get(),
                reason="db_update_failed",
                error_type=type(e).__name__,
            )
            db_failed = True

        rewrites_done = 0
        files_modified = 0
        stopped: str | None = None
        # The moved note's own rewrite, tracked apart from the backlink
        # sources: only it can change what is at the destination, so only it
        # can change which hash this call may honestly report.
        moved_rewrite_bytes: bytes | None = None
        moved_rewrite_conflicted = False
        for position, planned in enumerate(planned_rewrites):
            write_path, write_target, original_bytes, new_content, n = planned
            # The moved note is the one source written through the destination
            # target itself (`read_target, write_target = src_target,
            # dst_target` in the preflight), so identity is exact and needs no
            # path comparison against a name that may repeat.
            is_moved_note = write_target is dst_target
            # #88, one confirmation per publication. The confirmation taken
            # before the move covers none of this: the metadata transaction
            # above is an `await` of unbounded duration, and so is every
            # rewrite already done. Reusing it here would be the same staleness
            # this change exists to narrow, merely relocated inside one call.
            outcome: str | None = None
            try:
                err, _ = await _confirmed_publication(
                    uid,
                    lambda c, t=write_target, b=new_content, o=original_bytes: (
                        write_file_at(t, b, expected=o, confirmation=c)
                    ),
                )
                if err:
                    outcome = "reassigned"
            except VaultConfirmationUnavailable as exc:
                # The assignment could **not be read at all** — a database
                # outage, not a reassignment. Before the move this propagates
                # and the call fails; here the move has already stood, so
                # letting it escape would take a completed publication with it
                # and tell the agent nothing about the note that has already
                # been renamed. Stop the remaining rewrites and report the
                # partial outcome, naming the outage *as an outage*: reporting
                # it as a reassignment would put a claim in the audit trail
                # about something no administrator did.
                # `move_rewrite_failed`, reused: this is the same loop and
                # the same disposition as the `except Exception` below — one
                # rewrite that did not happen — and `error_type` already tells
                # the two apart (`VaultConfirmationUnavailable` against
                # whatever the write raised). A separate event would split one
                # operator question across two names.
                security_events.emit(
                    "move_rewrite_failed",
                    subject=_security_subject(),
                    tool=_current_tool_name.get(),
                    error_type=type(exc).__name__,
                )
                timing.record("error", _CONFIRMATION_UNAVAILABLE_MARKER)
                outcome = "unavailable"
            except UnconfirmedPublication:
                # A publish helper refusing an unconfirmed target is a bug in
                # this repository, not a per-source rewrite failure. Never let
                # it be logged as one.
                raise
            except Exception as e:
                security_events.emit(
                    "move_rewrite_failed",
                    subject=_security_subject(),
                    tool=_current_tool_name.get(),
                    error_type=type(e).__name__,
                )
                outcome = "failed"
                if is_moved_note and _is_in_call_conflict(e):
                    # The one case where the *rename's* hash is wrong too:
                    # somebody changed the destination between the rename and
                    # this rewrite's publication, so the bytes there are
                    # theirs and this call never read them.
                    moved_rewrite_conflicted = True

            if outcome in ("reassigned", "unavailable"):
                # Stop. Under a reassignment every remaining rewrite would
                # write into a vault the caller no longer holds, through
                # descriptors pinned before it; under an outage this server
                # cannot say which vault it holds at all. Either way the move
                # is **not** rolled back and the metadata update is **not**
                # undone: the note really is at its new path and the rows must
                # keep saying so. This source and every one after it are
                # reported unrewritten, through the same idiom a per-source
                # failure uses.
                stopped = outcome
                for remaining in planned_rewrites[position:]:
                    failed_rewrite_sources.append(remaining[0])
                    drop(remaining[1])
                break

            if outcome == "failed":
                failed_rewrite_sources.append(write_path)
            else:
                rewrites_done += n
                files_modified += 1
                if is_moved_note:
                    # Published, so these are the bytes at the destination —
                    # exactly what `write_file_at` encoded.
                    moved_rewrite_bytes = new_content.encode("utf-8")
            # Its descriptor has done both jobs now. Closing here — rather than
            # in the outer `finally` — keeps the peak at the number of
            # *planned* rewrites still awaiting their write, not the number of
            # sources the move started with.
            drop(write_target)

        parts = [f"Moved {from_rel} → {to_rel}"]
        if db_failed:
            parts.append("(warning: DB update failed; reindex will reconcile)")
        if rewrite_links:
            parts.append(
                f"rewrote {rewrites_done} link(s) across {files_modified} note(s)"
            )
            warning = _rewrite_failure_warning(
                failed_rewrite_sources, stopped=stopped, admitted_root=admitted_root
            )
            if warning is not None:
                parts.append(f"(warning: {warning})")

        if moved_rewrite_conflicted:
            # A post-rename partial success, and the one case that reports no
            # hash at all. The two statements are said in full — the move
            # completed, the rewrite did not — and the refusal is typed with
            # `nothing_written` **absent**: something was written (the
            # rename), and claiming otherwise would send the caller looking
            # for a note that has already relocated.
            parts.append(
                f"(the move completed, but the moved note's own link rewrite "
                f"did not: {to_rel} was changed by another writer between the "
                "rename and the rewrite's publication, so the destination "
                "holds bytes this call never read. No content_hash is "
                "reported, because none this server could name would describe "
                f"what is on disk. Re-read {to_rel} before writing to it.)"
            )
            return _concurrent_write_refusal(
                " — ".join(parts), to_rel, nothing_written=None
            )

        published = (
            moved_rewrite_bytes if moved_rewrite_bytes is not None else renamed_bytes
        )
        if published is not None:
            parts.append(f"content_hash: {content_hash_for_bytes(published)}")
        else:
            parts.append(_hash_unavailable_clause(to_rel))
        return " — ".join(parts) if len(parts) > 1 else parts[0]
    finally:
        for opened in targets:
            opened.close()
        # After the targets: they borrowed this descriptor, and `close()` on a
        # borrower deliberately leaves it alone.
        if shared_root_fd is not None:
            vault_fs.close_quietly(shared_root_fd, "shared vault root for rewrites")


# ────────────────────────────────────────────────────────────────────────────
# delete_note
# ────────────────────────────────────────────────────────────────────────────


@_tracked("delete_note", ["path", "permanent", "expected_hash"], write_class=True)
async def delete_note_impl(
    path: str, permanent: bool = False, expected_hash: str | None = None
) -> str:
    """Soft-delete a note to `.trash/`, or unlink it when `permanent=True`.

    Both forms confirm the caller's vault assignment immediately before they
    act, and both are refused by the shared publish helpers if they do not
    (#88). The confirmation narrows the window to the publishing call itself;
    it does not close it — a reassignment committing inside that call still
    takes effect in the former root, at the same optimistic level as
    `edit_note(expected=…)`.

    **`expected_hash` applies in both modes** (design O3). A permanent delete
    is irreversible and a soft delete puts the bytes under a `.trash` name only
    an agent that knows to look will find, so both destroy content the caller
    may have read; the comparison runs **before** the pin, the `.trash` rename
    and the unlink. Success reports no `content_hash` — nothing remains to
    hash. The incumbent is read **only** when a hash was supplied or the
    deployment requires one, so an unguarded delete reads nothing it does not
    read today.
    """
    if err := _require_write():
        return err

    # The tool's entry, before `open_mutable` and `_leaf_state_error`.
    if err := _precondition_syntax_error("delete_note", expected_hash, path=path):
        return err

    uid = current_user_id.get()
    try:
        target = open_mutable(path, user_id=uid)
    except ValueError as e:
        return str(e)
    with target:
        if err := _leaf_state_error(
            target, path, missing=f"Note not found: {path}"
        ):
            return err

        if expected_hash is not None or settings.write_precondition_required:
            # The read is not "solely to populate `current_hash`": under
            # required mode it is the only way to learn whether the incumbent
            # is over the cap, and `precondition_unavailable` must outrank
            # `precondition_required` there — telling such a caller to supply a
            # hash sends it after one it can never obtain. Having read, the
            # refusal carries the hash, which saves the compliant caller a call.
            cap_name, cap_bytes = _note_precondition_cap()
            try:
                incumbent, over_cap = _read_incumbent(target, path, cap_bytes)
            except OSError as e:
                return f"Failed to read {path}: {e}"
            if incumbent is None and not over_cap:
                # It was there for `_leaf_state_error` and is gone now. Today's
                # answer for a delete of a note that does not exist, not a
                # precondition refusal about bytes that no longer exist either.
                return f"Note not found: {path}"
            if err := _precondition_error(
                "delete_note",
                path,
                incumbent,
                expected_hash,
                cap_name=cap_name,
                cap_bytes=cap_bytes,
                over_cap=over_cap,
            ):
                return err

        if permanent:
            # #88. The unlink itself lives in `vault.unlink_at`, which takes
            # the confirmation — the refusal for an unconfirmed target comes
            # from the helper, not from a check written into this tool, which
            # is what makes the enforcement structural rather than
            # conventional.
            try:
                err, _ = await _confirmed_publication(
                    uid, lambda c: unlink_at(target, confirmation=c)
                )
                if err:
                    return err
            except OSError as e:
                return f"Permanent delete failed: {e}"
            return f"Permanently deleted: {path}"

        # One `renameat2(RENAME_NOREPLACE)` from the note's own parent
        # descriptor into `.trash`, walked from the same resolved root: the
        # trash name is created or refused (never overwritten), and whichever
        # inode sits at the source when the call runs is what moves.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

        def _probe_then_soft_delete(confirmation):
            # The trash probe runs **inside** the confirmed step and ahead of
            # the delete (#88): `check_trash_support` creates `.trash` and a
            # file inside it, so a delete refused for a changed assignment must
            # not reach it. It must also run *after* the not-found check above
            # — the probe creates `.trash`, and a delete of a note that does
            # not exist must not.
            try:
                vault_fs.check_trash_support(target.root, root_fd=target.root_fd)
            except vault_fs.UnsupportedFilesystem:
                raise
            except (OSError, vault_fs.VaultFSError) as exc:
                raise _TrashUnusable(f"Vault root is not usable: {exc}") from exc
            return soft_delete_target(
                target, stamp=stamp, label=target.rel, confirmation=confirmation
            )

        try:
            err, dest = await _confirmed_publication(uid, _probe_then_soft_delete)
            if err:
                return err
        except _TrashUnusable as e:
            return str(e)
        except vault_fs.UnsupportedFilesystem as e:
            return str(e)
        except FileNotFoundError:
            return f"Note not found: {path}"
        except vault_fs.UnsafePath as e:
            return str(e)
        except vault_fs.Conflict as e:
            return f"{e}. Nothing was deleted."
        except vault_fs.VaultFSError as e:
            return str(e)
        except OSError as e:
            return f"Soft-delete failed: {e}"
        return f"Soft-deleted: {path} → {dest}"


# ────────────────────────────────────────────────────────────────────────────
# set_frontmatter
# ────────────────────────────────────────────────────────────────────────────


def _same_frontmatter_value(current, proposed, _seen: set | None = None) -> bool:
    """Type-sensitive structural equality for one frontmatter value.

    `set_frontmatter` writes only when something actually changed, and the
    comparison that decides it cannot be plain `==`: in Python `True == 1` and
    `False == 0`, so setting `draft: true` over a stored `draft: 1` would look
    like a no-op and leave the note carrying the integer. YAML round-trips
    these as visibly different documents (`true` vs `1`), so an agent reading
    the note back would see the value it did not ask for and a success report
    that said nothing happened.

    Concrete types must match exactly at every level; containers are compared
    element-wise so a nested `1` never satisfies a nested `True`.

    Floats go through `float.hex()`, which is where `==` fails a second time:
    `-0.0 == 0.0` is True while `yaml.safe_dump` writes them as `-0.0` and
    `0.0`, so a caller correcting the sign of a zero would be told nothing
    changed and the note would keep the sign it had. `.hex()` is exact for
    every finite float and returns `'inf'` / `'-inf'` / `'nan'` for the
    non-finite ones — which makes two NaNs compare *equal* here, deliberately:
    YAML round-trips both to `.nan`, so "changing" one to the other would
    rewrite the note to the bytes it already holds.

    Mappings are compared in **order** as well as by content. `safe_dump` runs
    with `sort_keys=False`, so key order is part of the note's bytes: a
    reordered mapping that compared equal would be reported as no change while
    the order the caller asked for was silently dropped.

    `_seen` carries the container pairs already being compared, so a note whose
    YAML uses a recursive alias (`a: &A [*A]` — valid YAML that `safe_load`
    accepts and returns as a self-referencing list) is answered instead of
    raising `RecursionError`. A revisited pair is treated as equal, which is
    sound because `all()` short-circuits: a pair that already finished
    comparing UNequal has propagated that out before anything can revisit it.
    """
    if _seen is None:
        _seen = set()
    if type(current) is not type(proposed):
        return False
    if isinstance(current, float):
        return current.hex() == proposed.hex()
    if isinstance(current, (dict, list, tuple)):
        pair = (id(current), id(proposed))
        if pair in _seen:
            return True
        _seen.add(pair)
    if isinstance(current, dict):
        if len(current) != len(proposed):
            return False
        return all(
            _same_frontmatter_value(ck, pk, _seen)
            and _same_frontmatter_value(cv, pv, _seen)
            for (ck, cv), (pk, pv) in zip(current.items(), proposed.items())
        )
    if isinstance(current, (list, tuple)):
        if len(current) != len(proposed):
            return False
        return all(
            _same_frontmatter_value(a, b, _seen)
            for a, b in zip(current, proposed)
        )
    return current == proposed


@_tracked("set_frontmatter", ["path", "expected_hash"], write_class=True)
async def set_frontmatter_impl(
    path: str,
    updates: dict | None = None,
    remove: list[str] | None = None,
    expected_hash: str | None = None,
) -> str:
    """Merge `updates` into a note's YAML frontmatter and drop keys in `remove`.

    **A malformed block is refused, not worked around.** An unclosed line-1
    fence, a fenced block that fails YAML parsing, and one whose YAML is not a
    mapping (`null`, `~`, comments only, a list, a scalar) each produce an
    error naming the defect and pointing at
    `edit_note(replace_frontmatter=True)` as the repair; nothing is written,
    and in particular no second block is prepended above the broken one.
    `remove=` refuses identically rather than silently doing nothing. The
    diagnosis runs *before* the empty-`updates`/`remove` no-op check, so a
    caller that passes neither still learns the note is broken.

    A whitespace-only fenced block (`---\n---\n`) is a **valid empty
    mapping** and is updated in place. A note with no line-1 fence at all
    still gets a fresh block prepended ahead of its unchanged body.

    Only an *effective* mutation writes: `updates` that set every named key to
    the value it already holds (compared type-sensitively — `True` is not `1`)
    and `remove` naming only absent keys is a byte-identical no-op. That is
    what keeps a remove-of-nothing from dropping a valid empty block, which
    would promote a mapping-shaped body prefix into active frontmatter.
    Removing the last key removes the block entirely — no fences, no YAML
    region, no separator, exactly the prior body.

    Confirms the caller's vault assignment immediately before it
    publishes (#88). That narrows the window in which an administrator's
    reassignment can be missed to staging, the durability flush and one
    publishing call — it does not close it, the same optimistic guarantee
    the system declares for `edit_note(expected=…)`.

    **`expected_hash` binds the whole file** and is compared immediately after
    the incumbent read — ahead of the defect diagnosis, the lossy refusal and
    the empty-`updates` no-op (#205 D2). A "no changes" answer computed against
    a base the caller does not hold is a wrong answer, and a defect report on
    bytes it has not seen sends it to repair something it cannot see.
    """
    if err := _require_write():
        return err

    # The tool's entry, before `open_mutable` and before any read (#205 D2).
    if err := _precondition_syntax_error("set_frontmatter", expected_hash, path=path):
        return err

    updates = dict(updates or {})
    remove = list(remove or [])

    from src.services.vault import (
        parse_frontmatter_diagnose,
        serialize_frontmatter,
    )

    uid = current_user_id.get()
    try:
        # One resolution — and one open parent descriptor — for the whole
        # read-modify-write (see `edit_note_impl`).
        target = open_mutable(path, user_id=uid)
    except ValueError as e:
        return str(e)
    with target:
        if err := _leaf_state_error(
            target, path, missing=f"Note not found: {path}"
        ):
            return err

        cap_name, cap_bytes = _note_precondition_cap()
        try:
            raw_bytes = read_bytes_at(target, max_bytes=cap_bytes, label=path)
        except ValueError as e:
            # Over cap: the guard cannot run, which is its own answer and not a
            # mismatch (see the twin branch in `edit_note_impl`). Unguarded and
            # not required, today's message stands.
            if err := _precondition_error(
                "set_frontmatter",
                path,
                None,
                expected_hash,
                cap_name=cap_name,
                cap_bytes=cap_bytes,
                over_cap=True,
            ):
                return err
            return f"Failed to read {path}: {e}"
        except Exception as e:
            # OSError included — an ELOOP here means the leaf was swapped for a
            # link after validation; report it rather than raising.
            return f"Failed to read {path}: {e}"

        # Immediately after the read: ahead of the defect diagnosis, the lossy
        # refusal and the empty-`updates` no-op.
        if err := _precondition_error(
            "set_frontmatter",
            path,
            raw_bytes,
            expected_hash,
            cap_name=cap_name,
            cap_bytes=cap_bytes,
        ):
            return err

        try:
            raw = raw_bytes.decode("utf-8")
        except Exception as e:
            return f"Failed to read {path}: {e}"

        fm, body, diagnosis = parse_frontmatter_diagnose(raw)
        # D6: diagnosis precedes the no-op check. A caller passing neither
        # `updates` nor `remove` on a broken note must be told the note is
        # broken, not handed a success report about a file this tool cannot
        # safely touch.
        if diagnosis.defect is not None:
            return _frontmatter_defect_error("set_frontmatter", path, diagnosis)

        if diagnosis.lossy:
            # This tool rewrites the block from the parsed mapping, and the
            # parser drops what nothing can render (a hex integer past the
            # digit limit, an unpaired surrogate). Serializing the pruned
            # mapping would delete those keys from the note as a side effect of
            # setting an unrelated one — a destructive write reported as a
            # success, which is the class this server treats as the expensive
            # failure. Refused by name, like a defective block, and for the
            # same reason: the repair is the caller's to make deliberately.
            keys = ", ".join(f"`{k}`" for k in sorted(diagnosis.lossy))
            return (
                f"set_frontmatter: {path} has frontmatter this server cannot "
                f"represent, under {keys}, so rewriting the block from the "
                "parsed mapping would silently delete it. Edit the raw block "
                "with `edit_note(find=...)`, or replace it with "
                "`edit_note(replace_frontmatter=True)`."
            )

        if not updates and not remove:
            return f"No changes for {path} (empty updates and remove)"

        # The comparison baseline: the mapping as the file holds it, deep-copied
        # so nothing below can mutate it. Per-key bookkeeping alone was not
        # enough — it records the steps, and the question is whether the
        # *destination* differs. `updates={"temp": 1}, remove=["temp"]` takes
        # two recorded steps back to where it started, and on a note whose
        # valid EMPTY block sits above a mapping-shaped fenced body prefix,
        # serializing that unchanged mapping drops the block and promotes the
        # prefix into active frontmatter.
        original_fm = copy.deepcopy(fm)

        set_keys: list[str] = []
        for k, v in updates.items():
            # Only an *effective* set is named in the summary. `==` alone
            # conflates `True` with `1` and `False` with `0`, which would
            # report a real type change as a no-op and leave the note carrying
            # the old type.
            if k in fm and _same_frontmatter_value(fm[k], v):
                continue
            fm[k] = v
            set_keys.append(k)
        removed_keys: list[str] = []
        # Documented precedence: every update is applied first, then every
        # removal — so a key named in both ends up removed.
        for k in remove:
            if k in fm:
                del fm[k]
                removed_keys.append(k)

        if _same_frontmatter_value(original_fm, fm):
            # No NET change, so nothing is serialized. This is the guard that
            # stops a remove-of-nothing — and a cancelling update+remove pair —
            # from dropping a valid EMPTY block: `serialize_frontmatter({},
            # body)` emits no fences at all, which on a note whose body opens
            # with a mapping-shaped fenced block would promote that body prefix
            # into active frontmatter.
            return f"No changes for {path}"

        new_raw = serialize_frontmatter(fm, body)
        if new_raw == raw:
            return f"No changes for {path}"

        # Bound the result before writing (see `edit_note_impl`). A remove-only
        # call can only shrink the note, but the check is uniform.
        if err := _note_size_error(new_raw):
            return err

        try:
            err, _ = await _confirmed_publication(  # #88
                uid,
                lambda c: write_file_at(
                    target, new_raw, expected=raw_bytes, confirmation=c
                ),
            )
            if err:
                return err
        except (ValueError, RuntimeError, vault_fs.VaultFSError) as e:
            if _is_in_call_conflict(e):
                return _concurrent_write_refusal(str(e), path)
            return str(e)
        except OSError as e:
            return f"Failed to write {path}: {e}"

        summary: list[str] = []
        if set_keys:
            summary.append(f"set: {', '.join(set_keys)}")
        if removed_keys:
            summary.append(f"removed: {', '.join(removed_keys)}")
        if not fm and removed_keys:
            summary.append("frontmatter block removed (last key)")
        return (
            f"Updated frontmatter in {path} ({'; '.join(summary)})"
            + _published_hash_clause(new_raw.encode("utf-8"))
        )


# ────────────────────────────────────────────────────────────────────────────
# Raw file-access tools: read_file / write_file / list_files
# ────────────────────────────────────────────────────────────────────────────


def _base64_payload(path: str, data: bytes, mime: str) -> str:
    """Format raw bytes as a labeled base64 block.

    The header makes the encoding explicit and warns that the body is opaque
    (a skill/client decodes it; the model cannot read it). The base64 string
    is the final block, separated by a blank line.
    """
    b64 = base64.b64encode(data).decode("ascii")
    return (
        "encoding: base64\n"
        f"mime: {mime}\n"
        f"bytes: {len(data)}\n"
        f"path: {json.dumps(path)}\n"
        f"content_hash: {content_hash_for_bytes(data)}\n"
        "(opaque bytes — not human-readable; pass to a skill/client to decode)\n\n"
        f"{b64}"
    )


def _capped_text(text: str, path: str, offset: int, cap: int) -> str:
    """Return a context-safe window of decoded file text."""
    if offset == 0 and len(text) <= cap:
        return text
    chunk, next_offset = _window(text, offset, cap)
    if not chunk and offset > 0:
        if offset == len(text):
            return (
                f"read_file: offset {offset:,} is exactly the end of {path} "
                f"({len(text):,} chars) — the whole file has been read, there "
                f"is nothing further."
            )
        return (
            f"read_file: offset {offset:,} is past the end of {path} "
            f"({len(text):,} chars)."
        )
    shown_to = min(offset, len(text)) + len(chunk)
    notice = (
        f"\n\n---\n**[TRUNCATED]** Showing chars {offset:,}–{shown_to:,} "
        f"of {len(text):,} for {path}."
    )
    if next_offset is not None:
        notice += (
            f' Continue with `read_file(path="{path}", offset={next_offset})`.'
        )
    return chunk + notice


@_tracked("read_file", ["path", "encoding", "offset", "limit", "hash_only"])
async def read_file_impl(
    path: str,
    encoding: str = "auto",
    offset: int = 0,
    limit: int | None = None,
    hash_only: bool = False,
):
    """Read text, inline images, base64 bytes, or metadata with `hash_only`.

    Encoding is validated first, then hash_only/window compatibility, then
    ranges. A valid encoding has no effect under hash_only. Text is bare; use
    base64 for a byte-exact frontmatter block, or hash_only for its file hash.
    """
    if encoding not in ("auto", "text", "base64"):
        return f"Invalid encoding '{encoding}'. Use 'auto', 'text', or 'base64'."
    if hash_only and (offset != 0 or limit is not None):
        return "read_file: hash_only cannot be combined with offset or limit windows."
    if offset < 0:
        return f"read_file: offset must be >= 0 (got {offset})."
    cap = settings.max_read_response_chars
    if limit is not None:
        if limit < 1:
            return f"read_file: limit must be >= 1 (got {limit})."
        cap = min(limit, cap)

    uid = current_user_id.get()
    try:
        data = read_bytes(path, user_id=uid, max_bytes=settings.max_file_read_bytes)
    except FileNotFoundError:
        return f"File not found: {path}"
    except ValueError as e:
        return str(e)

    if hash_only:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return (
            f"path: {json.dumps(path)}\n"
            f"bytes: {len(data)}\n"
            f"mime: {mime}\n"
            f"content_hash: {content_hash_for_bytes(data)}"
        )

    if encoding == "text":
        try:
            return _capped_text(data.decode("utf-8"), path, offset, cap)
        except UnicodeDecodeError:
            return (
                f"Cannot decode {path} as UTF-8 text (not valid UTF-8). "
                'Use encoding="base64" for binary files.'
            )

    if encoding == "base64":
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return _base64_payload(path, data, mime)

    # encoding == "auto"
    kind, mime = classify_bytes(data, path)
    if kind == "text":
        try:
            return _capped_text(data.decode("utf-8"), path, offset, cap)
        except UnicodeDecodeError:
            return _base64_payload(path, data, mime)
    if kind == "image":
        # FastMCP wraps this into an MCP image content block. `format` becomes
        # the `image/<format>` MIME (e.g. "png", "jpeg", "gif", "webp").
        return Image(data=data, format=mime.split("/", 1)[1])
    return _base64_payload(path, data, mime)


def _write_cap_for(path: str) -> tuple[int, str]:
    """The byte cap for a raw-transport write to `path`, and its name.

    The note tools cap every note at `MAX_NOTE_BYTES`, but `write_file`, the
    transfer upload and `import_from_url` are byte transport with no extension
    allowlist — so a 25 MiB `.md` could be landed by the tool the note tools
    would have refused, and the indexer then reads it as a note. The cap
    follows the EXTENSION, not the tool.

    It is the *smaller* of the two limits so an operator who lowers
    `MAX_FILE_WRITE_BYTES` below 10 MiB is not surprised by a more permissive
    markdown limit, and the name that comes back is whichever one actually
    applied — a caller told "max 10,485,760" without being told which knob
    that is cannot act on it.
    """
    if path.lower().endswith(".md") and MAX_NOTE_BYTES < settings.max_file_write_bytes:
        return MAX_NOTE_BYTES, "MAX_NOTE_BYTES"
    return settings.max_file_write_bytes, "MAX_FILE_WRITE_BYTES"


@_tracked("write_file", ["path", "encoding", "overwrite", "expected_hash"], write_class=True)
async def write_file_impl(
    path: str,
    content: str,
    encoding: str = "base64",
    overwrite: bool = False,
    expected_hash: str | None = None,
) -> str:
    """Write a file into the vault from base64 or text content.

    Confirms the caller's vault assignment immediately before it
    publishes (#88). That narrows the window in which an administrator's
    reassignment can be missed to staging, the durability flush and one
    publishing call — it does not close it, the same optimistic guarantee
    the system declares for `edit_note(expected=…)`.
    """
    if err := _precondition_syntax_error("write_file", expected_hash, path=path):
        return err
    if not overwrite and expected_hash is not None:
        return _precondition_error(
            "write_file", path, None, expected_hash, no_incumbent=True
        )
    if err := _require_write():
        return err
    if encoding not in ("base64", "text"):
        return f"Invalid encoding '{encoding}'. Use 'base64' or 'text'."

    uid = current_user_id.get()
    # Validate before decoding and before the size check, for the same reason
    # as `create_note_impl`: a symlinked destination is a path problem, and
    # saying so beats sending the caller away to shrink its payload.
    try:
        target = open_mutable(path, user_id=uid)
    except ValueError as e:
        return str(e)

    with target:
        # Absence is fine — this tool creates — but a leaf that became a link
        # or a directory after validation is not. `overwrite=True` publishes
        # with `renameat`, which replaces the *link* rather than following it:
        # safe for the file it pointed at, but it silently consumes an alias
        # the caller still believes in and reports "Wrote N bytes" for it.
        #
        # Ahead of the decode and the size check for the same reason the
        # validation guard is: a path problem outranks a payload problem, and
        # reporting the size limit would send the caller off to shrink content
        # that was never why the write is refused.
        if err := _leaf_state_error(target, path):
            return err

        incumbent = None
        over_cap = False
        read_cap_name, read_cap = _file_precondition_cap()
        if overwrite and (expected_hash is not None or settings.write_precondition_required):
            try:
                incumbent, over_cap = _read_incumbent(target, path, read_cap)
            except OSError as exc:
                return _leaf_state_error(target, path) or f"Failed to read {path}: {exc}"
            if err := _precondition_error(
                "write_file", path, incumbent, expected_hash,
                no_incumbent=incumbent is None and not over_cap,
                over_cap=over_cap, cap_name=read_cap_name, cap_bytes=read_cap,
            ):
                return err
        elif overwrite:
            # Preserve unconditional writes: stat is enough to suppress a result
            # hash for an over-cap incumbent; never read its content here.
            info = target.lstat()
            over_cap = info is not None and info.st_size > read_cap

        if encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                return (
                    "Invalid base64 content: could not decode. "
                    "No file was written."
                )
        else:
            data = content.encode("utf-8")

        # **The cap follows the NORMALISED name, not the caller's string.**
        # `open_mutable` drops empty and `.` components, so `"big.md/."` is
        # written to `big.md` — while `"big.md/.".lower().endswith(".md")` is
        # False. Testing the raw argument therefore admitted a 25 MiB markdown
        # note through the tool whose whole job here is to refuse one, and the
        # indexer then read it as a note. `target.rel` is the path the bytes
        # actually land on, which is the only path a cap may be derived from.
        cap, cap_name = _write_cap_for(target.rel)
        if len(data) > cap:
            return (
                f"Content too large ({len(data):,} bytes, "
                f"max {cap:,} — {cap_name}). No file was written."
            )

        try:
            err, _ = await _confirmed_publication(  # #88
                uid,
                lambda c: write_bytes_at(
                    target, data, overwrite=overwrite, expected=incumbent, confirmation=c
                ),
            )
            if err:
                return err
        except FileExistsError:
            return _leaf_state_error(target, path) or (
                f"File already exists: {path}. Pass overwrite=True to replace it."
            )
        except (ValueError, RuntimeError, vault_fs.VaultFSError) as e:
            if _is_in_call_conflict(e):
                return _concurrent_write_refusal(str(e), path)
            return str(e)
        except OSError as e:
            return f"Failed to write {path}: {e}"
        result = f"Wrote {len(data):,} bytes to {path}"
        if over_cap or len(data) > read_cap:
            return result + (
                f" — hash not reported: incumbent or published file exceeds "
                f"{read_cap_name} ({read_cap:,} bytes)"
            )
        return result + _published_hash_clause(data)


@_tracked("list_files", ["folder", "pattern", "recursive", "limit"])
async def list_files_impl(
    folder: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    limit: int = 200,
) -> str:
    """Browse vault files and subdirectories (`ls`-style)."""
    uid = current_user_id.get()
    limit = max(1, min(limit, 1000))
    try:
        entries, truncated = list_dir(
            folder, pattern=pattern, recursive=recursive, limit=limit, user_id=uid
        )
    except NotADirectoryError as e:
        return str(e)
    except ValueError as e:
        return str(e)

    where = folder or "."
    if not entries:
        return f"No entries in '{where}' matching '{pattern}'"

    header = f"{len(entries)} " + ("entry" if len(entries) == 1 else "entries")
    header += f" in '{where}'"
    if pattern != "*":
        header += f" matching '{pattern}'"
    if recursive:
        header += " (recursive)"
    if truncated:
        header += ", truncated"
    lines = [header + ":\n"]
    for e in entries:
        if e["is_dir"]:
            lines.append(f"- 📁 `{e['path']}/`")
        else:
            mod = datetime.fromtimestamp(e["mtime"], timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
            lines.append(f"- `{e['path']}` ({e['size']:,}B, modified {mod})")
    if truncated:
        lines.append(
            f"\n… more than {limit} entries; narrow with `pattern` or a subfolder."
        )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# File-transfer tools: request_upload / check_upload / request_download /
# import_from_url / delete_file
#
# No MCP client can hand a tool raw attachment bytes, and no agent shell can
# reach into the user's downloads folder. These five close that gap without
# widening the vault's write surface: each mint pins one path, one direction
# and one identity into a short-lived capability, and the `/transfer/*` routes
# will act on nothing but what was pinned.
# ────────────────────────────────────────────────────────────────────────────


_NO_PUBLIC_ORIGIN = (
    "This server has no public origin configured, so it cannot build a "
    "shareable transfer link. Set MCP_HOSTNAME (preferred) or BASE_URL in the "
    "server's environment and restart. Nothing was minted."
)


def _transfer_identity() -> transfer.Identity:
    return transfer.Identity(
        key_id=current_api_key_id.get(),
        oauth_token_id=current_oauth_token_id.get(),
        user_id=current_user_id.get(),
    )


def _vault_context(path: str, uid: int | None) -> tuple[str, str]:
    """`(canonical vault root, canonical vault-relative path)` for a transfer.

    Both are frozen into the token, so both have to be exactly what the routes
    will later re-derive: the root as `_vault_root` yields it, the path as the
    caller named it.

    **The relative path is normalised lexically, not through `resolve()`.**
    `validate_visible_path` still runs — it is the shared traversal and dot-dir
    guard, and it is what refuses a link that points out of the vault — but its
    *return value* is the resolved path, and resolving follows symlinks. Taking
    the relative path from there would silently retarget the operation: a
    `delete_file("Attachments/alias.png")` where `alias.png` links to
    `secret.png` inside the vault would resolve to `secret.png` and delete
    that, reporting success for a path the caller never named. Keeping the
    caller's own components means the anchored `O_NOFOLLOW` walk in
    `vault_fs` is the thing that meets the symlink, and it refuses it.
    """
    root = _vault_root(uid)
    validate_visible_path(path, user_id=uid)

    rel = PurePosixPath(str(path).replace(os.sep, "/"))
    if rel.is_absolute():
        raise ValueError(f"Path traversal denied: {path}")
    parts = [part for part in rel.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"Path traversal denied: {path}")
    if not parts:
        raise ValueError(f"Not a file path: {path!r}")
    canonical = "/".join(parts)
    if is_hidden_path(canonical):
        raise ValueError(f"Hidden path denied: {path}")
    return str(root), canonical


def _fingerprint_of(root: str, rel_path: str) -> dict | None:
    """The target's identity at mint time, or `None` when it does not exist.

    `None` is meaningful: on an overwrite token it is the expected-*absence*
    sentinel and the publish step requires the target to still be absent.
    """
    root_fd = vault_fs.open_root(root)
    try:
        dir_fd, name = vault_fs.open_parent(root_fd, rel_path, create=False)
    except FileNotFoundError:
        return None  # the parent folder does not exist yet
    finally:
        os.close(root_fd)
    try:
        return vault_fs.fingerprint(
            dir_fd, name, hash_up_to=settings.max_file_write_bytes
        )
    finally:
        os.close(dir_fd)


def _mint_preflight(
    path: str, *, need_write: bool, overwrite: bool = False
) -> tuple | str:
    """Shared front half of the three mint tools: permission, origin, path, FS.

    Returns `(uid, root, rel, base)` or an error string. Every refusal happens
    before a row is written, so a failed mint leaves nothing behind.

    **The filesystem probe runs only for a write.** `probe_publication` creates
    a temp file and a hard link; running it for `request_download` would mean a
    read-only identity's read tool writing to the vault — on a fresh vault, the
    first thing it ever did would be to create files. A download publishes
    nothing, so it needs no proof that publication works.

    **So does the mount check**, for the same reason and one more: it is the
    only check here that can spare a body. A destination on a mount beneath the
    vault root refuses the link or rename that publishes it, and without this
    the refusal arrives after the whole 25 MB has been streamed. A download is
    a read and crosses nothing.
    """
    if need_write and (err := _require_write()):
        return err
    base = settings.public_base_url
    if base is None:
        return _NO_PUBLIC_ORIGIN
    uid = current_user_id.get()
    try:
        root, rel = _vault_context(path, uid)
    except ValueError as e:
        return str(e)
    except RuntimeError as e:  # cold vault-path cache in multi-user mode
        return str(e)
    if need_write:
        try:
            vault_fs.check_publication_support(root)
            # And that the destination is on the staging directory's mount
            # (D23). Publication is a `link`/`rename` out of a root-level
            # staging directory, and both refuse to cross a mount boundary —
            # which the publication probe cannot see, because it links
            # root→root and is cached per root while this is a property of the
            # *pair*. Here, before the row is inserted for `request_upload` and
            # before the fetch begins for `import_from_url`, is the only place
            # a boundary that is *already there* costs a syscall instead of a
            # whole body. A boundary established afterwards is caught inside
            # the publish gate — still pre-publication, but by then the body
            # has streamed.
            vault_fs.check_destination_mount(root, rel, overwrite=overwrite)
        except vault_fs.UnsupportedFilesystem as e:
            return str(e)
        except (OSError, vault_fs.VaultFSError) as e:
            return f"Vault root is not usable: {e}"
    return uid, root, rel, base.rstrip("/")


def _utc_stamp(value) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _expiry_line(row) -> str:
    return _utc_stamp(row.expires_at)


def _clamp_note(window) -> str:
    """Say so when the credential, not the caller, chose the deadline.

    The clamp is invisible otherwise — `expires_at` is simply earlier than
    asked — and an agent that quoted the TTL it requested would go on believing
    the link lives that long. Naming the cause is what makes the fix
    (re-authenticate, then mint) reachable.
    """
    if window is None or not window.clamped:
        return ""
    return (
        "NOTE: this link expires when the credential you are authenticated "
        "with does, which is sooner than the lifetime you asked for. "
        "Re-authenticate (or use a longer-lived API key) and mint again if you "
        "need the full window.\n\n"
    )


@_tracked("request_upload", ["path", "overwrite", "expires_in"])
async def request_upload_impl(
    path: str,
    overwrite: bool = False,
    expires_in: int | None = None,
) -> str:
    """Mint a one-shot link a human can drop a file onto."""
    pre = _mint_preflight(path, need_write=True, overwrite=overwrite)
    if isinstance(pre, str):
        return pre
    uid, root, rel, base = pre

    try:
        fingerprint = _fingerprint_of(root, rel)
    except vault_fs.UnsafePath as e:
        return str(e)
    if fingerprint is not None and not overwrite:
        return (
            f"File already exists: {rel}. Pass overwrite=True to replace it "
            "(the link will then refuse to publish if the file changes before "
            "the upload). Nothing was minted."
        )

    # The number the agent is told **must be the number the route enforces**.
    # `/transfer/upload/info` and `PUT /transfer/upload` both derive their cap
    # from the token's stored (normalised) path via `_upload_max_bytes`, so a
    # markdown destination aborts at `MAX_NOTE_BYTES`; printing
    # `MAX_FILE_WRITE_BYTES` here promised 25 MiB over a route that refuses at
    # 10 MiB, which is worse than printing no number at all. `rel` is the
    # normalised path frozen into the token, so this is the same decision the
    # route makes, taken from the same string (#203).
    cap, cap_name = _write_cap_for(rel)

    async with async_session() as session:
        try:
            # `mint_token` reads the credential and decides the deadline itself,
            # in this transaction, immediately before the INSERT — this tool
            # cannot hand it a window, only receive the one it computed.
            token, row, window = await transfer.mint_token(
                session,
                "upload",
                rel,
                overwrite=overwrite,
                identity=_transfer_identity(),
                vault_root=root,
                # On a no-overwrite token the publish is a kernel-linearizable
                # hard link, so there is nothing to compare against; the
                # fingerprint only means anything when we intend to replace.
                expected_fingerprint=fingerprint if overwrite else None,
                expires_in=expires_in,
            )
        except transfer.CredentialNotUsable as e:
            return f"{e} Nothing was minted."

    return (
        f"Upload link for `{rel}` (expires {_expiry_line(row)}):\n\n"
        f"{base}/transfer/upload#{token}\n\n"
        f"{_clamp_note(window)}"
        f"upload_id: {row.public_id}\n"
        f"max_bytes: {cap:,} ({cap_name})\n"
        f"overwrite: {overwrite}\n\n"
        "Give the URL to the person you are helping and ask them to open it — "
        "it is a page with a file picker. Treat it as a secret: anyone holding "
        "it can write this one path once, until it expires. From a shell you "
        "can upload directly instead:\n\n"
        f'  curl -H "Authorization: Bearer <the part after the #>" '
        f'-T <file> {base}/transfer/upload\n\n'
        f"Then call `check_upload(\"{row.public_id}\")` to confirm the bytes "
        "landed and get their sha256. Do not paste the token into a query "
        "string — that would put it in access logs."
    )


def _loggable_upload_id(value) -> str:
    """What `check_upload` logs in place of a malformed `upload_id`.

    An `upload_id` is 22 characters of URL-safe base64 and nothing else. An
    agent that passes the whole `…/transfer/upload#<token>` URL, or the token
    itself, would otherwise put a live capability into `usage_logs` — a table
    the panel renders. Anything off-shape is logged as a fixed marker, so the
    log records *that* the tool was misused without recording the secret.
    """
    return value if transfer.is_public_id(value) else "<invalid>"


@_tracked(
    "check_upload", ["upload_id"], transforms={"upload_id": _loggable_upload_id}
)
async def check_upload_impl(upload_id: str) -> str:
    """Report the state of an upload link this identity minted."""
    if not transfer.is_public_id(upload_id):
        # Refused before the lookup *and* before `_tracked` logs it. The
        # message deliberately does not echo the value back: it may be the
        # token, and the tool result is itself model context.
        return (
            "not found: that is not an upload_id. `check_upload` takes the "
            "`upload_id` from `request_upload` (22 characters), not the upload "
            "URL and not the token after the `#`."
        )
    # The identity is the request's credential as `APIKeyMiddleware` resolved
    # it. `lookup_by_public_id` is what turns it into a *principal*: an API key
    # is one, an OAuth access token is one hour of one, so the OAuth path
    # matches the whole grant family behind the presented token. Without that,
    # the hourly refresh made this tool answer "not minted by this identity"
    # about the agent's own completed upload (#74).
    identity = _transfer_identity()
    async with async_session() as session:
        row = await transfer.lookup_by_public_id(
            session, upload_id, identity=identity, direction="upload"
        )
        # **The liveness re-check runs inside the session**, before it closes.
        # `lookup_by_public_id` matches on public_id/direction/principal and
        # applies no state filter, but the redemption route decides usability
        # from a strictly larger predicate: `PUT /transfer/upload` also
        # requires `resolve_identity_ok(need_write=True)` and
        # `resolve_root_ok`. Reporting a row's own `state` alone therefore told
        # the agent a link was live after an OAuth scope downgrade or a vault
        # reassignment had already made every redemption a uniform 404 — the
        # agent's own status tool contradicting the page the human is looking
        # at. Only `pending`/`claimed` rows need it: a `completed` row records
        # something that already happened and no later revocation unhappens it.
        dead: list[str] = []
        if row is not None and row.state in (
            transfer.STATE_PENDING,
            transfer.STATE_CLAIMED,
        ):
            if not await transfer.resolve_identity_ok(session, row, need_write=True):
                dead.append(
                    "the credential that minted it no longer has write access "
                    "(revoked, downgraded, or expired)"
                )
            if not await transfer.resolve_root_ok(session, row):
                dead.append("the vault root it was minted against has changed")
    if row is None:
        # Also the answer for another identity's upload_id: an agent must not
        # be able to probe for handles it did not mint.
        return f"not found: no upload link with id {upload_id} was minted by this identity."

    # Precise status is allowed here and only here: this side is authenticated
    # and identity-scoped, which is exactly where the transfer design puts
    # detail rather than the uniform 404 the public routes must return.
    dead_note = ""
    if dead:
        dead_note = (
            "\nThis link can no longer be redeemed in any case: "
            + " and ".join(dead)
            + "."
        )

    if row.state == transfer.STATE_COMPLETED:
        return (
            f"completed: {row.path}\n"
            f"size: {row.size:,} bytes\n"
            f"sha256: {row.sha256}\n"
            f"mime: {row.mime}\n"
            f"completed_at: {_utc_stamp(row.completed_at)}"
        )

    # **Claimed is answered before expiry, and never as "never used".** A claim
    # means a stream started, and the one failure the design deliberately
    # strands in `claimed` — `PostPublishFailure` — happens *after* the bytes
    # are in the vault. The old order tested `expires_at` first, so the state
    # an agent is most likely to observe (the TTL is ten minutes) answered
    # "the link was never used", about a file that is sitting at the path.
    if row.state == transfer.STATE_CLAIMED:
        started = _utc_stamp(row.claimed_at) if row.claimed_at else "an unknown time"
        # The same absolute instant the upload route enforces the stream
        # against, compared with the same clock (`transfer.now_utc`). Reading a
        # different clock here is how the two surfaces disagreed about whether
        # a stream was still live.
        deadline = transfer.upload_stream_deadline(row)
        if transfer.now_utc() < deadline:
            return (
                f"uploading: someone is sending {row.path} right now (started "
                f"{started}). The stream has until {_utc_stamp(deadline)}; check "
                "again after that and this tool will say whether the bytes "
                "landed." + dead_note
            )
        return (
            f"unknown: an upload of {row.path} started ({started}) and the "
            "server never recorded how it finished. The bytes may already be "
            "in the vault — a publish can succeed and still fail to record its "
            f"completion. Check `{row.path}` with `list_files` or `read_file` "
            "before you mint another link or tell anyone the file did not "
            "arrive. Do not assume nothing landed." + dead_note
        )

    # `consumed` is hoisted above the expiry test for the same reason
    # `claimed` is: a consumed token *was* used — a stream started and was cut
    # short — so once its TTL passed, the expiry branch answered "was never
    # used" about it too. Unlike `claimed` this one is provably empty: the
    # deadline and idle-timeout paths raise from inside the stream, always
    # before `publish`. So it can say what happened *and* that nothing landed.
    if row.state == transfer.STATE_CONSUMED:
        return (
            f"expired: the upload of {row.path} was cut short (it stalled or ran "
            "past its deadline) and the link is spent. Nothing was published: "
            "the deadline and idle-timeout paths abort before the bytes reach "
            "the vault. Call `request_upload` again for a fresh one."
        )
    # Only a `pending` row reaches here, which is the one state for which
    # "never used" is true.
    if row.expires_at.astimezone(timezone.utc) <= transfer.now_utc():
        return (
            f"expired: the link for {row.path} was never used and can no longer "
            "be redeemed. Call `request_upload` again for a fresh one."
        )
    if dead:
        return (
            f"revoked: the link for {row.path} is no longer redeemable — "
            + " and ".join(dead)
            + ". Nothing has been uploaded through it. Mint a new link with "
            "`request_upload` from a credential that still has write access."
        )
    return (
        f"pending: nothing has been uploaded to {row.path} yet. The link is "
        f"valid until {_expiry_line(row)}."
    )


@_tracked("request_download", ["path", "expires_in"])
async def request_download_impl(path: str, expires_in: int | None = None) -> str:
    """Mint a link a human can download one vault file from."""
    pre = _mint_preflight(path, need_write=False)
    if isinstance(pre, str):
        return pre
    uid, root, rel, base = pre

    try:
        fingerprint = _fingerprint_of(root, rel)
    except vault_fs.UnsafePath as e:
        # Covers both a symlink and a directory: neither is a file we will
        # hand out, and the message says which.
        return str(e)
    if fingerprint is None:
        return f"File not found: {rel}. Nothing was minted."

    try:
        head = _head_bytes(root, rel)
    except OSError as e:
        return f"Could not read {rel}: {e}. Nothing was minted."
    _kind, mime = classify_bytes(head, PurePosixPath(rel).name)

    async with async_session() as session:
        try:
            token, row, window = await transfer.mint_token(
                session,
                "download",
                rel,
                overwrite=False,
                identity=_transfer_identity(),
                vault_root=root,
                expected_fingerprint=fingerprint,
                expires_in=expires_in,
            )
        except transfer.CredentialNotUsable as e:
            return f"{e} Nothing was minted."

    return (
        f"Download link for `{rel}` (expires {_expiry_line(row)}):\n\n"
        f"{base}/transfer/download#{token}\n\n"
        f"{_clamp_note(window)}"
        f"size: {fingerprint['size']:,} bytes\n"
        f"mime: {mime}\n\n"
        "Give the URL to the person you are helping — it is a page with a save "
        "button, and it keeps working until it expires. Treat it as a secret: "
        "anyone holding it can read this one file. From a shell:\n\n"
        f'  curl -H "Authorization: Bearer <the part after the #>" '
        f'-o <file> {base}/transfer/download/file\n\n'
        "The link is bound to the file as it is right now; if it is edited or "
        "replaced the link stops working and you should mint a new one."
    )


def _head_bytes(root: str, rel_path: str, count: int = 8192) -> bytes:
    """First bytes of a vault file, read through anchored descriptors."""
    root_fd = vault_fs.open_root(root)
    try:
        dir_fd, name = vault_fs.open_parent(root_fd, rel_path, create=False)
    finally:
        os.close(root_fd)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        return os.read(fd, count)
    finally:
        os.close(fd)


def _url_host(url) -> str:
    """What `import_from_url` logs in place of the URL.

    A URL is caller-supplied and can carry a credential in its query string, so
    only the host goes to `usage_logs` — enough to audit where the server was
    made to connect, not enough to replay it.
    """
    try:
        return urlsplit(str(url)).hostname or "<no host>"
    except ValueError:
        return "<unparsable>"


@_tracked(
    "import_from_url",
    ["url", "path", "overwrite"],
    transforms={"url": _url_host},
    write_class=True,
)
async def import_from_url_impl(url: str, path: str, overwrite: bool = False) -> str:
    """Fetch a public URL straight into the vault, under the outbound policy."""
    pre = _mint_preflight(path, need_write=True, overwrite=overwrite)
    if isinstance(pre, str):
        return pre
    _uid, root, rel, _base = pre

    try:
        fingerprint = _fingerprint_of(root, rel)
    except vault_fs.UnsafePath as e:
        return str(e)
    if fingerprint is not None and not overwrite:
        return (
            f"File already exists: {rel}. Pass overwrite=True to replace it. "
            "Nothing was fetched."
        )

    # The four fields `stream_to_vault` reads. Not a token row: an import is
    # authenticated by the caller's own MCP identity, so there is no capability
    # to mint — but there is still an identity to re-validate, because the
    # fetch can run for 30 s and the key can die inside that window.
    row = SimpleNamespace(
        vault_root=root,
        path=rel,
        overwrite=overwrite,
        expected_fingerprint=fingerprint if overwrite else None,
    )
    # `.md`-aware (#203): the same cap `write_file` applies, so an import
    # cannot land a markdown file the note tools would refuse.
    cap, cap_name = _write_cap_for(rel)
    identity = _transfer_identity()

    @asynccontextmanager
    async def gate():
        """Lock this caller's own credential and user rows across the publish.

        Same guarantee the upload route's token gate gives, for the tool that
        has no token: a revocation, downgrade, deletion or root reassignment
        committed while the body streams either waits for these locks or beats
        us to them, and in the second case nothing is published.
        """
        async with async_session() as session:
            async with transfer.lock_identity_for_publish(
                session, identity, vault_root=root, need_write=True
            ) as handle:
                yield handle

    try:
        async with transfer.fetch_url_guarded(url, max_bytes=cap) as fetched:
            written = await transfer.stream_to_vault(
                row,
                fetched.chunks,
                max_bytes=cap,
                deadline=time.monotonic() + transfer.DEFAULT_FETCH_DEADLINE,
                idle_timeout=30.0,
                before_publish=gate,
            )
            final_url = fetched.final_url
    except transfer.SSRFError as e:
        return f"Refused to fetch that URL: {e}"
    except transfer.TooLarge as e:
        return f"{e} ({cap_name}). Nothing was written."
    except transfer.QueueTimeout as e:
        # **Needs its own clause**: `QueueTimeout` is deliberately not a
        # `Timeout` subclass (the two are different verdicts about the same
        # request — see `transfer.QueueTimeout`), so without this it left the
        # tool as an exception rather than an in-band refusal. They are
        # siblings, not parent and child, so the order of the two clauses is
        # irrelevant — neither can shadow the other. An MCP tool that raises
        # returns a protocol error to the agent instead of a sentence it can
        # act on, and `_tracked` records the call as a server fault. Nothing
        # was staged and nothing was fetched; the same call may simply be
        # retried.
        return f"{e}. Nothing was written."
    except transfer.Timeout as e:
        return f"{e}. Nothing was written."
    except transfer.PrePublishAborted:
        return (
            f"Your credentials are no longer valid for writing to {rel} (the key "
            "was revoked, downgraded, or repointed while the fetch was in "
            "flight). Nothing was written."
        )
    except transfer.PostPublishFailure as e:
        # The one outcome where "failed" would be a lie. The bytes are at
        # `rel`; only the bookkeeping around them did not finish. An agent told
        # "could not write" retries, and a retry of an import that already
        # landed is either a redundant fetch or — with overwrite — a second
        # write over the first. Say what is actually true instead.
        return (
            f"Imported the file to {rel}, but the server could not finish "
            f"recording the import: {e}\n"
            "The file IS in place. Do not retry blindly — check it with "
            "`read_file` or `list_files` first."
        )
    except vault_fs.Conflict as e:
        return f"{e}. Nothing was written."
    except vault_fs.UnsafePath as e:
        return f"{e}. Nothing was written."
    except vault_fs.UnsupportedFilesystem as e:
        # Not an OSError, so the clause below does not cover it. Reachable
        # from the publish itself and, since #95, from the staging directory
        # refusing to be made private.
        return f"{e}. Nothing was written."
    except OSError as e:
        return f"Could not write {rel}: {e}"

    return (
        f"Imported {written['size']:,} bytes to {rel}\n"
        f"sha256: {written['sha256']}\n"
        f"mime: {written['mime']}\n"
        f"source: {final_url}"
    )


@_tracked("delete_file", ["path", "permanent", "expected_hash"], write_class=True)
async def delete_file_impl(
    path: str, permanent: bool = False, expected_hash: str | None = None
) -> str:
    """Delete a non-markdown vault file, soft by default.

    Confirms the caller's vault assignment immediately before deleting (#88).
    This tool does **not** publish through a `MutableTarget` — it resolves via
    `_vault_context` and walks from its own `vault_fs.open_root(root)` — so the
    target-bound check that covers the note tools cannot reach it. The whole
    delete therefore runs inside the confirmed step and consumes the
    confirmation against the `(user, root)` it resolved for itself, rather than
    being left as an unremarked gap: a destructive operation in a vault the
    caller has been reassigned away from is the same defect as a write into
    one. As everywhere else, the check narrows the window to the deleting call
    and does not close it.
    """
    if err := _precondition_syntax_error("delete_file", expected_hash, path=path):
        return err
    if err := _require_write():
        return err
    uid = current_user_id.get()
    try:
        root, rel = _vault_context(path, uid)
    except (ValueError, RuntimeError) as e:
        return str(e)

    # **Canonicalise first, then refuse.** The markdown guard has to run on the
    # component the filesystem will actually open, because the caller's string
    # and that component are not the same thing: `note.md/.`, `note.md/` and
    # `a//note.md` all reach a `.md` file while failing a naive
    # `path.lower().endswith(".md")`, which is how a note gets deleted by the
    # tool that does not know about the index or the backlink graph.
    if PurePosixPath(rel).name.lower().endswith(".md"):
        return (
            f"{rel} is a markdown note. Use `delete_note` for notes — it is the "
            "tool that knows about the index and about backlinks. `delete_file` "
            "handles everything else."
        )

    # #88, before `check_trash_support` creates anything and before either
    # delete runs. This tool holds no `MutableTarget`, so the confirmation is
    # checked against the vault context it resolved for itself — and the whole
    # delete runs inside the confirmed step, which is what stops an `await`
    # ever being introduced between the two.
    precondition_refusal = None

    def _check_trash():
        if not permanent:
            # Only the soft delete needs the trash to be usable; `permanent=True`
            # is a plain unlink, and probing for it would create `.trash` for a
            # caller who explicitly asked not to use it.
            try:
                vault_fs.check_trash_support(root)
            except vault_fs.UnsupportedFilesystem:
                raise
            except (OSError, vault_fs.VaultFSError) as exc:
                raise _TrashUnusable(f"Vault root is not usable: {exc}") from exc

    def _delete(confirmation):
        nonlocal precondition_refusal
        confirmation.consume(uid, root, f"delete {rel}")
        if expected_hash is not None or settings.write_precondition_required:
            # Use this tool's beneath-root walk, never open_mutable's different
            # path policy. The shared reader fstats and bounds one leaf fd.
            root_fd = vault_fs.open_root(root)
            try:
                parent_fd, name = vault_fs.open_parent(root_fd, rel)
                try:
                    target = SimpleNamespace(parent_fd=parent_fd, name=name, rel=rel)
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise vault_fs.UnsafePath(f"Not a regular file (symlink or directory): {rel}")
                    cap_name, cap_bytes = _file_precondition_cap()
                    incumbent, over_cap = _read_incumbent(target, rel, cap_bytes)
                    if incumbent is None and not over_cap:
                        raise FileNotFoundError(rel)
                    precondition_refusal = _precondition_error(
                        "delete_file", rel, incumbent, expected_hash,
                        over_cap=over_cap, cap_name=cap_name, cap_bytes=cap_bytes,
                    )
                    if precondition_refusal is not None:
                        return None
                    _check_trash()
                    # Keep the validated parent pinned through the destructive
                    # step. No second pathname walk may redirect the delete.
                    if permanent:
                        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            raise vault_fs.UnsafePath(f"Not a regular file: {rel}")
                        os.unlink(name, dir_fd=parent_fd)
                        vault_fs.flush_dir_quietly(parent_fd, f"parent directory of {rel}")
                        return None
                    return vault_fs.soft_delete_at(parent_fd, name, root_fd, label=rel)
                finally:
                    vault_fs.close_quietly(parent_fd, f"source directory for {rel}")
            finally:
                vault_fs.close_quietly(root_fd, f"vault root for {rel}")
        _check_trash()

        root_fd = vault_fs.open_root(root)
        try:
            if permanent:
                vault_fs.remove(root_fd, rel)
                return None
            return vault_fs.soft_delete(root_fd, rel)
        finally:
            # Bare, this close raising `EIO` would discard the return value of
            # a delete that already happened and surface as a generic OSError.
            vault_fs.close_quietly(root_fd, f"vault root for {rel}")

    try:
        err, dest = await _confirmed_publication(uid, _delete)
        if err:
            return err
    except _TrashUnusable as e:
        return str(e)
    except vault_fs.UnsupportedFilesystem as e:
        return str(e)
    except FileNotFoundError:
        return f"File not found: {rel}"
    except vault_fs.UnsafePath as e:
        # A symlink or a directory. Neither is something to delete on the
        # strength of a path an agent chose.
        return str(e)
    except vault_fs.Conflict as e:
        return f"{e}. Nothing was deleted."
    except vault_fs.VaultFSError as e:
        return str(e)
    except OSError as e:
        return f"Failed to delete {rel}: {e}"
    if precondition_refusal is not None:
        return precondition_refusal
    if permanent:
        return f"Permanently deleted {rel}"
    return (
        f"Moved {rel} to {dest}. It is out of the vault's visible tree but still "
        "on disk; pass permanent=True to unlink instead."
    )
