"""Optional Keycloak-profile login; provider credentials never become app credentials."""
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

CALLBACK_PATH = "/admin/auth/oidc/callback"
STATE_KEY = "oidc_login_state"
MAX_TRANSACTIONS = 1024
TRANSACTION_TTL = 300
MAX_RESPONSE_BYTES = 131072


class LoginRefused(Exception):
    """Closed, non-sensitive reason; never render an upstream exception."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Transaction:
    expires: float
    nonce: str
    verifier: str
    next_url: str


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


class BoundedTransport(httpx2.AsyncBaseTransport):
    """Bound all discovery/token/JWKS bodies before HTTP clients buffer them."""
    def __init__(self):
        self.inner = httpx2.AsyncHTTPTransport(trust_env=False)

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


def client() -> AsyncOAuth2Client:
    return AsyncOAuth2Client(
        settings.oidc_client_id, settings.oidc_client_secret,
        scope="openid profile", redirect_uri=settings.base_url.rstrip("/") + CALLBACK_PATH,
        token_endpoint_auth_method="client_secret_basic", code_challenge_method="S256",
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


async def metadata(http):
    response = await http.request("GET", settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration",
                              withhold_token=True)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("issuer") != settings.oidc_issuer:
        raise LoginRefused("provider")
    return {name: _endpoint(data.get(name)) for name in
            ("authorization_endpoint", "token_endpoint", "jwks_uri")}


async def authorization_url(state: str, tx: Transaction) -> str:
    async with asyncio.timeout(20), client() as http:
        meta = await metadata(http)
        url, _ = http.create_authorization_url(
            meta["authorization_endpoint"], state=state, nonce=tx.nonce,
            code_verifier=tx.verifier, response_mode="query",
        )
        return url


def verified_user_id(token: dict, jwks: dict, tx: Transaction) -> int:
    raw = token.get("id_token")
    if not isinstance(raw, str) or len(raw) > 32768:
        raise LoginRefused("token")
    decoded = jwt.decode(raw, KeySet.import_key_set(jwks), algorithms=["RS256"])
    claims = CodeIDToken(
        decoded.claims, decoded.header,
        {"iss": {"essential": True, "value": settings.oidc_issuer},
         "aud": {"essential": True, "value": settings.oidc_client_id}},
        {"client_id": settings.oidc_client_id, "nonce": tx.nonce,
         "access_token": token.get("access_token")},
    )
    claims.validate(leeway=0)
    if (any(not isinstance(claims.get(k), str) or not claims[k] for k in ("iss", "sub", "nonce"))
            or any(type(claims.get(k)) is not int for k in ("exp", "iat"))
            or claims["aud"] not in (settings.oidc_client_id, [settings.oidc_client_id])
            or ("azp" in claims and claims["azp"] != settings.oidc_client_id)):
        raise LoginRefused("token")
    groups = claims.get("groups")
    if (not isinstance(groups, list) or len(groups) > 256
            or any(not isinstance(g, str) or len(g) > 512 for g in groups)
            or settings.oidc_required_group not in groups):
        raise LoginRefused("entitlement")
    user_id = settings.oidc_subject_user_ids.get(claims["sub"])
    if user_id is None:
        raise LoginRefused("mapping")
    return user_id


async def exchange(code: str, tx: Transaction) -> int:
    if not code or len(code) > 4096:
        raise LoginRefused("token")
    async with asyncio.timeout(20), client() as http:
        meta = await metadata(http)
        token = await http.fetch_token(meta["token_endpoint"], code=code,
                                       code_verifier=tx.verifier, grant_type="authorization_code")
        response = await http.request("GET", meta["jwks_uri"], withhold_token=True)
        response.raise_for_status()
        # Only the existing local ID leaves this function. No token cache/refresh.
        return verified_user_id(token, response.json(), tx)
