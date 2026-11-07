"""RFC 7523 `private_key_jwt` client authentication for CIMD clients (#420).

A CIMD client whose document declares `token_endpoint_auth_method:
private_key_jwt` authenticates at `/token` (both grants) and `/revoke` with a
signed JWT assertion, verified against the JWKS snapshot pinned on its
synthetic `oauth_clients` row at approval. This module is that verification
and the two pieces of process state it needs:

* `authenticate` — design D6, steps 1–9 in order, each failure a closed
  `AssertionRefusal(sub)`. Exactly one key (from the row's snapshot only) and
  exactly one algorithm are handed to `joserfc`; every claim is judged here,
  never by the library's registry defaults.
* the **replay cache** — 16,384 entries keyed by SHA-256(`iss` "\\n" `jti`),
  held until the assertion's `exp`, per client URL (not per row), purged
  before an insert, and **refusing when full**: a live entry is never evicted,
  because evicting one would make its `jti` replayable. Only an assertion that
  passed steps 1–8 is recorded.
* `prephase` — design D7, the one narrow exception to "`/token` never
  fetches": before the handler opens its main session, an *eligible*
  credential whose row is `private_key_jwt` may refetch that row's pinned
  `jwks_uri` on a kid miss or a snapshot older than 24 h. It owns its short
  sessions, holds **no session, transaction or lock across the network hop**,
  decides no response, and is the only place in the server that refetches.

State is in-process and deliberately not persisted — the `--workers 1`
contract of `docs/architecture/rate-limits.md`. A restart forgets the replay
cache (L1).

Nothing here logs an assertion, a `jti`, a `kid`, key material, an unverified
`iss`, a `jwks_uri` or an `https://` client identifier. The one record this
module emits is `oauth_client_jwks_refetched`, with closed fields only (D10).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import heapq
import json
import logging
import math
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from joserfc import jwk, jwt
from sqlalchemy import column, select, table
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import DateTime, String

from src.config import settings
from src.database import async_session
from src.models.db import OAuthClient, OAuthCode, OAuthToken
from src.oauth import cimd
from src.services import security_events

# ── constants (D11: no settings) ────────────────────────────────────────────

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ALLOWED_ALGS = frozenset({"RS256", "PS256", "ES256"})
ASSERTION_MAX_BYTES = 8192
MAX_LIFETIME = 300
SKEW = 30
JTI_MAX_CHARS = 256
REPLAY_CACHE_ENTRIES = 16384
JWKS_REFETCH_COOLDOWN_SECONDS = 60
REFETCH_COOLDOWN_ENTRIES = 1024
JWKS_MAX_AGE_SECONDS = 86400
KID_MAX_CHARS = 256

#: Header members that would name a key source of the caller's choosing, or
#: extend processing past what this verifier understands. Refused, not ignored.
FORBIDDEN_HEADERS = frozenset({"crit", "jku", "jwk", "x5u", "x5c"})

#: Credential kinds `prephase` accepts, named after their form fields.
CREDENTIAL_KINDS = frozenset({"code", "refresh_token", "token"})

#: Endpoints `authenticate` accepts, and the `aud` values each admits (D6.7).
ENDPOINTS = frozenset({"token", "revoke"})

#: Every sub-reason an `AssertionRefusal` may carry (D10). The dispatcher in
#: `routes.py` renders it as `invalid_client.<sub>`,
#: `invalid_grant.spent_code_<sub>` or the `/revoke` `reason`.
SUBS = frozenset({
    "assertion_missing",
    "secret_presented",
    "assertion_unexpected",
    "assertion_type_invalid",
    "assertion_malformed",
    "assertion_header_invalid",
    "assertion_alg_invalid",
    "assertion_kid_unknown",
    "assertion_key_ambiguous",
    "assertion_signature_invalid",
    "assertion_claims_invalid",
    "assertion_audience_invalid",
    "assertion_expired",
    "assertion_lifetime_exceeded",
    "assertion_not_yet_valid",
    "assertion_replayed",
    "assertion_replay_cache_full",
})

#: The pre-phase's two triggers and three outcomes (D10).
TRIGGERS = frozenset({"kid_miss", "stale"})
OUTCOMES = frozenset({"updated", "kid_still_absent", "failed"})


class _RefetchFailed(Exception):
    """What a joined refetch flight resolves to on failure. Carries nothing."""


class AssertionRefusal(Exception):
    """A failed client assertion, with its closed sub-reason only.

    The message is the sub and nothing else, so no assertion byte can ride
    out in `str(exc)`.
    """

    def __init__(self, sub: str):
        assert sub in SUBS, sub
        super().__init__(sub)
        self.sub = sub


@dataclass(frozen=True)
class Unverified:
    """What the pre-phase may read off an assertion before verifying it."""

    alg: str
    kid: str | None
    iss: str


# ── parsing (no signature) ──────────────────────────────────────────────────


def _b64url_decode(segment: str) -> bytes:
    if not segment or any(
        not (c.isascii() and (c.isalnum() or c in "-_")) for c in segment
    ):
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("not base64url") from None


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate member")
        out[key] = value
    return out


def _json_object(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("not a JSON object") from None
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")
    return value


def _split(assertion) -> tuple[dict, str]:
    """D6 step 2: a bounded, ASCII, three-segment compact JWS with an object
    header. Returns the header and the raw payload segment; `ValueError`
    otherwise."""
    if not isinstance(assertion, str) or not assertion:
        raise ValueError("absent")
    if not assertion.isascii() or len(assertion) > ASSERTION_MAX_BYTES:
        raise ValueError("bounds")
    parts = assertion.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError("not compact JWS")
    return _json_object(_b64url_decode(parts[0])), parts[1]


def _check_header(header: dict, pinned_alg: str | None) -> str:
    """D6 step 3. Returns the alg, or raises `AssertionRefusal`."""
    if FORBIDDEN_HEADERS & header.keys():
        raise AssertionRefusal("assertion_header_invalid")
    kid = header.get("kid")
    if kid is not None and (not isinstance(kid, str) or not kid or len(kid) > KID_MAX_CHARS):
        raise AssertionRefusal("assertion_header_invalid")
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in ALLOWED_ALGS:
        raise AssertionRefusal("assertion_alg_invalid")
    if pinned_alg is not None and alg != pinned_alg:
        raise AssertionRefusal("assertion_alg_invalid")
    return alg


def parse_unverified(form) -> Unverified | None:
    """The assertion's `alg`, `kid` and unverified `iss`, or `None`. Never raises.

    Applies D6 steps 1–3 (type, structure and size, header) without verifying
    the signature, so the pre-phase can gate on them. The result is advisory
    and is never trusted for anything but the decision to refetch.
    """
    try:
        if form.get("client_assertion_type") != ASSERTION_TYPE:
            return None
        header, payload_segment = _split(form.get("client_assertion"))
        alg = _check_header(header, None)
        payload = _json_object(_b64url_decode(payload_segment))
        iss = payload.get("iss")
        if not isinstance(iss, str) or not iss:
            return None
        return Unverified(alg=alg, kid=header.get("kid"), iss=iss)
    except Exception:  # noqa: BLE001 - advisory parse; every failure is None
        return None


# ── key selection ───────────────────────────────────────────────────────────


def _snapshot_keys(snapshot) -> list[dict]:
    if snapshot is None:
        return []
    if hasattr(snapshot, "as_json"):
        snapshot = snapshot.as_json()
    keys = snapshot.get("keys") if isinstance(snapshot, dict) else None
    if not isinstance(keys, list):
        return []
    return [k for k in keys if isinstance(k, dict)]


def snapshot_kids(snapshot) -> frozenset[str]:
    return frozenset(
        k["kid"] for k in _snapshot_keys(snapshot) if isinstance(k.get("kid"), str)
    )


def _fits(key: dict, alg: str) -> bool:
    """The key's type (and curve) fits the alg family (D6 step 4)."""
    if alg in ("RS256", "PS256"):
        return key.get("kty") == "RSA"
    if alg == "ES256":
        return key.get("kty") == "EC" and key.get("crv") == "P-256"
    return False


def _select_key(snapshot, kid: str | None, alg: str) -> dict:
    keys = _snapshot_keys(snapshot)
    if kid is not None:
        matches = [k for k in keys if k.get("kid") == kid]
        if not matches:
            raise AssertionRefusal("assertion_kid_unknown")
        if len(matches) > 1:
            raise AssertionRefusal("assertion_key_ambiguous")
        key = matches[0]
        if not _fits(key, alg) or key.get("alg", alg) != alg:
            raise AssertionRefusal("assertion_alg_invalid")
        return key
    candidates = [k for k in keys if _fits(k, alg) and k.get("alg", alg) == alg]
    if len(candidates) != 1:
        raise AssertionRefusal("assertion_key_ambiguous")
    return candidates[0]


_PUBLIC_MEMBERS = {"RSA": ("kty", "n", "e"), "EC": ("kty", "crv", "x", "y")}


def _import_public(key: dict):
    members = _PUBLIC_MEMBERS.get(key.get("kty"), ())
    return jwk.import_key({m: key[m] for m in members if m in key})


# ── the replay cache ────────────────────────────────────────────────────────

_replay: dict[str, float] = {}
_replay_heap: list[tuple[float, str]] = []


def _replay_key(iss: str, jti: str) -> str:
    return hashlib.sha256(f"{iss}\n{jti}".encode("utf-8", "surrogatepass")).hexdigest()


def _replay_purge(now: float) -> None:
    while _replay_heap and _replay_heap[0][0] <= now:
        exp, key = heapq.heappop(_replay_heap)
        if _replay.get(key) == exp:
            del _replay[key]


def _replay_consume(iss: str, jti: str, exp: float, now: float) -> None:
    """Record `jti` under `iss` until `exp`, or raise. Never evicts a live entry."""
    _replay_purge(now)
    key = _replay_key(iss, jti)
    if key in _replay:
        raise AssertionRefusal("assertion_replayed")
    if len(_replay) >= REPLAY_CACHE_ENTRIES:
        raise AssertionRefusal("assertion_replay_cache_full")
    _replay[key] = exp
    heapq.heappush(_replay_heap, (exp, key))


def replay_cache_size() -> int:
    return len(_replay)


# ── authenticate (D6) ───────────────────────────────────────────────────────


def _is_number(value) -> bool:
    """A NumericDate: a finite int or float, never a bool.

    NaN passes every comparison the time checks make, and an infinite or
    float-overflowing integer breaks their arithmetic, so neither is a date.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def _epoch(now) -> float:
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.timestamp()
    return float(now)


def _accepted_audiences(endpoint: str) -> frozenset[str]:
    issuer = settings.base_url
    accepted = {issuer, f"{issuer}/token"}
    if endpoint == "revoke":
        accepted.add(f"{issuer}/revoke")
    return frozenset(accepted)


def authenticate(form, *, metadata_url, snapshot, pinned_alg, endpoint, now) -> None:
    """Authenticate a `private_key_jwt` row's request, or raise `AssertionRefusal`.

    `metadata_url`, `snapshot` and `pinned_alg` are the **credential-resolved**
    row's `client_id_metadata_url`, `jwks_snapshot` and
    `token_endpoint_auth_signing_alg`, read under the main transaction's
    locks. `endpoint` is `"token"` or `"revoke"`. `now` is a `datetime` (or
    epoch seconds), captured once by the caller.

    The dispatcher's two pre-checks are repeated first so the order is one
    definition: no assertion → `assertion_missing`; a `client_secret`, with or
    without an assertion → `secret_presented` (RFC 6749 §2.3). Then D6 1–9.
    """
    if endpoint not in ENDPOINTS:
        raise ValueError(f"unknown endpoint {endpoint!r}")
    if not isinstance(metadata_url, str) or not metadata_url:
        # A row without a URL is not a key-authenticated CIMD row; fail closed.
        raise AssertionRefusal("assertion_claims_invalid")
    assertion = form.get("client_assertion")
    if not assertion:
        raise AssertionRefusal("assertion_missing")
    if form.get("client_secret"):
        raise AssertionRefusal("secret_presented")
    now_s = _epoch(now)

    # 1. The assertion type, exactly.
    if form.get("client_assertion_type") != ASSERTION_TYPE:
        raise AssertionRefusal("assertion_type_invalid")

    # 2. Structure and size.
    try:
        header, _ = _split(assertion)
    except ValueError:
        raise AssertionRefusal("assertion_malformed") from None

    # 3. Header.
    alg = _check_header(header, pinned_alg)

    # 4. Key selection, from the row's snapshot only.
    key = _select_key(snapshot, header.get("kid"), alg)

    # 5. One key, one algorithm.
    try:
        decoded = jwt.decode(assertion, _import_public(key), algorithms=[alg])
    except Exception:  # noqa: BLE001 - every library failure is one closed sub
        raise AssertionRefusal("assertion_signature_invalid") from None
    if decoded.header.get("alg") != alg:
        raise AssertionRefusal("assertion_signature_invalid")
    claims = decoded.claims
    if not isinstance(claims, dict):
        raise AssertionRefusal("assertion_claims_invalid")

    # 6. iss / sub (and, defence in depth, a submitted client_id).
    if claims.get("iss") != metadata_url or claims.get("sub") != metadata_url:
        raise AssertionRefusal("assertion_claims_invalid")
    submitted = form.get("client_id")
    if submitted and submitted != metadata_url:
        raise AssertionRefusal("assertion_claims_invalid")

    # 7. aud: one value, in the endpoint's accepted set.
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) == 1:
        aud = aud[0]
    if not isinstance(aud, str) or aud not in _accepted_audiences(endpoint):
        raise AssertionRefusal("assertion_audience_invalid")

    # 8. Time. Types first, then zero-leeway exp, then the bounds.
    exp, iat, nbf = claims.get("exp"), claims.get("iat"), claims.get("nbf")
    if not _is_number(exp):
        raise AssertionRefusal("assertion_claims_invalid")
    if (iat is not None and not _is_number(iat)) or (nbf is not None and not _is_number(nbf)):
        raise AssertionRefusal("assertion_claims_invalid")
    if not exp > now_s:
        raise AssertionRefusal("assertion_expired")
    if exp - now_s > MAX_LIFETIME + SKEW:
        raise AssertionRefusal("assertion_lifetime_exceeded")
    if iat is not None:
        if iat > now_s + SKEW:
            raise AssertionRefusal("assertion_not_yet_valid")
        if exp - iat > MAX_LIFETIME:
            raise AssertionRefusal("assertion_lifetime_exceeded")
    if nbf is not None and nbf > now_s + SKEW:
        raise AssertionRefusal("assertion_not_yet_valid")

    # 9. jti, then consume it.
    jti = claims.get("jti")
    if not isinstance(jti, str) or not 1 <= len(jti) <= JTI_MAX_CHARS:
        raise AssertionRefusal("assertion_claims_invalid")
    _replay_consume(metadata_url, jti, float(exp), now_s)


# ── the pre-phase (D7) ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _RowView:
    """Plain values read in the pre-phase's short session (no ORM objects
    survive it, so nothing can lazy-load after the session closed)."""

    client_id: str
    metadata_url: str
    jwks_uri: str
    pinned_alg: str | None
    kids: frozenset[str]
    fetched_at: datetime | None


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _clock() -> datetime:
    """The token endpoint's clock: `routes._now`, so one seam governs both."""
    from src.oauth import routes

    return routes._now()


def _eligible(kind: str, cred, client, form, now: datetime) -> bool:
    """D7 step 2: every check the main phase makes of the credential that
    needs no network I/O, with the predicates `routes.py` uses. Advisory."""
    from src.oauth import routes
    from src.oauth.grants import clamp_token_expiry, grant_deadline

    if cred is None or client is None:
        return False
    if getattr(client, "token_endpoint_auth_method", None) != "private_key_jwt":
        return False
    metadata_url = getattr(client, "client_id_metadata_url", None)
    if not isinstance(metadata_url, str) or not getattr(client, "jwks_uri", None):
        return False
    submitted = form.get("client_id")
    if submitted:
        if cimd.is_internal_id(submitted):
            return False
        if not routes._caller_matches_row(cred.client_id, metadata_url, submitted):
            return False
    if kind == "code":
        if cred.used or _aware(cred.expires_at) < now:
            return False
        redirect_uri = form.get("redirect_uri")
        if not redirect_uri or cred.redirect_uri != redirect_uri:
            return False
        verifier = form.get("code_verifier")
        if not isinstance(verifier, str) or not routes._PKCE_RE.fullmatch(verifier):
            return False
        return secrets.compare_digest(routes._base64url_sha256(verifier), cred.code_challenge)
    # refresh_token / token: unrevoked, unexpired, inside the grant's lifetime.
    if cred.revoked or _aware(cred.expires_at) < now:
        return False
    return clamp_token_expiry(now, grant_deadline(cred.grant_issued_at)) is not None


async def _load(kind: str, credential: str):
    """The credential row and its client row, by hash, in one short session
    that is closed before returning. No `FOR UPDATE`, no write."""
    from src.oauth import routes

    digest = routes._hash(credential)
    async with async_session() as session:
        if kind == "code":
            query = select(OAuthCode).where(OAuthCode.code_hash == digest)
        elif kind == "refresh_token":
            query = select(OAuthToken).where(
                OAuthToken.token_hash == digest, OAuthToken.token_type == "refresh"
            )
        else:
            query = select(OAuthToken).where(OAuthToken.token_hash == digest)
        cred = (await session.execute(query)).scalars().first()
        if cred is None:
            return None, None
        client = (
            await session.execute(
                select(OAuthClient).where(OAuthClient.client_id == cred.client_id)
            )
        ).scalar_one_or_none()
        return cred, client


async def _read(kind: str, credential: str, form, now: datetime) -> _RowView | None:
    cred, client = await _load(kind, credential)
    if not _eligible(kind, cred, client, form, now):
        return None
    return _RowView(
        client_id=client.client_id,
        metadata_url=client.client_id_metadata_url,
        jwks_uri=client.jwks_uri,
        pinned_alg=getattr(client, "token_endpoint_auth_signing_alg", None),
        kids=snapshot_kids(getattr(client, "jwks_snapshot", None)),
        fetched_at=_aware(getattr(client, "jwks_fetched_at", None)),
    )


#: The CAS write is built on a lightweight table so it does not depend on the
#: ORM mapping's attribute set; the column names are migration 038's.
_clients = table(
    "oauth_clients",
    column("client_id", String),
    column("jwks_uri", String),
    column("jwks_snapshot", JSONB),
    column("jwks_fetched_at", DateTime(timezone=True)),
)


def _cas_statement(row_client_id: str, jwks_uri: str, snapshot):
    t = snapshot.fetched_at
    return (
        sa_update(_clients)
        .where(
            _clients.c.client_id == row_client_id,
            _clients.c.jwks_uri == jwks_uri,
            _clients.c.jwks_fetched_at < t,
        )
        .values(jwks_snapshot=snapshot.as_json(), jwks_fetched_at=t)
    )


async def _cas_write(row_client_id: str, jwks_uri: str, snapshot) -> int:
    """D7 step 4 in its own short transaction. Returns the rows written (0/1)."""
    async with async_session() as session:
        result = await session.execute(_cas_statement(row_client_id, jwks_uri, snapshot))
        await session.commit()
        return result.rowcount or 0


# Process state for the refetch: per-`jwks_uri` cooldown and in-flight joins.
_refetch_cooldown: "OrderedDict[str, float]" = OrderedDict()
_refetch_flights: dict[str, asyncio.Future] = {}


def _mono() -> float:
    return time.monotonic()


def _cooldown_active(uri: str) -> bool:
    started = _refetch_cooldown.get(uri)
    if started is None:
        return False
    if _mono() - started >= JWKS_REFETCH_COOLDOWN_SECONDS:
        del _refetch_cooldown[uri]
        return False
    return True


def _cooldown_arm(uri: str) -> None:
    _refetch_cooldown.pop(uri, None)
    _refetch_cooldown[uri] = _mono()
    while len(_refetch_cooldown) > REFETCH_COOLDOWN_ENTRIES:
        _refetch_cooldown.popitem(last=False)


def _fresh(fetched_at: datetime | None, now: datetime) -> bool:
    return fetched_at is not None and (now - fetched_at).total_seconds() <= JWKS_MAX_AGE_SECONDS


def _cache_qualifies(hit, view: _RowView, unverified: Unverified,
                     kid_miss: bool, stale: bool, now: datetime) -> bool:
    if hit is None:
        return False
    if kid_miss and unverified.kid not in hit.kids:
        return False
    if stale:
        hit_at = _aware(hit.fetched_at)
        if not _fresh(hit_at, now):
            return False
        if view.fetched_at is not None and not hit_at > view.fetched_at:
            return False
    return True


def _failure_reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason.startswith("jwks_"):
        return reason
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "jwks_fetch_timeout"
    return "jwks_fetch_failed"


def _emit_refetched(view: _RowView, *, trigger, outcome, reason, route, client_ip) -> None:
    security_events.emit(
        "oauth_client_jwks_refetched",
        level=logging.WARNING if outcome == "failed" else logging.INFO,
        subject=security_events.subject_for(ip=client_ip),
        client_id=view.client_id,
        cimd_host=cimd.host_for_log(view.metadata_url),
        client_ip=client_ip,
        trigger=trigger,
        outcome=outcome,
        reason=reason,
        route=route,
    )


async def _refetch(view: _RowView, unverified: Unverified, *, trigger: str,
                   route, client_ip):
    """Join, fetch. Returns a validated snapshot or None. Emits exactly one
    record for an actual attempt, none for a join or a busy cap.

    The process-wide in-flight cap of `cimd.GLOBAL_IN_FLIGHT` is enforced by
    `cimd.fetch_jwks_guarded` itself (`fetch_busy`), which also joins an
    `/authorize` flight of the same URI and fills the JWKS cache with the
    origin's own cache directive. This function therefore takes no slot of
    its own (one refetch is one slot) and never writes the cache (a second
    put would replace the origin's TTL with the default).
    """
    uri = view.jwks_uri
    flight = _refetch_flights.get(uri)
    if flight is not None:
        try:
            return await asyncio.shield(flight)
        except Exception:  # noqa: BLE001 - the attempter recorded the failure
            return None
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _refetch_flights[uri] = future
    snapshot = None
    reason = None
    busy = False
    try:
        snapshot = await cimd.fetch_jwks_guarded(uri)
    except Exception as exc:  # noqa: BLE001 - every failure is a closed reason
        busy = getattr(exc, "reason", None) == "fetch_busy"
        reason = _failure_reason(exc)
        future.set_exception(_RefetchFailed())
        future.exception()  # retrieved: a flight nobody joined must not warn
    except BaseException:
        future.set_exception(_RefetchFailed())
        future.exception()
        raise
    else:
        future.set_result(snapshot)
    finally:
        # Every attempt, success or failure, arms the cooldown (D7 step 5).
        # A busy cap is not an attempt: nothing left the process.
        if not busy:
            _cooldown_arm(uri)
        _refetch_flights.pop(uri, None)
    if busy:
        return None
    if snapshot is None:
        _emit_refetched(view, trigger=trigger, outcome="failed", reason=reason,
                        route=route, client_ip=client_ip)
        return None
    outcome = (
        "kid_still_absent"
        if unverified.kid is not None and unverified.kid not in snapshot.kids
        else "updated"
    )
    _emit_refetched(view, trigger=trigger, outcome=outcome, reason=None,
                    route=route, client_ip=client_ip)
    return snapshot


async def prephase(form, *, credential_kind, credential, route, client_ip) -> None:
    """D7: refetch the credential-resolved row's pinned JWKS when due. Never
    decides a response; never raises (except a catalogue violation under the
    test suite's strict mode, so drift cannot hide here).

    `credential_kind` is the form field that carries the credential: `"code"`
    or `"refresh_token"` at `/token`, `"token"` at `/revoke`. Call it **before**
    `async with async_session()` in the handler; it opens and closes its own
    short sessions and holds none across the fetch.
    """
    try:
        await _prephase(form, credential_kind, credential, route, client_ip)
    except security_events.SecurityEventFieldError:
        raise
    except Exception:  # noqa: BLE001 - advisory; the main phase decides
        return


async def _prephase(form, kind, credential, route, client_ip) -> None:
    if kind not in CREDENTIAL_KINDS:
        raise ValueError(f"unknown credential kind {kind!r}")
    unverified = parse_unverified(form)
    if unverified is None:
        return
    if not isinstance(credential, str) or not credential:
        return
    now = _clock()
    view = await _read(kind, credential, form, now)  # session closed here
    if view is None:
        return
    if unverified.iss != view.metadata_url:
        return
    if view.pinned_alg is not None and unverified.alg != view.pinned_alg:
        return  # the main phase refuses on the header; a fetch cannot help
    kid_miss = unverified.kid is not None and unverified.kid not in view.kids
    stale = not _fresh(view.fetched_at, now)
    if not (kid_miss or stale):
        return
    trigger = "kid_miss" if kid_miss else "stale"

    hit = cimd.jwks_cache_get(view.jwks_uri)
    if _cache_qualifies(hit, view, unverified, kid_miss, stale, now):
        await _cas_write(view.client_id, view.jwks_uri, hit)
        return
    # Cooldown, then join. The cooldown is armed when an attempt *ends*, so
    # a flight in progress is joined rather than skipped.
    if _cooldown_active(view.jwks_uri):
        return
    snapshot = await _refetch(view, unverified, trigger=trigger, route=route,
                              client_ip=client_ip)
    if snapshot is None:
        return
    await _cas_write(view.client_id, view.jwks_uri, snapshot)


# ── tests ───────────────────────────────────────────────────────────────────


def reset_state_for_tests() -> None:
    _replay.clear()
    _replay_heap.clear()
    _refetch_cooldown.clear()
    _refetch_flights.clear()
