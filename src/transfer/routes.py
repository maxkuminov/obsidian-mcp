"""The public `/transfer/*` routes — the only session-less write path in the app.

Everything here is redeemed with a **capability**, not a session: there is no
`APIKeyMiddleware` in front of these routes and no contextvars to read. Each
handler therefore derives every fact it acts on from the token row it just read
out of the database — path, vault root, overwrite flag, expected fingerprint,
minting identity — and never from the request. A request may say only *which*
token it holds.

Three properties are load-bearing and easy to break by accident:

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

from src.config import settings
from src.database import async_session
from src.limiter import limiter
from src.models.db import UsageLog
from src.services import transfer, vault_fs
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
    """A usable token row for `direction`, or `None` — the whole 404 matrix.

    Identity, root and path are re-derived from the database on every call.
    A token is a snapshot of a permission; this is where we check the snapshot
    still describes reality.
    """
    need_write = direction == "upload"
    row = await transfer.lookup_token(session, token, direction=direction)
    if row is None:
        return None
    if not await transfer.resolve_identity_ok(session, row, need_write=need_write):
        return None
    if not await transfer.resolve_root_ok(session, row):
        return None
    if not _path_ok(row.vault_root, row.path):
        return None
    return row


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
        return _not_found()
    async with async_session() as session:
        row = await _load_valid(session, token, direction="upload")
        if row is None:
            return _not_found()
        payload = {
            "path": row.path,
            "max_bytes": settings.max_file_write_bytes,
            "mime": mimetypes.guess_type(row.path)[0],
            "expires_at": _aware(row.expires_at).isoformat(),
            "overwrite": bool(row.overwrite),
        }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.api_route("/download/info", methods=["GET", "HEAD"])
@limiter.limit(PAGE_LIMIT)
async def download_info(request: Request, token: str | None = Depends(_bearer)) -> Response:
    if token is None:
        return _not_found()
    async with async_session() as session:
        row = await _load_valid(session, token, direction="download")
        if row is None:
            return _not_found()
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
    if token is None:
        return _not_found()

    async with async_session() as session:
        # (1) Claim first. Committed, conditional, linearizable — and *before*
        # a single body byte is read.
        row = await transfer.claim_upload(session, token)
        if row is None:
            return _not_found()

        # (2) Re-validate everything the token asserts, from the database.
        if (
            not await transfer.resolve_identity_ok(session, row, need_write=True)
            or not await transfer.resolve_root_ok(session, row)
            or not _path_ok(row.vault_root, row.path)
        ):
            await transfer.release_claim(session, row)
            return _not_found()

        try:
            # Publication only: the trash probe belongs to `delete_file`, and
            # this route never soft-deletes anything.
            vault_fs.check_publication_support(row.vault_root)
        except vault_fs.MountBoundary as exc:
            # Before the generic branch: `MountBoundary` subclasses
            # `UnsupportedFilesystem` so every existing surface keeps answering
            # it, which makes the ordering here load-bearing rather than
            # cosmetic.
            logger.error("Transfer refused at a mount boundary: %s", exc)
            await transfer.release_claim(session, row)
            return JSONResponse(dict(MOUNT_BOUNDARY_BODY), status_code=503)
        except vault_fs.UnsupportedFilesystem as exc:
            logger.error("Transfer refused: %s", exc)
            await transfer.release_claim(session, row)
            return JSONResponse(dict(UNSUPPORTED_FS_BODY), status_code=503)
        except (OSError, vault_fs.VaultFSError) as exc:
            logger.error("Vault root unusable for transfer: %s", exc)
            await transfer.release_claim(session, row)
            return _not_found()

        token_id = row.id

        @asynccontextmanager
        async def gate():
            """Lock, re-validate, hold the locks across `publish`, then commit.

            The transaction opened here stays open while the bytes are linked
            into place. That is the whole point: a revocation, a permission
            downgrade, a root reassignment or a cascade delete all need these
            same rows, so each either waits for us or beats us — there is no
            interleaving where we see a valid key and then publish under a
            revoked one.

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
                max_bytes=settings.max_file_write_bytes,
                content_length=_content_length(request),
                deadline=_upload_deadline(row),
                idle_timeout=30.0,
                before_publish=gate,
            )
        except transfer.TooLarge as exc:
            await transfer.release_claim(session, row)
            return JSONResponse({"error": str(exc)}, status_code=413)
        except transfer.Timeout as exc:
            # The stream was terminated mid-flight; a retry must mint afresh.
            await transfer.consume(session, row)
            return JSONResponse({"error": str(exc)}, status_code=408)
        except transfer.PrePublishAborted:
            await transfer.release_claim(session, row)
            return _not_found()
        except transfer.PostPublishFailure:
            # The bytes landed; only the bookkeeping failed. Releasing here
            # would hand back a replayable token over a path that is already
            # written, so the claim stands until the TTL expires.
            logger.exception("Upload published but not recorded (token left claimed)")
            raise
        except vault_fs.Conflict as exc:
            await transfer.release_claim(session, row)
            return JSONResponse({"error": str(exc)}, status_code=409)
        except vault_fs.UnsafePath as exc:
            await transfer.release_claim(session, row)
            return JSONResponse({"error": str(exc)}, status_code=409)
        except vault_fs.MountBoundary as exc:
            # The in-gate check, or the residual `EXDEV`/`EBUSY` from the
            # publish itself. Pre-publication either way, so the claim is
            # released and the same link may be retried once the mount is gone.
            # **Must stay above the `UnsupportedFilesystem` branch** — it is a
            # subclass, and Python takes the first match.
            logger.error("Transfer refused at a mount boundary: %s", exc)
            await transfer.release_claim(session, row)
            return JSONResponse(dict(MOUNT_BOUNDARY_BODY), status_code=503)
        except vault_fs.UnsupportedFilesystem:
            await transfer.release_claim(session, row)
            return JSONResponse(dict(UNSUPPORTED_FS_BODY), status_code=503)
        except ClientDisconnect:
            await transfer.release_claim(session, row)
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
            logger.exception("Upload failed before publication (claim released)")
            await transfer.release_claim(session, row)
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
        return _not_found()

    async with async_session() as session:
        row = await _load_valid(session, token, direction="download")
        if row is None:
            return _not_found()

        try:
            fd, st = _open_bound_file(row)
        except (FileNotFoundError, vault_fs.VaultFSError, OSError):
            # Deleted, replaced by a symlink, turned into a directory — all the
            # same uniform 404. Nothing here tells the caller which.
            return _not_found()

        # The descriptor is closed on every exit but one: the streaming
        # response, which takes ownership of it and closes it when the body is
        # done. A `return` inside this block — a fingerprint mismatch, a HEAD —
        # must not leak it, and a mismatching token can be retried at the rate
        # limit, so a leak here is a slow file-descriptor exhaustion.
        handed_off = False
        try:
            want = row.expected_fingerprint or {}
            if not _fingerprint_matches(want, st):
                return _not_found()
            if want.get("sha256") is not None and _sha256_fd(fd) != want["sha256"]:
                # An in-place edit that preserved length and restored mtime is
                # invisible to the metadata compare; the re-hash is what closes
                # it. Above `MAX_FILE_WRITE_BYTES` the mint recorded no hash and
                # the binding really is metadata-only (documented limitation).
                return _not_found()

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
