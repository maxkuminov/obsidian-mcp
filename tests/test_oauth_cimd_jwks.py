"""The JWKS of a `private_key_jwt` CIMD client (#420, design D2, D3): offline.

* `validate_jwks`: the shape, the whole-set private-member refusal, every
  "ignored" key rule (RSA bit length from `n`, the exponent bound, curve,
  `use`, `key_ops`, `alg`, import), duplicate `kid`, no usable key, and the
  normalised snapshot;
* `resolve_jwks` on the #357 fake document server: the ChatGPT flow, every
  fetch failure mode as a `jwks_*` reason, the SSRF deny list, the cache with
  its original `fetched_at`, the cooldown, and the **second charge** (a
  cached document does not make the JWKS free; a joined flight is free);
* `fetch_jwks_guarded` and the cache accessors that the `/token` pre-phase
  (`client_assertion`) builds on.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from joserfc.jwk import ECKey, OKPKey, RSAKey

from src.config import settings
from src.oauth import cimd
from tests.test_oauth_cimd import (  # noqa: F401 - pytest fixtures
    CHATGPT_DOC,
    CHATGPT_URL,
    FORBIDDEN,
    PUBLIC_ADDRESS,
    Clock,
    clock,
    origin,
    refusal,
    run,
)

JWKS_URI = CHATGPT_DOC["jwks_uri"]
ADDRESS = "198.51.100.7"


# ── keys ────────────────────────────────────────────────────────────────────


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


_RSA = RSAKey.generate_key(2048, private=True)
_EC = ECKey.generate_key("P-256", private=True)


def rsa_public(kid="rsa-1", **extra) -> dict:
    key = dict(_RSA.as_dict(private=False), kid=kid)
    key.update(extra)
    return key


def rsa_private(kid="rsa-1") -> dict:
    return dict(_RSA.as_dict(private=True), kid=kid)


def ec_public(kid="ec-1", **extra) -> dict:
    key = dict(_EC.as_dict(private=False), kid=kid)
    key.update(extra)
    return key


def rsa_with_bits(bits: int, kid: str, e: int = 65537) -> dict:
    """A syntactically valid RSA public JWK with an n of exactly `bits` bits."""
    n = (1 << (bits - 1)) | 0x1F3 | (0xA5 << (bits // 2))
    return {"kty": "RSA", "kid": kid, "n": _b64(n), "e": _b64(e)}


def jwks(*keys) -> dict:
    return {"keys": list(keys)}


def snapshot_of(*keys):
    return cimd.validate_jwks(jwks(*keys))


def reason_of(obj) -> str:
    with pytest.raises(cimd.CimdRefusal) as caught:
        cimd.validate_jwks(obj)
    return caught.value.reason


# ── validate_jwks: shape and whole-set refusals ─────────────────────────────


def test_a_valid_set_is_normalised_to_public_members():
    noisy = rsa_public(use="sig", key_ops=["verify"], alg="RS256", x5c=["AAAA"], ext=True)
    snapshot = snapshot_of(noisy, ec_public())
    keys = {k["kid"]: k for k in snapshot.as_json()["keys"]}
    assert set(keys["rsa-1"]) == {"kty", "kid", "alg", "n", "e"}
    assert set(keys["ec-1"]) == {"kty", "kid", "crv", "x", "y"}
    assert snapshot.kids == frozenset({"rsa-1", "ec-1"})
    assert snapshot.fetched_at.tzinfo is not None


@pytest.mark.parametrize(
    "obj",
    [
        None,
        [],
        "keys",
        {},
        {"keys": {}},
        {"keys": []},
        {"keys": ["not-an-object"]},
        {"keys": [rsa_public(), 3]},
    ],
)
def test_a_malformed_set_is_jwks_invalid(obj):
    assert reason_of(obj) == "jwks_invalid"


def test_seventeen_keys_are_refused_and_sixteen_accepted():
    sixteen = [rsa_public(kid=f"k{i}") for i in range(16)]
    assert len(cimd.validate_jwks(jwks(*sixteen)).kids) == 16
    assert reason_of(jwks(*sixteen, rsa_public(kid="k16"))) == "jwks_invalid"


@pytest.mark.parametrize("member", ["d", "p", "q", "dp", "dq", "qi", "oth", "k"])
def test_any_private_member_refuses_the_whole_set(member):
    leaky = rsa_public(kid="leaky", **{member: "AQAB"})
    assert reason_of(jwks(rsa_public(kid="good"), leaky)) == "jwks_private_key"


def test_a_full_private_rsa_key_and_an_oct_key_refuse_the_set():
    assert reason_of(jwks(rsa_private())) == "jwks_private_key"
    assert reason_of(jwks(rsa_public(), {"kty": "oct", "k": "c2VjcmV0"})) == "jwks_private_key"


def test_a_private_member_on_an_otherwise_ignored_key_still_refuses():
    ignored_but_leaky = dict(rsa_private(kid="enc"), use="enc")
    assert reason_of(jwks(rsa_public(), ignored_but_leaky)) == "jwks_private_key"


def test_a_duplicate_kid_among_usable_keys_refuses_the_set():
    assert reason_of(jwks(rsa_public(kid="same"), ec_public(kid="same"))) == "jwks_invalid"


def test_a_duplicate_kid_on_an_ignored_key_does_not():
    snapshot = snapshot_of(rsa_public(kid="same"), ec_public(kid="same", use="enc"))
    assert snapshot.kids == frozenset({"same"})


# ── validate_jwks: ignored keys ─────────────────────────────────────────────


_P384 = ECKey.generate_key("P-384", private=True).as_dict(private=False)
_OKP = OKPKey.generate_key("Ed25519", private=True).as_dict(private=False)


@pytest.mark.parametrize(
    "ignored",
    [
        rsa_with_bits(1024, "small"),
        rsa_with_bits(2047, "just-small"),
        rsa_with_bits(9000, "huge"),
        rsa_with_bits(8193, "just-huge"),
        dict(_P384, kid="p384"),
        dict(_OKP, kid="okp"),
        rsa_public(kid="enc", use="enc"),
        rsa_public(kid="use-odd", use="SIG"),
        rsa_public(kid="ops", key_ops=["encrypt"]),
        rsa_public(kid="ops-str", key_ops="verify"),
        rsa_public(kid="hs", alg="HS256"),
        rsa_public(kid="eddsa", alg="EdDSA"),
        rsa_public(kid="rsa-es", alg="ES256"),
        ec_public(kid="ec-rs", alg="RS256"),
        rsa_with_bits(2048, "e-one", e=1),
        rsa_with_bits(2048, "e-even", e=65536),
        rsa_with_bits(2048, "e-big", e=2**32 + 1),
        {"kty": "RSA", "kid": "bad-n", "n": "not base64!", "e": "AQAB"},
        {"kty": "RSA", "kid": "no-e", "n": rsa_public()["n"]},
        dict(ec_public(kid="off-curve"), x=ec_public()["y"]),
        {"kty": "EC", "kid": "no-crv", "x": ec_public()["x"], "y": ec_public()["y"]},
        {"kid": "no-kty", "n": rsa_public()["n"], "e": "AQAB"},
        rsa_public(kid=7),
        rsa_public(kid="nul\x00kid"),
        rsa_public(kid="x" * 257),
    ],
    ids=lambda k: str(k.get("kid")) if isinstance(k, dict) else None,
)
def test_an_unusable_key_is_ignored(ignored):
    snapshot = snapshot_of(rsa_public(kid="good"), ignored)
    assert snapshot.kids == frozenset({"good"})
    assert len(snapshot.keys) == 1
    assert reason_of(jwks(ignored)) == "jwks_no_usable_key"


@pytest.mark.parametrize("bits", [2048, 8192])
def test_rsa_bit_length_bounds_are_inclusive(bits):
    assert snapshot_of(rsa_with_bits(bits, "edge")).kids == frozenset({"edge"})


def test_the_exponent_bounds_are_inclusive():
    assert snapshot_of(rsa_with_bits(2048, "e3", e=3)).kids == {"e3"}
    assert snapshot_of(rsa_with_bits(2048, "emax", e=2**32 - 1)).kids == {"emax"}


def test_key_ops_with_verify_and_matching_alg_are_usable():
    snapshot = snapshot_of(
        rsa_public(kid="ps", alg="PS256", key_ops=["verify", "sign"]),
        ec_public(kid="es", alg="ES256"),
    )
    assert snapshot.kids == frozenset({"ps", "es"})


def test_keys_without_kid_are_usable():
    key = rsa_public()
    del key["kid"]
    snapshot = snapshot_of(key)
    assert snapshot.kids == frozenset() and len(snapshot.keys) == 1


def test_the_weak_and_encryption_keys_scenario():
    # spec: a 1,024-bit signing key and a 2,048-bit `use: enc` key.
    assert reason_of(jwks(rsa_with_bits(1024, "weak"), rsa_public(kid="enc", use="enc"))) == (
        "jwks_no_usable_key"
    )


# ── the snapshot ────────────────────────────────────────────────────────────


def test_the_snapshot_is_immutable():
    snapshot = snapshot_of(rsa_public())
    snapshot.as_json()["keys"].clear()
    snapshot.keys[0]["kid"] = "tampered"
    assert snapshot.kids == frozenset({"rsa-1"})
    with pytest.raises(Exception):
        snapshot.fetched_at = datetime.now(timezone.utc)  # type: ignore[misc]


def test_a_stored_snapshot_rebuilds_with_its_own_fetch_time():
    t0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    stored = snapshot_of(rsa_public()).as_json()
    rebuilt = cimd.validate_jwks(stored, fetched_at=t0)
    assert rebuilt.fetched_at == t0
    assert rebuilt.as_json() == stored
    with pytest.raises(ValueError):
        cimd.validate_jwks(stored, fetched_at=datetime(2026, 11, 1))


def test_normalisation_is_idempotent():
    once = snapshot_of(rsa_public(alg="RS256"), ec_public()).as_json()
    assert cimd.validate_jwks(once).as_json() == once


# ── resolve_jwks on the fake origin ────────────────────────────────────────


def _projection():
    return cimd.validate_document(CHATGPT_URL, CHATGPT_DOC)


def _serve_chatgpt(origin, *keys, **kwargs):
    origin.serve(CHATGPT_URL, CHATGPT_DOC)
    origin.serve(JWKS_URI, jwks(*(keys or (rsa_public(),))), **kwargs)


def _charges(address=ADDRESS) -> int:
    slot = cimd._address_table[cimd._slot_for(address)]
    return 0 if slot is None else slot.count


class WallClock:
    def __init__(self):
        self.now = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def wall(monkeypatch):
    w = WallClock()
    monkeypatch.setattr(cimd, "_utcnow", w)
    return w


def test_the_chatgpt_flow_resolves_document_then_jwks(origin, wall):
    _serve_chatgpt(origin, rsa_public(), ec_public())

    async def go():
        projection = await cimd.resolve(CHATGPT_URL, ADDRESS)
        return projection, await cimd.resolve_jwks(projection, ADDRESS)

    projection, snapshot = run(go())
    assert projection.auth_method == "private_key_jwt"
    assert snapshot.kids == frozenset({"rsa-1", "ec-1"})
    assert snapshot.fetched_at == wall.now
    assert origin.requests == [CHATGPT_URL, JWKS_URI]
    assert origin.connections[-1] == (PUBLIC_ADDRESS, "chatgpt.com")
    assert _charges() == 2  # one per outbound request


def test_resolve_jwks_refuses_a_none_projection(origin):
    from tests.test_oauth_cimd import CLAUDE_CODE_DOC, CLAUDE_CODE_URL

    with pytest.raises(ValueError):
        run(cimd.resolve_jwks(cimd.validate_document(CLAUDE_CODE_URL, CLAUDE_CODE_DOC), ADDRESS))


@pytest.mark.parametrize(
    "route_kwargs, reason",
    [
        ({"status": 302, "headers": {"location": "https://chatgpt.com/other.json"}}, "jwks_http_status"),
        ({"status": 404}, "jwks_http_status"),
        ({"status": 500}, "jwks_http_status"),
        ({"chunks": [b" " * 65536, b"x"]}, "jwks_too_large"),
        ({"headers": {"content-length": "65537"}, "body": b"{}"}, "jwks_too_large"),
        ({"headers": {"content-encoding": "gzip"}, "body": b"{}"}, "jwks_content_encoded"),
        ({"body": b"not json"}, "jwks_not_json"),
        ({"body": b"\xff\xfe{}"}, "jwks_not_json"),
        ({"body": b"[" * 100000 + b"]" * 100000}, "jwks_too_large"),
        ({"body": b"[1, 2]"}, "jwks_invalid"),
        ({"body": json.dumps(jwks(rsa_private())).encode()}, "jwks_private_key"),
        ({"body": json.dumps(jwks(rsa_with_bits(1024, "w"))).encode()}, "jwks_no_usable_key"),
    ],
)
def test_every_jwks_failure_is_a_closed_jwks_reason(origin, route_kwargs, reason):
    origin.serve(JWKS_URI, **route_kwargs)
    got = refusal(cimd.resolve_jwks(_projection(), ADDRESS))
    assert got.reason == reason
    assert got.host == "chatgpt.com"
    assert origin.requests == [JWKS_URI]  # no redirect followed


def test_a_body_of_exactly_64_kib_is_read(origin):
    body = json.dumps(jwks(rsa_public())).encode()
    body = body + b" " * (65536 - len(body))
    origin.serve(JWKS_URI, body=body)
    assert run(cimd.resolve_jwks(_projection(), ADDRESS)).kids == {"rsa-1"}


def test_a_deeply_nested_body_under_the_cap_is_not_json(origin):
    origin.serve(JWKS_URI, body=b"[" * 30000 + b"]" * 30000)
    assert refusal(cimd.resolve_jwks(_projection(), ADDRESS)).reason in {"jwks_not_json", "jwks_invalid"}


def test_a_slow_jwks_hits_the_deadline(origin, monkeypatch):
    monkeypatch.setattr(cimd, "FETCH_DEADLINE_SECONDS", 0.05)
    origin.serve(JWKS_URI, jwks(rsa_public()), gate=asyncio.Event())
    assert refusal(cimd.resolve_jwks(_projection(), ADDRESS)).reason == "jwks_fetch_timeout"


@pytest.mark.parametrize("address", FORBIDDEN)
def test_a_jwks_host_resolving_to_a_forbidden_address_is_never_connected(origin, address):
    origin.addresses["chatgpt.com"] = [address]
    origin.serve(JWKS_URI, jwks(rsa_public()))
    assert refusal(cimd.resolve_jwks(_projection(), ADDRESS)).reason == "jwks_ssrf_refused"
    assert origin.connections == []


def test_the_proxy_environment_is_ignored_for_the_jwks(origin, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    origin.serve(JWKS_URI, jwks(rsa_public()))
    run(cimd.resolve_jwks(_projection(), ADDRESS))
    assert origin.connections == [(PUBLIC_ADDRESS, "chatgpt.com")]


# ── the cache, the cooldown and the charge ─────────────────────────────────


def test_a_cached_document_does_not_make_the_jwks_free(origin, clock):
    _serve_chatgpt(origin, headers={"cache-control": "no-store"})
    origin.serve(CHATGPT_URL, CHATGPT_DOC)  # the document is cacheable
    run(cimd.resolve(CHATGPT_URL, ADDRESS))
    assert _charges() == 1
    projection = run(cimd.resolve(CHATGPT_URL, ADDRESS))  # a document cache hit
    assert _charges() == 1
    run(cimd.resolve_jwks(projection, ADDRESS))  # uncached JWKS
    assert _charges() == 2
    run(cimd.resolve_jwks(projection, ADDRESS))  # no-store: fetched and charged again
    assert _charges() == 3
    assert origin.requests.count(JWKS_URI) == 2


def test_a_jwks_cache_hit_charges_nothing(origin, clock):
    _serve_chatgpt(origin)
    run(cimd.resolve_jwks(_projection(), ADDRESS))
    for _ in range(5):
        run(cimd.resolve_jwks(_projection(), ADDRESS))
    assert _charges() == 1
    assert origin.requests.count(JWKS_URI) == 1


def test_the_jwks_charge_meets_the_shared_per_address_budget(origin, clock, monkeypatch):
    monkeypatch.setattr(settings, "oauth_cimd_fetches_per_address_per_minute", 1)
    _serve_chatgpt(origin)
    projection = run(cimd.resolve(CHATGPT_URL, ADDRESS))  # spends the only unit
    assert refusal(cimd.resolve_jwks(projection, ADDRESS)).reason == "fetch_budget_exhausted"
    assert origin.requests.count(JWKS_URI) == 0
    clock.now += 61
    assert run(cimd.resolve_jwks(projection, ADDRESS)).kids == {"rsa-1"}


def test_a_joined_jwks_flight_charges_nothing(origin, monkeypatch):
    monkeypatch.setattr(cimd, "GLOBAL_IN_FLIGHT", 1)
    gate = asyncio.Event()
    origin.serve(JWKS_URI, jwks(rsa_public()), gate=gate)
    projection = _projection()

    async def scenario():
        first = asyncio.create_task(cimd.resolve_jwks(projection, ADDRESS))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Same slot (its in-flight cap full) and the global cap full: joins.
        second = asyncio.create_task(cimd.resolve_jwks(projection, ADDRESS))
        third = asyncio.create_task(cimd.resolve_jwks(projection, "203.0.113.50"))
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(first, second, third)

    a, b, c = run(scenario())
    assert a is b is c
    assert origin.requests.count(JWKS_URI) == 1
    assert _charges() == 1
    assert _charges("203.0.113.50") == 0


def test_a_cache_hit_returns_the_original_fetched_at(origin, clock, wall):
    _serve_chatgpt(origin, headers={"cache-control": "max-age=86400"})
    t0 = wall.now
    first = run(cimd.resolve_jwks(_projection(), ADDRESS))
    clock.now += 23 * 3600 + 50 * 60
    wall.now = t0 + timedelta(hours=23, minutes=50)
    hit = run(cimd.resolve_jwks(_projection(), ADDRESS))
    assert hit.fetched_at == t0 == first.fetched_at
    assert cimd.jwks_cache_get(JWKS_URI).fetched_at == t0
    assert origin.requests.count(JWKS_URI) == 1


@pytest.mark.parametrize(
    "header, ttl",
    [("max-age=604800", 86400), ("max-age=60", 60), (None, 300), ("no-store", 0), ("no-cache", 0)],
)
def test_the_jwks_ttl_follows_the_document_rules(origin, clock, header, ttl):
    origin.serve(JWKS_URI, jwks(rsa_public()), headers={"cache-control": header} if header else {})
    run(cimd.resolve_jwks(_projection(), ADDRESS))
    if ttl:
        clock.now += ttl - 1
        run(cimd.resolve_jwks(_projection(), ADDRESS))
        assert origin.requests.count(JWKS_URI) == 1
        clock.now += 2
    run(cimd.resolve_jwks(_projection(), ADDRESS))
    assert origin.requests.count(JWKS_URI) == 2


def test_only_validated_sets_are_cached(origin, clock):
    origin.serve(JWKS_URI, jwks(rsa_private()))
    refusal(cimd.resolve_jwks(_projection(), ADDRESS))
    assert cimd.jwks_cache_get(JWKS_URI) is None
    with pytest.raises(TypeError):
        cimd.jwks_cache_put(JWKS_URI, {"keys": [rsa_public()]}, None)  # type: ignore[arg-type]


def test_the_jwks_cache_is_bounded(origin):
    snapshot = snapshot_of(rsa_public())
    for i in range(cimd.JWKS_CACHE_ENTRIES + 5):
        cimd.jwks_cache_put(f"https://e{i}.example.org/jwks", snapshot, None)
    assert len(cimd._jwks_cache) == cimd.JWKS_CACHE_ENTRIES
    assert cimd.jwks_cache_get("https://e0.example.org/jwks") is None


def test_a_failing_jwks_is_fetched_once_per_cooldown(origin, clock):
    origin.serve(JWKS_URI, status=503)
    refusal(cimd.resolve_jwks(_projection(), ADDRESS))
    for i in range(20):
        assert refusal(cimd.resolve_jwks(_projection(), f"203.0.113.{i}")).reason == "fetch_cooldown"
    assert origin.requests.count(JWKS_URI) == 1
    clock.now += 31
    refusal(cimd.resolve_jwks(_projection(), ADDRESS))
    assert origin.requests.count(JWKS_URI) == 2


def test_a_jwks_failure_does_not_arm_the_document_cooldown(origin, clock):
    origin.serve(CHATGPT_URL, CHATGPT_DOC, headers={"cache-control": "no-store"})
    origin.serve(JWKS_URI, status=500)
    projection = run(cimd.resolve(CHATGPT_URL, ADDRESS))
    refusal(cimd.resolve_jwks(projection, ADDRESS))
    run(cimd.resolve(CHATGPT_URL, ADDRESS))  # still fetchable
    assert origin.requests.count(CHATGPT_URL) == 2


def test_a_document_failure_does_not_arm_the_jwks_cooldown(origin, clock):
    origin.serve(CHATGPT_URL, status=500)
    origin.serve(JWKS_URI, jwks(rsa_public()))
    refusal(cimd.resolve(CHATGPT_URL, ADDRESS))
    assert run(cimd.resolve_jwks(_projection(), ADDRESS)).kids == {"rsa-1"}


# ── fetch_jwks_guarded (the pre-phase refetch) ─────────────────────────────


def test_the_refetch_is_uncharged_and_fills_the_cache(origin, wall):
    origin.serve(JWKS_URI, jwks(rsa_public(kid="new")))
    snapshot = run(cimd.fetch_jwks_guarded(JWKS_URI))
    assert snapshot.kids == {"new"} and snapshot.fetched_at == wall.now
    assert all(slot is None for slot in cimd._address_table)
    assert cimd.jwks_cache_get(JWKS_URI) is snapshot


def test_the_refetch_has_no_failure_cooldown_of_its_own(origin):
    origin.serve(JWKS_URI, status=500)
    for _ in range(3):
        assert refusal(cimd.fetch_jwks_guarded(JWKS_URI)).reason == "jwks_http_status"
    assert origin.requests.count(JWKS_URI) == 3


def test_the_refetch_validates_like_authorization(origin):
    origin.serve(JWKS_URI, jwks(rsa_private()))
    assert refusal(cimd.fetch_jwks_guarded(JWKS_URI)).reason == "jwks_private_key"
    assert cimd.jwks_cache_get(JWKS_URI) is None


@pytest.mark.parametrize("address", ["10.0.0.5", "169.254.169.254", "::1"])
def test_the_refetch_keeps_the_ssrf_deny_list(origin, address):
    origin.addresses["chatgpt.com"] = [address]
    origin.serve(JWKS_URI, jwks(rsa_public()))
    assert refusal(cimd.fetch_jwks_guarded(JWKS_URI)).reason == "jwks_ssrf_refused"
    assert origin.connections == []


@pytest.mark.parametrize("uri", ["https://chatgpt.com/k?x=1", "http://chatgpt.com/k", "https://10.0.0.5/k"])
def test_the_refetch_refuses_a_bad_uri_before_any_connection(origin, uri):
    reason = refusal(cimd.fetch_jwks_guarded(uri)).reason
    # A literal private address passes the URL syntax and is refused by the
    # guarded fetch's deny list, still before any connection.
    assert reason in {"jwks_uri_invalid", "jwks_ssrf_refused"}
    assert origin.resolutions == [] and origin.connections == []


def test_the_refetch_observes_the_global_cap_and_joins(origin, monkeypatch):
    monkeypatch.setattr(cimd, "GLOBAL_IN_FLIGHT", 1)
    gate = asyncio.Event()
    origin.serve(JWKS_URI, jwks(rsa_public()), gate=gate)
    other = "https://other.example.org/jwks.json"
    origin.serve(other, jwks(rsa_public()))

    async def scenario():
        first = asyncio.create_task(cimd.fetch_jwks_guarded(JWKS_URI))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        joined = asyncio.create_task(cimd.fetch_jwks_guarded(JWKS_URI))
        busy = asyncio.create_task(cimd.fetch_jwks_guarded(other))
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(first, joined, busy, return_exceptions=True)

    a, b, c = run(scenario())
    assert a is b
    assert isinstance(c, cimd.CimdRefusal) and c.reason == "fetch_busy"
    assert origin.requests.count(JWKS_URI) == 1 and origin.requests.count(other) == 0
    assert cimd._global_inflight == 0


def test_an_authorization_joins_an_in_progress_refetch_uncharged(origin):
    gate = asyncio.Event()
    origin.serve(JWKS_URI, jwks(rsa_public()), gate=gate)

    async def scenario():
        refetch = asyncio.create_task(cimd.fetch_jwks_guarded(JWKS_URI))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        consent = asyncio.create_task(cimd.resolve_jwks(_projection(), ADDRESS))
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(refetch, consent)

    a, b = run(scenario())
    assert a is b
    assert _charges() == 0
    assert origin.requests.count(JWKS_URI) == 1


def test_the_in_flight_counters_return_to_zero(origin):
    origin.serve(JWKS_URI, status=500)
    refusal(cimd.resolve_jwks(_projection(), ADDRESS))
    refusal(cimd.fetch_jwks_guarded(JWKS_URI))
    assert cimd._global_inflight == 0
    assert cimd._address_table[cimd._slot_for(ADDRESS)].inflight == 0
    assert cimd._jwks_flights == {}


def test_reset_clears_the_jwks_state(origin):
    cimd.jwks_cache_put(JWKS_URI, snapshot_of(rsa_public()), None)
    cimd.reset_state_for_tests()
    assert cimd.jwks_cache_get(JWKS_URI) is None


def test_every_new_reason_is_closed():
    for reason in (
        "jwks_uri_invalid", "jwks_uri_cross_origin", "jwks_inline_refused",
        "signing_alg_unsupported", "auth_method_downgrade", "jwks_fetch_failed",
        "jwks_fetch_timeout", "jwks_http_status", "jwks_too_large",
        "jwks_content_encoded", "jwks_ssrf_refused", "jwks_not_json",
        "jwks_invalid", "jwks_private_key", "jwks_no_usable_key",
    ):
        assert reason in cimd.REASONS
    _ = Clock  # imported for parity with the document tests' clock fixture
