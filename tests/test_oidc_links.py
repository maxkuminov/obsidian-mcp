"""Offline tests for `src/services/oidc_links.py` and migration 037's literals (#416).

The database behaviour (binding, the guard, uniqueness, D15 counts) is in
`tests/integration/test_oidc_links_pg.py`; the schema shape in
`tests/integration/test_schema_check.py`. This module pins the pure parts:
the D2 email normalization table, subject validation, and that the model and
the migration agree byte for byte on the markers and predicates.
"""
import importlib.util
import pathlib

import pytest

from src.models import db as models
from src.services import oidc_links

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _migration_037():
    path = next((ROOT / "alembic" / "versions").glob("037_*.py"))
    spec = importlib.util.spec_from_file_location("migration_037", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# normalize_email (D2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Case: ASCII letters only are lower-cased.
        ("Max@Example.com", "max@example.com"),
        ("MAX@EXAMPLE.COM", "max@example.com"),
        ("max@example.com", "max@example.com"),
        # Surrounding ASCII whitespace is stripped.
        ("  max@example.com\t\n", "max@example.com"),
        ("\r\x0b\x0cmax@example.com ", "max@example.com"),
        # Non-ASCII stays exactly as entered (no Unicode folding).
        ("Ünïcode@Example.com", "Ünïcode@example.com"),
        ("max@BÜCHER.de", "max@bÜcher.de"),
        ("ΜΑΞ@example.com", "ΜΑΞ@example.com"),
        # Kelvin sign and long s are not folded to k / s.
        ("K@example.com", "K@example.com"),
        # Plus addressing and dots are kept.
        ("Max.K+sso@Example.com", "max.k+sso@example.com"),
        # Length bounds: 3 and 254 characters are admitted.
        ("a@b", "a@b"),
        ("a" * 64 + "@" + "b" * 189, "a" * 64 + "@" + "b" * 189),
    ],
)
def test_normalize_email_accepts(raw, expected):
    assert oidc_links.normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "@",
        "a@",
        "@b",
        "ab",  # too short, no @
        "a" * 64 + "@" + "b" * 190,  # 255 characters
        "x" * 300 + "@example.com",
        "max",
        "max.example.com",
        # Exactly one @.
        "max@@example.com",
        "max@exa@mple.com",
        "a@b@c",
        # Internal whitespace, ASCII or not.
        "max k@example.com",
        "max@exa mple.com",
        "max\t@example.com",
        "max@example.com x",
        "max @example.com",
        # Surrounding non-ASCII whitespace is not stripped, so it is refused.
        " max@example.com",
        # Control characters.
        "max\x00@example.com",
        "max@example.com\x00",
        "max\x7f@example.com",
        "max\x1b@example.com",
        "max\x85@example.com",
        # A lone surrogate (the database could not encode it).
        "max\udcff@example.com",
    ],
)
def test_normalize_email_refuses(raw):
    assert oidc_links.normalize_email(raw) is None


@pytest.mark.parametrize(
    "value", [None, 42, 4.2, True, ["max@example.com"], {"email": "a@b"}, b"max@example.com"]
)
def test_normalize_email_non_string_is_none(value):
    assert oidc_links.normalize_email(value) is None


def test_normalize_email_is_idempotent():
    for raw in ("  Max@Example.com ", "Ünïcode@Example.COM", "a@b"):
        once = oidc_links.normalize_email(raw)
        assert oidc_links.normalize_email(once) == once


def test_normalized_email_fits_the_column():
    assert oidc_links.EMAIL_MAX_LENGTH == models.User.__table__.c.oidc_expected_email.type.length


# --------------------------------------------------------------------------
# subject validation, expiry
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,ok",
    [
        ("f81d4fae-7dec-11d0-a765-00a0c91e6bf6", True),
        ("x", True),
        ("~!@#$%^&*()", True),
        ("s" * 255, True),
        ("s" * 256, False),
        ("", False),
        ("has space", False),
        ("tab\there", False),
        ("ünïcode", False),
        ("nul\x00", False),
        ("del\x7f", False),
        (None, False),
        (123, False),
    ],
)
def test_valid_subject(value, ok):
    assert oidc_links.valid_subject(value) is ok


def test_pending_window_is_seven_days():
    import datetime

    assert oidc_links.PENDING_DAYS == 7
    t = datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc)
    assert oidc_links.pending_expires_at(t) == t + datetime.timedelta(days=7)


# --------------------------------------------------------------------------
# prepare_login short-circuits, reachable_admins_after validation
# --------------------------------------------------------------------------


class _RecordingSession:
    """Fails the test on any statement; records rollbacks."""

    def __init__(self):
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1

    async def execute(self, *a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("no statement should run for this resolution")


async def test_prepare_login_unverified_refuses_without_a_statement():
    session = _RecordingSession()
    res = oidc_links.Resolution("unverified", "https://idp", "sub", user_id=1, email="a@b")
    result = await oidc_links.prepare_login(session, res)
    assert result.reason == "email_unverified" and not result.ok
    assert result.user_id is None and result.session_version is None
    assert session.rollbacks == 1


async def test_prepare_login_none_refuses_unlinked():
    session = _RecordingSession()
    result = await oidc_links.prepare_login(
        session, oidc_links.Resolution("none", "https://idp", "sub")
    )
    assert result.reason == "unlinked"
    assert session.rollbacks == 1


async def test_reachable_admins_after_rejects_an_unknown_state():
    with pytest.raises(ValueError):
        await oidc_links.reachable_admins_after(
            _RecordingSession(), "https://idp", target_id=1, target_after="renamed"
        )


async def test_resolve_never_reads_email_when_bound(monkeypatch):
    """The bound branch returns before normalization is even called."""

    class _Result:
        def scalar_one_or_none(self):
            return 7

    class _Session:
        async def execute(self, *a, **k):
            return _Result()

    def boom(_value):  # pragma: no cover - must not be reached
        raise AssertionError("email examined on the bound branch")

    monkeypatch.setattr(oidc_links, "normalize_email", boom)
    res = await oidc_links.resolve(_Session(), "https://idp", "sub", ["not", "a", "str"], "true")
    assert res.kind == "bound" and res.user_id == 7 and res.email is None


# --------------------------------------------------------------------------
# the model and migration 037 agree
# --------------------------------------------------------------------------


def test_column_marker_is_byte_identical():
    assert _migration_037().COLUMN_MARKER == models._OIDC_LINK_COLUMN_MARKER


def test_constraint_marker_differs_from_the_column_marker():
    m = _migration_037()
    assert m.CONSTRAINT_MARKER == "OIDC account link constraint (037_oidc_account_links)"
    assert m.CONSTRAINT_MARKER != m.COLUMN_MARKER


def test_predicates_are_byte_identical():
    m = _migration_037()
    assert m.BOUND_COMPLETE_PREDICATE == models._OIDC_BOUND_COMPLETE_PREDICATE
    assert m.EXPECTED_COMPLETE_PREDICATE == models._OIDC_EXPECTED_COMPLETE_PREDICATE
    assert m.NOT_BOTH_PREDICATE == models._OIDC_NOT_BOTH_PREDICATE


def test_model_declares_the_six_columns_with_the_marker():
    table = models.User.__table__
    m = _migration_037()
    for name, fmt, _satype in m.COLUMNS:
        column = table.c[name]
        assert column.nullable is True, name
        assert column.server_default is None and column.default is None, name
        assert column.comment == models._OIDC_LINK_COLUMN_MARKER, name
    lengths = {n: table.c[n].type.length for n in
               ("oidc_issuer", "oidc_subject", "oidc_expected_issuer", "oidc_expected_email")}
    assert lengths == {"oidc_issuer": 2048, "oidc_subject": 255,
                       "oidc_expected_issuer": 2048, "oidc_expected_email": 254}
    for n in ("oidc_linked_at", "oidc_expected_set_at"):
        assert table.c[n].type.timezone is True


def test_model_declares_the_five_named_constraints():
    names = {c.name for c in models.User.__table__.constraints if c.name}
    assert set(_migration_037().CONSTRAINT_NAMES) <= names


def test_migration_chains_from_036():
    m = _migration_037()
    assert m.revision == "037" and m.down_revision == "036"
