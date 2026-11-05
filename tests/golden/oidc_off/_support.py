"""Shared harness for the OIDC-off regression goldens (oidc-account-linking D16).

Imported by `tests/test_oidc_off_regression.py` (offline, fake database) and
`tests/integration/test_oidc_off_consent_pg.py` (real Postgres). Captured on
the base commit before any linking slice; every later slice must leave the
golden files here byte-identical.

A golden is the rendered response after normalization:

    HTTP <status>
    <header>: <value>      every header except content-length and date, in
                           the order the server sent them
    <blank line>
    <body>

Normalized: the CSRF token, the CSP nonce, the consent page's server state,
`Set-Cookie` values and expiry, ISO dates and datetimes, authorization codes,
the synthetic CIMD row id, user ids (by a per-test map to a label) and the
per-test vault directory. Nothing else is: a byte that changes anywhere else
fails the comparison.

`UPDATE_OIDC_GOLDENS=1` rewrites the files instead of comparing. Never set it
on a slice branch: the goldens were captured on the base, and regenerating them
after a change is exactly what they exist to prevent.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

GOLDEN_DIR = Path(__file__).resolve().parent
UPDATE = os.environ.get("UPDATE_OIDC_GOLDENS") == "1"

# The one origin every mode runs under, so items 1–3 of D16 can be compared
# across the OIDC-off and OIDC-on columns. `localhost` is in the test
# process's derived `allowed_hosts`; https so the session and state cookies
# are sent back by the client.
BASE = "https://localhost"

ISSUER = "https://identity.example.test/realms/home"
OIDC_CLIENT = "obsidian"
OIDC_GROUP = "/apps/obsidian"
OIDC_SUBJECT = "immutable-sub-admin"

# The matrix (D16): name → settings overrides. `oidc_subject_user_ids` is
# filled per test with the real admin id (#415's map is the only binding
# mechanism on the base).
MODES = {
    "off_single": dict(multi_user_mode=False, oidc_enabled=False),
    "off_multi": dict(multi_user_mode=True, oidc_enabled=False),
    "on_multi": dict(
        multi_user_mode=True,
        oidc_enabled=True,
        oidc_issuer=ISSUER,
        oidc_client_id=OIDC_CLIENT,
        oidc_client_secret="test-client-secret",
        oidc_required_group=OIDC_GROUP,
        local_login_enabled=True,
    ),
}
MULTI_MODES = ("off_multi", "on_multi")


def apply_mode(monkeypatch, mode: str, *, subject_user_id: int | None = None) -> None:
    """Set the mode's settings (before `app_for`, which reads them at build)."""
    from src.config import settings

    monkeypatch.setattr(settings, "base_url", BASE)
    monkeypatch.setattr(settings, "panel_csp", "enforce")
    monkeypatch.setattr(settings, "oauth_cimd_enabled", True)
    monkeypatch.setattr(settings, "local_login_enabled", True)
    monkeypatch.setattr(settings, "oidc_subject_user_ids", {})
    for name, value in MODES[mode].items():
        monkeypatch.setattr(settings, name, value)
    if mode == "on_multi":
        monkeypatch.setattr(
            settings, "oidc_subject_user_ids",
            {OIDC_SUBJECT: subject_user_id if subject_user_id is not None else 1},
        )


_APPS: dict[str, object] = {}


def app_for(mode: str):
    """A private build of `src/main.py`'s app under the mode's settings.

    `src.main.app` is built once, at the first import anywhere in the
    process, and bakes in what the settings were at that moment: whether the
    auth router is mounted (`multi_user_mode`) and whether the session cookie
    is `Secure` (`base_url`). Which test imports it first is an accident of
    collection order, so the goldens cannot use it. Instead the module is
    executed again under a private name with the mode's settings already
    applied — exactly what a production process started with those settings
    builds. Its lifespan never runs. One build per mode per process; the
    caller must have applied the mode first.
    """
    import importlib.util
    import sys

    if mode not in _APPS:
        root = Path(__file__).resolve().parents[3]
        name = f"_oidc_golden_main_{mode}"
        spec = importlib.util.spec_from_file_location(name, root / "src" / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _APPS[mode] = module.app
    return _APPS[mode]


class mounted_app:
    """`app_for(mode)` with its dependency overrides restored on exit."""

    def __init__(self, mode: str):
        self.mode = mode

    def __enter__(self):
        self.app = app_for(self.mode)
        self.overrides = dict(self.app.dependency_overrides)
        return self.app

    def __exit__(self, *exc):
        self.app.dependency_overrides.clear()
        self.app.dependency_overrides.update(self.overrides)
        return False


# ── normalization ───────────────────────────────────────────────────────────

_RULES = [
    (re.compile(r'(name="csrf_token"\s+value=")[^"]*"'), r'\1<CSRF>"'),
    (re.compile(r'nonce="[^"]*"'), 'nonce="<NONCE>"'),
    (re.compile(r"'nonce-[A-Za-z0-9_\-]+'"), "'nonce-<NONCE>'"),
    (re.compile(r'(name="state"\s+value=")[^"]*"'), r'\1<STATE>"'),
    (re.compile(r"(?m)^(set-cookie: [^=]+)=[^;\r\n]*"), r"\1=<COOKIE>"),
    (re.compile(r"(?i)(expires=)[^;\r\n]*"), r"\1<EXPIRES>"),
    (re.compile(r"\bcode=[0-9a-f]{64}\b"), "code=<CODE>"),
    (re.compile(r"\bcimd_[0-9a-f]{16,}\b"), "cimd_<ID>"),
    (
        re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"),
        "<DATETIME>",
    ),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<DATE>"),
]


def normalize(text: str, *, user_ids: dict[int, str] | None = None,
              paths: dict[str, str] | None = None) -> str:
    for old, new in sorted((paths or {}).items(), key=lambda kv: -len(kv[0])):
        text = text.replace(old, new)
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    for uid, label in (user_ids or {}).items():
        text = re.sub(rf"/admin/users/{uid}(?=[/\"?])", f"/admin/users/<UID:{label}>", text)
        text = re.sub(rf'(name="user_id" value="){uid}"', rf'\g<1><UID:{label}>"', text)
    return text


def render(response, **normalize_kwargs) -> str:
    lines = [f"HTTP {response.status_code}"]
    for name, value in response.headers.multi_items():
        if name.lower() in ("content-length", "date"):
            continue
        lines.append(f"{name.lower()}: {value}")
    text = "\n".join(lines) + "\n\n" + response.text
    return normalize(text, **normalize_kwargs)


def check(name: str, text: str) -> None:
    """Compare `text` to `GOLDEN_DIR/<name>.golden`, byte for byte."""
    path = GOLDEN_DIR / f"{name}.golden"
    data = text.encode("utf-8")
    if UPDATE:
        path.write_bytes(data)
        return
    assert path.exists(), (
        f"missing golden {path.name}; goldens are captured on the base commit "
        "only (UPDATE_OIDC_GOLDENS=1 there)"
    )
    expected = path.read_bytes()
    if expected != data:
        import difflib

        diff = "\n".join(difflib.unified_diff(
            expected.decode("utf-8", "replace").splitlines(),
            text.splitlines(), "golden", "actual", lineterm="", n=2,
        ))
        raise AssertionError(f"golden {path.name} differs:\n{diff[:6000]}")


def csrf_from(body: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', body)
    assert match, "the page rendered no CSRF token"
    return match.group(1)


def hidden(body: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}"\s+value="([^"]*)"', body)
    assert match, f"no hidden field {name!r}"
    return match.group(1)


# ── the fake provider (OIDC on) ─────────────────────────────────────────────


class FakeProvider:
    """A provider for #415's real protocol client: discovery, token, JWKS.

    Mirrors `tests/test_native_oidc.py`'s MockTransport fake, signing a real
    RS256 ID token with the transaction's nonce, so the session in the OIDC-on
    column is minted by the shipped callback, never by a dependency override.
    """

    def __init__(self):
        from joserfc.jwk import RSAKey

        self.key = RSAKey.generate_key(2048, parameters={"kid": "golden-key"})
        self.nonce: str | None = None
        self.subject = OIDC_SUBJECT

    def install(self, monkeypatch) -> None:
        import httpx2

        from src.auth import oidc

        monkeypatch.setattr(oidc, "BoundedTransport", lambda: httpx2.MockTransport(self.handle))

    async def handle(self, req):
        import httpx2
        from joserfc import jwt

        path = req.url.path
        if path.endswith("openid-configuration"):
            return httpx2.Response(200, json=dict(
                issuer=ISSUER,
                authorization_endpoint=ISSUER + "/protocol/openid-connect/auth",
                token_endpoint=ISSUER + "/protocol/openid-connect/token",
                jwks_uri=ISSUER + "/protocol/openid-connect/certs",
            ))
        if path.endswith("/token"):
            now = int(time.time())
            claims = dict(iss=ISSUER, sub=self.subject, aud=OIDC_CLIENT, exp=now + 300,
                          iat=now, nonce=self.nonce, groups=[OIDC_GROUP])
            token = jwt.encode({"alg": "RS256", "kid": "golden-key"}, claims, self.key)
            return httpx2.Response(200, json={"id_token": token, "access_token": "transient",
                                              "token_type": "Bearer"})
        return httpx2.Response(200, json={"keys": [self.key.as_dict(private=False)]})

    async def sign_in(self, http, next_url: str = "/admin/"):
        """Drive /admin/auth/oidc/start → provider → callback on `http`."""
        from src.auth import oidc

        start = await http.get("/admin/auth/oidc/start", params={"next": next_url})
        assert start.status_code == 302, start.text[:300]
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        self.nonce = oidc._transactions[state].nonce
        callback = await http.get(oidc.CALLBACK_PATH, params={"state": state, "code": "golden-code"})
        assert callback.status_code == 302, callback.text[:300]
        return callback
