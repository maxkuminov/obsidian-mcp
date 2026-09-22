"""#189 — one setting states which peers may set `X-Forwarded-*`, and it is
canonical by the time the middleware sees it.

Proxy trust was expressed twice and inconsistently: a hard-coded literal in
`src/main.py` trusting `127.0.0.1` and the three RFC 1918 ranges, and a
`--forwarded-allow-ips 172.16.0.0/12,10.0.0.0/8` in the `Dockerfile` that
excluded the real proxy subnet and was therefore inert. Every slowapi limiter
and the `/mcp` failed-auth budget key on the address this middleware resolves,
so the list is a security control and not a deployment detail.

Two properties are asserted here and one of them is the reason the file exists.

* **The default changes nothing.** `TRUSTED_PROXY_IPS`' default is the list
  that was in force, so a deploy that touches no environment value resolves
  every client address exactly as before — including from `192.168.0.10`, a
  peer the app-level literal covered and the Dockerfile flag did not.
* **A CIDR carrying host bits is canonicalised, and the proof runs through the
  REAL installed `ProxyHeadersMiddleware`.** `ip_network(strict=False)` accepts
  `192.168.0.10/24`; uvicorn's own parse does not, and keeps it as a literal
  matching no peer. The failure is silent and inverts the intent — every
  proxied request then retains the *proxy's* address, collapsing every caller
  into one limiter bucket. Asserting the canonical string against our own
  parser would prove nothing about that, so these tests drive the middleware.
"""
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.config import Settings

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
# Every tracked file that starts the server. Discovered by glob rather than
# listed, so a compose file added later is covered without anyone remembering
# to extend this: a `command:` override REPLACES the image CMD, which is how
# the two reference stacks kept uvicorn's forwarded-header layer on after the
# Dockerfile had switched it off.
START_COMMAND_FILES = sorted(
    [DOCKERFILE, *REPO.glob("docker-compose*.yml")], key=lambda path: path.name
)


def _uvicorn_launches(path: Path) -> list[str]:
    """Every uvicorn invocation in `path`, one string of flags each.

    Compose folds a `command: >` block across continuation lines, so the YAML is
    whitespace-joined first and each launch is cut at the closing quote of the
    `sh -c "…"` it lives in. The Dockerfile's exec-form `CMD` is one line.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yml", ".yaml"}:
        joined = " ".join(text.split())
        return [
            segment.split('"')[0] for segment in joined.split("uvicorn src.main:app")[1:]
        ]
    return [
        line
        for line in text.splitlines()
        if line.startswith("CMD ") and "uvicorn" in line
    ]


def _settings(value=None) -> Settings:
    """`Settings` with an isolated environment; `None` keeps the default.

    `secret_key` is supplied because `_env_file=None` drops the repo `.env`
    that would otherwise carry one, and the boot validator refuses a
    placeholder — nothing here depends on its value.
    """
    kwargs = {"_env_file": None, "secret_key": "0" * 64}
    if value is not None:
        kwargs["trusted_proxy_ips"] = value
    return Settings(**kwargs)


async def _resolved_client(trusted, peer, forwarded="203.0.113.7"):
    """What the application sees as `scope["client"]` behind the real middleware.

    The whole point of routing through `ProxyHeadersMiddleware` rather than
    re-implementing its matching: this is the parse that rejects host bits.
    """
    seen: dict[str, object] = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"]

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
    await middleware(
        {
            "type": "http",
            "scheme": "http",
            "client": (peer, 45678),
            "headers": [(b"x-forwarded-for", forwarded.encode())],
        },
        None,
        None,
    )
    return seen["client"][0]


# --- the default is today's behaviour ------------------------------------


def test_the_default_is_the_list_that_was_hard_coded():
    assert _settings().trusted_proxy_ips == [
        "127.0.0.1",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]


@pytest.mark.parametrize("peer", ["192.168.0.10", "172.18.0.2"])
async def test_the_default_trusts_a_private_network_peer(peer):
    """The regression the Dockerfile flag would have shipped.

    `192.168.0.10` is inside the app-level literal and outside
    `172.16.0.0/12,10.0.0.0/8`. If the new setting had defaulted to the
    Dockerfile's list — the tempting "align the two" fix — this peer would have
    stopped resolving its clients' addresses on a deploy that changed no
    environment value.

    `172.18.0.2` is the second case because it is the address the requirement
    itself uses for a containerised proxy: a default that did not cover a
    Docker bridge peer would break the reference stacks on the first tick.
    """
    trusted = _settings().trusted_proxy_ips

    assert await _resolved_client(trusted, peer) == "203.0.113.7"


async def test_a_public_peer_is_not_trusted():
    trusted = _settings().trusted_proxy_ips

    assert (
        await _resolved_client(trusted, "203.0.113.50", forwarded="10.0.0.1")
        == "203.0.113.50"
    )


# --- canonicalisation, through the middleware that rejects host bits ------


async def test_a_cidr_with_host_bits_is_canonicalised_and_still_matches():
    settings = _settings("192.168.0.10/24")

    assert settings.trusted_proxy_ips == ["192.168.0.0/24"]
    assert await _resolved_client(settings.trusted_proxy_ips, "192.168.0.10") == (
        "203.0.113.7"
    )


async def test_the_uncanonicalised_string_would_have_matched_nothing():
    """Why the validator stores the canonical form rather than validating only.

    This is the silent failure, pinned: hand the middleware exactly what the
    operator wrote and the peer inside the network is no longer trusted, so its
    own address is what every limiter keys on.
    """
    assert await _resolved_client(["192.168.0.10/24"], "192.168.0.10") == (
        "192.168.0.10"
    )


def test_a_bare_address_stays_bare():
    """Not rewritten to `127.0.0.1/32`: the logged list reads as what was set."""
    assert _settings("127.0.0.1").trusted_proxy_ips == ["127.0.0.1"]


async def test_a_narrowed_list_excludes_a_former_peer():
    """The operator action this setting exists to make a one-line `.env` edit."""
    settings = _settings("192.168.0.10")

    assert await _resolved_client(settings.trusted_proxy_ips, "192.168.0.11") == (
        "192.168.0.11"
    )
    assert await _resolved_client(settings.trusted_proxy_ips, "192.168.0.10") == (
        "203.0.113.7"
    )


# --- spellings, and the refusal ------------------------------------------


def test_csv_and_json_spellings_parse_identically():
    csv = _settings("127.0.0.1, 192.168.0.0/24").trusted_proxy_ips
    json_form = _settings('["127.0.0.1","192.168.0.0/24"]').trusted_proxy_ips

    assert csv == json_form == ["127.0.0.1", "192.168.0.0/24"]


@pytest.mark.parametrize(
    "value, offending",
    [
        ("127.0.0.1,not-an-address", "not-an-address"),
        ("*", "*"),
        ("192.168.0.0/33", "192.168.0.0/33"),
    ],
)
def test_a_malformed_entry_refuses_startup_naming_it(value, offending):
    """Loudly, not silently dropped.

    A trust list that quietly loses a range leaves the operator believing in a
    boundary that is not there, and every limiter keyed on the resolved address
    changes meaning without anything saying so. There is deliberately no
    wildcard spelling, so `*` is a refusal too.
    """
    with pytest.raises(ValidationError) as exc:
        _settings(value)

    assert offending in str(exc.value)


def test_the_off_spellings_trust_nobody():
    for spelling in ("", "null", "NONE", "  "):
        assert _settings(spelling).trusted_proxy_ips == []


# --- what `src/main.py` installs, and what it says ------------------------


class _Capture(logging.Handler):
    """`src.main`'s logger does not propagate (`configure_logging` owns the
    handlers), so `caplog`'s root handler never sees these records."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def _install(trusted, monkeypatch):
    from src import main

    monkeypatch.setattr(main.settings, "trusted_proxy_ips", trusted)
    logger = logging.getLogger("src.main")
    handler = _Capture()
    logger.addHandler(handler)
    level = logger.level
    logger.setLevel(logging.DEBUG)
    app = FastAPI()
    try:
        installed = main._install_proxy_headers(app)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    return app, installed, [r.getMessage() for r in handler.records]


def test_the_middleware_is_installed_and_the_effective_list_is_logged(monkeypatch):
    app, installed, logged = _install(["192.168.0.0/24"], monkeypatch)

    assert installed == ["192.168.0.0/24"]
    assert [m.cls for m in app.user_middleware] == [ProxyHeadersMiddleware]
    assert app.user_middleware[0].kwargs["trusted_hosts"] == ["192.168.0.0/24"]
    # A trust boundary nobody can read from the logs is one nobody audits.
    assert any("192.168.0.0/24" in message for message in logged), logged


def test_the_disabled_spelling_installs_no_middleware(monkeypatch):
    """Not a middleware trusting nobody: none at all.

    The two behave identically, and installing nothing says so in the
    middleware stack, which is where an operator looks.
    """
    app, installed, logged = _install([], monkeypatch)

    assert installed == []
    assert app.user_middleware == []
    assert any("TRUSTED_PROXY_IPS" in message for message in logged), logged


# --- exactly one control --------------------------------------------------


@pytest.mark.parametrize("path", START_COMMAND_FILES, ids=lambda path: path.name)
def test_every_start_command_leaves_forwarded_headers_to_the_application(path):
    """`--no-proxy-headers`, and no second allow-list to diverge from the first.

    Merely aligning the two lists was rejected: uvicorn's `proxy_headers`
    defaults to *enabled* and its `forwarded_allow_ips` reads
    `$FORWARDED_ALLOW_IPS`, so "two controls that happen to agree" is a state
    an operator can break from the environment without editing either file.

    Asserting this of the `Dockerfile` alone was not enough. A compose
    `command:` override replaces the image CMD wholesale, so both reference
    stacks launched uvicorn with its forwarded-header layer *on* — and that
    layer rewrites `scope["client"]` before the application's middleware runs,
    which is the one ordering `TRUSTED_PROXY_IPS` cannot reach past. Every
    tracked file that starts the server is checked, discovered by glob.
    """
    for command in _uvicorn_launches(path):
        assert "--no-proxy-headers" in command, path.name
        assert "--forwarded-allow-ips" not in command, path.name
        # `--proxy-headers` is a substring of the flag required above, so the
        # negative is asserted against the text with that flag removed.
        assert "--proxy-headers" not in command.replace(
            "--no-proxy-headers", ""
        ), path.name
        # In-process rate control; a second worker multiplies every rate.
        assert "--workers 1" in command or '"--workers", "1"' in command, path.name


def test_the_start_command_files_are_the_ones_we_think_they_are():
    """The glob's own coverage, pinned.

    A discovery rule that silently matched nothing would make the test above
    vacuous. The `Dockerfile` carries exactly one launch; the maintainer's
    `docker-compose.yml` carries none and inherits the image CMD — and if it
    ever gains a `command:`, the rule above covers it with no further edit.
    """
    names = {path.name for path in START_COMMAND_FILES}

    assert names == {
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.proxy.yml",
        "docker-compose.simple.yml",
    }
    assert len(_uvicorn_launches(DOCKERFILE)) == 1
    assert _uvicorn_launches(REPO / "docker-compose.yml") == []
    for name in ("docker-compose.proxy.yml", "docker-compose.simple.yml"):
        assert len(_uvicorn_launches(REPO / name)) == 1, name


def test_the_documented_dev_command_leaves_forwarded_headers_to_the_application():
    """The README's outside-Docker launch is a start command too.

    It is not a deployment (so `--workers 1` is not demanded of a `--reload`
    dev server), but uvicorn's layer rewrites the client address ahead of the
    application either way, so a developer following it with a narrowed
    `TRUSTED_PROXY_IPS` would still see forged addresses.
    """
    launches = [
        line
        for line in (REPO / "README.md").read_text(encoding="utf-8").splitlines()
        if "uvicorn src.main:app" in line
    ]

    assert launches, "README no longer documents the outside-Docker launch"
    for command in launches:
        assert "--no-proxy-headers" in command, command
