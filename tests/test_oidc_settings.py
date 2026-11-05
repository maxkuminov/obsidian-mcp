"""OIDC settings validation (#416 slice B, design D3, D6, D7).

Every setting's validation table, the retired subject map's exact refusal
(OIDC on and off, by keyword and from the environment), the `OIDC_CA_FILE`
trust anchor, and that constructing the settings dials nothing.
"""
import datetime
import os
import socket
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from src.config import OIDC_SUBJECT_USER_IDS_RETIRED, Settings

ISSUER = "https://identity.example.test/realms/home"
BASE = dict(
    _env_file=None, secret_key="test-only-strong-secret-not-a-placeholder",
    multi_user_mode=True, oidc_enabled=True, oidc_issuer=ISSUER,
    oidc_client_id="obsidian", oidc_client_secret="test",
    base_url="https://notes.example.test",
)


def build(**overrides) -> Settings:
    return Settings(**(BASE | overrides))


def refused(**overrides) -> str:
    with pytest.raises(ValidationError) as error:
        build(**overrides)
    return str(error.value)


# ── the retired subject map (D3) ────────────────────────────────────────────


def test_retired_message_is_the_d3_text():
    assert OIDC_SUBJECT_USER_IDS_RETIRED == (
        "OIDC_SUBJECT_USER_IDS is retired (#416). Upgrade: (1) confirm a local admin "
        "password works; (2) set LOCAL_LOGIN_ENABLED=true; (3) remove "
        "OIDC_SUBJECT_USER_IDS; (4) start; (5) bind each former subject on its user's "
        "edit page (Single sign-on → Bind subject); (6) verify SSO and /authorize; "
        "(7) optionally set LOCAL_LOGIN_ENABLED=false. See docs/native-oidc.md, "
        "\"Upgrading from OIDC_SUBJECT_USER_IDS\"."
    )


@pytest.mark.parametrize("oidc_on", [True, False])
@pytest.mark.parametrize("value", [{"sub": 7}, {"sub": True}, {"": 0}, ["x"], "x"])
def test_retired_map_refused_with_oidc_on_or_off(oidc_on, value):
    message = refused(oidc_enabled=oidc_on, oidc_subject_user_ids=value)
    assert OIDC_SUBJECT_USER_IDS_RETIRED in message
    assert "sub" not in message.replace("subject", "")  # input hidden (hide_input_in_errors)


@pytest.mark.parametrize("raw", ['{"immutable-subject":1}', '["x"]'])
def test_retired_map_refused_from_the_environment(monkeypatch, raw):
    monkeypatch.setenv("OIDC_SUBJECT_USER_IDS", raw)
    with pytest.raises(ValidationError) as error:
        Settings(**(BASE | {"oidc_enabled": False}))
    assert OIDC_SUBJECT_USER_IDS_RETIRED in str(error.value)
    assert "immutable-subject" not in str(error.value)


@pytest.mark.parametrize("value", [{}, None])
def test_empty_retired_map_is_accepted(value):
    assert build(oidc_subject_user_ids=value).oidc_subject_user_ids == {}


def test_empty_retired_map_from_the_environment(monkeypatch):
    monkeypatch.setenv("OIDC_SUBJECT_USER_IDS", "{}")
    assert build().oidc_subject_user_ids == {}


# ── activation (unchanged rules) ────────────────────────────────────────────


def test_minimal_configuration_needs_no_group():
    s = build()
    assert (s.oidc_required_group, s.oidc_token_auth_method, s.oidc_extra_scopes,
            s.oidc_display_name, s.oidc_ca_file, s.oidc_ssl_context) == (
        "", "client_secret_basic", "", "SSO", None, None)


@pytest.mark.parametrize("override", [
    {"multi_user_mode": False}, {"mcp_sandbox_mode": True},
    {"oidc_issuer": "http://identity.example.test/realms/home"},
    {"oidc_issuer": "https://user@identity.example.test"}, {"oidc_issuer": "https://h.test/?q"},
    {"oidc_issuer": "https://h.test/#f"}, {"oidc_issuer": ""},
    {"oidc_issuer": "https://identity.example.test/" + "r" * 2048},
    {"base_url": "https://notes.example.test/path"}, {"base_url": "http://notes.example.test"},
    {"oidc_client_secret": ""}, {"oidc_client_id": " "},
    {"oidc_enabled": False, "local_login_enabled": False},
    {"oidc_required_group": "g" * 513}, {"oidc_required_group": "   "},
])
def test_activation_fails_closed(override):
    refused(**override)


def test_issuer_length_bound_is_inclusive():
    issuer = "https://identity.example.test/"
    issuer += "r" * (2048 - len(issuer))
    assert len(build(oidc_issuer=issuer).oidc_issuer) == 2048
    assert "at most 2048" in refused(oidc_issuer=issuer + "r")


def test_group_bound_is_inclusive():
    assert build(oidc_required_group="g" * 512).oidc_required_group == "g" * 512


# ── OIDC_TOKEN_AUTH_METHOD ──────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
def test_auth_method_accepted(method):
    assert build(oidc_token_auth_method=method).oidc_token_auth_method == method


@pytest.mark.parametrize("method", ["private_key_jwt", "none", "client_secret_jwt", "",
                                    "CLIENT_SECRET_POST", " client_secret_post"])
def test_auth_method_refused(method):
    assert "OIDC_TOKEN_AUTH_METHOD" in refused(oidc_token_auth_method=method)


# ── OIDC_EXTRA_SCOPES ───────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,canonical", [
    ("", ""), ("groups", "groups"), ("  groups   roles ", "groups roles"),
    ("openid email profile", ""), ("groups openid groups", "groups"),
    ("a!#[]~", "a!#[]~"), ("s" * 64, "s" * 64),
    ("a b c d e f g h", "a b c d e f g h"),
    ("https://graph.example/read", "https://graph.example/read"),
])
def test_extra_scopes_accepted(raw, canonical):
    assert build(oidc_extra_scopes=raw).oidc_extra_scopes == canonical


@pytest.mark.parametrize("raw", [
    "offline_access", "groups offline_access", "OFFLINE_ACCESS",
    "a b c d e f g h i", "s" * 65, 'quote"d', "back\\slash", "tab\tsep", "ünicode",
    "ctrl\x01", "nl\nx",
])
def test_extra_scopes_refused(raw):
    message = refused(oidc_extra_scopes=raw)
    assert "OIDC_EXTRA_SCOPES" in message


def test_offline_access_message():
    assert "offline_access" in refused(oidc_extra_scopes="offline_access")


# ── OIDC_DISPLAY_NAME ───────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["SSO", "Keycloak", "x", "n" * 40, "Kanidm (home)", "Zürich ID"])
def test_display_name_accepted(name):
    assert build(oidc_display_name=name).oidc_display_name == name


@pytest.mark.parametrize("name", ["", " ", "n" * 41, "tab\there", "nl\n", "bell\x07", "del\x7f", "​zw"])
def test_display_name_refused(name):
    assert "OIDC_DISPLAY_NAME" in refused(oidc_display_name=name)


# ── OIDC off: nothing new validated beyond type (D7) ────────────────────────


def test_oidc_off_does_not_validate_new_settings(tmp_path):
    s = build(oidc_enabled=False, oidc_token_auth_method="bogus", oidc_extra_scopes="offline_access",
              oidc_display_name="", oidc_ca_file=str(tmp_path / "missing.pem"),
              oidc_issuer="http://anything")
    assert s.oidc_ssl_context is None


# ── OIDC_CA_FILE (D6) ───────────────────────────────────────────────────────


def _cert(path, *, ca: bool):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test ca" if ca else "leaf")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


def test_ca_file_parsed_once_into_a_verifying_context(tmp_path):
    path = _cert(tmp_path / "ca.pem", ca=True)
    s = build(oidc_ca_file=str(path))
    ctx = s.oidc_ssl_context
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert [c["subject"] for c in ctx.get_ca_certs()] == [((("commonName", "test ca"),),)]
    path.unlink()  # never re-read
    assert s.oidc_ssl_context is ctx


@pytest.mark.parametrize("off", ["", "off", "OFF", "none", "null"])
def test_ca_file_off_spellings(off):
    assert build(oidc_ca_file=off).oidc_ssl_context is None


def test_ca_file_without_a_ca_refused(tmp_path):
    message = refused(oidc_ca_file=str(_cert(tmp_path / "leaf.pem", ca=False)))
    assert "OIDC_CA_FILE contains no CA certificate" in message


def test_ca_file_not_pem_refused(tmp_path):
    path = tmp_path / "junk.pem"
    path.write_text("not a certificate\n")
    assert "OIDC_CA_FILE" in refused(oidc_ca_file=str(path))


def test_ca_file_missing_or_directory_refused(tmp_path):
    assert "OIDC_CA_FILE names a file that does not exist" in refused(
        oidc_ca_file=str(tmp_path / "missing.pem"))
    assert "OIDC_CA_FILE does not name a regular file" in refused(oidc_ca_file=str(tmp_path))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_ca_file_unreadable_refused(tmp_path):
    path = _cert(tmp_path / "ca.pem", ca=True)
    path.chmod(0)
    try:
        assert "OIDC_CA_FILE names a file this process cannot read" in refused(oidc_ca_file=str(path))
    finally:
        path.chmod(0o600)


# ── no network at validation (D7, D8) ───────────────────────────────────────


def test_validation_contacts_no_provider(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("settings validation opened a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    s = build(oidc_required_group="/apps/obsidian", oidc_token_auth_method="client_secret_post",
              oidc_extra_scopes="groups", oidc_display_name="Keycloak",
              oidc_ca_file=str(_cert(tmp_path / "ca.pem", ca=True)))
    assert s.oidc_enabled and s.oidc_ssl_context is not None
