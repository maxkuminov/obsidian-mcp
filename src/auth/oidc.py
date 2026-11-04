"""Optional standards-compliant OIDC login; provider credentials never become app credentials.

The protocol half of #415/#416: discovery, PKCE authorization, the token
exchange and ID-token validation. It returns a `VerifiedIdentity` — claims,
never a local user id. Which local user (if any) that identity resolves to is
decided by `src/services/oidc_links.py` in the callback's database phase.
See `openspec/changes/oidc-account-linking/design.md` D2, D5 and D6.
"""
import asyncio
import hmac
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx2
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet

from src.config import settings
from src.services.transport_security import certifi_ssl_context

CALLBACK_PATH = "/admin/auth/oidc/callback"
STATE_KEY = "oidc_login_state"
MAX_TRANSACTIONS = 1024
TRANSACTION_TTL = 300
MAX_RESPONSE_BYTES = 131072

#: The asymmetric algorithms this server will ever verify (D5). The effective
#: set is this ∩ discovery's `id_token_signing_alg_values_supported`. `none`,
#: every HS* and everything else are never accepted, whatever discovery says.
ALLOWED_SIGNING_ALGS = ("RS256", "PS256", "ES256")
#: OIDC Discovery §3: an absent `id_token_signing_alg_values_supported` means RS256.
DEFAULT_SIGNING_ALGS = ("RS256",)
BASE_SCOPES = ("openid", "email", "profile")
MAX_SUBJECT_CHARS = 255
MAX_EMAIL_CHARS = 254


class LoginRefused(Exception):
    """Closed, non-sensitive reason; never render an upstream exception.

    Reasons raised here: `capacity`, `state`, `provider`, `token`,
    `entitlement`. The resolution reasons (`unlinked`, `email_unverified`,
    `link_conflict`, `account`, `session_mint`) belong to the callback.
    """
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Transaction:
    expires: float
    nonce: str
    verifier: str
    next_url: str


@dataclass(frozen=True, repr=False)
class VerifiedIdentity:
    """A fully validated ID token's identity — the only thing `exchange()` returns.

    `email` is the claim only if it is a string of at most 254 characters,
    else `None` (a non-matching sentinel): email is never a reason to refuse
    here (D2). `email_verified` is `True` only for the JSON boolean `true`.
    The repr is redacted so that a stray log line cannot carry a subject or
    an address.
    """
    iss: str
    sub: str
    email: str | None
    email_verified: bool

    def __repr__(self) -> str:
        return "VerifiedIdentity(<redacted>)"


@dataclass(frozen=True)
class ProviderMetadata:
    """The parts of the discovery document this server acts on."""
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    #: ALLOWED_SIGNING_ALGS ∩ advertised, in our order; may be empty (→ `token`).
    signing_algs: tuple[str, ...]
    #: RFC 9207: discovery advertised `authorization_response_iss_parameter_supported: true`.
    iss_parameter_required: bool


_transactions: dict[str, Transaction] = {}
_start_times: list[float] = []


def begin(session: dict, next_url: str) -> tuple[str, Transaction]:
    # No await: atomic within the application's mandatory single worker.
    now = time.monotonic()
    for state, tx in list(_transactions.items()):
        if tx.expires <= now:
            del _transactions[state]
    _start_times[:] = [t for t in _start_times if t > now - 60]
    if len(_transactions) >= MAX_TRANSACTIONS or len(_start_times) >= 60:
        raise LoginRefused("capacity")
    _start_times.append(now)
    previous = session.pop(STATE_KEY, None)
    if isinstance(previous, str):
        _transactions.pop(previous, None)
    state = secrets.token_urlsafe(32)
    tx = Transaction(now + TRANSACTION_TTL, secrets.token_urlsafe(32),
                     secrets.token_urlsafe(48), next_url)
    _transactions[state] = tx
    session[STATE_KEY] = state
    return state, tx


def consume(session: dict, state: str) -> Transaction:
    expected = session.get(STATE_KEY)
    if (not isinstance(expected, str) or not state or len(state) > 128
            or not hmac.compare_digest(expected, state)):
        raise LoginRefused("state")
    session.pop(STATE_KEY, None)
    tx = _transactions.pop(state, None)
    if tx is None or tx.expires <= time.monotonic():
        raise LoginRefused("state")
    return tx


def ssl_context():
    """The back-channel trust anchor (D6): `OIDC_CA_FILE`'s context, else certifi.

    Explicit, because httpx2's `verify=True` means the operating-system store
    (httpcore2's `default_ssl_context`), not certifi.
    """
    return settings.oidc_ssl_context or certifi_ssl_context()


class BoundedTransport(httpx2.AsyncBaseTransport):
    """Bound all discovery/token/JWKS bodies before HTTP clients buffer them."""
    def __init__(self):
        self.inner = httpx2.AsyncHTTPTransport(trust_env=False, verify=ssl_context())

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        try:
            if response.headers.get("content-encoding", "identity") != "identity":
                raise LoginRefused("provider")
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise LoginRefused("provider")
                body.extend(chunk)
            return httpx2.Response(response.status_code, headers=response.headers,
                                   content=bytes(body), request=request)
        finally:
            await response.aclose()

    async def aclose(self):
        await self.inner.aclose()


def scope() -> str:
    """`openid email profile` plus the (already validated, de-duplicated) extras."""
    extra = settings.oidc_extra_scopes.split()
    return " ".join((*BASE_SCOPES, *(s for s in extra if s not in BASE_SCOPES)))


def client() -> AsyncOAuth2Client:
    return AsyncOAuth2Client(
        settings.oidc_client_id, settings.oidc_client_secret,
        scope=scope(), redirect_uri=settings.base_url.rstrip("/") + CALLBACK_PATH,
        token_endpoint_auth_method=settings.oidc_token_auth_method,
        code_challenge_method="S256",
        timeout=5, trust_env=False, follow_redirects=False,
        headers={"Accept-Encoding": "identity"}, transport=BoundedTransport(),
    )


def _endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise LoginRefused("provider")
    parsed, issuer = urlsplit(value), urlsplit(settings.oidc_issuer)
    if (parsed.scheme != "https" or parsed.hostname != issuer.hostname
            or (parsed.port or 443) != (issuer.port or 443)
            or parsed.username or parsed.password or parsed.fragment
            or any(ord(c) <= 32 or c == "\\" for c in value)):
        raise LoginRefused("provider")
    return value


def _string_list(data: dict, name: str) -> list[str] | None:
    """An optional discovery array of strings; malformed → `provider`."""
    if name not in data:
        return None
    value = data[name]
    if not isinstance(value, list) or len(value) > 64 or any(not isinstance(v, str) for v in value):
        raise LoginRefused("provider")
    return value


async def metadata(http) -> ProviderMetadata:
    response = await http.request("GET", settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration",
                                  withhold_token=True)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("issuer") != settings.oidc_issuer:
        raise LoginRefused("provider")
    methods = _string_list(data, "token_endpoint_auth_methods_supported")
    if methods is not None and settings.oidc_token_auth_method not in methods:
        raise LoginRefused("provider")
    advertised = _string_list(data, "id_token_signing_alg_values_supported")
    if advertised is None:
        advertised = list(DEFAULT_SIGNING_ALGS)
    return ProviderMetadata(
        authorization_endpoint=_endpoint(data.get("authorization_endpoint")),
        token_endpoint=_endpoint(data.get("token_endpoint")),
        jwks_uri=_endpoint(data.get("jwks_uri")),
        signing_algs=tuple(a for a in ALLOWED_SIGNING_ALGS if a in advertised),
        iss_parameter_required=data.get("authorization_response_iss_parameter_supported") is True,
    )


def check_iss_parameter(meta: ProviderMetadata, iss: str | None) -> None:
    """RFC 9207 (D5): a present `iss` must equal the issuer; an absent one is
    refused when discovery advertised the parameter. `None` means absent."""
    if iss is None:
        if meta.iss_parameter_required:
            raise LoginRefused("provider")
    elif not hmac.compare_digest(iss.encode(), settings.oidc_issuer.encode()):
        raise LoginRefused("provider")


async def authorization_url(state: str, tx: Transaction) -> str:
    async with asyncio.timeout(20), client() as http:
        meta = await metadata(http)
        url, _ = http.create_authorization_url(
            meta.authorization_endpoint, state=state, nonce=tx.nonce,
            code_verifier=tx.verifier, response_mode="query",
        )
        return url


def verified_identity(token: dict, jwks: dict, tx: Transaction,
                      algorithms: tuple[str, ...] = DEFAULT_SIGNING_ALGS) -> VerifiedIdentity:
    raw = token.get("id_token")
    if not isinstance(raw, str) or len(raw) > 32768:
        raise LoginRefused("token")
    algorithms = tuple(a for a in algorithms if a in ALLOWED_SIGNING_ALGS)
    if not algorithms:
        raise LoginRefused("token")
    try:
        decoded = jwt.decode(raw, KeySet.import_key_set(jwks), algorithms=list(algorithms))
        if decoded.header.get("alg") not in algorithms:
            raise LoginRefused("token")
        claims = CodeIDToken(
            decoded.claims, decoded.header,
            {"iss": {"essential": True, "value": settings.oidc_issuer},
             "aud": {"essential": True, "value": settings.oidc_client_id}},
            {"client_id": settings.oidc_client_id, "nonce": tx.nonce,
             "access_token": token.get("access_token")},
        )
        claims.validate(leeway=0)
    except LoginRefused:
        raise
    except Exception:
        raise LoginRefused("token") from None
    if (any(not isinstance(claims.get(k), str) or not claims[k] for k in ("iss", "sub", "nonce"))
            or len(claims["sub"]) > MAX_SUBJECT_CHARS
            or any(type(claims.get(k)) is not int for k in ("exp", "iat"))
            or claims["aud"] not in (settings.oidc_client_id, [settings.oidc_client_id])
            or ("azp" in claims and claims["azp"] != settings.oidc_client_id)):
        raise LoginRefused("token")
    if settings.oidc_required_group:
        groups = claims.get("groups")
        if (not isinstance(groups, list) or len(groups) > 256
                or any(not isinstance(g, str) or len(g) > 512 for g in groups)
                or settings.oidc_required_group not in groups):
            raise LoginRefused("entitlement")
    # Email is carried, never judged (D2): anything but a bounded string is None.
    email = claims.get("email")
    if not isinstance(email, str) or len(email) > MAX_EMAIL_CHARS:
        email = None
    return VerifiedIdentity(
        iss=claims["iss"], sub=claims["sub"], email=email,
        email_verified=claims.get("email_verified") is True,
    )


async def exchange(code: str, tx: Transaction, *, iss: str | None = None) -> VerifiedIdentity:
    """Discovery → RFC 9207 `iss` check → token → JWKS → validated identity.

    `iss` is the callback's `iss` query parameter, `None` when absent. It is
    checked after discovery (which says whether it is mandatory) and before
    the token request, so a refused `iss` never reaches the token endpoint.
    """
    if not code or len(code) > 4096:
        raise LoginRefused("token")
    async with asyncio.timeout(20), client() as http:
        meta = await metadata(http)
        check_iss_parameter(meta, iss)
        token = await http.fetch_token(meta.token_endpoint, code=code,
                                       code_verifier=tx.verifier, grant_type="authorization_code")
        response = await http.request("GET", meta.jwks_uri, withhold_token=True)
        response.raise_for_status()
        # Only validated claims leave this function. No token cache/refresh.
        return verified_identity(token, response.json(), tx, meta.signing_algs)
