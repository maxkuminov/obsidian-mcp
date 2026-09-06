import secrets

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.config import settings
from src.services import security_events

_MAX_AGE = 3600


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="csrf-token")


def generate_csrf_token(request: Request) -> str:
    try:
        session = request.session
    except (AssertionError, AttributeError):
        return ""
    if "csrf_nonce" not in session:
        session["csrf_nonce"] = secrets.token_hex(16)
    return _serializer().dumps(session["csrf_nonce"])


def validate_csrf_token(request: Request, token: str | None) -> bool:
    try:
        session = request.session
    except (AssertionError, AttributeError):
        return False
    nonce = session.get("csrf_nonce")
    if nonce is None:
        return False
    if not token:
        return False
    try:
        payload = _serializer().loads(token, max_age=_MAX_AGE)
        return secrets.compare_digest(payload, nonce)
    except (BadSignature, SignatureExpired):
        return False


def _request_route(request) -> str | None:
    """`request.url.path`, defensively. A logging call may not raise.

    Not paranoia about Starlette: these emitters are reached from plain
    helpers that a test — or a future non-HTTP caller — can drive with
    something that is not a full `Request`. `security_events.emit` swallows its
    own failures, but the field expressions are evaluated *before* it is
    called, so an attribute error here would escape into a request path and
    turn a 403 into a 500. The record is allowed to be poorer; the response is
    not allowed to change.
    """
    try:
        return request.url.path
    except (AttributeError, TypeError):
        return None


def _request_method(request) -> str | None:
    """`request.method`, defensively. Same reason as `_request_route`."""
    try:
        return request.method
    except (AttributeError, TypeError):
        return None


def _session_user_id(request: Request) -> int | None:
    """The signed-in user id, or `None` — for the record, never for a decision.

    Single-user mode mounts no `SessionMiddleware`, so touching `request.session`
    there raises `AssertionError`; a logging helper may not care.
    """
    try:
        value = request.session.get("user_id")
    except (AssertionError, AttributeError):
        return None
    return value if isinstance(value, int) else None


async def verify_csrf(request: Request):
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    token = request.headers.get("x-csrf-token")
    if token is None:
        try:
            form = await request.form()
        except Exception:
            form = {}
        token = form.get("csrf_token")
    if not validate_csrf_token(request, token):
        # The record only. **The 403 and its detail are unchanged**: a CSRF
        # failure is indistinguishable to the caller whether it was a stale
        # token, a missing session or a forged cross-site post, and the log is
        # where an operator finds out which route was being posted to and by
        # whom. `verify_csrf` is a router-wide dependency on an unauthenticated
        # path, so it goes through the suppressor like every other refusal a
        # caller can drive on demand.
        user_id = _session_user_id(request)
        security_events.emit(
            "csrf_refused",
            subject=security_events.subject_for(user_id=user_id, request=request),
            route=_request_route(request),
            method=_request_method(request),
            user_id=user_id,
            client_ip=security_events.client_ip(request),
        )
        raise HTTPException(status_code=403, detail="CSRF validation failed")
