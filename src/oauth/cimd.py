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
import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

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

#: Every sub-reason a refusal may carry; `client_metadata.<reason>` in the log.
REASONS = frozenset({
    "internal_id_submitted", "url_invalid", "url_not_canonical",
    "fetch_cooldown", "fetch_budget_exhausted", "fetch_busy", "ssrf_refused",
    "fetch_failed", "fetch_timeout", "http_status", "too_large",
    "content_encoded", "not_json", "client_id_mismatch", "document_invalid",
    "redirect_uris_invalid", "auth_method_unsupported", "secret_present",
    "grant_types_invalid", "scope_invalid", "redirect_uri_mismatch",
    "metadata_changed",
})


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

    @property
    def authorization_hash(self) -> str:
        """SHA-256 of the authorization projection only (D4, Codex R2-2)."""
        canonical = json.dumps(
            {
                "client_id": self.client_id,
                "redirect_uris": sorted(self.redirect_uris),
                "scope": " ".join(sorted(self.scope.split())),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def loopback_only(self) -> bool:
        return all(is_loopback_uri(uri) for uri in self.redirect_uris)


def validate_document(url: str, document, host: str | None = None) -> Projection:
    if not isinstance(document, dict):
        raise CimdRefusal("not_json", host)
    if document.get("client_id") != url or not isinstance(document.get("client_id"), str):
        raise CimdRefusal("client_id_mismatch", host)
    name = document.get("client_name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_CLIENT_NAME_CHARS:
        raise CimdRefusal("document_invalid", host)
    if "client_secret" in document or "client_secret_expires_at" in document:
        raise CimdRefusal("secret_present", host)
    method = document.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise CimdRefusal("auth_method_unsupported", host)
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
    if not isinstance(raw_scope, str):
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
    )


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


async def _fetch(url: str, host: str) -> tuple[Projection, object]:
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
        document = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CimdRefusal("not_json", host) from None
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
