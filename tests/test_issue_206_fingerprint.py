"""The settings fingerprints, the comparison rule and the rotation cursor (#206).

`note_embeddings` records nothing about the model that produced it, so a
same-dimension model swap — bge-m3 for another 1024-dim model — mixes two
vector spaces in one column permanently and makes cosine distance meaningless
with nothing to notice. The fingerprint is the thing that notices. These cases
pin the three properties that make it work: exactly one spelling per
configuration, every field that changes what a stored row *is* inside it, and
the two deliberately opposite dispositions — a fingerprint fails closed, the
rotation cursor fails open.
"""
import json

import pytest
from pydantic import ValidationError

from src.config import MAX_CHUNKS_PER_NOTE, Settings, settings
from src.models import db as db_models
from src.services import index_state
from src.services.index_state import (
    FINGERPRINT_VERSION,
    INDEX_GENERATION_LOCK_KEY,
    INDEXER_STATE_KEYS,
    KEY_EMBEDDING_FINGERPRINT,
    KEY_FTS_FINGERPRINT,
    KEY_ROTATION_CURSOR,
    FingerprintStatus,
    active_embedding_model,
    compare_fingerprint,
    embedding_fingerprint,
    fts_fingerprint,
    parse_rotation_cursor,
)


#: `Settings` refuses a placeholder SECRET_KEY, and the suite's hermetic
#: environment is restored after `src.config` is imported — so a case that
#: builds a *valid* `Settings` has to supply one.
_A_REAL_KEY = "0" * 64


@pytest.fixture
def ollama(monkeypatch):
    """A known-good Ollama configuration, so every case varies one thing."""
    monkeypatch.setattr(settings, "embedding_provider", "ollama")
    monkeypatch.setattr(settings, "embedding_model", "bge-m3")
    monkeypatch.setattr(settings, "openai_embedding_model", "text-embedding-3-small")
    monkeypatch.setattr(settings, "embedding_dimensions", 1024)
    monkeypatch.setattr(settings, "chunk_size", 512)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    monkeypatch.setattr(settings, "ollama_url", "http://ollama:11434")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "fts_configs", ["english"])
    return settings


# --------------------------------------------------------------------------
# the keys agree in all three places
# --------------------------------------------------------------------------


def test_the_three_declarations_of_the_key_set_agree():
    """The keys are spelled in three places — this module, the ORM's CHECK and
    migration 023 — because a migration must keep describing the schema it
    created even after the model moves on. A drift between them is the failure
    the CHECK exists to prevent, arriving through the CHECK itself: a key the
    application writes that the database rejects, or worse, a key the
    application *reads* that nothing ever wrote, which reads as absent and
    makes the startup guard adopt instead of refuse."""
    assert INDEXER_STATE_KEYS == db_models.INDEXER_STATE_KEYS
    assert set(INDEXER_STATE_KEYS) == {
        KEY_EMBEDDING_FINGERPRINT,
        KEY_FTS_FINGERPRINT,
        KEY_ROTATION_CURSOR,
    }
    assert len(set(INDEXER_STATE_KEYS)) == 3


def test_the_generation_lock_key_is_a_literal_in_signed_64_bit_range():
    """`pg_advisory_xact_lock` takes a bigint. The value is written out rather
    than derived from a hash: two builds computing different keys are two
    builds holding no lock at all, silently."""
    assert isinstance(INDEX_GENERATION_LOCK_KEY, int)
    assert 0 < INDEX_GENERATION_LOCK_KEY < 2**63


# --------------------------------------------------------------------------
# canonical rendering
# --------------------------------------------------------------------------


def test_the_embedding_rendering_is_stable_and_single_spelled(ollama):
    """Byte equality is the comparison, so the rendering must admit exactly one
    spelling per configuration: sorted keys, no whitespace, and the same string
    every time it is asked."""
    first = embedding_fingerprint()
    assert first == embedding_fingerprint()
    assert " " not in first
    assert first == json.dumps(
        json.loads(first), sort_keys=True, separators=(",", ":")
    )
    assert json.loads(first) == {
        "v": FINGERPRINT_VERSION,
        "provider": "ollama",
        "model": "bge-m3",
        "dimensions": 1024,
        "chunk_size": 512,
        "chunk_overlap": 0,
        "max_chunks_per_note": MAX_CHUNKS_PER_NOTE,
    }


def test_the_fts_rendering_is_stable_and_single_spelled(ollama):
    first = fts_fingerprint()
    assert first == fts_fingerprint()
    assert " " not in first
    assert json.loads(first) == {"v": FINGERPRINT_VERSION, "configs": ["english"]}


# --------------------------------------------------------------------------
# what moves the embedding fingerprint
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute,value,field",
    [
        ("embedding_provider", "openai", "provider"),
        ("embedding_model", "nomic-embed-text", "model"),
        ("embedding_dimensions", 768, "dimensions"),
        ("chunk_size", 256, "chunk_size"),
        ("chunk_overlap", 64, "chunk_overlap"),
    ],
)
def test_each_generating_setting_moves_the_embedding_fingerprint(
    ollama, monkeypatch, attribute, value, field
):
    """Every field in the fingerprint decides what a stored vector *is*. A
    change to any of them leaves the existing rows describing something else,
    and the fingerprint is what turns that from a silent permanent corruption
    into a refused startup naming the field."""
    before = embedding_fingerprint()
    monkeypatch.setattr(settings, attribute, value)
    after = embedding_fingerprint()
    assert after != before
    verdict = compare_fingerprint(before, after)
    assert verdict.status is FingerprintStatus.DIFFERS
    assert field in verdict.fields


def test_the_chunk_cap_moves_the_embedding_fingerprint(ollama, monkeypatch):
    """The cap is in the fingerprint deliberately (design D7). At cap N a long
    note holds N chunks and its tail is absent; at 2N it would hold a different
    set. Lowering it leaves rows beyond the new bound and raising it leaves
    rows silently incomplete against the new policy that **nothing will ever
    re-select**, because their `embedded_content_hash` still matches."""
    before = embedding_fingerprint()
    monkeypatch.setattr(index_state, "MAX_CHUNKS_PER_NOTE", MAX_CHUNKS_PER_NOTE * 2)
    after = embedding_fingerprint()
    assert after != before
    verdict = compare_fingerprint(before, after)
    assert verdict.status is FingerprintStatus.DIFFERS
    assert verdict.fields == ("max_chunks_per_note",)


def test_the_inactive_providers_model_is_not_in_the_fingerprint(ollama, monkeypatch):
    """`model` is the model of the **active** provider, selected by the branch
    `get_provider()` takes. Reading the inactive one while the provider is
    chosen elsewhere is the exact defect this guard exists to catch, so the
    selection lives in one function — and changing the model nothing is calling
    must not refuse a startup."""
    assert active_embedding_model() == "bge-m3"
    before = embedding_fingerprint()
    monkeypatch.setattr(settings, "openai_embedding_model", "text-embedding-3-large")
    assert embedding_fingerprint() == before

    monkeypatch.setattr(settings, "embedding_provider", "openai")
    assert active_embedding_model() == "text-embedding-3-large"
    switched = embedding_fingerprint()
    assert json.loads(switched)["model"] == "text-embedding-3-large"
    # And now the *other* model is the inactive one.
    monkeypatch.setattr(settings, "embedding_model", "some-other-local-model")
    assert embedding_fingerprint() == switched


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("ollama_url", "http://other-host:11434"),
        ("openai_base_url", "https://example.azure.com/openai/v1"),
    ],
)
def test_endpoint_identity_is_excluded_and_that_is_intentional(
    ollama, monkeypatch, attribute, value
):
    """Accepted limitation L1, pinned so it stays a decision rather than an
    oversight. Repointing at another host or proxy is an infrastructure change
    that usually serves the identical artifact, and including the endpoint
    would demand a full vault re-embed for one.

    The cost is stated at both model keys in `.env.example` and `README.md`:
    the fingerprint records the **configuration, not the artifact**, so
    re-pulling a mutable tag or pointing at a host serving different weights
    under the same name mixes vector spaces undetected, and `make
    reset-embeddings` is the operator's only recourse."""
    before = embedding_fingerprint()
    monkeypatch.setattr(settings, attribute, value)
    assert embedding_fingerprint() == before


# --------------------------------------------------------------------------
# what moves the FTS fingerprint
# --------------------------------------------------------------------------


def test_reordering_the_fts_configs_does_not_move_the_fingerprint(ollama, monkeypatch):
    """A note's stored vector is one vector per config concatenated with `||`
    and a query is one tsquery per config OR'd; both are order-insensitive over
    lexeme sets. Refusing startup over a reordering would be a false alarm on a
    configuration that produces byte-identical tsvectors."""
    monkeypatch.setattr(settings, "fts_configs", ["english", "norwegian"])
    forwards = fts_fingerprint()
    monkeypatch.setattr(settings, "fts_configs", ["norwegian", "english"])
    assert fts_fingerprint() == forwards


def test_a_membership_change_moves_the_fts_fingerprint(ollama, monkeypatch):
    """The failure this catches is not a recall shortfall. Under `english` the
    token `running` is stored as the lexeme `run`, so a query under `simple`
    for `run` matches a note that does not contain the word — a false positive
    indistinguishable from a real hit, handed to an agent."""
    before = fts_fingerprint()
    monkeypatch.setattr(settings, "fts_configs", ["simple"])
    after = fts_fingerprint()
    assert after != before
    verdict = compare_fingerprint(before, after)
    assert verdict.status is FingerprintStatus.DIFFERS
    assert verdict.fields == ("configs",)


# --------------------------------------------------------------------------
# the comparison rule
# --------------------------------------------------------------------------


def test_absent_is_absent_not_a_mismatch(ollama):
    """Absent is adopted, not refused: refusing there would take every existing
    deployment down on upgrade over a configuration nobody changed."""
    verdict = compare_fingerprint(None, embedding_fingerprint())
    assert verdict.status is FingerprintStatus.ABSENT
    assert verdict.stored is None
    assert verdict.fields == ()


def test_an_identical_fingerprint_matches(ollama):
    current = embedding_fingerprint()
    verdict = compare_fingerprint(current, current)
    assert verdict.status is FingerprintStatus.MATCH
    assert verdict.fields == ()


def test_differs_names_every_changed_field_and_carries_both_sides(ollama, monkeypatch):
    before = embedding_fingerprint()
    monkeypatch.setattr(settings, "embedding_model", "nomic-embed-text")
    monkeypatch.setattr(settings, "chunk_size", 256)
    after = embedding_fingerprint()

    verdict = compare_fingerprint(before, after)
    assert verdict.status is FingerprintStatus.DIFFERS
    assert verdict.fields == ("chunk_size", "model")
    assert verdict.stored == before
    assert verdict.current == after


def test_a_field_present_on_one_side_only_is_reported_as_differing(ollama):
    """A stored value from a build that had one field fewer, at the same `v`,
    must not compare equal on the fields they share."""
    current = embedding_fingerprint()
    trimmed = dict(json.loads(current))
    del trimmed["chunk_overlap"]
    stored = json.dumps(trimmed, sort_keys=True, separators=(",", ":"))

    verdict = compare_fingerprint(stored, current)
    assert verdict.status is FingerprintStatus.DIFFERS
    assert verdict.fields == ("chunk_overlap",)


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "   ",
        "not json at all",
        '{"v":1,"provider":"ollama"',
        "[1, 2, 3]",
        '"a bare string"',
        "null",
    ],
)
def test_an_unparseable_stored_value_is_unreadable_and_never_rewritten(
    ollama, garbage
):
    """A value this build cannot read cannot certify the rows, so it refuses —
    and it must not be silently overwritten with the current fingerprint, which
    would convert an unreadable claim into a confident false one. The refusal
    carries the stored bytes verbatim so the caller can print what it found and
    write nothing."""
    verdict = compare_fingerprint(garbage, embedding_fingerprint())
    assert verdict.status is FingerprintStatus.UNREADABLE
    assert verdict.reason
    assert verdict.stored == garbage


@pytest.mark.parametrize("version", [0, 2, 99, "1", None])
def test_an_unknown_format_version_is_unreadable(ollama, version):
    """`clean_at_version`'s rule in a new place: an unknown stamped version
    counts as *differs*, never as adopt. A `v` from a newer build is the
    downgrade case and refuses for the same reason."""
    payload = dict(json.loads(embedding_fingerprint()))
    payload["v"] = version
    stored = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    verdict = compare_fingerprint(stored, embedding_fingerprint())
    assert verdict.status is FingerprintStatus.UNREADABLE
    assert "version" in (verdict.reason or "")
    assert verdict.stored == stored


def test_a_missing_version_field_is_unreadable(ollama):
    payload = dict(json.loads(embedding_fingerprint()))
    del payload["v"]
    stored = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    assert (
        compare_fingerprint(stored, embedding_fingerprint()).status
        is FingerprintStatus.UNREADABLE
    )


# --------------------------------------------------------------------------
# the rotation cursor — the opposite disposition
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "not-a-number",
        "-1",
        "-42",
        "+7",
        "3.5",
        "7 users",
        "1_0",
        "0x10",
        "nan",
        "inf",
        str(2**63),
        str(2**200),
        "٣",
    ],
)
def test_an_unusable_cursor_is_none_and_never_raises(raw):
    """A cursor is scheduling state whose worst possible consequence is an
    *order*, so it fails open where a fingerprint fails closed: anything
    unusable returns `None`, the caller starts at the first tenant, and the
    pass it runs is complete and correct. Failing closed on a stray character
    in a bookkeeping row would stop every tenant's indexing to protect
    nothing."""
    assert parse_rotation_cursor(raw) is None


@pytest.mark.parametrize(
    "raw,expected",
    [("0", 0), ("7", 7), (" 42 ", 42), ("000012", 12), (str(2**63 - 1), 2**63 - 1)],
)
def test_a_usable_cursor_parses(raw, expected):
    assert parse_rotation_cursor(raw) == expected


def test_an_out_of_range_but_numeric_cursor_needs_no_special_case():
    """"The smallest id strictly greater than N" selects nothing and wraps to
    the first user, so a cursor larger than every live id reaches the ordinary
    outcome by the ordinary rule — no branch of its own."""
    assert parse_rotation_cursor("999999999") == 999999999


# --------------------------------------------------------------------------
# the configuration the fingerprint describes must itself be sane
# --------------------------------------------------------------------------


def test_chunk_overlap_equal_to_chunk_size_is_rejected():
    """The chunker steps by `max(char_size - char_overlap, 1)` — the #10
    infinite-loop guard. At equality that step is one character, so ~3 KB of
    prose becomes ~3,000 chunks and every ordinary note hits
    `MAX_CHUNKS_PER_NOTE`: a typo silently truncating the embedding of the
    whole vault, with the cap's ERROR line firing thousands of times. The guard
    turned a hang into a quiet catastrophe; it did not make the configuration
    sane."""
    with pytest.raises(ValidationError) as exc:
        Settings(chunk_size=512, chunk_overlap=512, _env_file=None)
    message = str(exc.value)
    assert "CHUNK_OVERLAP (512)" in message
    assert "CHUNK_SIZE (512)" in message


def test_chunk_overlap_above_chunk_size_is_rejected():
    with pytest.raises(ValidationError) as exc:
        Settings(chunk_size=256, chunk_overlap=512, _env_file=None)
    assert "CHUNK_OVERLAP (512)" in str(exc.value)
    assert "CHUNK_SIZE (256)" in str(exc.value)


def test_an_overlap_below_the_chunk_size_is_accepted():
    loaded = Settings(
        chunk_size=512, chunk_overlap=511, secret_key=_A_REAL_KEY, _env_file=None
    )
    assert loaded.chunk_overlap == 511


def test_the_embed_budgets_default_and_admit_zero_as_disabled():
    """`0` disables either budget; both are enforced only when a pass serves
    more than one active user scope, which is what keeps the single-tenant
    deployment's behaviour identical to today's."""
    loaded = Settings(secret_key=_A_REAL_KEY, _env_file=None)
    assert loaded.embed_chunk_budget_per_user == 5000
    assert loaded.embed_time_budget_seconds_per_user == 300

    disabled = Settings(
        embed_chunk_budget_per_user=0,
        embed_time_budget_seconds_per_user=0,
        secret_key=_A_REAL_KEY,
        _env_file=None,
    )
    assert disabled.embed_chunk_budget_per_user == 0
    assert disabled.embed_time_budget_seconds_per_user == 0

    with pytest.raises(ValidationError):
        Settings(
            embed_chunk_budget_per_user=-1, secret_key=_A_REAL_KEY, _env_file=None
        )
    with pytest.raises(ValidationError):
        Settings(
            embed_time_budget_seconds_per_user=-1,
            secret_key=_A_REAL_KEY,
            _env_file=None,
        )
