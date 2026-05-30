import secrets

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.config import settings

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
        raise HTTPException(status_code=403, detail="CSRF validation failed")
