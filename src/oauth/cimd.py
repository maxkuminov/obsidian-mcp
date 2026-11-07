"""OAuth Client ID Metadata Documents (#357, MCP 2026-07-28, CIMD draft-00).

A CIMD client names itself by an `https` URL; this module turns that URL into
a validated **projection** the authorization server can act on, under the
bounds an unauthenticated caller cannot escape:

* `validate_client_id_url` — the D1 syntax and canonical-form rules, before
  any DNS or connection.
* `resolve` — cache → join an in-progress fetch (charged nothing) → cooldown →
  per-address budget (the failed-auth algorithm: fixed salted slots) → one
  fetch in flight per slot → eight globally → the transfer subsystem's
  guarded fetch with no redirects, 10 s, 64 KiB.
* `validate_document` — the D4 structure rules, producing a `Projection`.
  A document may declare `none` or `private_key_jwt` (#420); the latter needs
  a same-origin `jwks_uri`, no inline `jwks`, and an optional pinned alg.
* `resolve_jwks` — a `private_key_jwt` client's JWKS at `/authorize`, under
  the document's admission chain with its **own** charge and its own cache;
  `validate_jwks` reduces it to a normalised `JwksSnapshot`.
* `fetch_jwks_guarded` — the same guarded fetch and validation with no
  address charge, for the `/token` pre-phase refetch (`client_assertion`).
* `redirect_uri_allowed` — exact match, plus port-agnostic loopback for CIMD.
* `cimd_row_id` — the synthetic `oauth_clients.client_id`, an HMAC under a
  `SECRET_KEY`-derived key so nobody without the secret can compute it.

Nothing here logs. Every refusal is a `CimdRefusal` carrying a closed
sub-reason; `src/oauth/routes.py` turns it into the constant `invalid_client`
response and the one `oauth_authorize_refused` record. See
`docs/architecture/oauth-and-grants.md`, "Client ID Metadata Documents".
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

from joserfc.jwk import ECKey, RSAKey

from src.config import settings
from src.oauth.scope import VALID_SCOPES, has_vault_scope
from src.oauth.trust import normalize_redirect_uri, to_ascii_host
from src.services import rate_limits, transfer

CIMD_PREFIX = "https://"
INTERNAL_PREFIX = "cimd_"
DEFAULT_CLIENT_SCOPE = "read readwrite offline_access"

MAX_URL_CHARS = 2048
FETCH_DEADLINE_SECONDS = 10.0
MAX_DOCUMENT_BYTES = 65536
DEFAULT_TTL_SECONDS = 300
FAILURE_COOLDOWN_SECONDS = 30.0
POSITIVE_CACHE_ENTRIES = 256
COOLDOWN_ENTRIES = 1024
ADDRESS_TABLE_SIZE = 4096
ADDRESS_WINDOW_SECONDS = 60.0
PER_SLOT_IN_FLIGHT = 1
GLOBAL_IN_FLIGHT = 8
MAX_REDIRECT_URIS = 20
MAX_REDIRECT_URI_CHARS = 2048
MAX_CLIENT_NAME_CHARS = 255
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")

ID_KEY_LABEL = b"oauth-cimd-client-id/v1"

#: The token-endpoint authentication methods a CIMD document may declare.
AUTH_METHOD_NONE = "none"
AUTH_METHOD_PRIVATE_KEY_JWT = "private_key_jwt"
AUTH_METHODS = (AUTH_METHOD_NONE, AUTH_METHOD_PRIVATE_KEY_JWT)
#: The assertion algorithms a `private_key_jwt` client may use (D1, D3).
SIGNING_ALGS = ("RS256", "PS256", "ES256")

# JWKS bounds (D3, D11): constants, never settings.
JWKS_MAX_KEYS = 16
RSA_MIN_BITS = 2048
RSA_MAX_BITS = 8192
RSA_MIN_EXPONENT = 3
RSA_MAX_EXPONENT = 2**32
JWKS_CACHE_ENTRIES = 256
MAX_KID_CHARS = 256
#: Members that make a JWK private or symmetric; any one refuses the set.
PRIVATE_KEY_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})

#: Every sub-reason a refusal may carry; `client_metadata.<reason>` in the log.
REASONS = frozenset({
    "internal_id_submitted", "url_invalid", "url_not_canonical",
    "fetch_cooldown", "fetch_budget_exhausted", "fetch_busy", "ssrf_refused",
    "fetch_failed", "fetch_timeout", "http_status", "too_large",
    "content_encoded", "not_json", "client_id_mismatch", "document_invalid",
    "redirect_uris_invalid", "auth_method_unsupported", "secret_present",
    "grant_types_invalid", "scope_invalid", "redirect_uri_mismatch",
    "metadata_changed",
    # #420: private_key_jwt documents (D1) and the approve downgrade (D5).
    "jwks_uri_invalid", "jwks_uri_cross_origin", "jwks_inline_refused",
    "signing_alg_unsupported", "auth_method_downgrade",
    # #420: the JWKS fetch and validation (D2, D3).
    "jwks_fetch_failed", "jwks_fetch_timeout", "jwks_http_status",
    "jwks_too_large", "jwks_content_encoded", "jwks_ssrf_refused",
    "jwks_not_json", "jwks_invalid", "jwks_private_key", "jwks_no_usable_key",
})

#: A document-fetch reason → its JWKS counterpart (D2).
_JWKS_FETCH_REASONS = {
    "fetch_failed": "jwks_fetch_failed",
    "fetch_timeout": "jwks_fetch_timeout",
    "http_status": "jwks_http_status",
    "too_large": "jwks_too_large",
    "content_encoded": "jwks_content_encoded",
    "ssrf_refused": "jwks_ssrf_refused",
    "not_json": "jwks_not_json",
}


class CimdRefusal(Exception):
    """A CIMD refusal with its closed sub-reason and, when known, the host."""

    def __init__(self, reason: str, host: str | None = None):
        assert reason in REASONS, reason
        super().__init__(reason)
        self.reason = reason
        self.host = host


# ── identification ─────────────────────────────────────────────────────────


def is_cimd_client_id(value) -> bool:
    return isinstance(value, str) and value.startswith(CIMD_PREFIX)


def is_internal_id(value) -> bool:
    return isinstance(value, str) and value.startswith(INTERNAL_PREFIX)


def host_for_log(value) -> str | None:
    """The ASCII host of a submitted https client_id, or None. Never raises."""
    if not is_cimd_client_id(value):
        return None
    try:
        return to_ascii_host(urlsplit(value).hostname)
    except Exception:  # noqa: BLE001 - a logging helper may not raise
        return None


def ref_for_log(value) -> str | None:
    """First 16 hex of SHA-256 over the submitted value: correlation, not content."""
    if not isinstance(value, str):
        return None
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def submitted_for_log(value):
    """What may go in `client_id_submitted`: never an https client_id (D12)."""
    return None if is_cimd_client_id(value) else value


class SubmittedFields(NamedTuple):
    client_id_submitted: object
    cimd_host: str | None
    cimd_ref: str | None


def submitted_fields(value, *, resolved: bool = False) -> SubmittedFields:
    """The one definition of how a submitted client_id is logged (D12).

    An `https://` value is never logged: `cimd_host` and the 16-hex `cimd_ref`
    stand in for it. A DCR value goes in `client_id_submitted` unless a row
    already `resolved` (then only the row's `client_id` may appear, D15).
    Every OAuth emission site that records a submitted client identifier
    takes its three fields from here.
    """
    if is_cimd_client_id(value):
        return SubmittedFields(None, host_for_log(value), ref_for_log(value))
    return SubmittedFields(None if resolved else value, None, None)


def validate_client_id_url(value: str) -> str:
    """Return the canonical host of a valid CIMD URL, or raise `CimdRefusal`.

    Runs before any DNS resolution or connection.
    """
    host = host_for_log(value)
    if (
        len(value) > MAX_URL_CHARS
        or any(not (0x21 <= ord(c) <= 0x7E) for c in value)
        or "?" in value
        or "#" in value
    ):
        raise CimdRefusal("url_invalid", host)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise CimdRefusal("url_invalid", host) from None
    if "@" in parts.netloc or parts.path in ("", "/"):
        raise CimdRefusal("url_invalid", host)
    for segment in parts.path.split("/"):
        if unquote(segment) in (".", ".."):
            raise CimdRefusal("url_invalid", host)
    try:
        canonical = transfer.canonicalise(value, allow_http=False)
    except transfer.SSRFError:
        raise CimdRefusal("url_invalid", host) from None
    if canonical.url != value:
        raise CimdRefusal("url_not_canonical", host)
    return canonical.host


# ── the synthetic row id ───────────────────────────────────────────────────


def _id_key() -> bytes:
    return hmac.new(settings.secret_key.encode(), ID_KEY_LABEL, hashlib.sha256).digest()


def cimd_row_id(url: str, user_id: int | None) -> str:
    """`cimd_` + 59 hex of HMAC-SHA256(K, url "\\n" owner); 64 characters (D6)."""
    owner = str(user_id) if user_id is not None else "-"
    digest = hmac.new(_id_key(), f"{url}\n{owner}".encode(), hashlib.sha256).hexdigest()
    return INTERNAL_PREFIX + digest[:59]


# ── redirect URIs ──────────────────────────────────────────────────────────


def _authority(uri: str) -> tuple[str, str, str] | None:
    """(`host literal`, `port text`, `rest`) of an `http://` URI, or None."""
    if not uri.startswith("http://"):
        return None
    remainder = uri[len("http://"):]
    cut = len(remainder)
    for sep in "/?#":
        position = remainder.find(sep)
        if position != -1:
            cut = min(cut, position)
    netloc, rest = remainder[:cut], remainder[cut:]
    if "@" in netloc or "#" in rest:
        return None
    if netloc.startswith("["):
        close = netloc.find("]")
        if close == -1:
            return None
        host, after = netloc[: close + 1], netloc[close + 1:]
        if after and not after.startswith(":"):
            return None
        port = after[1:] if after else ""
    else:
        host, _, port = netloc.partition(":")
    if port and not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
        return None
    return host, port, rest


def _loopback_entry(uri: str) -> bool:
    parsed = _authority(uri)
    return parsed is not None and parsed[0] in LOOPBACK_HOSTS


def _https_entry(uri: str) -> str | None:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.netloc or parts.fragment or "#" in uri:
        return None
    return normalize_redirect_uri(uri)


def is_loopback_uri(uri: str) -> bool:
    return _loopback_entry(uri)


def redirect_uri_allowed(registered, requested, *, cimd: bool) -> bool:
    """One matching function for GET and POST /authorize (D5)."""
    if not isinstance(requested, str):
        return False
    if requested in registered:
        return True
    if not cimd:
        return False
    req = _authority(requested)
    if req is None or req[0] not in LOOPBACK_HOSTS:
        return False
    for entry in registered:
        reg = _authority(entry)
        if reg is None or reg[0] not in LOOPBACK_HOSTS:
            continue
        if reg[0] == req[0] and reg[2] == req[2]:
            return True
    return False


# ── the projection ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Projection:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    scope: str
    #: `none` or `private_key_jwt` (#420). The last two are None for `none`.
    auth_method: str = AUTH_METHOD_NONE
    jwks_uri: str | None = None
    signing_alg: str | None = None

    @property
    def authorization_hash(self) -> str:
        """SHA-256 of the authorization projection only (D4, Codex R2-2).

        A `none` projection hashes exactly the pre-#420 object: the three
        key-source members are omitted, not set to `none`/null, so a consent
        in flight across the deploy still matches. A `private_key_jwt`
        projection adds the method, the `jwks_uri` and the alg (or null),
        never key material, which rotates.
        """
        hashed: dict = {
            "client_id": self.client_id,
            "redirect_uris": sorted(self.redirect_uris),
            "scope": " ".join(sorted(self.scope.split())),
        }
        if self.auth_method != AUTH_METHOD_NONE:
            hashed["token_endpoint_auth_method"] = self.auth_method
            hashed["jwks_uri"] = self.jwks_uri
            hashed["token_endpoint_auth_signing_alg"] = self.signing_alg
        canonical = json.dumps(hashed, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def loopback_only(self) -> bool:
        return all(is_loopback_uri(uri) for uri in self.redirect_uris)


def _has_control(value: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def validate_document(url: str, document, host: str | None = None) -> Projection:
    if not isinstance(document, dict):
        raise CimdRefusal("not_json", host)
    if document.get("client_id") != url or not isinstance(document.get("client_id"), str):
        raise CimdRefusal("client_id_mismatch", host)
    name = document.get("client_name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_CLIENT_NAME_CHARS:
        raise CimdRefusal("document_invalid", host)
    # PostgreSQL text cannot hold NUL; a persisted string with one would fail
    # at the upsert instead of answering the constant refusal (Codex impl R1).
    if "\x00" in name:
        raise CimdRefusal("document_invalid", host)
    if "client_secret" in document or "client_secret_expires_at" in document:
        raise CimdRefusal("secret_present", host)
    # The declared method is honoured, never downgraded: a document that
    # declares private_key_jwt is key-authenticated even if another member
    # lists `none` (D1).
    method = document.get("token_endpoint_auth_method", AUTH_METHOD_NONE)
    if not isinstance(method, str) or method not in AUTH_METHODS:
        raise CimdRefusal("auth_method_unsupported", host)
    jwks_uri = signing_alg = None
    if method == AUTH_METHOD_PRIVATE_KEY_JWT:
        jwks_uri, signing_alg = _key_source(url, document, host)
    grant_types = document.get("grant_types")
    if grant_types is not None and (
        not isinstance(grant_types, list) or "authorization_code" not in grant_types
    ):
        raise CimdRefusal("grant_types_invalid", host)
    response_types = document.get("response_types")
    if response_types is not None and (
        not isinstance(response_types, list) or "code" not in response_types
    ):
        raise CimdRefusal("grant_types_invalid", host)

    uris = document.get("redirect_uris")
    if (
        not isinstance(uris, list)
        or not 1 <= len(uris) <= MAX_REDIRECT_URIS
        or any(not isinstance(u, str) or len(u) > MAX_REDIRECT_URI_CHARS for u in uris)
        or any(_has_control(u) for u in uris)
        or len(set(uris)) != len(uris)
    ):
        raise CimdRefusal("redirect_uris_invalid", host)
    normalized: list[str] = []
    for uri in uris:
        if _loopback_entry(uri):
            normalized.append(uri)
            continue
        https = _https_entry(uri)
        if https is None:
            raise CimdRefusal("redirect_uris_invalid", host)
        normalized.append(https)
    if len(set(normalized)) != len(normalized):
        raise CimdRefusal("redirect_uris_invalid", host)

    raw_scope = document.get("scope", DEFAULT_CLIENT_SCOPE)
    if not isinstance(raw_scope, str) or "\x00" in raw_scope:
        raise CimdRefusal("scope_invalid", host)
    tokens = set(raw_scope.split())
    if not tokens or tokens - VALID_SCOPES:
        raise CimdRefusal("scope_invalid", host)
    scope = " ".join(sorted(tokens))
    if not has_vault_scope(scope):
        raise CimdRefusal("scope_invalid", host)

    return Projection(
        client_id=url,
        client_name=name.strip(),
        redirect_uris=tuple(normalized),
        scope=scope,
        auth_method=method,
        jwks_uri=jwks_uri,
        signing_alg=signing_alg,
    )


def _origin(url: str) -> tuple[str, str, int]:
    parts = transfer.canonicalise(url, allow_http=False)
    return parts.scheme, parts.host, parts.port


def _key_source(url: str, document: dict, host: str | None) -> tuple[str, str | None]:
    """The (jwks_uri, signing alg) of a private_key_jwt document, or raise (D1).

    One key source only (RFC 7591 §2): an inline `jwks` is refused even next
    to a valid `jwks_uri`. The `jwks_uri` meets every client-identifier URL
    rule and is same origin (scheme, canonical host, effective port) as the
    document's own URL.
    """
    if "jwks" in document:
        raise CimdRefusal("jwks_inline_refused", host)
    jwks_uri = document.get("jwks_uri")
    if not isinstance(jwks_uri, str):
        raise CimdRefusal("jwks_uri_invalid", host)
    try:
        validate_client_id_url(jwks_uri)
    except CimdRefusal:
        raise CimdRefusal("jwks_uri_invalid", host) from None
    try:
        same_origin = _origin(jwks_uri) == _origin(url)
    except transfer.SSRFError:
        raise CimdRefusal("jwks_uri_invalid", host) from None
    if not same_origin:
        raise CimdRefusal("jwks_uri_cross_origin", host)
    signing_alg = None
    if "token_endpoint_auth_signing_alg" in document:
        signing_alg = document["token_endpoint_auth_signing_alg"]
        if not isinstance(signing_alg, str) or signing_alg not in SIGNING_ALGS:
            raise CimdRefusal("signing_alg_unsupported", host)
    return jwks_uri, signing_alg


# ── process state: cache, cooldown, admission ──────────────────────────────


@dataclass
class _Slot:
    window_start: float
    count: int
    inflight: int = 0


_cache: "OrderedDict[str, tuple[Projection, float]]" = OrderedDict()
_cooldown: "OrderedDict[str, float]" = OrderedDict()
_flights: dict[str, asyncio.Future] = {}
_address_salt: bytes = secrets.token_bytes(16)
_address_table: list[_Slot | None] = [None] * ADDRESS_TABLE_SIZE
_global_inflight = 0

#: Test seam: an injected resolver for the guarded fetch (None = real DNS).
_resolver = None


def reset_state_for_tests(table_size: int = ADDRESS_TABLE_SIZE) -> None:
    global _address_salt, _address_table, _global_inflight
    _cache.clear()
    _cooldown.clear()
    _flights.clear()
    _jwks_cache.clear()
    _jwks_flights.clear()
    _address_salt = secrets.token_bytes(16)
    _address_table = [None] * table_size
    _global_inflight = 0


def address_table_size() -> int:
    return len(_address_table)


def _now() -> float:
    return time.monotonic()


def _cache_get(url: str) -> Projection | None:
    entry = _cache.get(url)
    if entry is None:
        return None
    projection, expires = entry
    if _now() >= expires:
        del _cache[url]
        return None
    _cache.move_to_end(url)
    return projection


def _ttl(directive) -> float:
    ceiling = settings.oauth_cimd_cache_max_seconds
    if directive is not None and directive.no_store:
        return 0
    if directive is not None and directive.max_age is not None:
        return min(directive.max_age, ceiling)
    return min(DEFAULT_TTL_SECONDS, ceiling)


def _cache_put(url: str, projection: Projection, directive) -> None:
    ttl = _ttl(directive)
    _cache.pop(url, None)
    if ttl <= 0:
        return
    _cache[url] = (projection, _now() + ttl)
    while len(_cache) > POSITIVE_CACHE_ENTRIES:
        _cache.popitem(last=False)


def _cooldown_put(url: str) -> None:
    _cooldown.pop(url, None)
    _cooldown[url] = _now()
    while len(_cooldown) > COOLDOWN_ENTRIES:
        _cooldown.popitem(last=False)


def _in_cooldown(url: str) -> bool:
    started = _cooldown.get(url)
    if started is None:
        return False
    if _now() - started >= FAILURE_COOLDOWN_SECONDS:
        del _cooldown[url]
        return False
    return True


def _slot_for(address: str | None) -> int:
    return rate_limits.salted_slot_index(address, len(_address_table), _address_salt)


def _admit(address: str | None, host: str) -> int:
    """Charge one fetch to the address's slot, or raise. Returns the slot index."""
    global _global_inflight
    index = _slot_for(address)
    slot = _address_table[index]
    now = _now()
    limit = settings.oauth_cimd_fetches_per_address_per_minute
    in_window = slot is not None and now - slot.window_start < ADDRESS_WINDOW_SECONDS
    if in_window and slot.count >= limit:
        raise CimdRefusal("fetch_budget_exhausted", host)
    if slot is not None and slot.inflight >= PER_SLOT_IN_FLIGHT:
        raise CimdRefusal("fetch_busy", host)
    if _global_inflight >= GLOBAL_IN_FLIGHT:
        raise CimdRefusal("fetch_busy", host)
    if slot is None:
        slot = _address_table[index] = _Slot(window_start=now, count=0)
    elif not in_window:
        slot.window_start, slot.count = now, 0
    slot.count += 1
    slot.inflight += 1
    _global_inflight += 1
    return index


def _release(index: int) -> None:
    global _global_inflight
    slot = _address_table[index] if index < len(_address_table) else None
    if slot is not None and slot.inflight > 0:
        slot.inflight -= 1
    _global_inflight = max(0, _global_inflight - 1)


def _map_fetch_error(exc: Exception, host: str) -> CimdRefusal:
    if isinstance(exc, transfer.TooLarge):
        return CimdRefusal("too_large", host)
    if isinstance(exc, transfer.Timeout):
        return CimdRefusal("fetch_timeout", host)
    if isinstance(exc, transfer.SSRFError):
        text = str(exc)
        if "content-encoded" in text:
            return CimdRefusal("content_encoded", host)
        if text.startswith("Expected HTTP") or "redirect" in text.lower():
            return CimdRefusal("http_status", host)
        if (
            text.startswith("Fetch failed")
            or text.startswith("Malformed Content-Length")
            or text.startswith("Could not resolve")
            or text.endswith("did not resolve")
        ):
            return CimdRefusal("fetch_failed", host)
        return CimdRefusal("ssrf_refused", host)
    return CimdRefusal("fetch_failed", host)


async def _fetch_json(url: str, host: str):
    """GET `url` through the guarded fetch: (parsed JSON, cache directive).

    Every failure is a `CimdRefusal` with a document-fetch reason; the JWKS
    path maps those to their `jwks_*` counterparts.
    """
    try:
        async with transfer.fetch_url_guarded(
            url,
            allow_http=False,
            max_bytes=MAX_DOCUMENT_BYTES,
            deadline=FETCH_DEADLINE_SECONDS,
            resolver=_resolver,
            max_redirects=0,
        ) as result:
            body = bytearray()
            async for chunk in result.chunks:
                body += chunk
                if len(body) > MAX_DOCUMENT_BYTES:
                    raise CimdRefusal("too_large", host)
            directive = result.cache_control
    except CimdRefusal:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is a closed reason
        raise _map_fetch_error(exc, host) from None
    try:
        parsed = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CimdRefusal("not_json", host) from None
    return parsed, directive


async def _fetch(url: str, host: str) -> tuple[Projection, object]:
    document, directive = await _fetch_json(url, host)
    return validate_document(url, document, host), directive


async def resolve(url: str, address: str | None) -> Projection:
    """The validated projection for a valid CIMD URL (D3 admission order)."""
    host = validate_client_id_url(url)
    cached = _cache_get(url)
    if cached is not None:
        return cached
    flight = _flights.get(url)
    if flight is not None:
        return await asyncio.shield(flight)
    if _in_cooldown(url):
        raise CimdRefusal("fetch_cooldown", host)
    index = _admit(address, host)
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _flights[url] = future
    try:
        projection, directive = await _fetch(url, host)
    except CimdRefusal as refusal:
        _cooldown_put(url)
        future.set_exception(refusal)
        future.exception()  # retrieved: a flight nobody joined must not warn
        raise
    except BaseException:
        if not future.done():
            future.set_exception(CimdRefusal("fetch_failed", host))
            future.exception()
        raise
    else:
        _cache_put(url, projection, directive)
        future.set_result(projection)
        return projection
    finally:
        _flights.pop(url, None)
        _release(index)


# ── the JWKS of a private_key_jwt client (#420, D2, D3) ────────────────────


def _utcnow() -> datetime:
    """Wall-clock UTC, the time a snapshot's bytes arrived. A test seam."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True, eq=True)
class JwksSnapshot:
    """A validated, normalised JWKS and the moment it left the origin.

    Immutable: the keys are held as canonical JSON text and every accessor
    returns a fresh copy. `fetched_at` is the origin fetch time (UTC); a
    cache hit returns the same object, so it is never the time of the hit,
    and it is the only value ever written to `oauth_clients.jwks_fetched_at`.
    """

    _keys_text: str
    fetched_at: datetime

    def as_json(self) -> dict:
        """The normalised `{"keys": [...]}`: what `jwks_snapshot` stores."""
        return {"keys": json.loads(self._keys_text)}

    @property
    def keys(self) -> tuple[dict, ...]:
        """The usable keys, public members only, as fresh dicts."""
        return tuple(json.loads(self._keys_text))

    @property
    def kids(self) -> frozenset[str]:
        return frozenset(k["kid"] for k in json.loads(self._keys_text) if "kid" in k)


_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _b64url_uint(value) -> int | None:
    """The unsigned integer of an unpadded base64url string, or None."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if any(c not in _B64URL for c in value) or len(value) % 4 == 1:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return None
    return int.from_bytes(raw, "big")


def _usable_key(key: dict) -> dict | None:
    """The normalised public form of one JWK, or None when it is ignored (D3.3)."""
    use = key.get("use")
    if use is not None and use != "sig":
        return None
    if "key_ops" in key:
        ops = key["key_ops"]
        if not isinstance(ops, list) or "verify" not in ops:
            return None
    kty = key.get("kty")
    if kty not in ("RSA", "EC"):
        return None
    alg = key.get("alg")
    if alg is not None and alg not in SIGNING_ALGS:
        return None
    kid = key.get("kid")
    if kid is not None and (
        not isinstance(kid, str)
        or not 1 <= len(kid) <= MAX_KID_CHARS
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in kid)
    ):
        # A kid the snapshot could not store (JSONB holds no NUL) or match
        # byte-exactly is treated as a key that does not import.
        return None

    if kty == "RSA":
        if alg == "ES256":
            return None
        n, e = _b64url_uint(key.get("n")), _b64url_uint(key.get("e"))
        if n is None or e is None:
            return None
        if not RSA_MIN_BITS <= n.bit_length() <= RSA_MAX_BITS:
            return None
        if not (RSA_MIN_EXPONENT <= e <= RSA_MAX_EXPONENT and e % 2 == 1):
            return None
        public = {"kty": "RSA", "n": key["n"], "e": key["e"]}
        importer = RSAKey
    else:
        if key.get("crv") != "P-256" or alg not in (None, "ES256"):
            return None
        if not (isinstance(key.get("x"), str) and isinstance(key.get("y"), str)):
            return None
        public = {"kty": "EC", "crv": "P-256", "x": key["x"], "y": key["y"]}
        importer = ECKey
    try:
        if importer.import_key(dict(public)).is_private:
            return None
    except Exception:  # noqa: BLE001 - "does not import as a public key"
        return None
    if kid is not None:
        public["kid"] = kid
    if alg is not None:
        public["alg"] = alg
    return public


def validate_jwks(
    obj, *, fetched_at: datetime | None = None, host: str | None = None
) -> JwksSnapshot:
    """A normalised `JwksSnapshot` of `obj`, or raise `CimdRefusal` (D3).

    In order: the shape (1–16 objects in `keys`); any private or symmetric
    member refuses the whole set; unusable keys are ignored; two usable keys
    sharing a `kid` refuse the set; at least one usable key is required.
    `fetched_at` defaults to now (UTC); a caller rebuilding a stored snapshot
    passes the row's `jwks_fetched_at`.
    """
    if not isinstance(obj, dict):
        raise CimdRefusal("jwks_invalid", host)
    keys = obj.get("keys")
    if (
        not isinstance(keys, list)
        or not 1 <= len(keys) <= JWKS_MAX_KEYS
        or any(not isinstance(k, dict) for k in keys)
    ):
        raise CimdRefusal("jwks_invalid", host)
    if any(PRIVATE_KEY_MEMBERS.intersection(k) for k in keys):
        raise CimdRefusal("jwks_private_key", host)
    usable = [u for u in (_usable_key(k) for k in keys) if u is not None]
    kids = [u["kid"] for u in usable if "kid" in u]
    if len(kids) != len(set(kids)):
        raise CimdRefusal("jwks_invalid", host)
    if not usable:
        raise CimdRefusal("jwks_no_usable_key", host)
    if fetched_at is None:
        fetched_at = _utcnow()
    elif fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    text = json.dumps(usable, sort_keys=True, separators=(",", ":"))
    return JwksSnapshot(_keys_text=text, fetched_at=fetched_at.astimezone(timezone.utc))


# The JWKS cache: a second bounded LRU under the document cache's TTL rules,
# holding only validated snapshots. Flights are keyed by `jwks_uri`; the
# failure cooldown shares the document's table under a key no URL can be
# (it holds a newline), so a JWKS failure never arms the document's cooldown.
_jwks_cache: "OrderedDict[str, tuple[JwksSnapshot, float]]" = OrderedDict()
_jwks_flights: dict[str, asyncio.Future] = {}


def _jwks_cooldown_key(uri: str) -> str:
    return "jwks\n" + uri


def jwks_cache_get(uri: str) -> JwksSnapshot | None:
    """The cached snapshot for `uri`, with its original `fetched_at`, or None."""
    entry = _jwks_cache.get(uri)
    if entry is None:
        return None
    snapshot, expires = entry
    if _now() >= expires:
        del _jwks_cache[uri]
        return None
    _jwks_cache.move_to_end(uri)
    return snapshot


def jwks_cache_put(uri: str, snapshot: JwksSnapshot, directive) -> None:
    """Cache a validated snapshot under the document TTL rules (D2)."""
    if not isinstance(snapshot, JwksSnapshot):
        raise TypeError("only a validated JwksSnapshot is cached")
    ttl = _ttl(directive)
    _jwks_cache.pop(uri, None)
    if ttl <= 0:
        return
    _jwks_cache[uri] = (snapshot, _now() + ttl)
    while len(_jwks_cache) > JWKS_CACHE_ENTRIES:
        _jwks_cache.popitem(last=False)


async def _fetch_jwks(uri: str, host: str) -> tuple[JwksSnapshot, object]:
    try:
        obj, directive = await _fetch_json(uri, host)
    except CimdRefusal as refusal:
        reason = _JWKS_FETCH_REASONS.get(refusal.reason, "jwks_fetch_failed")
        raise CimdRefusal(reason, host) from None
    # Every byte has arrived: this is the origin fetch time.
    return validate_jwks(obj, fetched_at=_utcnow(), host=host), directive


async def _jwks_flight(uri: str, host: str) -> JwksSnapshot:
    """Run one JWKS fetch as a joinable flight; cache a validated result."""
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _jwks_flights[uri] = future
    try:
        snapshot, directive = await _fetch_jwks(uri, host)
    except CimdRefusal as refusal:
        future.set_exception(refusal)
        future.exception()  # retrieved: a flight nobody joined must not warn
        raise
    except BaseException:
        if not future.done():
            future.set_exception(CimdRefusal("jwks_fetch_failed", host))
            future.exception()
        raise
    else:
        jwks_cache_put(uri, snapshot, directive)
        future.set_result(snapshot)
        return snapshot
    finally:
        _jwks_flights.pop(uri, None)


async def resolve_jwks(projection: Projection, address: str | None) -> JwksSnapshot:
    """The validated JWKS of a `private_key_jwt` projection at `/authorize` (D2).

    JWKS cache hit → join an in-progress fetch of the same `jwks_uri` (both
    charged nothing) → the 30 s failure cooldown → **one more charge** to the
    caller's per-address budget, separate from the document's → one in
    flight per slot → eight per process → the guarded fetch → `validate_jwks`.
    """
    if projection.auth_method != AUTH_METHOD_PRIVATE_KEY_JWT or not projection.jwks_uri:
        raise ValueError("resolve_jwks needs a private_key_jwt projection")
    uri = projection.jwks_uri
    host = host_for_log(uri)
    cached = jwks_cache_get(uri)
    if cached is not None:
        return cached
    flight = _jwks_flights.get(uri)
    if flight is not None:
        return await asyncio.shield(flight)
    if _in_cooldown(_jwks_cooldown_key(uri)):
        raise CimdRefusal("fetch_cooldown", host)
    index = _admit(address, host)
    try:
        return await _jwks_flight(uri, host)
    except CimdRefusal:
        _cooldown_put(_jwks_cooldown_key(uri))
        raise
    finally:
        _release(index)


async def fetch_jwks_guarded(jwks_uri: str) -> JwksSnapshot:
    """The guarded fetch and validation of a pinned `jwks_uri`, uncharged (D7).

    For the `/token` and `/revoke` pre-phase refetch. No per-address charge
    and no failure cooldown here (the caller owns its 60 s refetch cooldown),
    but an in-progress fetch of the same URI is joined and the process-wide
    in-flight cap of eight applies (`fetch_busy`). A validated result is put
    in the JWKS cache with the response's own cache directive, so the caller
    need not call `jwks_cache_put`. Raises `CimdRefusal`: a `jwks_*` reason,
    `jwks_uri_invalid`, or `fetch_busy`.
    """
    global _global_inflight
    try:
        host = validate_client_id_url(jwks_uri)
    except CimdRefusal:
        raise CimdRefusal("jwks_uri_invalid", host_for_log(jwks_uri)) from None
    flight = _jwks_flights.get(jwks_uri)
    if flight is not None:
        return await asyncio.shield(flight)
    if _global_inflight >= GLOBAL_IN_FLIGHT:
        raise CimdRefusal("fetch_busy", host)
    _global_inflight += 1
    try:
        return await _jwks_flight(jwks_uri, host)
    finally:
        _global_inflight = max(0, _global_inflight - 1)
