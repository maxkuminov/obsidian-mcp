"""State about the index as a whole: the two settings fingerprints, the embed
rotation cursor, and the generation lock that makes the fingerprints an
interlock rather than a startup check (#206, #202).

A separate module, deliberately. `src/services/embeddings.py` takes the
generation lock inside `embed_note`, `src/services/indexer.py` reads the
fingerprint and the cursor, `src/main.py` compares both at startup, and the
reset script and the panel write them — and `indexer.py` already imports
`embeddings.py`. Putting any of this in either of them makes a cycle; putting
it here makes the dependency one-directional from every caller.

## What a fingerprint is for

`note_embeddings` records nothing about the provider, the model, the chunk size
or the overlap that produced it, and `content_tsvector` records nothing about
`FTS_CONFIGS`. The dimension guard reads the live column width from
`pg_attribute` — a *physical* fact about the table — so it catches a dump
restored into a differently configured deployment and cannot catch a
same-dimension model swap. That swap mixes two vector spaces in one column
permanently and makes cosine distance meaningless, silently, for ever.

So each derived kind carries one stored fingerprint of the configuration its
rows were built under, compared at startup by byte equality of a canonical
rendering. Canonical JSON rather than a delimited string: a model name may
contain any character, so a delimiter would need an escaping rule that would
then have to be specified, versioned and tested. JSON has one already, it
parses on both sides so a mismatch can name **which field changed** rather than
printing two opaque strings, and `sort_keys` with compact separators admits
exactly one spelling per configuration — the property byte equality needs.

## Dispositions differ on purpose

- **Fingerprints fail closed.** Absent is adopted (refusing there would take
  every existing deployment down on upgrade over a configuration nobody
  changed); different refuses; unparseable or an unknown `v` refuses and is
  **never overwritten**, because overwriting converts a claim this build cannot
  read into a confident false one. That is `clean_at_version`'s rule — an
  unknown stamped version counts as *differs* — in a new place.
- **The cursor fails open.** A stray character in a bookkeeping row must not
  stop every tenant's indexing to protect nothing: `parse_rotation_cursor`
  returns `None` for anything unusable and the caller starts at the first
  tenant, which is a complete and correct pass.

## What the fingerprint cannot see (accepted limitation L1)

Endpoint identity — `OLLAMA_URL`, `OPENAI_BASE_URL` — is deliberately excluded.
Moving between hosts or proxies is an infrastructure change that usually serves
the identical artifact, and including it would demand a full vault re-embed for
one. The consequence is that **the fingerprint records the configuration, not
the artifact**: `bge-m3` is a mutable Ollama tag, so `ollama pull` can replace
the weights behind it and a second host can serve different weights under the
same name. No value available to this process distinguishes those cases, and a
probe would have to trust the endpoint it is checking. The operator rule that
stands in its place is documented beside the model keys in `.env.example` and
`README.md`: a change of model artifact requires `make reset-embeddings`, and
nothing will detect it if you skip that.
"""
import enum
import json
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import MAX_CHUNKS_PER_NOTE, settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------

#: Canonical JSON describing the configuration the stored vectors were built
#: under. Written by the maintenance workflows and by the first startup that
#: finds it absent; read by the startup guard and, under the generation lock,
#: by every certifying transaction.
KEY_EMBEDDING_FINGERPRINT = "embedding_fingerprint"

#: Canonical JSON describing the `FTS_CONFIGS` the stored tsvectors were built
#: under. Written only by a rebuild that completed **every** retained scope.
KEY_FTS_FINGERPRINT = "fts_fingerprint"

#: The user id the last periodic pass finished, as a decimal string. The next
#: cycle starts at the smallest active id strictly greater than it, wrapping.
KEY_ROTATION_CURSOR = "embed_rotation_cursor"

#: Every key `indexer_state` admits. Mirrored by `INDEXER_STATE_KEYS` in
#: `src/models/db.py` (which writes the model's CHECK) and by `STATE_KEYS` in
#: migration 023 (which pins its own copy, as a migration must). A key outside
#: this set is rejected by the database — which matters because a key the
#: *application* misspells reads as absent, and absent is what makes the
#: startup guard adopt rather than refuse.
INDEXER_STATE_KEYS = (
    KEY_EMBEDDING_FINGERPRINT,
    KEY_FTS_FINGERPRINT,
    KEY_ROTATION_CURSOR,
)

#: The rendering version both fingerprints carry. Bumping it changes every
#: fingerprint, which is the point: adding a field must be a deliberate act
#: whose change ships either a rewrite of the stored value or an instruction to
#: reset. A stored value carrying a `v` this build does not know is
#: `UNREADABLE`, never adopted.
FINGERPRINT_VERSION = 1


# --------------------------------------------------------------------------
# the generation lock
# --------------------------------------------------------------------------

#: The one advisory-lock key guarding "which configuration the derived rows
#: were built under". `0x6f6d637067656e31` — the ASCII bytes ``omcpgen1`` — as
#: a positive signed 64-bit integer, **written out literally**. It is
#: deliberately not derived at runtime from a hash of anything: a key computed
#: from a string, a version or a table name can differ between builds, and two
#: processes holding different keys are two processes holding no lock at all,
#: with a failure mode that is silent and permanent.
INDEX_GENERATION_LOCK_KEY = 8029183045093649969


async def acquire_generation_lock(session: AsyncSession) -> None:
    """Take the index generation lock for the rest of this transaction.

    `pg_advisory_xact_lock` — **transaction-scoped, never session-scoped**.
    A session lock leaked into a pooled connection would be held by whatever
    ran next and a crashed holder would strand it; this one is released by
    commit or rollback with no operator action.

    **Ordering rule, and it is a property of the transaction rather than of the
    statement that needs the fingerprint: this lock is acquired before any row
    or table lock the transaction takes.** One direction everywhere, so the
    lock cannot close a cycle with the row locks the pass and the panel already
    contend for. For a transaction that mutates rows before it reaches the
    write that depends on the configuration — the incremental index pass, whose
    upserts, move updates, prunes, link rows and certification invalidation all
    precede its tsvector write — that means the head of the transaction, and
    the caller is required to audit what its transaction touches rather than
    reason backwards from the write.

    The embed path is the one place where "before the first row lock" and
    "after the provider call" agree rather than conflict: the only row-locking
    statement in that per-note transaction is the certification, so the lock is
    taken immediately before it and no lock of any kind is ever held across a
    provider round trip.

    Callers must re-read the relevant fingerprint **after** this returns and
    refuse to write on a mismatch. Acquiring the lock without re-reading buys
    nothing: the value may have changed while the caller waited for it.

    A caller that waits here is waiting for an in-flight pass to commit, which
    is the intended behaviour — a reset must not land mid-pass — so the
    maintenance paths deliberately do not set a short `lock_timeout`.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": INDEX_GENERATION_LOCK_KEY},
    )


# --------------------------------------------------------------------------
# the key/value accessor
# --------------------------------------------------------------------------


async def state_table_exists(session: AsyncSession) -> bool:
    """Whether migration 023 has run on this database.

    `to_regclass` answers without raising, which is what makes it usable as the
    first statement of a startup check: a `SELECT` against a missing table
    aborts the transaction, so the guard could not then go on to defer to
    alembic. The dimension check takes the same stance when its column is
    absent.
    """
    result = await session.execute(
        text("SELECT to_regclass('indexer_state')")
    )
    return result.scalar() is not None


async def get_state(session: AsyncSession, key: str) -> str | None:
    """The stored value for `key`, or `None` when no row holds it.

    `None` means **absent**, and absent is a meaningful state to both readers:
    the fingerprint guard adopts on it, the rotation starts at the first tenant
    on it. It is never used to paper over a read that failed — a failure
    propagates.
    """
    result = await session.execute(
        text("SELECT value FROM indexer_state WHERE key = :key"),
        {"key": key},
    )
    return result.scalar_one_or_none()


async def set_state(session: AsyncSession, key: str, value: str) -> None:
    """Upsert `key` to `value`, stamping `updated_at`.

    Written as SQL rather than assembled through the ORM so the conflict target
    and the two assignments read at a glance.

    **No internal `try`/`except`: a failure propagates.** For a fingerprint
    that is the whole point (design D7d) — it is not instrumentation but the
    claim a later startup refuses on, so a reset that wiped the column and then
    swallowed a failed record would leave a stored value naming the *previous*
    configuration over rows about to be built under the new one. The caller
    runs this inside the maintenance operation's own transaction, and a failure
    rolls that operation back and surfaces to the operator who invoked it.

    The rotation cursor is the exception, and it is the *caller's* exception:
    the pass wraps its cursor write in its own swallow, exactly as
    `_write_indexer_run` does, because a lost cursor costs an order and nothing
    else. This function does not decide that for it.

    Does not commit. The transaction belongs to the caller, which is what lets
    a fingerprint be recorded in the same transaction as the wipe or the
    rebuild it describes.
    """
    await session.execute(
        text(
            "INSERT INTO indexer_state (key, value, updated_at) "
            "VALUES (:key, :value, now()) "
            "ON CONFLICT (key) DO UPDATE "
            "SET value = EXCLUDED.value, updated_at = now()"
        ),
        {"key": key, "value": value},
    )


# --------------------------------------------------------------------------
# the fingerprints
# --------------------------------------------------------------------------


def active_embedding_model() -> str:
    """The model of the **active** provider.

    One function, because reading the inactive provider's model while the
    provider is chosen somewhere else is the exact defect the fingerprint
    exists to catch. The branch is `get_provider()`'s, spelled out here rather
    than imported: `embeddings.py` imports this module, so importing it back
    would be a cycle.
    """
    if settings.embedding_provider == "openai":
        return settings.openai_embedding_model
    return settings.embedding_model


def _canonical(payload: dict) -> str:
    """The one spelling of `payload`. Sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def embedding_fingerprint() -> str:
    """Canonical JSON naming everything that decides what a stored vector *is*.

    ``{"chunk_overlap":0,"chunk_size":512,"dimensions":1024,
    "max_chunks_per_note":1000,"model":"bge-m3","provider":"ollama","v":1}``

    `max_chunks_per_note` is in it deliberately. It changes what a note's
    stored vector set is: at cap N a long note holds N chunks and its tail is
    absent, and the same note at cap 2N would hold a different set. Lowering
    the cap leaves rows beyond the new bound; raising it leaves rows silently
    incomplete against the new policy that **nothing will ever re-select**,
    because their `embedded_content_hash` still matches. Including it makes a
    cap change a declared reset instead of a permanent, invisible
    under-embedding. The cost — a re-embed even for a change that would only
    widen coverage — is accepted rather than optimised away with a comparison
    rule that would have to know which direction is safe, and the fields are
    not independent: a larger cap with a smaller chunk size is not a widening.

    Endpoint identity is **not** in it — see the module docstring, L1.
    """
    return _canonical(
        {
            "v": FINGERPRINT_VERSION,
            "provider": settings.embedding_provider,
            "model": active_embedding_model(),
            "dimensions": settings.embedding_dimensions,
            "chunk_size": settings.chunk_size,
            "chunk_overlap": settings.chunk_overlap,
            "max_chunks_per_note": MAX_CHUNKS_PER_NOTE,
        }
    )


def fts_fingerprint() -> str:
    """Canonical JSON naming the text-search configs the tsvectors were built
    under. ``{"configs":["english"],"v":1}``

    The list is **sorted**, so membership is compared and order is not. A
    note's stored vector is one vector per config concatenated with `||` and a
    query is one tsquery per config OR'd; both operators are order-insensitive
    over lexeme sets, so `["english","norwegian"]` and `["norwegian","english"]`
    produce identical stored vectors and identical matches. Comparing them as
    ordered lists would refuse startup over a reordering that changed nothing.
    """
    return _canonical(
        {
            "v": FINGERPRINT_VERSION,
            "configs": sorted(settings.fts_configs),
        }
    )


class FingerprintStatus(enum.Enum):
    """How a stored fingerprint stands against the current configuration."""

    #: No value stored. Adopt: record the current one and warn that it was
    #: assumed rather than verified.
    ABSENT = "absent"
    #: Byte-identical. Proceed silently.
    MATCH = "match"
    #: Parseable, this build's `v`, and different. Refuse, naming the fields.
    DIFFERS = "differs"
    #: Not parseable, or a `v` this build does not know. Refuse, and **write
    #: nothing** — a value this build cannot read must not be overwritten with
    #: a confident one.
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class FingerprintVerdict:
    """The comparison's result, and everything the refusal message needs."""

    status: FingerprintStatus
    #: For `DIFFERS`: the field names whose values are not equal, sorted. A
    #: field present on one side only counts as differing.
    fields: tuple[str, ...] = ()
    #: For `UNREADABLE`: why the stored value could not be interpreted.
    reason: str | None = None
    #: The stored value, verbatim, so the caller can print both sides. `None`
    #: for `ABSENT`.
    stored: str | None = None
    #: The current rendering, so the caller can print both sides.
    current: str = ""


#: Sentinel for "this side does not have the field at all", so a field present
#: on one side only is reported as differing rather than compared against None.
_MISSING = object()


def _differing_fields(stored: dict, current: dict) -> tuple[str, ...]:
    return tuple(
        sorted(
            name
            for name in set(stored) | set(current)
            if stored.get(name, _MISSING) != current.get(name, _MISSING)
        )
    )


def compare_fingerprint(stored: str | None, current: str) -> FingerprintVerdict:
    """Compare a stored fingerprint against the current rendering.

    `current` is what `embedding_fingerprint()` or `fts_fingerprint()` returned
    in this process; `stored` is whatever `get_state` read, which is text a
    previous build wrote, an operator may have edited, and a downgrade may have
    left in a shape this build does not know.

    The resolution is the one the spec fixes: absent → adopt, equal → proceed,
    different → refuse naming the fields, unreadable → refuse and write
    nothing. `UNREADABLE` covers a value that is not JSON, is JSON but not an
    object, or carries a `v` this build does not recognise — including a `v`
    from a *newer* build, which is the downgrade case.
    """
    if stored is None:
        return FingerprintVerdict(
            status=FingerprintStatus.ABSENT, current=current
        )

    try:
        parsed = json.loads(stored)
    except (ValueError, TypeError) as exc:
        return FingerprintVerdict(
            status=FingerprintStatus.UNREADABLE,
            reason=f"it is not valid JSON ({exc.__class__.__name__})",
            stored=stored,
            current=current,
        )

    if not isinstance(parsed, dict):
        return FingerprintVerdict(
            status=FingerprintStatus.UNREADABLE,
            reason=(
                f"it is a JSON {type(parsed).__name__}, not an object of "
                "fingerprint fields"
            ),
            stored=stored,
            current=current,
        )

    version = parsed.get("v")
    if version != FINGERPRINT_VERSION:
        return FingerprintVerdict(
            status=FingerprintStatus.UNREADABLE,
            reason=(
                f"it carries format version {version!r}, which this build does "
                f"not recognise (it writes and reads v={FINGERPRINT_VERSION})"
            ),
            stored=stored,
            current=current,
        )

    if stored == current:
        return FingerprintVerdict(
            status=FingerprintStatus.MATCH, stored=stored, current=current
        )

    # `current` is this module's own rendering, so it parses by construction.
    fields = _differing_fields(parsed, json.loads(current))
    return FingerprintVerdict(
        status=FingerprintStatus.DIFFERS,
        fields=fields,
        stored=stored,
        current=current,
    )


# --------------------------------------------------------------------------
# the rotation cursor
# --------------------------------------------------------------------------

#: Largest value a cursor may carry. `users.id` is an `integer`, so anything
#: this large already selects no successor — but a Python int wider than a
#: signed 64-bit column cannot be bound as a query parameter at all, and the
#: rule for an unusable cursor is "start at the first tenant", never "raise".
_MAX_CURSOR = 2**63 - 1


def parse_rotation_cursor(raw: str | None) -> int | None:
    """The user id to resume after, or `None` to start at the first tenant.

    Deliberately the **opposite disposition** from the fingerprints'. The value
    lives as text in a key/value table, so it can be absent, non-numeric,
    negative, or wider than the column it will be compared against — through
    drift, a hand-edited row, or a downgrade. A cursor is scheduling state
    whose worst possible consequence is an *order*; a fingerprint is a claim
    about what the stored rows are, and its worst consequence is a permanently
    wrong answer. Failing closed on a stray character here would stop every
    tenant's indexing to protect nothing, so anything unusable returns `None`
    and the caller runs a complete, correct pass beginning at the first user in
    the deterministic order — precisely today's behaviour.

    An out-of-*range* numeric value needs no special case from the caller
    either: "the smallest id strictly greater than N" simply selects nothing
    and wraps to the first, which is the same outcome by the ordinary rule.

    Never raises. The caller logs the value it could not use, once, at WARNING.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    # Plain ASCII decimal digits only. `int()` alone would accept `"1_0"` as
    # 10 and `"\u0663"` as 3; a sign is rejected here rather than by the range
    # test below, so every unusable spelling reaches the one outcome.
    if not (candidate.isascii() and candidate.isdigit()):
        return None
    value = int(candidate)
    if value > _MAX_CURSOR:
        return None
    return value
