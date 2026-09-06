"""The public `/transfer/*` routes — the only session-less write path in the app.

Everything here is redeemed with a **capability**, not a session: there is no
`APIKeyMiddleware` in front of these routes and no contextvars to read. Each
handler therefore derives every fact it acts on from the token row it just read
out of the database — path, vault root, overwrite flag, expected fingerprint,
minting identity — and never from the request. A request may say only *which*
token it holds.

Four properties are load-bearing and easy to break by accident:

1. **The token is redeemed from the `Authorization` header only.** The
   human-facing URLs put it in the *fragment* (`…/transfer/upload#<token>`),
   which browsers never send. A token in the path or query string is ignored,
   so an accidental paste into a URL bar cannot be redeemed out of an access
   log. `_bearer` reads exactly one place.
2. **`PUT` claims before it reads.** `transfer.claim_upload` is a committed
   conditional `UPDATE`; only after it returns a row does the handler touch
   `request.stream()`. An unknown token with a multi-gigabyte body costs a
   header parse and one indexed lookup.
3. **Every non-usable token gets the same 404.** Unknown, expired, consumed,
   claimed by someone else, minted by a since-revoked key, minted for a user
   whose root was reassigned — one body, one status. `_not_found()` is the only
   way to say no. (The claim is about the *response*, not about latency: the
   branches do different amounts of work and nothing here is constant-time.)
4. **No session is held across a wait or a stream.** A pooled connection is a
   shared, 15-deep resource on a single worker; holding one while bytes trickle
   in from a client, or while a request queues for an upload slot, converts one
   tenant's slow upload into every tenant's 500 (#208). `upload` therefore
   commits and closes phase 1 before it waits or reads, and `download_file`
   closes its session before returning the `StreamingResponse`. Every later
   database action opens its own short-lived session.

`GZipMiddleware` is app-wide, so `download/file` sets `Content-Encoding:
identity`; Starlette's middleware leaves a response that already declares an
encoding alone, which is what keeps `GET` and `HEAD` agreeing on
`Content-Length`.
"""
from __future__ import annotations

import datetime
import errno
import hashlib
import logging
import mimetypes
import os
import secrets
import stat
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import ClientDisconnect

from src.config import MAX_NOTE_BYTES, settings
from src.database import async_session
from src.limiter import limiter
from src.models.db import UsageLog
from src.services import security_events, transfer, vault_fs
from src.services.vault import classify_bytes, is_hidden_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/transfer", tags=["transfer"])

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "control_panel", "templates")
)

# Per-IP limits (D9). The pages and the metadata endpoints are cheap; the two
# endpoints that move bytes are not.
PAGE_LIMIT = "30/minute"
BYTES_LIMIT = "10/minute"

_READ_CHUNK = 64 * 1024
_MIME_SNIFF_BYTES = 8192

# One body for every refusal. Built fresh per response so no handler can
# accidentally mutate a shared dict, but always from this constant.
NOT_FOUND_BODY = {"error": "not found"}

UNSUPPORTED_FS_BODY = {
    "error": (
        "The vault filesystem does not support the atomic no-clobber "
        "publication this transfer requires."
    )
}

# A mount boundary is a *different* refusal and must not be collapsed into the
# one above, which would tell the person their filesystem cannot do atomic
# publication when it can — and would be flatly false for an `overwrite=True`
# link, which does not use the no-clobber publish at all.
#
# **Path-free, deliberately.** This route is bearer-protected and unauthenticated
# beyond the token, so it says *what kind* of thing refused and nothing about
# where the vault keeps anything. The path, and the boundary's exact side, come
# from the authenticated surfaces: the mint tools' error and `check_upload`.
# Every unknown, expired, consumed or otherwise unusable token stays on the
# uniform 404 — this is reached only for a token that was valid and whose
# destination stopped being publishable after it was minted.
MOUNT_BOUNDARY_BODY = {
    "error": (
        "The destination is on a different mount from the vault's staging "
        "directory, or is itself a mount point, so this transfer cannot be "
        "published there. The filesystem is fine; the mount layout is what "
        "refuses."
    )
}


def _not_found() -> JSONResponse:
    """The single refusal. Never add a reason: the reason is the oracle."""
    return JSONResponse(dict(NOT_FOUND_BODY), status_code=404)


def _bearer(request: Request) -> str | None:
    """The token from `Authorization: Bearer …`, or `None`.

    Header only — deliberately not `request.query_params` or a path segment.
    A caller who puts the token in the URL has already leaked it to every log
    between here and them; honouring it too would make the leak useful.
    """
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


# ── validation shared by every bearer-protected endpoint ────────────────────


def _aware(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _path_ok(vault_root: str, rel_path: str) -> bool:
    """Re-run the vault path guards against the token's *stored* root.

    Not `validate_visible_path`: that resolves the root through
    `vault._vault_root(user_id)`, i.e. through the process-local cache these
    routes are specifically forbidden to trust (D4). The rules are the same —
    no escape, no dot-directory component — applied to the root the token
    committed to and the database just confirmed.
    """
    if not rel_path or is_hidden_path(rel_path):
        return False
    try:
        root = Path(vault_root)
        resolved = (root / rel_path).resolve()
        resolved.relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


async def _load_valid(session, token: str, *, direction: str):
    """A usable token row for `direction`, or why not — the whole 404 matrix.

    Identity, root and path are re-derived from the database on every call.
    A token is a snapshot of a permission; this is where we check the snapshot
    still describes reality.

    Three return shapes, and the distinction is only about what the *log* can
    say — every caller answers all three with the same `_not_found()`:

    * a row — the token is usable;
    * a `TransferRefusal` — one of the three predicates *this function*
      evaluates said no, so the reason is already known and no diagnosis read
      is needed;
    * `None` — `lookup_token` returned nothing, and that one query collapses
      unknown, expired, wrong-direction, claimed, completed and consumed into
      a single answer by design. The caller diagnoses that case afterwards,
      behind a permit (`transfer.classify_token_refusal`).

    The predicates, their order and their outcomes are unchanged; only the
    value carrying "no" got more specific.
    """
    need_write = direction == "upload"
    row = await transfer.lookup_token(session, token, direction=direction)
    if row is None:
        return None
    if not await transfer.resolve_identity_ok(session, row, need_write=need_write):
        return transfer.TransferRefusal("credential_invalid", row)
    if not await transfer.resolve_root_ok(session, row):
        return transfer.TransferRefusal("root_reassigned", row)
    if not _path_ok(row.vault_root, row.path):
        return transfer.TransferRefusal("path_invalid", row)
    return row


def _refused(outcome) -> bool:
    """Did `_load_valid` refuse? True for both refusal shapes, never for a row.

    Deliberately **not** `isinstance(outcome, TransferToken)`: the route tests
    stand in a plain object for the ORM row, and a type check here would make
    the routes untestable without a database for no gain.
    """
    return outcome is None or isinstance(outcome, transfer.TransferRefusal)


async def _refuse(
    request: Request,
    *,
    reason: str | None = None,
    refusal=None,
    token: str | None = None,
    session=None,
    direction: str | None = None,
    row=None,
) -> JSONResponse:
    """Record why, then return the one refusal. The response is never affected.

    The order is the whole point, and it is the order of design D8:

    1. The admission decision has **already been taken** by the caller. Nothing
       below can change it, and every path through here returns the same
       `_not_found()`.
    2. Acquire the suppression permit **first**, keyed on the trusted client
       address — the only subject computable before the diagnosis, and the one
       a caller cannot mint by rotating bogus bearer tokens. An accepted
       request never reaches this function at all, and a refusal whose source
       is already at its allowance stops here, so neither pays for a read.
    3. Only with a permit in hand, and only when the reason is not already
       known, run the read-only diagnosis. It issues a database read on a path
       whose entire contract is a fixed 404, so it is wrapped in its own
       `try/except`: a dead connection or an exhausted pool answers
       `diagnosis_failed` and the 404 goes out unchanged. A diagnosis must
       never turn a refusal into a 500.

    Identity is carried only where a row actually resolved; `token_tag` only
    where a token was actually presented. Nothing is invented for a record.
    """
    permit = security_events.acquire(
        "transfer_refused",
        subject=security_events.subject_for(request=request),
        level=logging.WARNING,
    )
    if permit is None:
        return _not_found()

    if refusal is not None:
        reason = refusal.reason
        if row is None:
            row = refusal.row

    error_type = None
    if reason is None:
        # The collapsed case: `lookup_token`/`claim_upload` said no and cannot
        # say why. Diagnose it now, outside the decision, behind the permit.
        try:
            diagnosed = await transfer.classify_token_refusal(
                session, token, direction=direction
            )
            reason = diagnosed.reason
            if row is None:
                row = diagnosed.row
        except Exception as exc:  # noqa: BLE001 - a read here may not 500 the route
            reason = "diagnosis_failed"
            error_type = type(exc).__name__

    security_events.emit(
        permit,
        level=logging.WARNING,
        reason=reason,
        token_tag=security_events.redacted_token_tag(token),
        route=request.url.path,
        method=request.method,
        client_ip=security_events.client_ip(request),
        user_id=getattr(row, "user_id", None),
        key_id=getattr(row, "key_id", None),
        oauth_token_id=getattr(row, "oauth_token_id", None),
        error_type=error_type,
    )
    return _not_found()


def _log_row(row, tool: str, params: dict, response_size: int | None = None) -> UsageLog:
    """A `usage_logs` row attributed to the *minting* identity.

    The request carried no identity of its own, so attribution comes from the
    token. `params` never contains the token: the transfer's public id and its
    bound path are enough to correlate, and a log line is exactly the place a
    capability must not appear.

    The FKs alone were not attribution (issue #92). Both are allowed to lose
    their target while the log row stays — deleting an OAuth client cascades
    its tokens and `usage_logs.oauth_token_id` is ON DELETE SET NULL, and the
    panel NULLs a key's `usage_logs.key_id` by hand before deleting the key —
    so every `upload_file` / `download_file` line rendered "unknown" after the
    one action an operator takes when they suspect a credential. The three
    `actor_*` values were recorded on the token at mint, from the credential
    that request authenticated with, and are copied across here. A row minted
    before migration 017 carries NULLs and keeps its old shape: nothing is
    invented for it, and the panel still attributes it by join.
    """
    return UsageLog(
        key_id=row.key_id,
        oauth_token_id=row.oauth_token_id,
        user_id=row.user_id,
        tool=tool,
        params=params,
        response_size=response_size,
        actor_kind=row.actor_kind,
        actor_label=row.actor_label,
        actor_ref=row.actor_ref,
    )


# ── static pages ────────────────────────────────────────────────────────────


def _page(request: Request, template: str) -> Response:
    """Render one of the two transfer pages with a per-response nonce CSP.

    The page is identical for every token — it renders nothing token- or
    path-specific server-side, so the only injection surface is the JSON→DOM
    step, which the script does with `textContent`. The nonce CSP is the belt to
    that braces: no external origin, no inline script but ours, no form posts.
    """
    nonce = secrets.token_urlsafe(16)
    response = templates.TemplateResponse(request, template, {"nonce": nonce})
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        "connect-src 'self'; "
        "form-action 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.api_route("/upload", methods=["GET", "HEAD"])
@limiter.limit(PAGE_LIMIT)
async def upload_page(request: Request) -> Response:
    return _page(request, "transfer_upload.html")


@router.api_route("/download", methods=["GET", "HEAD"])
@limiter.limit(PAGE_LIMIT)
async def download_page(request: Request) -> Response:
    return _page(request, "transfer_download.html")


# ── metadata ────────────────────────────────────────────────────────────────


@router.api_route("/upload/info", methods=["GET", "HEAD"])
@limiter.limit(PAGE_LIMIT)
async def upload_info(request: Request, token: str | None = Depends(_bearer)) -> Response:
    if token is None:
        return await _refuse(request, reason="missing_token")
    async with async_session() as session:
        row = await _load_valid(session, token, direction="upload")
        if _refused(row):
            return await _refuse(
                request,
                refusal=row,
                token=token,
                session=session,
                direction="upload",
            )
        payload = {
            "path": row.path,
            "max_bytes": _upload_max_bytes(row),
            "mime": mimetypes.guess_type(row.path)[0],
            "expires_at": _aware(row.expires_at).isoformat(),
            "overwrite": bool(row.overwrite),
        }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.api_route("/download/info", methods=["GET", "HEAD"])
@limiter.limit(PAGE_LIMIT)
async def download_info(request: Request, token: str | None = Depends(_bearer)) -> Response:
    if token is None:
        return await _refuse(request, reason="missing_token")
    async with async_session() as session:
        row = await _load_valid(session, token, direction="download")
        if _refused(row):
            return await _refuse(
                request,
                refusal=row,
                token=token,
                session=session,
                direction="download",
            )
        fingerprint = row.expected_fingerprint or {}
        payload = {
            "path": row.path,
            "size": fingerprint.get("size"),
            "mime": mimetypes.guess_type(row.path)[0],
            "expires_at": _aware(row.expires_at).isoformat(),
        }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


# ── upload ──────────────────────────────────────────────────────────────────


def _upload_deadline(row) -> datetime.datetime:
    """The stream deadline, as the **absolute UTC instant** the tool reports.

    Both the arithmetic (`min(expires_at, claimed_at +
    TRANSFER_MAX_UPLOAD_SECONDS)`) and the clock it is measured against are
    shared with `check_upload`, and both halves are load-bearing. A copy of the
    arithmetic would drift. Converting to `time.monotonic()` here — which is
    what this used to do — kept the arithmetic shared but split the *clock*: the
    route froze the deadline into the monotonic domain at claim time while the
    status tool kept comparing wall clocks, so a realtime step made the two
    describe different instants, and the tool would call a stream live that the
    route had already killed. `transfer._deadline_remaining` measures this
    against `transfer.now_utc()`, the one clock both surfaces read.
    """
    return transfer.upload_stream_deadline(row)


def _upload_max_bytes(row) -> int:
    """The byte cap this transfer is held to: the `.md` note cap, or the file cap.

    The indexer treats *any* `.md` as a note, so the cap follows the extension
    rather than the tool: a markdown file that lands here over `MAX_NOTE_BYTES`
    is one `create_note`/`edit_note` would have refused, and the index then has
    to parse it anyway. The bound is the *smaller* of the two so an operator who
    lowers `MAX_FILE_WRITE_BYTES` below 10 MiB is not surprised by a more
    permissive limit for markdown than for everything else.

    Both `/transfer/upload/info` and `PUT /transfer/upload` read it, deliberately
    through one function: the consent page prints "Maximum size" from the info
    payload, and a page that advertises 25 MiB over a route that aborts at
    10 MiB would be worse than no number at all.
    """
    if row.path.lower().endswith(".md"):
        return min(MAX_NOTE_BYTES, settings.max_file_write_bytes)
    return settings.max_file_write_bytes


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@router.put("/upload")
@limiter.limit(BYTES_LIMIT)
async def upload(request: Request, token: str | None = Depends(_bearer)) -> Response:
    """Claim, validate, *let go of the connection*, then stream.

    **Phase 1 holds a pooled database connection; nothing after it does.** The
    handler used to keep one `async_session()` open around the whole body —
    `claim_upload` committed, but the two re-validation queries immediately
    autobegan a fresh transaction that was never committed, so the connection
    stayed checked out across the semaphore wait *and* the entire stream. The
    pool is 5 + 10 on a single worker, so 15 slow uploads from one tenant pinned
    every connection and every other caller — MCP tools, OAuth `/token`, the
    panel — got a `TimeoutError` → 500 after `pool_timeout` (#208). A
    `statement_timeout` cannot help: those backends are idle *in transaction*.

    `download_file` already had the right shape (commit, close, then hand back
    a `StreamingResponse`); upload was the outlier, not a considered exception.
    The rule now, for anything under `src/transfer/`: **no session is held
    across a wait or a stream.** Phase 1 commits and exits; the claimed row is
    used detached afterwards and every later database action — `release_claim`,
    `consume`, the publish gate — opens its own short-lived session.

    The detached row is safe to read because `TransferToken` has no
    relationships and no deferred columns, `claim_upload` commits under
    `expire_on_commit=False`, and `close()` expunges without expiring; only
    plain columns are read after phase 1. A test asserts exactly that, with the
    session closed.
    """
    if token is None:
        return await _refuse(request, reason="missing_token")

    # ── phase 1: claim + re-validate, then hand the connection back ─────────
    async with async_session() as session:
        # (1) Claim first. Committed, conditional, linearizable — and *before*
        # a single body byte is read.
        row = await transfer.claim_upload(session, token)
        if row is None:
            return await _refuse(
                request,
                token=token,
                session=session,
                direction="upload",
            )

        # (2) Re-validate everything the token asserts, from the database.
        # The `or` chain is split into an `elif` ladder so the *log* can name
        # which of the three said no. Evaluation is identical — each predicate
        # still runs only if its predecessors passed — and the response is the
        # same 404 for all three. Only the record got more specific.
        revalidation = None
        if not await transfer.resolve_identity_ok(session, row, need_write=True):
            revalidation = "credential_invalid"
        elif not await transfer.resolve_root_ok(session, row):
            revalidation = "root_reassigned"
        elif not _path_ok(row.vault_root, row.path):
            revalidation = "path_invalid"
        if revalidation is not None:
            await transfer.release_claim(session, row)
            return await _refuse(
                request, reason=revalidation, token=token, row=row
            )

        try:
            # Publication only: the trash probe belongs to `delete_file`, and
            # this route never soft-deletes anything.
            vault_fs.check_publication_support(row.vault_root)
        except vault_fs.MountBoundary as exc:
            # Before the generic branch: `MountBoundary` subclasses
            # `UnsupportedFilesystem` so every existing surface keeps answering
            # it, which makes the ordering here load-bearing rather than
            # cosmetic.
            security_events.emit(
                "transfer_refused_mount_boundary",
                level=logging.ERROR,
                subject=security_events.subject_for(
                    user_id=row.user_id, request=request
                ),
                error_type=type(exc).__name__,
                route=request.url.path,
                method=request.method,
            )
            await transfer.release_claim(session, row)
            return JSONResponse(dict(MOUNT_BOUNDARY_BODY), status_code=503)
        except vault_fs.UnsupportedFilesystem as exc:
            security_events.emit(
                "transfer_refused_unsupported_fs",
                level=logging.ERROR,
                subject=security_events.subject_for(
                    user_id=row.user_id, request=request
                ),
                error_type=type(exc).__name__,
                route=request.url.path,
                method=request.method,
            )
            await transfer.release_claim(session, row)
            return JSONResponse(dict(UNSUPPORTED_FS_BODY), status_code=503)
        except (OSError, vault_fs.VaultFSError) as exc:
            security_events.emit(
                "transfer_root_unusable",
                level=logging.ERROR,
                subject=security_events.subject_for(
                    user_id=row.user_id, request=request
                ),
                error_type=type(exc).__name__,
                user_id=row.user_id,
                route=request.url.path,
                method=request.method,
            )
            await transfer.release_claim(session, row)
            return await _refuse(
                request, reason="publication_unsupported", token=token, row=row
            )

        # **Commit before the session closes.** `claim_upload` commits, but the
        # `resolve_identity_ok` / `resolve_root_ok` / `lock_for_publish`-free
        # reads after it autobegan a fresh read transaction that nothing ever
        # ended, so the connection went back to the pool having its rollback
        # issued by `close()` rather than by this handler. The behaviour is the
        # same either way; what changes is that the code now says what the spec
        # says — phase 1 *commits* and exits — so a future reader cannot mistake
        # the implicit rollback for a considered choice. Safe for the detached
        # row: the sessionmaker is `expire_on_commit=False`.
        await session.commit()

        token_id = row.id

    # ── phase 2: no connection held from here to the response ───────────────
    # Every handler below opens its own short session. They take the detached
    # `row` because that is the existing signature, and they read only `row.id`
    # from it.

    async def release() -> None:
        async with async_session() as s:
            await transfer.release_claim(s, row)

    async def consume() -> None:
        async with async_session() as s:
            await transfer.consume(s, row)

    @asynccontextmanager
    async def gate():
        """Lock, re-validate, hold the locks across `publish`, then commit.

        The transaction opened here stays open while the bytes are linked
        into place. That is the whole point: a revocation, a permission
        downgrade, a root reassignment or a cascade delete all need these
        same rows, so each either waits for us or beats us — there is no
        interleaving where we see a valid key and then publish under a
        revoked one.

        It has always opened its own session; what changed with #208 is that
        it is no longer *nested* inside a longer-lived one, so a streaming
        upload now pins one connection for the length of the gate rather than
        two for the length of the request.

        `stream_to_vault` calls `handle.complete(result, published=…)` the
        instant the bytes are in place and before the context closes, so
        the completion row and the usage-log row are written by *this*
        transaction — the one holding the locks — rather than by a later
        one that a revocation could slip in front of.
        """
        async with async_session() as inner:
            async with inner.begin():
                locked = await transfer.lock_for_publish(inner, token_id)
                ok = locked is not None and transfer.locked_rows_ok(
                    locked, need_write=True
                )

                async def record(result: dict, published: bool) -> None:
                    if not published:  # pragma: no cover - defensive
                        return
                    await transfer.complete_upload(
                        inner,
                        locked.token,
                        result["size"],
                        result["sha256"],
                        result["mime"],
                        commit=False,
                    )
                    inner.add(
                        _log_row(
                            locked.token,
                            "upload_file",
                            {"path": locked.token.path, "size": result["size"]},
                            response_size=result["size"],
                        )
                    )

                yield transfer.GateHandle(
                    ok=ok, session=inner, on_complete=record if ok else None
                )

    try:
        result = await transfer.stream_to_vault(
            row,
            request.stream(),
            max_bytes=_upload_max_bytes(row),
            content_length=_content_length(request),
            deadline=_upload_deadline(row),
            idle_timeout=30.0,
            before_publish=gate,
        )
    except transfer.TooLarge as exc:
        await release()
        return JSONResponse({"error": str(exc)}, status_code=413)
    except transfer.QueueTimeout as exc:
        # The server was busy, not the capability expired. Nothing was staged
        # and the token's own window is still open, so the claim goes back to
        # `pending` and the *same* link may be retried — which is why this is
        # not the 408/`consumed` path. `Retry-After` is a hint, not a promise:
        # the queue is shared and the next attempt races like the first.
        #
        # Reached only after the claim and the re-validation succeeded, so it
        # says nothing about a token that was never usable — the uniform 404
        # still covers every one of those.
        await release()
        return JSONResponse(
            {"error": str(exc)}, status_code=503, headers={"Retry-After": "5"}
        )
    except transfer.Timeout as exc:
        # The stream was terminated mid-flight — or never started, because the
        # deadline ran out while it queued. Either way a retry must mint afresh.
        await consume()
        return JSONResponse({"error": str(exc)}, status_code=408)
    except transfer.PrePublishAborted:
        # The locked gate refused. `GateHandle` exposes only `ok`, so this one
        # reason covers a revocation, a permission downgrade, a root
        # reassignment and a cascade delete alike — an accepted limitation
        # (design D8, residual R9), not an oversight.
        await release()
        return await _refuse(
            request, reason="prepublish_revalidation_failed", token=token, row=row
        )
    except transfer.PostPublishFailure:
        # The bytes landed; only the bookkeeping failed. Releasing here
        # would hand back a replayable token over a path that is already
        # written, so the claim stands until the TTL expires.
        security_events.emit(
            "transfer_post_publish_failure",
            level=logging.ERROR,
            subject=security_events.subject_for(user_id=row.user_id, request=request),
            exc_info=True,
            user_id=row.user_id,
            route=request.url.path,
        )
        raise
    except vault_fs.Conflict as exc:
        await release()
        return JSONResponse({"error": str(exc)}, status_code=409)
    except vault_fs.UnsafePath as exc:
        await release()
        return JSONResponse({"error": str(exc)}, status_code=409)
    except vault_fs.MountBoundary as exc:
        # The in-gate check, or the residual `EXDEV`/`EBUSY` from the
        # publish itself. Pre-publication either way, so the claim is
        # released and the same link may be retried once the mount is gone.
        # **Must stay above the `UnsupportedFilesystem` branch** — it is a
        # subclass, and Python takes the first match.
        security_events.emit(
            "transfer_refused_mount_boundary",
            level=logging.ERROR,
            subject=security_events.subject_for(user_id=row.user_id, request=request),
            error_type=type(exc).__name__,
            route=request.url.path,
            method=request.method,
        )
        await release()
        return JSONResponse(dict(MOUNT_BOUNDARY_BODY), status_code=503)
    except vault_fs.UnsupportedFilesystem:
        await release()
        return JSONResponse(dict(UNSUPPORTED_FS_BODY), status_code=503)
    except ClientDisconnect:
        await release()
        raise
    except Exception:
        # Everything else out of `stream_to_vault` is *demonstrably*
        # pre-publication: a full disk while writing the staged body, a
        # database error opening the gate, a walk that failed. The helper
        # raises `PostPublishFailure` — handled above — for every failure
        # once the bytes are in place, so reaching here means nothing was
        # published and no temp file survives. Releasing is then the
        # correct answer: the human gets to retry the same link instead of
        # holding a capability that is stuck until its TTL for a transfer
        # that never touched the vault.
        security_events.emit(
            "transfer_prepublish_failure",
            level=logging.ERROR,
            subject=security_events.subject_for(user_id=row.user_id, request=request),
            exc_info=True,
            user_id=row.user_id,
            route=request.url.path,
        )
        await release()
        raise

    # Reaching here means the gate recorded `result` and its transaction
    # committed: `stream_to_vault` raises `PostPublishFailure` otherwise.
    return JSONResponse(
        {
            "path": row.path,
            "size": result["size"],
            "sha256": result["sha256"],
            "mime": result["mime"],
        }
    )


# ── download ────────────────────────────────────────────────────────────────


def _sha256_fd(fd: int) -> str:
    """Hash the open descriptor, then rewind it.

    Hashing *this* descriptor rather than reopening the name is the point: the
    bytes we verify are provably the bytes we are about to send, even if the
    name is replaced a microsecond later.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _fingerprint_matches(want: dict, st: os.stat_result) -> bool:
    return (
        want.get("dev") == st.st_dev
        and want.get("inode") == st.st_ino
        and want.get("size") == st.st_size
        and want.get("mtime_ns") == st.st_mtime_ns
        and want.get("ctime_ns") == st.st_ctime_ns
    )


def _open_bound_file(row) -> tuple[int, os.stat_result]:
    """Open the token's bound path `O_NOFOLLOW` through anchored directories."""
    root_fd = vault_fs.open_root(row.vault_root)
    try:
        dir_fd, name = vault_fs.open_parent(root_fd, row.path, create=False)
    finally:
        os.close(root_fd)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise vault_fs.UnsafePath(f"Refusing to follow a symlink: {row.path}")
            raise
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise vault_fs.UnsafePath(f"Not a regular file: {row.path}")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _disposition(name: str) -> str:
    """`attachment` with an ASCII fallback and an RFC 5987 UTF-8 form.

    CR, LF and quotes are stripped before either form is built — a filename is
    attacker-influenced content that ends up in a response header, which is
    exactly the shape of a header-injection bug.
    """
    cleaned = "".join(c for c in name if c not in '\r\n"\\')
    ascii_name = cleaned.encode("ascii", "ignore").decode("ascii").strip() or "download"
    quoted = urllib.parse.quote(cleaned or "download", safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


async def _stream_fd(fd: int, size: int):
    try:
        remaining = size
        while remaining > 0:
            chunk = os.read(fd, min(_READ_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        os.close(fd)


@router.api_route("/download/file", methods=["GET", "HEAD"])
@limiter.limit(BYTES_LIMIT)
async def download_file(request: Request, token: str | None = Depends(_bearer)) -> Response:
    if token is None:
        return await _refuse(request, reason="missing_token")

    async with async_session() as session:
        row = await _load_valid(session, token, direction="download")
        if _refused(row):
            return await _refuse(
                request,
                refusal=row,
                token=token,
                session=session,
                direction="download",
            )

        try:
            fd, st = _open_bound_file(row)
        except (FileNotFoundError, vault_fs.VaultFSError, OSError):
            # Deleted, replaced by a symlink, turned into a directory — all the
            # same uniform 404. Nothing here tells the caller which.
            return await _refuse(
                request, reason="file_unreadable", token=token, row=row
            )

        # The descriptor is closed on every exit but one: the streaming
        # response, which takes ownership of it and closes it when the body is
        # done. A `return` inside this block — a fingerprint mismatch, a HEAD —
        # must not leak it, and a mismatching token can be retried at the rate
        # limit, so a leak here is a slow file-descriptor exhaustion.
        handed_off = False
        try:
            want = row.expected_fingerprint or {}
            if not _fingerprint_matches(want, st):
                return await _refuse(
                    request, reason="fingerprint_mismatch", token=token, row=row
                )
            if want.get("sha256") is not None and _sha256_fd(fd) != want["sha256"]:
                # An in-place edit that preserved length and restored mtime is
                # invisible to the metadata compare; the re-hash is what closes
                # it. Above `MAX_FILE_WRITE_BYTES` the mint recorded no hash and
                # the binding really is metadata-only (documented limitation).
                return await _refuse(
                    request, reason="content_changed", token=token, row=row
                )

            head = os.read(fd, _MIME_SNIFF_BYTES)
            os.lseek(fd, 0, os.SEEK_SET)
            _kind, mime = classify_bytes(head, PurePosixPath(row.path).name)

            headers = {
                "Content-Length": str(st.st_size),
                # Bypass the app-wide GZipMiddleware: it leaves a response that
                # already declares an encoding alone, so GET and HEAD agree on
                # Content-Length even under `Accept-Encoding: gzip`.
                "Content-Encoding": "identity",
                "Content-Disposition": _disposition(PurePosixPath(row.path).name),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
                # Range is ignored rather than supported; saying so is honest.
                "Accept-Ranges": "none",
            }

            if request.method == "HEAD":
                return Response(status_code=200, media_type=mime, headers=headers)

            session.add(
                _log_row(
                    row,
                    "download_file",
                    {"path": row.path, "size": st.st_size},
                    response_size=st.st_size,
                )
            )
            await session.commit()
            handed_off = True
        finally:
            if not handed_off:
                os.close(fd)

    return StreamingResponse(
        _stream_fd(fd, st.st_size), media_type=mime, headers=headers
    )
