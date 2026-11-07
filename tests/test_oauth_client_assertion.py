"""`private_key_jwt` client assertions for CIMD clients (#420): the offline half.

* `authenticate` — design D6 steps 1–9, every sub-reason, the forbidden
  headers, alg pinning and key/alg fit, the `aud` forms per endpoint, the
  zero-leeway `exp` and the lifetime/skew bounds, the `jti` bounds;
* the replay cache — per client URL (not per row), refusing when full, never
  recording an assertion that failed an earlier step;
* `parse_unverified` — bounded and never raising;
* `prephase` — D7: credential eligibility (the advisory mirror of the main
  phase's checks), both triggers sharing one refetch, the JWKS cache with its
  24 h rule, the 60 s per-`jwks_uri` cooldown, the in-flight join, the global
  cap, the compare-and-set write, no session open across the hop, one record
  per actual attempt, and `OAUTH_CIMD_ENABLED` ignored;
* no log record carries an assertion, `jti`, `kid`, key material or URL.

Slice J's `cimd.fetch_jwks_guarded` / `cimd.jwks_cache_get` / `jwks_cache_put`
and `JwksSnapshot`, and Slice S's `OAuthClient` columns, do not exist on this
slice's base; they are faked here. The route wiring and the real-Postgres
no-lock proof are Slice R's and §6's.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from joserfc import jwk, jwt
from sqlalchemy.dialects import postgresql

from src.config import settings
from src.oauth import cimd
from src.oauth import client_assertion as ca
from src.oauth import routes
from src.oauth.grants import grant_lifetime
from src.services import security_events

ISSUER = "https://mcp.example"
URL = "https://chatgpt.com/oauth/client.json"
JWKS_URI = "https://chatgpt.com/oauth/jwks.json"
OTHER_URL = "https://other.example/oauth/client.json"
NOW = 1_800_000_000
NOW_DT = datetime.fromtimestamp(NOW, tz=timezone.utc)
TYPE = ca.ASSERTION_TYPE

RSA_A = jwk.RSAKey.generate_key(2048, parameters={"kid": "rsa-a"})
RSA_B = jwk.RSAKey.generate_key(2048, parameters={"kid": "rsa-b"})
EC_A = jwk.ECKey.generate_key("P-256", parameters={"kid": "ec-a"})
ATTACKER = jwk.RSAKey.generate_key(2048, parameters={"kid": "rsa-a"})


def pub(key, **extra) -> dict:
    data = dict(key.as_dict(private=False))
    data.update(extra)
    return data


SNAPSHOT = {"keys": [pub(RSA_A, alg="RS256"), pub(EC_A)]}


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    ca.reset_state_for_tests()
    monkeypatch.setattr(settings, "base_url", ISSUER)
    yield
    ca.reset_state_for_tests()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def claims(**overrides) -> dict:
    base = {
        "iss": URL,
        "sub": URL,
        "aud": f"{ISSUER}/token",
        "exp": NOW + 60,
        "iat": NOW,
        "jti": "jti-1",
    }
    for name, value in overrides.items():
        if value is _DROP:
            base.pop(name, None)
        else:
            base[name] = value
    return base


_DROP = object()


def sign(key=RSA_A, alg="RS256", header=None, **claim_overrides) -> str:
    head = {"alg": alg, "kid": key.kid} if header is None else header
    return jwt.encode(head, claims(**claim_overrides), key, algorithms=[alg])


def forge(header: dict, payload: dict | None = None, signature: str = "AAAA") -> str:
    """An unsigned-but-well-formed compact JWS, for checks before verification."""
    payload = claims() if payload is None else payload
    return ".".join(
        (b64(json.dumps(header).encode()), b64(json.dumps(payload).encode()), signature)
    )


def form(assertion, **extra) -> dict:
    data = {"client_assertion_type": TYPE, "client_assertion": assertion}
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


def auth(f, *, snapshot=SNAPSHOT, pinned_alg=None, endpoint="token", now=NOW,
         metadata_url=URL):
    return ca.authenticate(
        f, metadata_url=metadata_url, snapshot=snapshot, pinned_alg=pinned_alg,
        endpoint=endpoint, now=now,
    )


def refused(sub, f, **kwargs):
    with pytest.raises(ca.AssertionRefusal) as info:
        auth(f, **kwargs)
    assert info.value.sub == sub
    assert str(info.value) == sub
    return info.value


# ── authenticate: the happy paths ───────────────────────────────────────────


def test_valid_rs256_assertion_authenticates_and_is_recorded():
    auth(form(sign()))
    assert ca.replay_cache_size() == 1


def test_valid_es256_and_ps256_authenticate():
    auth(form(sign(EC_A, "ES256", jti="e")))
    snapshot = {"keys": [pub(RSA_B)]}
    auth(form(sign(RSA_B, "PS256", jti="p")), snapshot=snapshot)


def test_now_may_be_a_datetime():
    auth(form(sign()), now=NOW_DT)


def test_kid_absent_with_one_compatible_key_selects_it():
    token = sign(header={"alg": "RS256"})
    auth(form(token))  # SNAPSHOT has one RSA key and one EC key


def test_kid_absent_with_two_compatible_keys_is_ambiguous():
    snapshot = {"keys": [pub(RSA_A), pub(RSA_B)]}
    refused("assertion_key_ambiguous", form(sign(header={"alg": "RS256"})), snapshot=snapshot)


def test_kid_absent_with_no_compatible_key_is_ambiguous():
    snapshot = {"keys": [pub(EC_A)]}
    refused("assertion_key_ambiguous", form(sign(header={"alg": "RS256"})), snapshot=snapshot)


def test_snapshot_object_with_as_json_is_accepted():
    snap = FakeSnapshot(SNAPSHOT["keys"], NOW_DT)
    auth(form(sign()), snapshot=snap)


# ── authenticate: every sub-reason ──────────────────────────────────────────


def test_missing_assertion():
    refused("assertion_missing", {"client_assertion_type": TYPE})
    refused("assertion_missing", {"client_secret": "s"})


def test_secret_presented_with_or_without_assertion():
    refused("secret_presented", form(sign(), client_secret="s"))


@pytest.mark.parametrize("value", [None, "", "urn:ietf:params:oauth:client-assertion-type:jwt-bearer ",
                                   "urn:ietf:params:oauth:client-assertion-type:saml2-bearer"])
def test_assertion_type_must_be_exact(value):
    f = form(sign())
    if value is None:
        f.pop("client_assertion_type")
    else:
        f["client_assertion_type"] = value
    refused("assertion_type_invalid", f)


@pytest.mark.parametrize("assertion", [
    "a.b",                                   # two segments
    "a.b.c.d.e",                             # JWE shape
    "{}",                                    # JSON serialisation
    "é" + "a.b.c",                           # non-ASCII
    "x" * (ca.ASSERTION_MAX_BYTES + 1),      # oversized
    b64(b"[1]") + ".e30.AAAA",               # header not an object
    b64(b"not json") + ".e30.AAAA",
    "a!b.e30.AAAA",                          # not base64url
    b64(b'{"alg":"RS256","alg":"none"}') + ".e30.AAAA",  # duplicate member
    "..",
])
def test_malformed(assertion):
    refused("assertion_malformed", form(assertion))


def test_oversized_but_otherwise_valid_assertion_is_malformed():
    token = sign(pad="x" * ca.ASSERTION_MAX_BYTES)
    refused("assertion_malformed", form(token))


@pytest.mark.parametrize("member", sorted(ca.FORBIDDEN_HEADERS))
def test_each_forbidden_header_is_refused(member):
    header = {"alg": "RS256", "kid": "rsa-a", member: ["x"] if member == "crit" else "x"}
    refused("assertion_header_invalid", form(forge(header)))
    assert ca.replay_cache_size() == 0


def test_jwk_header_with_attacker_key_is_never_a_key_source():
    header = {"alg": "RS256", "kid": "rsa-a", "jwk": pub(ATTACKER)}
    token = jwt.encode(header, claims(), ATTACKER, algorithms=["RS256"])
    refused("assertion_header_invalid", form(token))


def test_attacker_key_under_a_known_kid_fails_the_signature():
    token = jwt.encode({"alg": "RS256", "kid": "rsa-a"}, claims(), ATTACKER, algorithms=["RS256"])
    refused("assertion_signature_invalid", form(token))
    assert ca.replay_cache_size() == 0


@pytest.mark.parametrize("kid", [7, "", "k" * (ca.KID_MAX_CHARS + 1)])
def test_bad_kid_header(kid):
    refused("assertion_header_invalid", form(forge({"alg": "RS256", "kid": kid})))


@pytest.mark.parametrize("alg", ["none", "HS256", "RS384", "EdDSA", "ES384", None, 1])
def test_alg_outside_allow_list(alg):
    header = {"kid": "rsa-a"} if alg is None else {"alg": alg, "kid": "rsa-a"}
    refused("assertion_alg_invalid", form(forge(header)))


def test_hs256_keyed_by_the_rsa_public_key_is_refused():
    # The classic confusion: HMAC "signed" with the public modulus as secret.
    secret = jwk.OctKey.import_key(RSA_A.as_dict(private=False)["n"].encode())
    token = jwt.encode({"alg": "HS256", "kid": "rsa-a"}, claims(), secret, algorithms=["HS256"])
    refused("assertion_alg_invalid", form(token))


def test_alg_pinned_versus_unpinned():
    snapshot = {"keys": [pub(RSA_A)]}  # no key alg member
    ps = sign(RSA_A, "PS256")
    auth(form(ps), snapshot=snapshot)  # unpinned: any allowed alg
    refused("assertion_alg_invalid", form(sign(RSA_A, "PS256", jti="2")),
            snapshot=snapshot, pinned_alg="RS256")
    auth(form(sign(RSA_A, "RS256", jti="3")), snapshot=snapshot, pinned_alg="RS256")


def test_key_alg_member_mismatch_is_refused():
    # SNAPSHOT's rsa-a carries alg RS256; a PS256 assertion under it is refused.
    refused("assertion_alg_invalid", form(sign(RSA_A, "PS256")))


def test_rsa_key_with_es256_is_refused():
    token = forge({"alg": "ES256", "kid": "rsa-a"})
    refused("assertion_alg_invalid", form(token))


def test_ec_key_with_rs256_is_refused():
    token = forge({"alg": "RS256", "kid": "ec-a"})
    refused("assertion_alg_invalid", form(token))


def test_ec_key_on_another_curve_does_not_fit():
    p384 = jwk.ECKey.generate_key("P-384", parameters={"kid": "p384"})
    snapshot = {"keys": [pub(p384)]}
    refused("assertion_alg_invalid", form(forge({"alg": "ES256", "kid": "p384"})), snapshot=snapshot)


def test_kid_unknown():
    refused("assertion_kid_unknown", form(sign(RSA_B)))


def test_empty_or_missing_snapshot_is_kid_unknown():
    refused("assertion_kid_unknown", form(sign()), snapshot=None)
    refused("assertion_kid_unknown", form(sign()), snapshot={"keys": "nope"})


def test_bad_signature():
    token = sign()
    head, payload, sig = token.split(".")
    tampered = ".".join((head, b64(json.dumps(claims(jti="other")).encode()), sig))
    refused("assertion_signature_invalid", form(tampered))


@pytest.mark.parametrize("override", [
    {"iss": OTHER_URL}, {"sub": OTHER_URL}, {"iss": _DROP}, {"sub": _DROP},
    {"iss": [URL]}, {"sub": URL + "/"},
])
def test_iss_and_sub_must_equal_the_row_url(override):
    refused("assertion_claims_invalid", form(sign(**override)))


def test_assertion_for_another_client_is_refused():
    # Validly signed by "another client" whose key happens to be in the
    # snapshot, but naming its own URL: verification is against this row.
    refused("assertion_claims_invalid", form(sign(iss=OTHER_URL, sub=OTHER_URL)))


def test_submitted_client_id_must_match_when_present():
    refused("assertion_claims_invalid", form(sign(), client_id=OTHER_URL))
    auth(form(sign(jti="ok"), client_id=URL))


@pytest.mark.parametrize("endpoint,aud,ok", [
    ("token", ISSUER, True),
    ("token", f"{ISSUER}/token", True),
    ("token", f"{ISSUER}/revoke", False),
    ("revoke", f"{ISSUER}/revoke", True),
    ("revoke", ISSUER, True),
    ("revoke", f"{ISSUER}/token", True),
    ("token", [f"{ISSUER}/token"], True),
    ("token", [f"{ISSUER}/token", ISSUER], False),
    ("token", [ISSUER, "https://other.example"], False),
    ("token", [], False),
    ("token", f"{ISSUER}/", False),
    ("token", f"{ISSUER}/token/", False),
    ("token", "https://MCP.example", False),
    ("token", _DROP, False),
    ("token", 5, False),
])
def test_audience_forms(endpoint, aud, ok):
    f = form(sign(aud=aud))
    if ok:
        auth(f, endpoint=endpoint)
    else:
        refused("assertion_audience_invalid", f, endpoint=endpoint)


def test_unknown_endpoint_is_a_programming_error():
    with pytest.raises(ValueError):
        auth(form(sign()), endpoint="authorize")


def test_exp_equal_to_now_is_expired():
    refused("assertion_expired", form(sign(exp=NOW, iat=NOW - 10)))


def test_exp_in_the_past_is_expired():
    refused("assertion_expired", form(sign(exp=NOW - 1, iat=NOW - 10)))


def test_exp_one_second_ahead_is_accepted():
    auth(form(sign(exp=NOW + 1, iat=NOW)))


def test_exp_331_ahead_is_refused_and_330_accepted():
    refused("assertion_lifetime_exceeded", form(sign(exp=NOW + 331, iat=_DROP)))
    auth(form(sign(exp=NOW + 330, iat=_DROP, jti="b")))


def test_exp_600_ahead_is_refused():
    refused("assertion_lifetime_exceeded", form(sign(exp=NOW + 600, iat=_DROP)))


def test_iat_31_ahead_is_not_yet_valid_and_30_accepted():
    refused("assertion_not_yet_valid", form(sign(iat=NOW + 31, exp=NOW + 100)))
    auth(form(sign(iat=NOW + 30, exp=NOW + 100, jti="b")))


def test_exp_minus_iat_301_is_refused_and_300_accepted():
    refused("assertion_lifetime_exceeded", form(sign(iat=NOW - 1, exp=NOW + 300)))
    auth(form(sign(iat=NOW, exp=NOW + 300, jti="b")))


def test_nbf_skew():
    refused("assertion_not_yet_valid", form(sign(nbf=NOW + 31)))
    auth(form(sign(nbf=NOW + 30, jti="b")))


@pytest.mark.parametrize("override", [
    {"exp": _DROP}, {"exp": "1800000060"}, {"exp": True}, {"exp": None},
    {"iat": "now"}, {"nbf": [1]},
])
def test_non_numeric_or_missing_time_claims(override):
    refused("assertion_claims_invalid", form(sign(**override)))


@pytest.mark.parametrize("claim", ["exp", "iat", "nbf"])
@pytest.mark.parametrize("value", [
    float("nan"), float("inf"), float("-inf"), 10**400, -(10**400),
], ids=["nan", "+inf", "-inf", "huge-int", "-huge-int"])
def test_non_finite_numeric_dates_are_malformed(claim, value):
    """#420 review F2: NaN passes every comparison, inf/huge ints break the
    arithmetic; none is a NumericDate, and nothing is recorded."""
    refused("assertion_claims_invalid", form(sign(**{claim: value})))
    assert ca.replay_cache_size() == 0


@pytest.mark.parametrize("claim, value, sub", [
    ("exp", 1e308, "assertion_lifetime_exceeded"),
    ("exp", -1e308, "assertion_expired"),
    ("iat", 1e308, "assertion_not_yet_valid"),
    ("iat", -1e308, "assertion_lifetime_exceeded"),
    ("nbf", 1e308, "assertion_not_yet_valid"),
])
def test_huge_finite_numeric_dates_do_not_overflow(claim, value, sub):
    refused(sub, form(sign(**{claim: value})))
    assert ca.replay_cache_size() == 0


def test_huge_negative_nbf_is_accepted():
    auth(form(sign(nbf=-1e308)))


@pytest.mark.parametrize("jti", ["", "j" * 257, 5, None, _DROP])
def test_jti_bounds(jti):
    refused("assertion_claims_invalid", form(sign(jti=jti)))
    assert ca.replay_cache_size() == 0


def test_jti_of_256_characters_is_accepted():
    auth(form(sign(jti="j" * 256)))


def test_claims_that_are_not_an_object():
    token = jwt.encode({"alg": "RS256", "kid": "rsa-a"}, claims(), RSA_A, algorithms=["RS256"])
    # joserfc refuses a non-object payload itself; either way nothing passes.
    head, _, _ = token.split(".")
    with pytest.raises(ca.AssertionRefusal):
        auth(form(".".join((head, b64(b"[1,2]"), "AAAA"))))


def test_row_without_metadata_url_fails_closed():
    refused("assertion_claims_invalid", form(sign()), metadata_url=None)


def test_subs_is_the_closed_d10_set():
    assert ca.SUBS == {
        "assertion_missing", "secret_presented", "assertion_unexpected",
        "assertion_type_invalid", "assertion_malformed", "assertion_header_invalid",
        "assertion_alg_invalid", "assertion_kid_unknown", "assertion_key_ambiguous",
        "assertion_signature_invalid", "assertion_claims_invalid",
        "assertion_audience_invalid", "assertion_expired",
        "assertion_lifetime_exceeded", "assertion_not_yet_valid",
        "assertion_replayed", "assertion_replay_cache_full",
    }
    with pytest.raises(AssertionError):
        ca.AssertionRefusal("something_else")


# ── the replay cache ────────────────────────────────────────────────────────


def test_replayed_assertion_is_refused():
    token = sign()
    auth(form(token))
    refused("assertion_replayed", form(token))


def test_replay_across_two_rows_of_the_same_url():
    # User A's row and user B's row: different snapshots, same client URL.
    token = sign()
    auth(form(token), snapshot={"keys": [pub(RSA_A)]})
    refused("assertion_replayed", form(token), snapshot={"keys": [pub(RSA_A), pub(EC_A)]})


def test_same_jti_under_another_iss_is_independent():
    snapshot = {"keys": [pub(RSA_A)]}
    auth(form(sign()), snapshot=snapshot)
    auth(form(sign(iss=OTHER_URL, sub=OTHER_URL)), snapshot=snapshot, metadata_url=OTHER_URL)


def test_full_cache_refuses_and_never_evicts(monkeypatch):
    monkeypatch.setattr(ca, "REPLAY_CACHE_ENTRIES", 3)
    tokens = [sign(jti=f"j{i}") for i in range(3)]
    for token in tokens:
        auth(form(token))
    refused("assertion_replay_cache_full", form(sign(jti="new")))
    for token in tokens:  # every recorded jti stays refused
        refused("assertion_replayed", form(token))
    assert ca.replay_cache_size() == 3


def test_entries_expire_at_exp_and_are_purged_before_insert(monkeypatch):
    monkeypatch.setattr(ca, "REPLAY_CACHE_ENTRIES", 1)
    first = sign(exp=NOW + 10, jti="a")
    auth(form(first))
    later = NOW + 10  # entry held until exp; at exp it is gone
    auth(form(sign(exp=later + 60, iat=later, jti="b")), now=later)
    assert ca.replay_cache_size() == 1


def test_failed_assertions_never_occupy_the_cache():
    for i in range(50):
        token = jwt.encode({"alg": "RS256", "kid": "rsa-a"}, claims(jti=f"x{i}"),
                           ATTACKER, algorithms=["RS256"])
        with pytest.raises(ca.AssertionRefusal):
            auth(form(token))
    for override in ({"aud": "https://x"}, {"exp": NOW}, {"iss": OTHER_URL}):
        with pytest.raises(ca.AssertionRefusal):
            auth(form(sign(**override)))
    assert ca.replay_cache_size() == 0


def test_cache_key_is_per_url_and_jti_hash():
    assert ca._replay_key(URL, "j") == hashlib.sha256(f"{URL}\nj".encode()).hexdigest()


# ── parse_unverified ────────────────────────────────────────────────────────


def test_parse_unverified_reads_alg_kid_iss():
    u = ca.parse_unverified(form(sign()))
    assert u == ca.Unverified(alg="RS256", kid="rsa-a", iss=URL)
    assert ca.parse_unverified(form(sign(header={"alg": "ES256"}, key=EC_A, alg="ES256"))).kid is None


@pytest.mark.parametrize("f", [
    {},
    {"client_assertion": "a.b.c"},
    form(None),
    form(123),
    form("x" * (ca.ASSERTION_MAX_BYTES + 1)),
    form(forge({"alg": "none"})),
    form(forge({"alg": "RS256", "jku": "https://evil"})),
    form(forge({"alg": "RS256"}, payload={"iss": 5})),
    form(".".join((b64(b'{"alg":"RS256"}'), b64(b"[]"), "A"))),
    {"client_assertion_type": "wrong", "client_assertion": "a.b.c"},
])
def test_parse_unverified_never_raises(f):
    assert ca.parse_unverified(f) is None


def test_parse_unverified_tolerates_a_hostile_form():
    class Boom:
        def get(self, _):
            raise RuntimeError("x")
    assert ca.parse_unverified(Boom()) is None


# ── prephase fixtures ───────────────────────────────────────────────────────


@dataclass
class FakeSnapshot:
    keys: list
    fetched_at: datetime
    directive: object = None

    def as_json(self) -> dict:
        return {"keys": list(self.keys)}

    @property
    def kids(self) -> frozenset:
        return frozenset(k["kid"] for k in self.keys if "kid" in k)


VERIFIER = "v" * 43
CHALLENGE = routes._base64url_sha256(VERIFIER)
REDIRECT = "https://chatgpt.com/connector/oauth/cb"


def client_row(**overrides):
    base = dict(
        client_id="cimd_" + "a" * 59,
        client_id_metadata_url=URL,
        token_endpoint_auth_method="private_key_jwt",
        jwks_uri=JWKS_URI,
        token_endpoint_auth_signing_alg="RS256",
        jwks_snapshot={"keys": [pub(RSA_A)]},
        jwks_fetched_at=NOW_DT - timedelta(hours=1),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def code_row(**overrides):
    base = dict(client_id="cimd_" + "a" * 59, used=False,
                expires_at=NOW_DT + timedelta(minutes=5),
                redirect_uri=REDIRECT, code_challenge=CHALLENGE)
    base.update(overrides)
    return SimpleNamespace(**base)


def token_row(**overrides):
    base = dict(client_id="cimd_" + "a" * 59, revoked=False,
                expires_at=NOW_DT + timedelta(days=1),
                grant_issued_at=NOW_DT - timedelta(days=1))
    base.update(overrides)
    return SimpleNamespace(**base)


def code_form(assertion, **extra):
    data = {"grant_type": "authorization_code", "code": "the-code",
            "redirect_uri": REDIRECT, "code_verifier": VERIFIER}
    data.update(form(assertion))
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


def refresh_form(assertion, **extra):
    data = {"grant_type": "refresh_token", "refresh_token": "the-rt"}
    data.update(form(assertion))
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


@dataclass
class Harness:
    cred: object
    client: object
    fetches: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    events: list = field(default_factory=list)
    served: object = None
    fail: BaseException | None = None
    gate: asyncio.Event | None = None
    cache: dict = field(default_factory=dict)
    puts: list = field(default_factory=list)


@pytest.fixture
def harness(monkeypatch):
    h = Harness(cred=token_row(), client=client_row())
    h.served = FakeSnapshot([pub(RSA_A), pub(RSA_B)], NOW_DT)

    async def fake_load(kind, credential):
        h.loaded = (kind, credential)
        return h.cred, h.client

    async def fake_fetch(uri):
        h.fetches.append(uri)
        if h.gate is not None:
            await h.gate.wait()
        if h.fail is not None:
            raise h.fail
        return h.served

    async def fake_cas(row, uri, snapshot):
        h.writes.append((row, uri, snapshot))
        return 1

    def fake_emit(event, **fields):
        h.events.append((event, fields))

    monkeypatch.setattr(ca, "_load", fake_load)
    monkeypatch.setattr(ca, "_cas_write", fake_cas)
    monkeypatch.setattr(ca, "_clock", lambda: NOW_DT)
    monkeypatch.setattr(ca.security_events, "emit", fake_emit)
    # TODO(#420): real after merge — Slice J's names, faked by name.
    monkeypatch.setattr(cimd, "fetch_jwks_guarded", fake_fetch, raising=False)
    monkeypatch.setattr(cimd, "jwks_cache_get", lambda uri: h.cache.get(uri), raising=False)
    monkeypatch.setattr(cimd, "jwks_cache_put",
                        lambda uri, snap, directive: h.puts.append((uri, snap)), raising=False)
    monkeypatch.setattr(cimd, "_global_inflight", 0)
    return h


async def run(f, kind="refresh_token", credential="the-rt", route="/token"):
    await ca.prephase(f, credential_kind=kind, credential=credential,
                      route=route, client_ip="203.0.113.9")


def unknown_kid_assertion():
    return sign(RSA_B)  # rsa-b is served but not in the row's snapshot


def stale(h, hours=25):
    h.client.jwks_fetched_at = NOW_DT - timedelta(hours=hours)


# ── prephase: kid miss ──────────────────────────────────────────────────────


async def test_kid_miss_refetches_once_and_writes_the_snapshot(harness):
    await run(refresh_form(unknown_kid_assertion()))
    assert harness.fetches == [JWKS_URI]
    assert harness.writes == [(harness.client.client_id, JWKS_URI, harness.served)]
    # `fetch_jwks_guarded` fills the cache with the origin's own directive;
    # the pre-phase must not put a second time (seam fix, #420 Slice R).
    assert harness.puts == []
    [(event, fields)] = harness.events
    assert event == "oauth_client_jwks_refetched"
    assert fields["trigger"] == "kid_miss" and fields["outcome"] == "updated"
    assert fields["client_id"] == harness.client.client_id
    assert fields["cimd_host"] == "chatgpt.com"
    assert fields["route"] == "/token" and fields["level"] == logging.INFO
    assert fields.get("reason") is None


async def test_kid_still_absent_writes_the_fresh_snapshot(harness):
    harness.served = FakeSnapshot([pub(RSA_A)], NOW_DT)
    await run(refresh_form(unknown_kid_assertion()))
    assert len(harness.writes) == 1
    assert harness.events[0][1]["outcome"] == "kid_still_absent"


async def test_known_kid_and_fresh_snapshot_does_nothing(harness):
    await run(refresh_form(sign(RSA_A)))
    assert harness.fetches == [] and harness.writes == [] and harness.events == []


async def test_kid_miss_needs_a_kid(harness):
    await run(refresh_form(sign(RSA_B, header={"alg": "RS256"})))
    assert harness.fetches == []


async def test_failed_kid_miss_writes_nothing_and_records_failed(harness):
    harness.fail = cimd.CimdRefusal("fetch_timeout")
    await run(refresh_form(unknown_kid_assertion()))
    assert harness.fetches == [JWKS_URI] and harness.writes == []
    [(event, fields)] = harness.events
    assert fields["outcome"] == "failed" and fields["level"] == logging.WARNING
    assert fields["reason"] == "jwks_fetch_failed"  # a non-jwks_ reason is mapped


async def test_failure_reason_keeps_a_jwks_sub():
    assert ca._failure_reason(SimpleNamespace(reason="jwks_private_key")) == "jwks_private_key"
    assert ca._failure_reason(asyncio.TimeoutError()) == "jwks_fetch_timeout"
    assert ca._failure_reason(RuntimeError("x")) == "jwks_fetch_failed"


# ── prephase: stale ─────────────────────────────────────────────────────────


async def test_stale_snapshot_refetches_before_verification(harness):
    stale(harness)
    await run(refresh_form(sign(RSA_A)))
    assert harness.fetches == [JWKS_URI]
    assert harness.writes[0][2] is harness.served
    assert harness.events[0][1]["trigger"] == "stale"
    assert harness.events[0][1]["outcome"] == "updated"


async def test_23h59_is_not_stale(harness):
    stale(harness, hours=23.98)
    await run(refresh_form(sign(RSA_A)))
    assert harness.fetches == []


async def test_stale_with_the_key_removed_writes_the_reduced_set(harness):
    stale(harness)
    harness.served = FakeSnapshot([pub(RSA_B)], NOW_DT)
    await run(refresh_form(sign(RSA_A)))
    assert harness.writes[0][2].kids == {"rsa-b"}
    assert harness.events[0][1]["outcome"] == "kid_still_absent"
    # The main phase then verifies against the written set and refuses.
    refused("assertion_kid_unknown", form(sign(RSA_A)), snapshot=harness.writes[0][2])


@pytest.mark.parametrize("failure", [
    cimd.CimdRefusal("fetch_timeout"), RuntimeError("500"), asyncio.TimeoutError(),
])
async def test_stale_failure_writes_nothing(harness, failure):
    stale(harness)
    harness.fail = failure
    await run(refresh_form(sign(RSA_A)))
    assert harness.writes == [] and harness.puts == []
    [(event, fields)] = harness.events
    assert fields["trigger"] == "stale" and fields["outcome"] == "failed"
    # The main phase verifies against the stored snapshot and succeeds.
    auth(form(sign(RSA_A)), snapshot=harness.client.jwks_snapshot, pinned_alg="RS256")


async def test_failed_stale_refetch_retries_only_after_the_cooldown(harness, monkeypatch):
    stale(harness)
    harness.fail = RuntimeError("down")
    clock = [1000.0]
    monkeypatch.setattr(ca, "_mono", lambda: clock[0])
    await run(refresh_form(sign(RSA_A, jti="1")))
    clock[0] += 59
    await run(refresh_form(sign(RSA_A, jti="2")))
    assert len(harness.fetches) == 1
    assert len(harness.events) == 1  # a cooldown skip is not recorded
    clock[0] += 2
    await run(refresh_form(sign(RSA_A, jti="3")))
    assert len(harness.fetches) == 2


async def test_stale_and_kid_miss_share_one_fetch(harness):
    stale(harness)
    await run(refresh_form(unknown_kid_assertion()))
    assert harness.fetches == [JWKS_URI]
    assert harness.events[0][1]["trigger"] == "kid_miss"


async def test_twenty_requests_in_a_minute_make_one_fetch(harness):
    for i in range(20):
        await run(refresh_form(sign(RSA_B, jti=f"k{i}")))
    assert len(harness.fetches) == 1


async def test_twenty_stale_requests_in_a_minute_make_one_fetch(harness):
    stale(harness)
    for i in range(20):
        await run(refresh_form(sign(RSA_A, jti=f"s{i}")))
    assert len(harness.fetches) == 1


async def test_concurrent_requests_join_one_flight(harness):
    harness.gate = asyncio.Event()
    tasks = [asyncio.create_task(run(refresh_form(sign(RSA_B, jti=f"c{i}")))) for i in range(10)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    harness.gate.set()
    await asyncio.gather(*tasks)
    assert len(harness.fetches) == 1
    assert len(harness.events) == 1
    assert len(harness.writes) == 10  # every joiner writes its own row (CAS)


async def test_global_cap_busy_records_nothing_and_arms_no_cooldown(harness):
    # The cap itself is `cimd.fetch_jwks_guarded`'s (`fetch_busy`).
    harness.fail = cimd.CimdRefusal("fetch_busy")
    await run(refresh_form(unknown_kid_assertion()))
    assert harness.events == [] and harness.writes == []
    # A busy cap is not an attempt: the next request may try at once.
    harness.fail = None
    await run(refresh_form(sign(RSA_B, jti="again")))
    assert len(harness.fetches) == 2
    assert harness.events[0][1]["outcome"] == "updated"


async def test_the_prephase_takes_no_global_slot_of_its_own(harness):
    seen = []

    async def fetch(uri):
        seen.append(cimd._global_inflight)
        return harness.served

    ca_fetch = cimd.fetch_jwks_guarded
    try:
        cimd.fetch_jwks_guarded = fetch
        await run(refresh_form(unknown_kid_assertion()))
    finally:
        cimd.fetch_jwks_guarded = ca_fetch
    assert seen == [0]
    assert cimd._global_inflight == 0


async def test_cancelled_fetch_releases_state(harness):
    harness.gate = asyncio.Event()
    task = asyncio.create_task(run(refresh_form(unknown_kid_assertion())))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cimd._global_inflight == 0 and ca._refetch_flights == {}


# ── prephase: the JWKS cache ────────────────────────────────────────────────


async def test_kid_miss_served_from_the_cache_without_a_fetch(harness):
    hit = FakeSnapshot([pub(RSA_A), pub(RSA_B)], NOW_DT - timedelta(minutes=5))
    harness.cache[JWKS_URI] = hit
    await run(refresh_form(unknown_kid_assertion()))
    assert harness.fetches == [] and harness.events == []
    assert harness.writes == [(harness.client.client_id, JWKS_URI, hit)]


async def test_kid_miss_cache_hit_without_the_kid_fetches(harness):
    harness.cache[JWKS_URI] = FakeSnapshot([pub(RSA_A)], NOW_DT)
    await run(refresh_form(unknown_kid_assertion()))
    assert harness.fetches == [JWKS_URI]


async def test_stale_fresh_cache_hit_is_used_and_carries_its_fetch_time(harness):
    stale(harness)
    hit = FakeSnapshot([pub(RSA_A)], NOW_DT - timedelta(hours=2))
    harness.cache[JWKS_URI] = hit
    await run(refresh_form(sign(RSA_A)))
    assert harness.fetches == []
    assert harness.writes[0][2].fetched_at == NOW_DT - timedelta(hours=2)


async def test_stale_cache_hit_older_than_24h_is_not_used(harness):
    stale(harness, hours=30)
    harness.cache[JWKS_URI] = FakeSnapshot([pub(RSA_A)], NOW_DT - timedelta(hours=25))
    await run(refresh_form(sign(RSA_A)))
    assert harness.fetches == [JWKS_URI]


async def test_stale_cache_hit_not_newer_than_the_row_is_not_used(harness):
    stale(harness, hours=25)
    harness.cache[JWKS_URI] = FakeSnapshot([pub(RSA_A)], NOW_DT - timedelta(hours=26))
    await run(refresh_form(sign(RSA_A)))
    assert harness.fetches == [JWKS_URI]


async def test_approval_from_a_23h50_cache_entry_goes_stale_11_minutes_later(harness):
    # The row stores the entry's origin fetch time, not the approval time.
    harness.client.jwks_fetched_at = NOW_DT - timedelta(hours=23, minutes=50) - timedelta(minutes=11)
    await run(refresh_form(sign(RSA_A)))
    assert harness.fetches == [JWKS_URI]


# ── prephase: the gate and eligibility ──────────────────────────────────────


async def test_no_assertion_no_fetch(harness):
    await run({"grant_type": "refresh_token", "refresh_token": "the-rt"})
    assert harness.fetches == [] and not hasattr(harness, "loaded")


async def test_no_credential_no_fetch(harness):
    await run(refresh_form(unknown_kid_assertion()), credential=None)
    assert harness.fetches == []


async def test_iss_not_the_row_url_no_fetch(harness):
    await run(refresh_form(sign(RSA_B, iss=OTHER_URL, sub=OTHER_URL)))
    assert harness.fetches == []


async def test_pinned_alg_mismatch_no_fetch(harness):
    await run(refresh_form(sign(RSA_B, "PS256")))
    assert harness.fetches == []


async def test_unknown_credential_no_fetch(harness):
    harness.cred = None
    harness.client = None
    await run(refresh_form(unknown_kid_assertion()))
    assert harness.fetches == []


async def test_none_row_no_fetch(harness):
    harness.client = client_row(token_endpoint_auth_method="none", jwks_uri=None)
    await run(refresh_form(unknown_kid_assertion()))
    assert harness.fetches == []


async def test_dcr_row_no_fetch(harness):
    harness.client = client_row(client_id_metadata_url=None)
    await run(refresh_form(unknown_kid_assertion()))
    assert harness.fetches == []


@pytest.mark.parametrize("cred_overrides", [
    {"revoked": True},
    {"expires_at": NOW_DT - timedelta(seconds=1)},
    {"grant_issued_at": NOW_DT - grant_lifetime()},
    {"grant_issued_at": None},
])
async def test_dead_refresh_token_no_fetch(harness, cred_overrides):
    harness.cred = token_row(**cred_overrides)
    await run(refresh_form(unknown_kid_assertion()))
    assert harness.fetches == []


@pytest.mark.parametrize("client_id,ok", [
    (None, True), (URL, True), (OTHER_URL, False),
    ("cimd_" + "a" * 59, False), ("some-dcr-id", False),
])
async def test_client_id_must_match_when_present(harness, client_id, ok):
    await run(refresh_form(unknown_kid_assertion(), client_id=client_id))
    assert len(harness.fetches) == (1 if ok else 0)


@pytest.mark.parametrize("cred_overrides,form_overrides", [
    ({"used": True}, {}),
    ({"expires_at": NOW_DT - timedelta(seconds=1)}, {}),
    ({}, {"redirect_uri": REDIRECT + "x"}),
    ({}, {"redirect_uri": None}),
    ({}, {"code_verifier": "short"}),
    ({}, {"code_verifier": "w" * 43}),
    ({}, {"code_verifier": None}),
    ({"used": True}, {"code_verifier": "w" * 43}),
    ({"expires_at": NOW_DT - timedelta(minutes=1)}, {"code_verifier": "w" * 43}),
])
async def test_dead_or_unproven_code_no_fetch(harness, cred_overrides, form_overrides):
    harness.cred = code_row(**cred_overrides)
    await run(code_form(unknown_kid_assertion(), **form_overrides), kind="code",
              credential="the-code")
    assert harness.fetches == []


async def test_live_code_with_proof_fetches(harness):
    harness.cred = code_row()
    await run(code_form(unknown_kid_assertion()), kind="code", credential="the-code")
    assert harness.fetches == [JWKS_URI]


@pytest.mark.parametrize("cred_overrides,client_id,ok", [
    ({}, None, True),
    ({}, URL, True),
    ({"revoked": True}, None, False),
    ({"expires_at": NOW_DT - timedelta(seconds=1)}, None, False),
    ({}, OTHER_URL, False),
])
async def test_revoke_token_eligibility(harness, cred_overrides, client_id, ok):
    harness.cred = token_row(**cred_overrides)
    f = {"token": "the-at", **form(unknown_kid_assertion())}
    if client_id:
        f["client_id"] = client_id
    await run(f, kind="token", credential="the-at", route="/revoke")
    assert len(harness.fetches) == (1 if ok else 0)
    if ok:
        assert harness.events[0][1]["route"] == "/revoke"


async def test_unknown_kind_fetches_nothing(harness):
    await run(refresh_form(unknown_kid_assertion()), kind="password")
    assert harness.fetches == []


async def test_prephase_ignores_oauth_cimd_enabled(harness, monkeypatch):
    monkeypatch.setattr(settings, "oauth_cimd_enabled", False)
    await run(refresh_form(unknown_kid_assertion()))
    stale(harness)
    harness.client.jwks_snapshot = {"keys": [pub(RSA_A), pub(RSA_B)]}
    ca.reset_state_for_tests()
    await run(refresh_form(sign(RSA_A, jti="s")))
    assert len(harness.fetches) == 2


async def test_prephase_never_raises(harness, monkeypatch):
    async def broken(kind, credential):
        raise RuntimeError("db down")
    monkeypatch.setattr(ca, "_load", broken)
    await run(refresh_form(unknown_kid_assertion()))  # no raise


async def test_prephase_never_touches_the_replay_cache(harness):
    await run(refresh_form(unknown_kid_assertion()))
    assert ca.replay_cache_size() == 0


async def test_prephase_never_fetches_the_document(harness, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("document fetched")
    monkeypatch.setattr(cimd, "resolve", boom)
    monkeypatch.setattr(cimd, "_fetch", boom)
    stale(harness)
    await run(refresh_form(unknown_kid_assertion()))
    assert harness.fetches == [JWKS_URI] and len(harness.writes) == 1


# ── the eligibility predicate against the real helpers ─────────────────────


def test_eligibility_uses_the_routes_predicates():
    client = client_row()
    ok = {"redirect_uri": REDIRECT, "code_verifier": VERIFIER}
    assert ca._eligible("code", code_row(), client, ok, NOW_DT)
    assert not ca._eligible("code", code_row(code_challenge="x" * 43), client, ok, NOW_DT)
    assert ca._eligible("refresh_token", token_row(), client, {}, NOW_DT)
    naive = token_row(expires_at=(NOW_DT + timedelta(days=1)).replace(tzinfo=None))
    assert ca._eligible("token", naive, client, {}, NOW_DT)
    assert not ca._eligible("token", None, client, {}, NOW_DT)


# ── no session across the hop; the queries; the CAS ────────────────────────


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def first(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class FakeSessions:
    def __init__(self, results):
        self.results = list(results)
        self.open = 0
        self.statements = []
        self.commits = 0

    def __call__(self):
        return self

    async def __aenter__(self):
        self.open += 1
        return self

    async def __aexit__(self, *exc):
        self.open -= 1
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        if self.results:
            return FakeResult(self.results.pop(0))
        return SimpleNamespace(rowcount=1)

    async def commit(self):
        self.commits += 1


async def test_no_session_is_open_during_the_fetch(monkeypatch):
    client = client_row()
    sessions = FakeSessions([token_row(), client])
    open_during_fetch = []
    served = FakeSnapshot([pub(RSA_A), pub(RSA_B)], NOW_DT)

    async def fetch(uri):
        open_during_fetch.append(sessions.open)
        return served

    monkeypatch.setattr(ca, "async_session", sessions)
    monkeypatch.setattr(ca, "_clock", lambda: NOW_DT)
    monkeypatch.setattr(ca.security_events, "emit", lambda *a, **k: None)
    monkeypatch.setattr(cimd, "fetch_jwks_guarded", fetch, raising=False)
    monkeypatch.setattr(cimd, "jwks_cache_get", lambda uri: None, raising=False)
    monkeypatch.setattr(cimd, "jwks_cache_put", lambda *a: None, raising=False)
    await run(refresh_form(unknown_kid_assertion()))
    assert open_during_fetch == [0]
    assert sessions.open == 0
    select_token, select_client, update = (
        str(s.compile(dialect=postgresql.dialect())) for s in sessions.statements
    )
    assert "oauth_tokens.token_hash" in select_token and "token_type" in select_token
    assert "FOR UPDATE" not in select_token
    assert "oauth_clients.client_id" in select_client
    assert update.startswith("UPDATE oauth_clients")
    assert sessions.commits == 1


def test_cas_statement_conditions_on_uri_and_older_fetch_time():
    snap = FakeSnapshot([pub(RSA_A)], NOW_DT)
    stmt = ca._cas_statement("cimd_x", JWKS_URI, snap)
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "oauth_clients.client_id = %(client_id_1)s" in sql
    assert "oauth_clients.jwks_uri = %(jwks_uri_1)s" in sql
    assert "oauth_clients.jwks_fetched_at < %(jwks_fetched_at_1)s" in sql
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert params["jwks_fetched_at"] == NOW_DT and params["jwks_fetched_at_1"] == NOW_DT
    assert params["jwks_snapshot"] == {"keys": [pub(RSA_A)]}


async def test_code_lookup_is_by_hash_without_lock(monkeypatch):
    sessions = FakeSessions([None])
    monkeypatch.setattr(ca, "async_session", sessions)
    assert await ca._load("code", "the-code") == (None, None)
    sql = str(sessions.statements[0].compile(dialect=postgresql.dialect()))
    assert "oauth_codes.code_hash" in sql and "FOR UPDATE" not in sql
    assert sessions.statements[0].compile().params["code_hash_1"] == routes._hash("the-code")


# ── nothing secret in any log record ────────────────────────────────────────


async def test_no_log_record_carries_assertion_material(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    client = client_row()
    served = FakeSnapshot([pub(RSA_A), pub(RSA_B)], NOW_DT)
    sessions = FakeSessions([token_row(), client])

    async def fetch(uri):
        return served

    monkeypatch.setattr(ca, "async_session", sessions)
    monkeypatch.setattr(ca, "_clock", lambda: NOW_DT)
    monkeypatch.setattr(cimd, "fetch_jwks_guarded", fetch, raising=False)
    monkeypatch.setattr(cimd, "jwks_cache_get", lambda uri: None, raising=False)
    monkeypatch.setattr(cimd, "jwks_cache_put", lambda *a: None, raising=False)
    captured = []
    real_emit = security_events.emit

    def spy(event, **fields):
        captured.append((event, fields))
        real_emit(event, **fields)

    monkeypatch.setattr(ca.security_events, "emit", spy)
    token = sign(RSA_B, jti="secret-jti-value")
    with security_events.suppression_disabled():
        await run(refresh_form(token))
        snapshot = {"keys": [pub(RSA_A), pub(RSA_B)]}
        auth(form(token), snapshot=snapshot, now=NOW)
        for bad in (sign(RSA_B, jti="secret-jti-value"), forge({"alg": "RS256", "jku": JWKS_URI})):
            with pytest.raises(ca.AssertionRefusal):
                auth(form(bad), snapshot=snapshot)
        stale_client = client_row(jwks_fetched_at=NOW_DT - timedelta(hours=30))
        sessions.results = [token_row(), stale_client]
        monkeypatch.setattr(cimd, "fetch_jwks_guarded",
                            lambda uri: (_ for _ in ()).throw(RuntimeError(JWKS_URI)), raising=False)
        ca.reset_state_for_tests()
        await run(refresh_form(sign(RSA_A, jti="other-secret")))
    assert len(captured) == 2
    forbidden = [token, "secret-jti-value", "other-secret", "rsa-b", "rsa-a", JWKS_URI, URL,
                 pub(RSA_A)["n"][:32], pub(RSA_B)["n"][:32]]
    for event, fields in captured:
        blob = json.dumps(fields, default=str)
        for value in forbidden:
            assert value not in blob, (event, value)
    for record in caplog.records:
        text = record.getMessage() + json.dumps(record.__dict__, default=str)
        for value in forbidden:
            assert value not in text, value
