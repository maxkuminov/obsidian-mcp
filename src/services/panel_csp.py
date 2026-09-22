"""The control panel's Content-Security-Policy (#195).

Every HTML response rendered by the four panel, auth and consent
`Jinja2Templates` instances carries a per-response nonce policy. This module
owns the nonce, the marker that says a response belongs to that surface, and
the policy string; `add_security_headers` in `src/main.py` writes the header.
`PANEL_CSP` (`enforce` | `report-only` | `off`) picks which header, if any —
see `docs/architecture/control-panel.md`.

**Scripts: the nonce and nothing else.** `script-src 'nonce-N'` lists no host
source, not even `'self'`, so an injected `<script src="/admin/static/…">` is
refused as surely as an inline one. No `'unsafe-inline'`, no `'unsafe-eval'`,
no `'strict-dynamic'`: every template script carries the nonce, Chart.js
evaluates nothing, and nothing loads script dynamically.

**Styles are split on purpose.** Style *elements* are the real CSS injection
primitive — attribute-selector scraping of the CSRF token's value, font-based
text probes — so `style-src-elem` admits only the nonce and the Google Fonts
stylesheet. Style *attributes* stay allowed through `style-src-attr
'unsafe-inline'`: the templates carry a few hundred of them, and an injected
one can only restyle the element it sits on, cannot select anything else, and
cannot beacon off-origin because every `url()` is still bound by `img-src` and
`font-src`. Moving them to classes is a follow-up. The `style-src` fallback
carries **no nonce**, deliberately: it is read only by a browser without the
CSP3 `-elem`/`-attr` directives, and in such a browser a nonce would make it
ignore `'unsafe-inline'` and refuse every style attribute, breaking the panel.

**The consent page's `form-action` is `'self' https:`.** Its form posts to
`/authorize`, which answers with a 302 to the client's registered redirect URI
for approve and deny alike, and browsers apply `form-action` to every hop of a
form-submission navigation — so a flat `'self'` breaks every OAuth connection.
The exact origin of the redirect URI was tried and rejected: a callback that
redirects on to another HTTPS origin is blocked on its second hop, and a host
registered in a non-canonical spelling (`https://127.1/cb`) does not match the
canonical URL the browser navigates to. A broken approve on a live connector
is the most expensive failure available here, so the page admits any HTTPS
target and still refuses `http:`, `javascript:` and `data:`. Where a code can
actually go is decided by the HTTPS-only registration rule and `authorize_post`'s
exact-match re-validation, never by this header. No request-derived value is
interpolated into the policy: the directive is a constant selected by which
page rendered the response, and the only variable is the nonce this module
generated.

**Scope is a marker, not a path.** The policy is correct only for markup
written to carry its nonce, and "rendered by one of the four panel instances"
is exactly that set. `template_context` — the context processor registered on
those instances and only those — marks the request while it renders, so the
HTML error renders (login 401, bootstrap register 400) and any future panel
route are covered with no inventory to maintain, and everything else is
excluded by construction: the transfer pages (their own instance, their own
stricter policy) and FastAPI's `/docs`, `/redoc` and `/docs/oauth2-redirect`,
which would break under a nonce-only policy. A path prefix would need an
exception list and would still miss a panel template rendered under a new
prefix. The marker and the nonce live on `request.state`, which the
middleware and the endpoint share through the one ASGI `scope["state"]`.
"""
from __future__ import annotations

import re
import secrets

from starlette.requests import Request
from starlette.responses import Response

#: `request.state` attribute names. Private to this module.
_NONCE = "panel_csp_nonce"
_SURFACE = "panel_csp_surface"
_CONSENT = "panel_csp_consent"

ENFORCE_HEADER = "Content-Security-Policy"
REPORT_ONLY_HEADER = "Content-Security-Policy-Report-Only"

#: What `secrets.token_urlsafe` produces. `build_policy` refuses anything else,
#: so a nonce can never carry a `;`, a quote or a line break into the header.
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22,}$")


def nonce_for(request: Request) -> str:
    """This request's nonce: created on first use, the same value thereafter.

    16 bytes from `secrets` (128 bits, 22 URL-safe characters) — the size the
    transfer pages use. Stored on `request.state`, so the template render and
    the middleware read the same value and the next request gets a new one.
    """
    nonce = getattr(request.state, _NONCE, None)
    if nonce is None:
        nonce = secrets.token_urlsafe(16)
        setattr(request.state, _NONCE, nonce)
    return nonce


def template_context(request: Request) -> dict[str, str]:
    """The `Jinja2Templates` context processor: `csp_nonce`, and the marker.

    Registered on the panel, auth and consent template instances and **not**
    on the transfer one. A context processor rather than a Jinja global because
    tests render templates through their own `Environment`, where a missing
    variable renders empty but a missing callable raises.
    """
    setattr(request.state, _SURFACE, True)
    return {"csp_nonce": nonce_for(request)}


def mark_consent(request: Request) -> None:
    """Give this response the consent page's `form-action 'self' https:`."""
    setattr(request.state, _CONSENT, True)


def is_panel_surface(request: Request) -> bool:
    return getattr(request.state, _SURFACE, False) is True


def is_consent(request: Request) -> bool:
    return getattr(request.state, _CONSENT, False) is True


def build_policy(nonce: str, consent: bool) -> str:
    """The panel policy for one response. `nonce` is the only variable part."""
    if not _NONCE_RE.match(nonce):
        raise ValueError("panel CSP nonce is not a server-generated token")
    form_action = "'self' https:" if consent else "'self'"
    return (
        "default-src 'self'; "
        f"script-src 'nonce-{nonce}'; "
        "style-src https://fonts.googleapis.com 'unsafe-inline'; "
        f"style-src-elem 'nonce-{nonce}' https://fonts.googleapis.com; "
        "style-src-attr 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        f"form-action {form_action}"
    )


def _is_html(response: Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return content_type.split(";", 1)[0].strip().lower() == "text/html"


def apply_policy(request: Request, response: Response, mode: str) -> None:
    """Write the panel policy onto `response` if it belongs to the surface.

    Nothing happens when `mode` is `off`, when the request was not marked by a
    panel template render, or when the response is not `text/html`. A response
    that already carries an **enforcing** policy keeps it byte-for-byte; a
    pre-existing report-only header does not count, because a report-only
    policy enforces nothing and must not switch enforcement off.
    """
    if mode == "off":
        return
    if not is_panel_surface(request) or not _is_html(response):
        return
    if ENFORCE_HEADER in response.headers:
        return
    if mode == "enforce":
        header = ENFORCE_HEADER
    elif mode == "report-only":
        header = REPORT_ONLY_HEADER
    else:  # pragma: no cover - `Settings` refuses any other value at boot
        raise ValueError(f"unknown PANEL_CSP mode: {mode!r}")
    response.headers[header] = build_policy(
        nonce_for(request), consent=is_consent(request)
    )
