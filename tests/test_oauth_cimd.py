"""Client ID Metadata Documents (#357): the offline half.

Everything a CIMD flow decides before the database:

* the client-identifier URL rules (D1), checked before any DNS or connection;
* the document rules and the projection (D4), including the authorization
  hash the consent pin binds (Codex R2-2);
* port-agnostic loopback redirect matching (D5);
* the HMAC synthetic id (Codex R2-1);
* the bounded `Cache-Control` the guarded fetch exposes (Codex finding 8);
* a **fake document server** driven through the real `fetch_url_guarded`:
  every failure mode, the SSRF deny list, DNS pinning, and the cache;
* fetch admission (D3): join-before-charge, one in flight per address slot,
  eight globally, the salted fixed-slot budget at table size + 1 addresses;
* the consent page and the internal-id probe through a real `TestClient`.

The database half — the synthetic row upsert, the CHECK, the sweep, the
token exchange — is `tests/integration/test_oauth_cimd_pg.py`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import settings
from src.limiter import limiter
from src.oauth import cimd
from src.oauth import resource as oauth_resource
from src.oauth import routes
from src.services import security_events, transfer

CLAUDE_CODE_URL = "https://claude.ai/oauth/claude-code-client-metadata"
CLAUDE_CODE_DOC = {
    "client_id": CLAUDE_CODE_URL,
    "client_name": "Claude Code",
    "client_uri": "https://claude.ai",
    "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}
PUBLIC_ADDRESS = "93.184.216.34"
PKCE = "a" * 43


# ── the fake document server ────────────────────────────────────────────────


@dataclass
class Route:
    status: int = 200
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    chunks: list | None = None
    gate: asyncio.Event | None = None


class Origin:
    """Serves canned responses through the real guarded fetch.

    `transfer.PinnedTransport` is replaced by a transport that records the
    address it was pinned to and the Host it kept, so DNS pinning is asserted
    on what the real `_fetch_hops` built. The resolver is injected through
    `cimd._resolver`; every resolved name and every connection is recorded.
    """

    def __init__(self):
        self.routes: dict[str, Route] = {}
        self.addresses: dict[str, list[str]] = {}
        self.resolutions: list[str] = []
        self.connections: list[tuple[str, str]] = []
        self.requests: list[str] = []

    def serve(self, url, document=None, **kwargs):
        if document is not None and "body" not in kwargs:
            kwargs["body"] = json.dumps(document).encode()
        self.routes[url] = Route(**kwargs)
        return self.routes[url]

    async def resolve(self, host, port):
        self.resolutions.append(host)
        return self.addresses.get(host, [PUBLIC_ADDRESS])


class _Stream(httpx.AsyncByteStream):
    def __init__(self, route: Route):
        self._route = route

    async def __aiter__(self):
        if self._route.gate is not None:
            await self._route.gate.wait()
        for chunk in self._route.chunks if self._route.chunks is not None else [self._route.body]:
            yield chunk

    async def aclose(self):
        pass


@pytest.fixture
def origin(monkeypatch):
    server = Origin()

    class FakePinned(httpx.AsyncBaseTransport):
        def __init__(self, *, address, host_header, **_kw):
            self.address = address
            self.host_header = host_header

        async def handle_async_request(self, request):
            server.connections.append((self.address, self.host_header))
            url = str(request.url)
            server.requests.append(url)
            route = server.routes.get(url, Route(status=404))
            return httpx.Response(
                route.status,
                headers=route.headers,
                stream=_Stream(route),
                request=request,
            )

    monkeypatch.setattr(transfer, "PinnedTransport", FakePinned)
    monkeypatch.setattr(cimd, "_resolver", server.resolve)
    monkeypatch.setattr(settings, "oauth_cimd_enabled", True)
    monkeypatch.setattr(settings, "oauth_cimd_cache_max_seconds", 86400)
    monkeypatch.setattr(settings, "oauth_cimd_fetches_per_address_per_minute", 10)
    cimd.reset_state_for_tests()
    yield server
    cimd.reset_state_for_tests()


def run(coro):
    return asyncio.run(coro)


def refusal(coro) -> cimd.CimdRefusal:
    with pytest.raises(cimd.CimdRefusal) as caught:
        run(coro)
    return caught.value


def doc_for(url, **overrides):
    document = dict(CLAUDE_CODE_DOC, client_id=url)
    document.update(overrides)
    return document


# ── D1: the client identifier URL ──────────────────────────────────────────


def test_detection_is_the_exact_https_prefix():
    assert cimd.is_cimd_client_id(CLAUDE_CODE_URL)
    assert not cimd.is_cimd_client_id("a3f19c7e5b2d4081a3f19c7e5b2d4081")
    assert not cimd.is_cimd_client_id("HTTPS://claude.ai/x")
    assert cimd.is_internal_id("cimd_" + "a" * 59)


@pytest.mark.parametrize(
    "url, reason",
    [
        ("https://example.com", "url_invalid"),
        ("https://example.com/", "url_invalid"),
        ("https://a@example.com/c", "url_invalid"),
        ("https://example.com/c#f", "url_invalid"),
        ("https://example.com/c?", "url_invalid"),
        ("https://a.example/x?token=secret", "url_invalid"),
        ("https://example.com/a/../c", "url_invalid"),
        ("https://example.com/a/%2e%2e/c", "url_invalid"),
        ("https://example.com/./c", "url_invalid"),
        ("https://example.com:5432/c", "url_invalid"),
        ("https://localhost/c", "url_invalid"),
        ("https://exa mple.com/c", "url_invalid"),
        ("https://example.com/" + "a" * 2048, "url_invalid"),
        ("https://Claude.AI/oauth/x", "url_not_canonical"),
        ("https://claude.ai:443/oauth/x", "url_not_canonical"),
    ],
)
def test_bad_client_identifiers_are_refused_before_any_connection(origin, url, reason):
    got = refusal(cimd.resolve(url, "198.51.100.1"))
    assert got.reason == reason
    assert origin.resolutions == [] and origin.connections == []


def test_a_canonical_url_on_8443_is_accepted():
    assert cimd.validate_client_id_url("https://example.com:8443/c") == "example.com"


# ── D4: the document and the projection ────────────────────────────────────


def test_the_live_claude_code_document_validates():
    projection = cimd.validate_document(CLAUDE_CODE_URL, CLAUDE_CODE_DOC)
    assert projection.client_name == "Claude Code"
    assert projection.redirect_uris == ("http://localhost/callback", "http://127.0.0.1/callback")
    assert projection.scope == "offline_access read readwrite"
    assert projection.loopback_only


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"client_id": CLAUDE_CODE_URL + "/"}, "client_id_mismatch"),
        ({"client_id": CLAUDE_CODE_URL.upper()}, "client_id_mismatch"),
        ({"client_id": None}, "client_id_mismatch"),
        ({"client_name": ""}, "document_invalid"),
        ({"client_name": "x" * 256}, "document_invalid"),
        ({"client_name": 7}, "document_invalid"),
        ({"redirect_uris": []}, "redirect_uris_invalid"),
        ({"redirect_uris": ["https://a.example/cb"] * 2}, "redirect_uris_invalid"),
        ({"redirect_uris": [f"https://a.example/cb{i}" for i in range(21)]}, "redirect_uris_invalid"),
        ({"redirect_uris": ["http://example.com/cb"]}, "redirect_uris_invalid"),
        ({"redirect_uris": ["https://a.example/cb#f"]}, "redirect_uris_invalid"),
        ({"redirect_uris": ["http://user@localhost/cb"]}, "redirect_uris_invalid"),
        ({"redirect_uris": "https://a.example/cb"}, "redirect_uris_invalid"),
        ({"token_endpoint_auth_method": "client_secret_post"}, "auth_method_unsupported"),
        ({"token_endpoint_auth_method": "private_key_jwt"}, "auth_method_unsupported"),
        ({"client_secret": "s"}, "secret_present"),
        ({"client_secret_expires_at": 0}, "secret_present"),
        ({"grant_types": ["refresh_token"]}, "grant_types_invalid"),
        ({"response_types": ["token"]}, "grant_types_invalid"),
        ({"scope": "offline_access"}, "scope_invalid"),
        ({"scope": "admin"}, "scope_invalid"),
        ({"scope": 3}, "scope_invalid"),
    ],
)
def test_invalid_documents_are_refused(overrides, reason):
    with pytest.raises(cimd.CimdRefusal) as caught:
        cimd.validate_document(CLAUDE_CODE_URL, dict(CLAUDE_CODE_DOC, **overrides))
    assert caught.value.reason == reason


def test_absent_auth_method_is_none():
    document = dict(CLAUDE_CODE_DOC)
    del document["token_endpoint_auth_method"]
    assert cimd.validate_document(CLAUDE_CODE_URL, document).scope


def test_the_authorization_hash_ignores_the_name_and_redirect_order():
    base = cimd.validate_document(CLAUDE_CODE_URL, CLAUDE_CODE_DOC)
    renamed = cimd.validate_document(
        CLAUDE_CODE_URL, dict(CLAUDE_CODE_DOC, client_name="Something Else")
    )
    reordered = cimd.validate_document(
        CLAUDE_CODE_URL,
        dict(CLAUDE_CODE_DOC, redirect_uris=list(reversed(CLAUDE_CODE_DOC["redirect_uris"]))),
    )
    assert base.authorization_hash == renamed.authorization_hash == reordered.authorization_hash


@pytest.mark.parametrize(
    "overrides",
    [
        {"redirect_uris": ["http://localhost/callback"]},
        {"redirect_uris": ["http://localhost/callback", "http://127.0.0.1/other"]},
        {"scope": "read"},
    ],
)
def test_the_authorization_hash_changes_with_redirects_or_scope(overrides):
    base = cimd.validate_document(CLAUDE_CODE_URL, CLAUDE_CODE_DOC)
    changed = cimd.validate_document(CLAUDE_CODE_URL, dict(CLAUDE_CODE_DOC, **overrides))
    assert base.authorization_hash != changed.authorization_hash


# ── D5: redirect matching ──────────────────────────────────────────────────


LOOPBACK = ["http://localhost/callback", "http://127.0.0.1/callback"]


@pytest.mark.parametrize(
    "registered, requested, cimd_client, allowed",
    [
        (LOOPBACK, "http://localhost:3118/callback", True, True),
        (LOOPBACK, "http://127.0.0.1:49152/callback", True, True),
        (LOOPBACK, "http://localhost/callback", True, True),
        (["http://[::1]/cb"], "http://[::1]:8080/cb", True, True),
        (["http://127.0.0.1/callback"], "http://localhost:3118/callback", True, False),
        (LOOPBACK, "http://localhost:3118/callback/x", True, False),
        (LOOPBACK, "http://localhost:3118/callback?a=1", True, False),
        (LOOPBACK, "http://localhost:3118/callback#f", True, False),
        (LOOPBACK, "http://user@localhost:3118/callback", True, False),
        (LOOPBACK, "http://localhost:99999/callback", True, False),
        (LOOPBACK, "http://localhost:abc/callback", True, False),
        (LOOPBACK, "https://localhost:3118/callback", True, False),
        (LOOPBACK, "http://evil.example:3118/callback", True, False),
        (["https://claude.ai/api/mcp/auth_callback"], "https://claude.ai:8443/api/mcp/auth_callback", True, False),
        (["https://claude.ai/api/mcp/auth_callback"], "https://claude.ai/api/mcp/auth_callback", True, True),
        # DCR keeps exact membership, byte for byte.
        (LOOPBACK, "http://localhost:3118/callback", False, False),
        (LOOPBACK, "http://localhost/callback", False, True),
    ],
)
def test_redirect_matching(registered, requested, cimd_client, allowed):
    assert cimd.redirect_uri_allowed(registered, requested, cimd=cimd_client) is allowed


# ── the synthetic id ───────────────────────────────────────────────────────


def test_the_synthetic_id_is_64_characters_per_url_and_user(monkeypatch):
    a = cimd.cimd_row_id(CLAUDE_CODE_URL, 1)
    assert a.startswith("cimd_") and len(a) == 64
    assert a != cimd.cimd_row_id(CLAUDE_CODE_URL, 2)
    assert a != cimd.cimd_row_id(CLAUDE_CODE_URL, None)
    assert a == cimd.cimd_row_id(CLAUDE_CODE_URL, 1)


def test_the_synthetic_id_is_not_computable_without_the_secret(monkeypatch):
    first = cimd.cimd_row_id(CLAUDE_CODE_URL, 7)
    monkeypatch.setattr(settings, "secret_key", "a-different-deployment-secret-0123456789")
    assert cimd.cimd_row_id(CLAUDE_CODE_URL, 7) != first
    plain = "cimd_" + hashlib.sha256(f"{CLAUDE_CODE_URL}\n7".encode()).hexdigest()[:59]
    assert first != plain


# ── the bounded cache directive ────────────────────────────────────────────


@pytest.mark.parametrize(
    "header, expected",
    [
        (None, None),
        ("max-age=300", transfer.CacheDirective(300, False)),
        ("public, max-age=604800", transfer.CacheDirective(604800, False)),
        ("no-store", transfer.CacheDirective(None, True)),
        ("no-cache, max-age=60", transfer.CacheDirective(60, True)),
        ("max-age=-1", transfer.CacheDirective(None, False)),
        ("max-age=abc", transfer.CacheDirective(None, False)),
        ('max-age="120"', transfer.CacheDirective(120, False)),
        ("max-age=99999999999999", transfer.CacheDirective(2**31 - 1, False)),
    ],
)
def test_cache_control_parsing(header, expected):
    assert transfer.parse_cache_control(header) == expected


def test_the_fetch_result_exposes_no_raw_header(origin):
    origin.serve(
        CLAUDE_CODE_URL,
        CLAUDE_CODE_DOC,
        headers={"cache-control": "public, max-age=300", "x-secret": "leak"},
    )

    async def go():
        async with transfer.fetch_url_guarded(
            CLAUDE_CODE_URL, allow_http=False, max_bytes=65536,
            resolver=origin.resolve, max_redirects=0,
        ) as result:
            return result

    result = run(go())
    assert result.cache_control == transfer.CacheDirective(300, False)
    flattened = json.dumps({k: str(v) for k, v in vars(result).items()})
    assert "leak" not in flattened and "public" not in flattened


# ── the fake document server: success, pinning, failure modes ──────────────


def test_a_valid_document_is_fetched_through_the_pinned_address(origin):
    origin.serve(CLAUDE_CODE_URL, CLAUDE_CODE_DOC)
    projection = run(cimd.resolve(CLAUDE_CODE_URL, "198.51.100.1"))
    assert projection.client_name == "Claude Code"
    assert origin.connections == [(PUBLIC_ADDRESS, "claude.ai")]


def _sub(url_path="/doc"):
    return f"https://docs.example.org{url_path}"


@pytest.mark.parametrize(
    "route_kwargs, reason",
    [
        ({"status": 301, "headers": {"location": "https://claude.ai/x"}}, "http_status"),
        ({"status": 302, "headers": {"location": "/elsewhere"}}, "http_status"),
        ({"status": 404}, "http_status"),
        ({"status": 500}, "http_status"),
        ({"chunks": [b"x" * 40000, b"x" * 30000]}, "too_large"),
        ({"headers": {"content-length": "70000"}, "body": b"{}"}, "too_large"),
        ({"headers": {"content-encoding": "gzip"}, "body": b"{}"}, "content_encoded"),
        ({"body": b"not json"}, "not_json"),
        ({"body": b"[1, 2]"}, "not_json"),
        ({"body": b"\xff\xfe{}"}, "not_json"),
    ],
)
def test_every_fetch_failure_mode_is_a_closed_reason(origin, route_kwargs, reason):
    origin.serve(_sub(), **route_kwargs)
    got = refusal(cimd.resolve(_sub(), "198.51.100.1"))
    assert got.reason == reason
    assert got.host == "docs.example.org"


def test_a_redirect_is_never_followed(origin):
    origin.serve(_sub(), status=302, headers={"location": "https://claude.ai/oauth/claude-code-client-metadata"})
    origin.serve(CLAUDE_CODE_URL, CLAUDE_CODE_DOC)
    refusal(cimd.resolve(_sub(), None))
    assert origin.requests == [_sub()]


def test_a_slow_body_hits_the_deadline(origin, monkeypatch):
    monkeypatch.setattr(cimd, "FETCH_DEADLINE_SECONDS", 0.05)
    origin.serve(_sub(), CLAUDE_CODE_DOC, gate=asyncio.Event())
    assert refusal(cimd.resolve(_sub(), None)).reason == "fetch_timeout"


def test_a_document_naming_another_url_is_refused(origin):
    origin.serve(_sub(), CLAUDE_CODE_DOC)  # claims to be Claude Code
    assert refusal(cimd.resolve(_sub(), None)).reason == "client_id_mismatch"


def test_proxy_environment_is_ignored(origin, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    origin.serve(CLAUDE_CODE_URL, CLAUDE_CODE_DOC)
    run(cimd.resolve(CLAUDE_CODE_URL, None))
    assert origin.connections == [(PUBLIC_ADDRESS, "claude.ai")]


# ── SSRF: the transfer deny list, for resolved names and literals ──────────


FORBIDDEN = [
    "127.0.0.1", "::1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "fc00::1", "fe80::1", "0.0.0.0", "64:ff9b::a00:5", "2002:a00:1::1",
    "2001:0:4136:e378:8000:63bf:f5ff:fffe", "::ffff:10.0.0.5", "198.18.0.1", "224.0.0.1",
]


@pytest.mark.parametrize("address", FORBIDDEN)
def test_a_name_resolving_to_a_forbidden_address_is_never_connected(origin, address):
    origin.addresses["evil.example.org"] = [address]
    url = "https://evil.example.org/doc"
    origin.serve(url, doc_for(url))
    got = refusal(cimd.resolve(url, None))
    assert got.reason == "ssrf_refused"
    assert origin.connections == []


@pytest.mark.parametrize("literal", ["127.0.0.1", "[::1]", "10.0.0.5", "169.254.169.254", "[fc00::1]"])
def test_a_literal_forbidden_address_is_never_connected(origin, literal):
    url = f"https://{literal}/doc"
    got = refusal(cimd.resolve(url, None))
    assert got.reason in {"ssrf_refused", "url_invalid", "url_not_canonical"}
    assert origin.connections == []


def test_mixed_public_and_private_answers_are_refused(origin):
    origin.addresses["mixed.example.org"] = [PUBLIC_ADDRESS, "10.0.0.5"]
    url = "https://mixed.example.org/doc"
    origin.serve(url, doc_for(url))
    assert refusal(cimd.resolve(url, None)).reason == "ssrf_refused"
    assert origin.connections == []


# ── the cache and the cooldown ─────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(cimd, "_now", c)
    return c


def _fetches(origin, url):
    return origin.requests.count(url)


@pytest.mark.parametrize(
    "header, ttl",
    [
        ("max-age=604800", 86400),
        ("max-age=60", 60),
        (None, 300),
        ("no-store", 0),
        ("no-cache", 0),
        ("max-age=0", 0),
    ],
)
def test_cache_ttl_follows_cache_control_under_the_ceiling(origin, clock, header, ttl):
    url = _sub("/ttl")
    headers = {"cache-control": header} if header else {}
    origin.serve(url, doc_for(url), headers=headers)
    run(cimd.resolve(url, None))
    if ttl:
        clock.now += ttl - 1
        run(cimd.resolve(url, None))
        assert _fetches(origin, url) == 1
        clock.now += 2
    run(cimd.resolve(url, None))
    assert _fetches(origin, url) == 2


def test_a_zero_ceiling_never_caches(origin, clock, monkeypatch):
    monkeypatch.setattr(settings, "oauth_cimd_cache_max_seconds", 0)
    url = _sub("/zero")
    origin.serve(url, doc_for(url), headers={"cache-control": "max-age=3600"})
    run(cimd.resolve(url, None))
    run(cimd.resolve(url, None))
    assert _fetches(origin, url) == 2


def test_a_failed_refetch_never_serves_the_expired_document(origin, clock):
    url = _sub("/expire")
    origin.serve(url, doc_for(url), headers={"cache-control": "max-age=10"})
    run(cimd.resolve(url, None))
    clock.now += 11
    origin.serve(url, status=500)
    assert refusal(cimd.resolve(url, None)).reason == "http_status"


def test_a_failing_url_is_fetched_at_most_once_per_cooldown(origin, clock):
    url = _sub("/down")
    origin.serve(url, status=503)
    refusal(cimd.resolve(url, "198.51.100.1"))
    for i in range(50):
        assert refusal(cimd.resolve(url, f"203.0.113.{i}")).reason == "fetch_cooldown"
    assert _fetches(origin, url) == 1
    clock.now += 31
    refusal(cimd.resolve(url, "198.51.100.1"))
    assert _fetches(origin, url) == 2


def test_cache_hits_spend_no_budget(origin, clock, monkeypatch):
    monkeypatch.setattr(settings, "oauth_cimd_fetches_per_address_per_minute", 1)
    cached = _sub("/cached")
    origin.serve(cached, doc_for(cached))
    run(cimd.resolve(cached, "198.51.100.1"))
    other = _sub("/other")
    origin.serve(other, doc_for(other))
    assert refusal(cimd.resolve(other, "198.51.100.1")).reason == "fetch_budget_exhausted"
    run(cimd.resolve(cached, "198.51.100.1"))  # still served


# ── admission ──────────────────────────────────────────────────────────────


def test_the_per_address_budget_refuses_the_eleventh_fetch(origin, clock):
    for i in range(10):
        url = _sub(f"/n{i}")
        origin.serve(url, doc_for(url), headers={"cache-control": "no-store"})
        run(cimd.resolve(url, "198.51.100.1"))
    url = _sub("/n10")
    origin.serve(url, doc_for(url))
    assert refusal(cimd.resolve(url, "198.51.100.1")).reason == "fetch_budget_exhausted"
    assert _fetches(origin, url) == 0
    clock.now += 61
    run(cimd.resolve(url, "198.51.100.1"))


def test_one_address_cannot_block_another_clients_authorization(origin):
    """Codex round 1: four slow hostile fetches, then a Claude flow."""
    gate = asyncio.Event()
    hostile = [_sub(f"/slow{i}") for i in range(4)]
    for url in hostile:
        origin.serve(url, doc_for(url), gate=gate)
    origin.serve(CLAUDE_CODE_URL, CLAUDE_CODE_DOC)

    async def scenario():
        tasks = [asyncio.create_task(cimd.resolve(url, "198.51.100.66")) for url in hostile]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        projection = await cimd.resolve(CLAUDE_CODE_URL, "203.0.113.9")
        gate.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        return projection, outcomes

    projection, outcomes = run(scenario())
    assert projection.client_name == "Claude Code"
    admitted = [o for o in outcomes if isinstance(o, cimd.Projection)]
    busy = [o for o in outcomes if isinstance(o, cimd.CimdRefusal) and o.reason == "fetch_busy"]
    assert len(admitted) == 1 and len(busy) == 3


def test_joining_a_flight_is_never_refused_by_the_caps(origin, monkeypatch):
    monkeypatch.setattr(cimd, "GLOBAL_IN_FLIGHT", 1)
    gate = asyncio.Event()
    url = _sub("/shared")
    origin.serve(url, doc_for(url), gate=gate)

    async def scenario():
        first = asyncio.create_task(cimd.resolve(url, "198.51.100.1"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Same slot, slot in-flight full, global cap full: the join still waits.
        second = asyncio.create_task(cimd.resolve(url, "198.51.100.1"))
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(first, second)

    a, b = run(scenario())
    assert a == b
    assert _fetches(origin, url) == 1
    slot = cimd._address_table[cimd._slot_for("198.51.100.1")]
    assert slot.count == 1


def test_the_global_cap_is_eight(origin):
    gate = asyncio.Event()
    urls = [_sub(f"/g{i}") for i in range(9)]
    for url in urls:
        origin.serve(url, doc_for(url), gate=gate)

    async def scenario():
        tasks = [
            asyncio.create_task(cimd.resolve(url, f"198.51.100.{i}"))
            for i, url in enumerate(urls)
        ]
        for _ in range(4):
            await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(*tasks, return_exceptions=True)

    outcomes = run(scenario())
    refused = [o for o in outcomes if isinstance(o, cimd.CimdRefusal)]
    assert len(refused) <= 1
    assert sum(isinstance(o, cimd.Projection) for o in outcomes) >= 8


def test_address_churn_does_not_refill_a_spent_slot(origin, clock, monkeypatch):
    """The failed-auth algorithm at table size + 1 addresses: nothing evicts."""
    monkeypatch.setattr(settings, "oauth_cimd_fetches_per_address_per_minute", 1)
    cimd.reset_state_for_tests(table_size=16)
    spent = "198.51.100.1"
    first = _sub("/spent")
    origin.serve(first, doc_for(first), headers={"cache-control": "no-store"})
    run(cimd.resolve(first, spent))
    for i in range(17):
        url = _sub(f"/churn{i}")
        origin.serve(url, doc_for(url), headers={"cache-control": "no-store"})
        try:
            run(cimd.resolve(url, f"203.0.113.{i}"))
        except cimd.CimdRefusal as exc:
            assert exc.reason == "fetch_budget_exhausted"  # a forced collision: stricter
    assert cimd.address_table_size() == 16
    assert refusal(cimd.resolve(first, spent)).reason == "fetch_budget_exhausted"
    clock.now += 61
    run(cimd.resolve(first, spent))


def test_a_request_without_an_address_is_charged_to_slot_zero(origin):
    assert cimd._slot_for(None) == 0
    assert cimd._slot_for("") == 0
    assert cimd._slot_for("198.51.100.1") != 0


# ── the consent page and the internal-id probe ─────────────────────────────


class _NoRowResult:
    def scalar_one_or_none(self):
        return None


class _RecordingSession:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, *a, **kw):
        self.log.append(str(stmt))
        return _NoRowResult()


@pytest.fixture
def client(monkeypatch, origin):
    queries: list[str] = []
    monkeypatch.setattr(settings, "multi_user_mode", False)
    monkeypatch.setattr(routes, "async_session", lambda: _RecordingSession(queries))
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(routes.router)
    test_client = TestClient(app)
    test_client.queries = queries
    return test_client


@pytest.fixture
def emitted(monkeypatch):
    records: list[tuple[str, dict]] = []
    real = security_events.emit

    def capture(event, **kwargs):
        records.append((event, kwargs))
        return real(event, **kwargs)

    monkeypatch.setattr(security_events, "emit", capture)
    return records


def _authorize(client, client_id, redirect_uri="http://localhost:3118/callback", **extra):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": PKCE,
        "code_challenge_method": "S256",
        **extra,
    }
    return client.get("/authorize", params=params)


def test_the_consent_page_shows_the_host_and_url_never_the_name(client, origin):
    origin.serve(CLAUDE_CODE_URL, dict(CLAUDE_CODE_DOC, client_name="Totally Not Claude"))
    response = _authorize(client, CLAUDE_CODE_URL)
    assert response.status_code == 200, response.text
    page = response.text
    assert "claude.ai" in page
    assert CLAUDE_CODE_URL in page
    assert "Totally Not Claude" not in page
    assert "fetched that document" in page
    assert "program on this computer" in page  # loopback-only warning
    # The header itself is added by the app's CSP middleware (pinned in
    # test_panel_csp_headers.py); here, the page is marked as the consent page.
    from src.services import panel_csp

    assert "form-action 'self' https: http://localhost:* http://127.0.0.1:*" in (
        panel_csp.build_policy("A" * 22, consent=True)
    )
    assert " style=" not in page and "onclick" not in page


def test_a_redirect_mismatch_is_the_constant_refusal(client, origin, emitted):
    origin.serve(CLAUDE_CODE_URL, CLAUDE_CODE_DOC)
    mismatch = _authorize(client, CLAUDE_CODE_URL, redirect_uri="http://localhost:3118/elsewhere")
    cimd.reset_state_for_tests()
    origin.routes.clear()
    origin.serve(CLAUDE_CODE_URL, status=404)
    missing = _authorize(client, CLAUDE_CODE_URL)
    cimd.reset_state_for_tests()
    other = "https://docs.example.org/doc"
    origin.serve(other, CLAUDE_CODE_DOC)
    wrong_id = _authorize(client, other)
    for response in (mismatch, missing, wrong_id):
        assert response.status_code == 400
        assert response.content == b'{"error":"invalid_client"}'
    reasons = [kw["reason"] for event, kw in emitted if event == "oauth_authorize_refused"]
    assert reasons == [
        "client_metadata.redirect_uri_mismatch",
        "client_metadata.http_status",
        "client_metadata.client_id_mismatch",
    ]
    for event, kw in emitted:
        assert kw.get("client_id_submitted") is None
        assert CLAUDE_CODE_URL not in json.dumps(kw, default=str)


def test_another_users_synthetic_id_reveals_nothing(client, emitted):
    """Codex R2-1: refused before any lookup, identical to a nonexistent id."""
    real = _authorize(client, cimd.cimd_row_id(CLAUDE_CODE_URL, 1), "https://claude.ai/cb")
    fake = _authorize(client, "cimd_" + "0" * 59, "https://claude.ai/cb")
    assert real.status_code == fake.status_code == 400
    assert real.content == fake.content == b'{"error":"invalid_client"}'
    assert client.queries == []
    reasons = [kw["reason"] for event, kw in emitted if event == "oauth_authorize_refused"]
    assert reasons == ["client_metadata.internal_id_submitted"] * 2


def test_a_query_string_client_id_is_never_logged(client, origin, emitted):
    response = _authorize(client, "https://a.example/x?token=secret")
    assert response.content == b'{"error":"invalid_client"}'
    assert "token=secret" not in json.dumps(emitted, default=str)
    (record,) = [kw for event, kw in emitted if event == "oauth_authorize_refused"]
    assert record["cimd_host"] == "a.example"
    assert len(record["cimd_ref"]) == 16
    assert origin.connections == []


def test_cimd_disabled_is_an_unknown_client(client, origin, monkeypatch, emitted):
    monkeypatch.setattr(settings, "oauth_cimd_enabled", False)
    origin.serve(CLAUDE_CODE_URL, CLAUDE_CODE_DOC)
    response = _authorize(client, CLAUDE_CODE_URL)
    assert response.status_code == 400
    assert origin.requests == []
    (record,) = [kw for event, kw in emitted if event == "oauth_authorize_refused"]
    assert record["reason"] == "unknown_client"
    assert record["client_id_submitted"] is None


# ── AS metadata ────────────────────────────────────────────────────────────


def test_metadata_advertises_both_values_claude_needs(client, monkeypatch):
    body = client.get("/.well-known/oauth-authorization-server").json()
    assert body["client_id_metadata_document_supported"] is True
    assert "none" in body["token_endpoint_auth_methods_supported"]
    assert body["authorization_response_iss_parameter_supported"] is True
    assert body["registration_endpoint"].endswith("/register")
    monkeypatch.setattr(settings, "oauth_cimd_enabled", False)
    body = client.get("/.well-known/oauth-authorization-server").json()
    assert "client_id_metadata_document_supported" not in body
    assert body["registration_endpoint"].endswith("/register")


# ── RFC 8707 classification ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "values, expected",
    [
        ([], ("absent", None)),
        (["{base}/mcp"], ("accepted_1", None)),
        (["{base}/mcp/"], ("accepted_2", None)),
        (["{base}"], ("accepted_3", None)),
        (["{base}/"], ("accepted_4", None)),
        (["{base}/mcp", "{base}/mcp"], ("accepted_1", None)),
        (["{base}/mcp", "{base}"], ("unexpected", "<base-host>")),
        (["https://other.example/mcp?k=secret"], ("unexpected", "other.example")),
        (["not a url"], ("unexpected", "<unparseable>")),
    ],
)
def test_resource_classification(values, expected):
    base = settings.base_url.rstrip("/")
    from urllib.parse import urlsplit

    host = urlsplit(base).hostname
    values = [v.replace("{base}", base) for v in values]
    expected = (expected[0], host if expected[1] == "<base-host>" else expected[1])
    assert oauth_resource.classify(values) == expected


def test_a_consent_render_emits_one_classification_only_record(client, origin, emitted):
    origin.serve(CLAUDE_CODE_URL, CLAUDE_CODE_DOC)
    response = _authorize(client, CLAUDE_CODE_URL, resource="https://other.example/mcp?k=secret")
    assert response.status_code == 200
    classified = [kw for event, kw in emitted if event == "oauth_resource_classified"]
    assert classified == [
        {
            "level": logging.INFO,
            "subject": classified[0]["subject"],
            "resource_class": "unexpected",
            "resource_host": "other.example",
            "route": "/authorize",
        }
    ]
    assert "k=secret" not in json.dumps(emitted, default=str)
    assert 'name="resource"' in response.text  # carried to the POST


def test_a_refusal_carries_the_class_and_emits_no_extra_record(client, emitted):
    base = settings.base_url.rstrip("/")
    _authorize(client, "cimd_" + "0" * 59, resource=f"{base}/mcp")
    assert [event for event, _ in emitted] == ["oauth_authorize_refused"]
    assert emitted[0][1]["resource_class"] == "accepted_1"


def test_the_login_redirect_is_classified(client, monkeypatch, emitted):
    monkeypatch.setattr(settings, "multi_user_mode", True)

    async def nobody(*_a, **_kw):
        return None

    monkeypatch.setattr(routes, "get_active_session_user", nobody)
    response = _authorize(client, CLAUDE_CODE_URL)
    assert response.status_code in (302, 307) or response.history
    assert [kw["resource_class"] for event, kw in emitted if event == "oauth_resource_classified"] == ["absent"]


# ── terminal paths outside the handlers: the 429 and framework 422s ────────


def _app_with_handlers():
    from fastapi.exceptions import RequestValidationError

    import src.main as main

    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(routes.router)
    app.add_exception_handler(RequestValidationError, main._validation_error_handler)
    return app


def test_a_malformed_consent_request_is_classified_from_its_query(monkeypatch, emitted):
    monkeypatch.setattr(settings, "multi_user_mode", False)
    response = TestClient(_app_with_handlers()).get(
        "/authorize", params={"resource": "https://other.example/mcp", "client_id": "x"}
    )
    assert response.status_code == 422
    classified = [kw for event, kw in emitted if event == "oauth_resource_classified"]
    assert [(c["resource_class"], c["resource_host"]) for c in classified] == [
        ("unexpected", "other.example")
    ]


def test_a_malformed_post_is_unclassified_without_a_body_read(monkeypatch, emitted):
    response = TestClient(_app_with_handlers()).post(
        "/authorize", data={"resource": "https://other.example/mcp"}
    )
    assert response.status_code == 422
    classified = [kw for event, kw in emitted if event == "oauth_resource_classified"]
    assert [c["resource_class"] for c in classified] == ["unclassified_malformed"]
    assert "other.example" not in json.dumps(emitted, default=str)


def test_an_unhandled_exception_at_token_is_classified(monkeypatch, emitted):
    async def explode(*_a, **_kw):
        raise RuntimeError("injected")

    monkeypatch.setattr(routes, "_handle_auth_code", explode)
    limiter.reset()
    response = TestClient(_app_with_handlers(), raise_server_exceptions=False).post(
        "/token",
        data={"grant_type": "authorization_code", "resource": "https://other.example/mcp"},
    )
    assert response.status_code == 500
    classified = [kw for event, kw in emitted if event == "oauth_resource_classified"]
    assert [(c["resource_class"], c["resource_host"]) for c in classified] == [
        ("unexpected", "other.example")
    ]


def test_an_unhandled_exception_at_authorize_is_classified(client, origin, monkeypatch, emitted):
    async def explode(*_a, **_kw):
        raise RuntimeError("injected")

    monkeypatch.setattr(routes, "_authorize_get", explode)
    response = TestClient(client.app, raise_server_exceptions=False).get(
        "/authorize", params={
            "response_type": "code", "client_id": CLAUDE_CODE_URL,
            "redirect_uri": "http://localhost:1/callback", "code_challenge": PKCE,
        },
    )
    assert response.status_code == 500
    assert [kw["resource_class"] for e, kw in emitted if e == "oauth_resource_classified"] == ["absent"]


def test_the_shared_submitted_fields_helper():
    assert cimd.submitted_fields("https://a.example/x?y") == cimd.SubmittedFields(
        None, "a.example", hashlib.sha256(b"https://a.example/x?y").hexdigest()[:16]
    )
    assert cimd.submitted_fields("a3f1") == cimd.SubmittedFields("a3f1", None, None)
    assert cimd.submitted_fields("a3f1", resolved=True) == cimd.SubmittedFields(None, None, None)


def test_the_token_429_rides_rate_limit_exceeded_without_reading_the_body(emitted):
    from types import SimpleNamespace

    import limits
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address
    from slowapi.wrappers import Limit
    from starlette.requests import Request

    import src.main as main

    limit = Limit(
        limit=limits.parse("10/minute"), key_func=get_remote_address, scope="",
        per_method=False, methods=None, error_message=None, exempt_when=None,
        cost=1, override_defaults=False,
    )

    async def receive():  # pragma: no cover - must never be called
        raise AssertionError("the 429 path read the request body")

    request = Request(
        {
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "https",
            "path": "/token", "raw_path": b"/token", "root_path": "", "query_string": b"",
            "headers": [(b"host", b"testserver")], "client": ("198.51.100.4", 5),
            "server": ("testserver", 443),
            "app": SimpleNamespace(state=SimpleNamespace(limiter=limiter)), "state": {},
        },
        receive,
    )
    request.state.view_rate_limit = (limit.limit, ["198.51.100.4"])
    response = main._rate_limit_handler(request, RateLimitExceeded(limit))
    assert response.status_code == 429
    assert [event for event, _ in emitted] == ["rate_limit_exceeded"]
    assert emitted[0][1]["resource_class"] == "unclassified_rate_limited"
