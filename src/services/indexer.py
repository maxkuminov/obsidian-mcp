import asyncio
import contextlib
import ctypes
import enum
import errno
import fnmatch
import hashlib
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (
    String,
    bindparam,
    delete,
    func,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, insert

from src.config import MAX_CHUNKS_PER_NOTE, MAX_LINKS_PER_NOTE, settings
from src.database import async_session
from src.models.db import (
    IndexerRun,
    NoteEmbedding,
    NoteLink,
    NoteMetadata,
    OAuthCode,
    OAuthToken,
    User,
    UserSession,
)
from src.services.embeddings import (
    EmbedNoteFailure,
    NoteEmbedOutcome,
    StaleCertification,
    certify_embedded,
    chunk_text_bounded,
    clean_at_version,
    clean_for_embedding,
    embed_note,
)
from src.services.fts import index_tsvector_sql
from src.services.rate_limits import flush_expired
from src.services.index_state import (
    KEY_EMBEDDING_FINGERPRINT,
    KEY_FTS_FINGERPRINT,
    KEY_ROTATION_CURSOR,
    FingerprintStatus,
    acquire_generation_lock_unbounded,
    compare_fingerprint,
    embedding_fingerprint,
    fts_fingerprint,
    get_state,
    parse_rotation_cursor,
    set_state,
    state_table_exists,
)
from src.services.links import (
    build_vault_index,
    extract_links_bounded,
    resolve_target,
)
from src.oauth.grants import lock_account_guard
from src.services import vault_overlap
from src.services.transfer import canonical_vault_root
from src.services.vault import (
    _vault_root,
    canonical_key,
    extract_tags,
    non_finite_token,
    note_title,
    parse_frontmatter,
    warm_user_vault_cache,
)

# Module-level flag the dashboard reads to surface "link extraction in
# progress" while the one-shot backfill is running.
link_backfill_in_progress: bool = False

# In-process heartbeat for the indexer: when a pass last *finished*, and
# whether it finished cleanly. The dashboard used to infer this from
# `max(notes_metadata.indexed_at)`, but that column only moves for notes a
# pass actually upserted or moved — a pass over an unchanged vault writes it
# nowhere. So a healthy indexer on an idle vault reported a last run of days
# ago, which is an invitation to reach for the Danger zone and re-embed the
# whole vault for nothing (#78).
#
# Deliberately in-process, not a table: it answers "is this process's loop
# alive", which is exactly a property of this process, and it costs no write
# per tick. It resets to None on restart — the startup pass sets it moments
# later, so the window where the dashboard reads "Never" is the window in
# which the first pass has genuinely not finished yet.
last_index_run_at: datetime | None = None
last_index_run_ok: bool | None = None


def _record_index_run(ok: bool) -> None:
    global last_index_run_at, last_index_run_ok
    last_index_run_at = datetime.now(timezone.utc)
    last_index_run_ok = ok


# ── Persistent pass history (#160, migration 019) ──────────────────────────
#
# The heartbeat above is in-process and answers "is this process's loop alive".
# `indexer_runs` answers the other question — "how long have passes been taking,
# and did any of them fail" — which has to outlive a redeploy to be worth
# asking. Log parsing was the alternative and was rejected: logs rotate with the
# container.

#: The history is bounded because nothing ever asks for the 501st-newest pass.
#: A five-minute loop writes ~288 rows a day per user; unbounded, this table
#: would be the largest thing in the database inside a year, holding data no
#: view reads.
MAX_INDEXER_RUNS = 500

# One pass's error column is for an operator to read, not for a machine to
# parse, and a stack of per-stage failures from a multi-user pass can be long.
MAX_RUN_ERROR_CHARS = 4000


@dataclass
class EmbedPassResult:
    """What one `embed_vault` call embedded — **and what it could not**.

    The count alone was a falsely clean report. `embed_vault` catches every
    per-note exception, logs a warning and carries on, which is the right
    behaviour (one poisoned note must not stop the backlog) and used to be the
    pass's *only* record of it: a total Ollama or OpenAI outage embedded
    nothing, raised nothing, and wrote a run row with `notes_embedded = 0` and
    `error = NULL` — byte-for-byte the row a pass with nothing to embed writes.
    An operator watching the history through a provider outage would have seen
    a wall of healthy passes.

    So the failures ride back with the count. `notes_embedded` stays truthful
    (a pass that embedded 40 of 50 notes reports 40), and the summary lands in
    the run row's `error` beside it.
    """

    embedded: int = 0
    #: **Notes for which an embedding provider call was issued** — the "of M"
    #: in the summary. It used to be initialised from the backlog's size, which
    #: counted work *contemplated* rather than work done: a sweep that decided
    #: about 16,700 certification-current rows without calling anything would
    #: have rendered three failures out of three calls as "3 of 16,700", and a
    #: pass over 400 notes of which 50 cleaned to zero chunks would have
    #: claimed 400 provider calls it never made.
    #:
    #: **One rule, one increment point**: `record_attempt()` is called exactly
    #: once per note whose `EmbedNoteResult.chunks_submitted` is non-zero, at
    #: the two `embed_note` call sites and nowhere else. Every non-attempt is a
    #: consequence of that rule rather than an enumerated exception — a
    #: zero-chunk certification, an excluded note, a hash mismatch, a note left
    #: by a pause or a budget stop and a sweep row that agreed with the
    #: configuration all issue no call, so none of them moves the denominator.
    attempted: int = 0
    failures: int = 0
    #: The first failure's message. One, not all: the row's `error` is bounded
    #: and 2,000 identical "connection refused" lines say nothing the count
    #: does not already say.
    first_error: str | None = None

    def record_attempt(self) -> None:
        """One note issued a provider call. The only writer of `attempted`."""
        self.attempted += 1

    def record_failure(self, exc: BaseException) -> None:
        """A failure the caller still holds an exception for.

        Kept for the exceptions that genuinely escape around the call — a
        database error, a rollback failure — so the two entry points converge
        on one counter and one summary.
        """
        self.failures += 1
        if self.first_error is None:
            self.first_error = f"{type(exc).__name__}: {exc}"

    def record_failure_detail(self, failure: EmbedNoteFailure) -> None:
        """A failure `embed_note` swallowed, described by its own record.

        `embed_note` catches the provider exception itself, so by the time the
        pass sees a `PROVIDER_FAILED` there is no exception left to inspect and
        `first_error` would read `"... first: None"` — the operator's only view
        of a total outage, saying nothing. The structured failure is what the
        message is built from instead: `"{exc_type}: {message}"` for a raise,
        and the cardinality mismatch's own rendering for the mismatch. Both
        increment the same counter as `record_failure`, because the run row
        distinguishes them by their message and not by a second count.
        """
        self.failures += 1
        if self.first_error is None:
            if failure.exc_type == "CardinalityMismatch":
                self.first_error = (
                    f"CardinalityMismatch: {failure.received} vectors for "
                    f"{failure.requested} chunks"
                )
            else:
                self.first_error = f"{failure.exc_type}: {failure.message}"

    @property
    def failure_summary(self) -> str | None:
        if not self.failures:
            return None
        return (
            f"embed failures: {self.failures} of {self.attempted} — "
            f"first: {self.first_error}"
        )

    def __int__(self) -> int:
        return self.embedded


@dataclass
class PassStats:
    """What one pass did, accumulated as its stages report.

    The `record_*` methods tolerate a `None` result on purpose: `index_vault`
    and `embed_vault` are monkeypatched with plain no-op coroutines throughout
    the test suite, and a recorder that insisted on a tuple would turn every one
    of those into a failure about instrumentation rather than about indexing.
    """

    notes_scanned: int = 0
    notes_indexed: int = 0
    notes_embedded: int = 0
    errors: list[str] = field(default_factory=list)
    #: Set by a pass that found there was nothing to do and never started —
    #: today only the link backfill, whose "this scope already has link rows"
    #: probe fires on every startup after the first. A row per startup for a
    #: pass that did no work is noise, and noise in a 500-row history evicts
    #: the passes an operator came to read.
    skipped: bool = False

    def record_index(self, result) -> None:
        """Absorb `index_vault`'s `(scanned, indexed)`."""
        if not result:
            return
        scanned, indexed = result
        self.notes_scanned += int(scanned)
        self.notes_indexed += int(indexed)

    def record_embedded(self, result) -> None:
        """Absorb `embed_vault`'s count of notes it actually embedded.

        An `EmbedPassResult` brings its swallowed per-note failures with it, and
        they go into the row's `error` — a pass that embedded nothing because
        the embedding provider was down must not be indistinguishable from a
        pass that had nothing to embed.
        """
        if isinstance(result, EmbedPassResult):
            self.notes_embedded += result.embedded
            summary = result.failure_summary
            if summary is not None:
                self.errors.append(summary)
            return
        if not result:
            return
        self.notes_embedded += int(result)

    def record_error(self, stage: str, exc: BaseException) -> None:
        self.errors.append(f"{stage}: {type(exc).__name__}: {exc}")

    @property
    def error_text(self) -> str | None:
        if not self.errors:
            return None
        return "\n".join(self.errors)[:MAX_RUN_ERROR_CHARS]


#: PostgreSQL's `foreign_key_violation`. Read off the driver's exception rather
#: than matched in the message, which is localised.
_FK_VIOLATION_SQLSTATE = "23503"


def _is_fk_violation(exc: BaseException) -> bool:
    """Is this the FK on `indexer_runs.user_id` refusing a vanished user?

    asyncpg raises `ForeignKeyViolationError` with `sqlstate`; SQLAlchemy wraps
    it in `IntegrityError` and exposes it as `.orig`. Both attribute spellings
    are checked because psycopg names the same field `pgcode`, and the class
    name is the last resort so a driver swap degrades to "retry once" rather
    than to "lose the row".
    """
    orig = getattr(exc, "orig", None)
    for attr in ("sqlstate", "pgcode"):
        if getattr(orig, attr, None) == _FK_VIOLATION_SQLSTATE:
            return True
    return type(orig).__name__ == "ForeignKeyViolationError"


async def _insert_run_row(
    session, trigger: str, user_id: int | None, started_at: datetime, stats: PassStats
) -> None:
    """The insert and its prune, in one transaction. Raises on either."""
    await session.execute(
        insert(IndexerRun).values(
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            trigger=trigger,
            user_id=user_id,
            notes_scanned=stats.notes_scanned,
            notes_indexed=stats.notes_indexed,
            notes_embedded=stats.notes_embedded,
            error=stats.error_text,
        )
    )
    # `started_at DESC, id DESC` and not `id DESC` alone: passes are
    # inserted at their *finish*, so two passes that started minutes
    # apart can land in the other order. The history is read by start
    # time, so it is pruned by start time.
    await session.execute(
        text(
            "DELETE FROM indexer_runs WHERE id NOT IN ("
            "  SELECT id FROM indexer_runs "
            "  ORDER BY started_at DESC, id DESC LIMIT :keep"
            ")"
        ),
        {"keep": MAX_INDEXER_RUNS},
    )
    await session.commit()


async def _write_indexer_run(
    trigger: str, user_id: int | None, started_at: datetime, stats: PassStats
) -> None:
    """Insert one run row and prune to the newest `MAX_INDEXER_RUNS`.

    **Session discipline.** This is only ever called by the *holder* of
    `index_pass_lock`, never by something waiting for it, and it opens and
    closes its own short-lived session inside that call. That is the same rule
    the panel's destructive actions follow from the other side
    (`_pass_lock_without_a_connection` in `src/control_panel/routes.py`): a
    waiter that keeps a pooled connection while blocking on the lock deadlocks
    against a holder that needs one to finish. A holder taking a connection is
    exactly what the lock exists to make safe.

    The insert and the prune share one transaction, so the table is never
    briefly over the cap and a rollback loses both.

    **It never raises.** Failing to record that a pass happened must not turn a
    successful pass into a failed one, and this runs in a `finally` where a
    raise would replace the exception the operator actually needs to see.

    **A user deleted mid-pass costs the owner label, not the row.** `user_id`
    is captured when the pass starts and inserted when it finishes, and a pass
    over a large vault runs for minutes; an administrator deleting that user in
    between makes the FK reject the INSERT. `ON DELETE SET NULL` cannot help —
    it fires on rows that already exist, and this one never got in. Swallowed
    by the handler below, the whole pass would vanish from the history: the
    longest passes are the likeliest to lose the race, and "the operator
    deleted a user" is exactly the moment they open the page. So an FK
    violation is retried once with a NULL owner, which is what the column would
    hold a moment later anyway.
    """
    try:
        async with async_session() as session:
            await _insert_run_row(session, trigger, user_id, started_at, stats)
        return
    except Exception as e:  # noqa: BLE001 - recording must never fail a pass
        if user_id is None or not _is_fk_violation(e):
            logger.warning("Could not record indexer run (%s): %s", trigger, e)
            return
        logger.info(
            "user_id=%s was deleted while its %s pass ran; recording the pass "
            "with no owner rather than dropping it.",
            user_id,
            trigger,
        )

    # A fresh session: the first one's transaction is aborted, and every
    # statement on an aborted transaction fails with the same error.
    try:
        async with async_session() as session:
            await _insert_run_row(session, trigger, None, started_at, stats)
    except Exception as e:  # noqa: BLE001 - recording must never fail a pass
        logger.warning(
            "Could not record indexer run (%s) even with no owner: %s", trigger, e
        )


@contextlib.asynccontextmanager
async def record_indexer_run(trigger: str, user_id: int | None = None):
    """Wrap one pass; yield the `PassStats` it fills in.

    The row is written in `finally`, so **a pass that raises still records
    one** — with its `finished_at` and its error. A failed pass is precisely
    the one an operator goes looking for, and a scheme that only records
    successes has nothing to say about the case it exists for.

    `CancelledError` is the exception: lifespan shutdown cancels the indexer
    task, that is not a failed pass, and awaiting a database write inside a
    cancelled task's `finally` is not something to do on the way out. Same
    treatment `_record_index_run` gives it.
    """
    stats = PassStats()
    started_at = datetime.now(timezone.utc)
    cancelled = False
    try:
        yield stats
    except asyncio.CancelledError:
        cancelled = True
        raise
    except BaseException as exc:
        stats.record_error("pass", exc)
        # **A raising pass is never a skipped one.** `skipped` is the link
        # backfill's "this scope needed no backfill" answer, and it is set
        # *up-front* and cleared only once every guard has passed. So a guard
        # phase that raised — an unreadable root, a provenance query that
        # failed, a database blip in the `existing`/`rows` probes — left the
        # flag standing and the `finally` below suppressed the write. The one
        # pass an operator would go looking for was the one that recorded
        # nothing at all, which is the exact defect the recorder exists to
        # remove. An exception is evidence the pass ran; clearing it here means
        # nothing that raised can be filed as "did no work".
        stats.skipped = False
        raise
    finally:
        if not cancelled and not stats.skipped:
            await _write_indexer_run(trigger, user_id, started_at, stats)

# Serializes full index/embed passes so the periodic loop and a
# panel-triggered on-demand reindex can never run index_vault/embed_vault
# concurrently for the same scope. Two overlapping passes share no DB lock and
# would race on move-detection, deleted-path removal, and per-note embedding
# delete+insert (duplicate-key errors, lost/duplicated rows). Both
# `run_indexer_loop` and `_reindex_background` acquire this before doing work.
index_pass_lock: asyncio.Lock = asyncio.Lock()


class VaultRootQuarantined(RuntimeError):
    """A pass stage refused to run for a user the overlap snapshot names.

    Carries the **operator-facing** wording — both accounts, both roots, the
    relation or the errno — because the two places it lands are the ERROR log
    and `indexer_runs.error`, and both are read by the person who has to fix the
    configuration. The agent-facing refusals (`vault.VaultRootOverlap` and its
    siblings) name nothing at all; these are the other half of that split.

    A `RuntimeError`, so the per-stage `except Exception` in `_index_pass_once`
    and in the startup block record it on the run row and mark the pass failed
    without any of them learning a new type.
    """


async def detect_root_overlaps(where: str) -> None:
    """Publish a quarantine snapshot before this entry point begins a pass.

    **Called before `index_pass_lock` is taken**, deliberately: the check must
    not queue behind the pass it exists to gate. `detect_and_publish` serializes
    itself and its publication is monotonic, so two entry points overlapping
    here is ordinary rather than a race.

    A detection failure is logged and swallowed rather than aborting the caller.
    It is not a per-root failure — a root that cannot be opened is a per-user
    verdict — so the only way it raises is that the user enumeration failed,
    which means the database is unavailable and the pass is going to fail
    anyway. Swallowing it does not open the gate: either a previous snapshot
    still stands (retained, never cleared) or nothing has been published and
    `_refuse_quarantined_pass` refuses every multi-user stage until some later
    entry point publishes one.
    """
    try:
        await vault_overlap.detect_and_publish()
    except Exception as e:  # noqa: BLE001 - detection must not abort the caller
        logger.error(
            "Vault-root overlap detection failed before the %s pass "
            "(no pass will run for any assigned user until one publishes): %s",
            where,
            e,
        )


def _refuse_quarantined_pass(user_id: int | None, stage: str) -> None:
    """Refuse `stage` for a user the published snapshot names. **The one skip.**

    It lives in the shared pass helpers — `index_vault`, `link_backfill_pass`,
    `embed_vault`, `_rebuild_tsvectors_single_scope_for_tests` — rather than in each loop, so that every
    caller inherits it: the periodic tick, the startup block, the panel's
    on-demand reindex and the standalone tsvector rebuild are four entry points
    today and a fifth added later must not have to remember this. A skip
    re-implemented per loop is a skip one loop will be missing.

    Placed **ahead of `_vault_root`**, which refuses a quarantined caller too:
    that refusal carries the agent-facing wording that names nothing, and what
    the log line and the run row need is the operator-facing one that names both
    accounts and both roots.

    Never applies to `user_id is None`. Single-user mode has one root and no
    second assignment, so there is nothing to detect and nothing to be ready
    for, and a pass there behaves exactly as it did before this guard existed.
    """
    if user_id is None:
        return
    snapshot = vault_overlap.published_snapshot()
    if snapshot is None:
        # No pass may begin over a vault root that nothing has checked. This is
        # reachable only when a detection raised before publishing anything, so
        # the correct answer is to refuse and retry at the next entry point.
        raise VaultRootQuarantined(
            f"{stage} skipped for user_id={user_id}: no vault-root overlap "
            "snapshot has been published in this process, so no root has been "
            "checked."
        )
    entry = snapshot.entry_for(user_id)
    if entry is None:
        return
    raise VaultRootQuarantined(
        f"{stage} skipped: {vault_overlap.operator_text(entry)}"
    )


async def record_quarantined_runs(trigger: str) -> None:
    """Write one `indexer_runs` row per quarantined user without running a pass.

    The path a **paused** iteration takes. A pause suppresses index and embed
    work, and it must not suppress the record: a pause is entered precisely when
    an operator is doing something destructive and watching the panel, which is
    the worst moment for a quarantine to become invisible.

    **Both records, not one.** The ERROR log reaches the in-process error ring
    buffer, which is 100 entries and process-lifetime — the line naming a
    quarantine at deploy time is gone by the next restart while the
    misconfiguration persists. The run row is what an operator reads *after* a
    restart, and a pass that quietly did no work for a user is otherwise
    indistinguishable from a pass that found nothing to do. The log line is
    emitted here rather than left to the detection so that the paused path is
    self-sufficient: a caller that publishes without logging still records.

    Reads the snapshot's own recorded facts and re-reads no `users` row, so a
    peer the operator has just edited or deleted is still named.
    """
    snapshot = vault_overlap.published_snapshot()
    if snapshot is None or not snapshot.entries:
        return
    for entry in snapshot.entries.values():
        text_ = vault_overlap.operator_text(entry)
        logger.error("No pass run (%s): %s", trigger, text_)
        try:
            async with record_indexer_run(trigger, entry.user_id) as stats:
                stats.record_error("vault root", VaultRootQuarantined(text_))
        except Exception as e:  # noqa: BLE001 - a record must not stop the loop
            logger.error(
                "Failed to record the vault-root quarantine of user_id=%s: %s",
                entry.user_id,
                e,
            )


# ── the two questions `_sanitize_value` used to answer at once (#154) ───────
#
# One function fed both `notes_metadata.frontmatter` (JSONB) and `_note_title`
# (VARCHAR(512)), so its return value silently decided two different
# questions — "what may this value become inside a JSON document?" and "what
# is this note called?" — and a change made for the column re-keyed titles.
# They are separate functions now, over the one shared token helper in
# `vault`, and each says which question it answers.
#
# **Why the coercion is here and not at the parse.** `_scrub_frontmatter`'s
# predicate is "nothing can render this"; both YAML and Python render a
# non-finite float, so it keeps the float — and it must, because that parsed
# mapping is what `set_frontmatter` re-serialises. A `".nan"` string in the
# mapping would rewrite `x: .nan` to `x: '.nan'` in the note's own bytes as a
# side effect of setting an unrelated key, which is the destructive-write
# class. So each *boundary* converts instead: this module's JSONB write, this
# module's title, `vault.read_file`'s title (which `read_note` and the panel
# inherit) and `read_note`'s frontmatter view. See design D10 and
# `docs/architecture/indexing-and-embeddings.md`.


def _jsonb_value(v):
    """Recursively coerce a frontmatter value into a JSON-serializable form.

    The answer to "what may this value become inside the `frontmatter` JSONB
    document?", and nothing else. Lists and dicts are walked
    element-by-element; non-string dict keys and any non-serializable scalar
    (e.g. a YAML date/datetime) are stringified.

    **A non-finite float becomes its canonical YAML token here, ahead of the
    finite-float passthrough.** `json.dumps` — which is what SQLAlchemy hands
    the driver, with no `json_serializer` set on the engine — emits the bare
    tokens `NaN` / `Infinity` / `-Infinity`, none of which is JSON and all of
    which PostgreSQL's `jsonb` parser rejects. The batch upsert has no per-note
    retreat, so one such note raised inside the batch, aborted the pass's
    single transaction, committed nothing, left every `content_hash` where it
    was and made every subsequent tick retry the same fatal batch: indexing
    dead for the whole owner because of one note (#154, #126's failure mode by
    a new route).

    **Keys take the same token, and the first key wins a post-coercion
    collision.** `.nan: 1` beside `".nan": 2` renders one JSON key; today's
    dict comprehension silently kept the *last*, which is an accident of
    iteration order rather than a decision. The index has no channel through
    which to report the loss and must never fail the pass, so a deterministic,
    documented winner is the whole available remedy. The read view, which
    *can* report a loss, omits the view whole instead (design D10, L14).
    """
    token = non_finite_token(v)
    if token is not None:
        return token
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    elif isinstance(v, list):
        return [_jsonb_value(i) for i in v]
    elif isinstance(v, dict):
        out: dict = {}
        for k, val in v.items():
            rendered = canonical_key(k)
            if rendered in out:
                continue  # first key wins, stated rather than inherited
            out[rendered] = _jsonb_value(val)
        return out
    else:
        return str(v)


def _sanitize_frontmatter(fm: dict) -> dict:
    """Convert non-JSON-serializable values (dates, non-finite floats) to strings."""
    return _jsonb_value(fm)


def _note_title(frontmatter: dict, filename: str) -> str:
    """Frontmatter `title` coerced to a bounded string, or the filename stem.

    The answer to "what is this note called?", and nothing else — the other
    half of the split above.

    YAML parses `title: 2026-08-25` into a date and `title: [a, b]` into a
    list, and nothing bounds the length; `notes_metadata.title` is
    VARCHAR(512). An unsanitized value makes the whole batch INSERT raise,
    aborting the pass transaction — and since nothing commits, the content
    hash never advances and every subsequent tick retries the same fatal
    batch forever (#126).

    The rule itself lives in `vault.note_title` (#154, design D10b) because it
    is shared: **this** behaviour — the container stringification, the falsy
    fallback to the stem, the 512-character bound, plus the non-finite token —
    is the canonical one, and `read_note` and the control panel adopt it from
    there. This function stays as the indexer's name for it, so both move
    paths and the batch keep one call site each.
    """
    return note_title(frontmatter, filename)

logger = logging.getLogger(__name__)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
# The extraction-version marker (#150)
# ══════════════════════════════════════════════════════════════════════════
#
# Links, tags and vectors are all derived through the shared fence recognizer,
# and re-derivation is gated on `content_hash` — which a grammar change cannot
# move, because it hashes the note's bytes and those are unchanged. So a
# grammar change ships a marker instead: `notes_metadata.extraction_version`
# (migration 018) records which grammar derived a row, and a row whose marker
# is behind this constant is treated as changed for the whole pass — parsed,
# re-tagged, re-linked, re-tsvectored and re-stamped.
#
# **Bump this in the same commit as any change to the fence grammar — or to
# the link grammar.** Both are invisible to `content_hash` for the same reason
# (the bytes on disk do not move) and both leave derived state stale in the
# same way, so both are the marker's business. A grammar change without a bump
# leaves every unchanged note deriving from the old grammar forever; a bump
# without a grammar change costs one no-op pass.
#
# Version 2 is a **link**-grammar bump (#180/#203): the wikilink and
# markdown-link grammars became linear and bounded, so every note's
# `note_links` rows must be re-derived once. The *fence* grammar did not
# change, so version 2's cleaning function is version 1's under a new key —
# `_grammar_changed_the_embedding_text` therefore compares equal for every v1
# row and **no note is re-embedded** on account of this bump. The pass
# re-extracts links and tags for every note once and re-stamps the marker.
#
# Embedding invalidation is scoped, not blanket: the pass compares the text the
# row's *stamped* version would have embedded against the text the current one
# embeds (`clean_at_version`, whose per-version registry keeps every version's
# whole cleaning function frozen while a row still carries it) and clears
# `embedded_content_hash` only when they differ. **Cleaned output, not
# recognised spans** — v0's cleaner substituted sequentially, so span equality
# is neither necessary nor sufficient for embedded-text equality. It only ever
# CLEARS, so it can never suppress an invalidation another rule mandates — a
# content change, a `file_path` change, a provider change, exclusion
# reconciliation.
CURRENT_EXTRACTION_VERSION = 2


def _grammar_changed_the_embedding_text(stamped_version: int, body: str) -> bool:
    """Would this note's `clean_for_embedding` output differ across versions?

    An unknown stamped version — a build downgraded past a bump — answers True:
    a row whose grammar this build cannot reproduce must be re-embedded rather
    than certified against a comparison that was never made.
    """
    if stamped_version == CURRENT_EXTRACTION_VERSION:
        return False
    was = clean_at_version(stamped_version, body)
    now = clean_at_version(CURRENT_EXTRACTION_VERSION, body)
    return was is None or now is None or was != now


# ══════════════════════════════════════════════════════════════════════════
# The keyword vector: attempt the whole note, retreat per note (#127, D4)
# ══════════════════════════════════════════════════════════════════════════
#
# Both tsvector writers used to bind `content[:100000]` unconditionally, so
# every term past 100,000 characters was invisible to `keyword_search` — for a
# note the tool cheerfully reported on, with no indication that it had only
# been half read. Simply removing the slice is not the fix: PostgreSQL rejects
# a tsvector larger than 1 MiB, and an uncaught statement error aborts the pass
# transaction, so nothing commits, no content hash advances, and the same fatal
# batch is retried on every tick for ever — the #126 freeze class.
#
# So the write attempts the full body and retreats by halving, each attempt in
# its own savepoint, down to a floor of **exactly** the old 100,000 characters.
TSVECTOR_CONTENT_FLOOR_CHARS = 100_000


async def write_tsvector_bounded(
    session, statement, content: str, params: dict, *, label: str
) -> int:
    """Run one `content_tsvector` UPDATE, retreating on failure. Returns the
    prefix length that succeeded.

    `statement` binds the body under `:content`; `params` carries everything
    else (the FTS config names, the row key). Two properties are load-bearing:

    **The `try` sits outside `async with session.begin_nested()`**, so a
    database error unwinds the savepoint through the context manager's own
    `__aexit__` rollback *before* the `except` body runs. Catching inside the
    block would leave the outer transaction in the driver's aborted state and
    every later statement — the retry included — would fail with it.

    **A failure at the floor propagates**, and that is deliberately the
    pre-change behaviour rather than a new escape hatch. A floor statement that
    fails here also failed before this change, and size-class failures cannot
    reach it (a 100,000-character input cannot exceed the 1 MiB tsvector
    limit). The first draft had a skip list instead; review showed it stranded
    a committed `content_hash` beside a stale keyword vector, permanently — the
    note would never be selected again and `keyword_search` would answer from
    content it no longer has.

    The two call sites carry different, individually stated guarantees: the
    incremental pass commits nothing on a floor failure, so the note is retried
    next tick; `_rebuild_tsvectors_single_scope_for_tests` is atomic, so a floor failure rolls the whole
    rebuild back and surfaces to the operator who invoked it.

    Returns `(prefix_length, rowcount)`. **The rowcount is not something this
    helper acts on, deliberately.** `_rebuild_tsvectors_single_scope_for_tests` addresses its UPDATE by
    a certified predicate (id + owner + path + hash), so a zero-row result
    means the row moved or its content advanced under the rebuild — staleness,
    which halving the prefix cannot fix and must never be retried here. It is
    handed back so the caller routes it to its own re-read-or-abort path.
    """
    length = len(content)
    while True:
        try:
            async with session.begin_nested():
                result = await session.execute(
                    statement, {**params, "content": content[:length]}
                )
                rowcount = result.rowcount
        except Exception as exc:
            if length <= TSVECTOR_CONTENT_FLOOR_CHARS:
                # The floor. Propagate exactly as the pre-change code did.
                logger.exception(
                    "Failed to update the keyword vector for %s at %d "
                    "characters, at or below the %d-character floor; "
                    "propagating, as the pre-change implementation did",
                    label, length, TSVECTOR_CONTENT_FLOOR_CHARS,
                )
                raise
            attempted, length = length, max(
                length // 2, TSVECTOR_CONTENT_FLOOR_CHARS
            )
            logger.warning(
                "Keyword vector for %s did not build at %d characters (%s); "
                "retreating to a %d-character prefix. Terms past that prefix "
                "are not searchable for this note.",
                label, attempted, exc, length,
            )
            continue
        return length, rowcount


# ══════════════════════════════════════════════════════════════════════════
# Index provenance (issue #91, migration 016)
# ══════════════════════════════════════════════════════════════════════════
#
# The question this record answers is **"did the assignment change?"**, not
# "is this the same directory?". The event it exists to detect is an operator
# repointing a user at another vault, which is a change to a value this system
# itself stores and writes; detecting that is exact and no input defeats it.
# Proving directory identity across time is a different and unwinnable
# question — a bit-identical clone of a filesystem presents the same inode
# numbers, generation counters and therefore the same file handles, at the same
# pathname, under the same assignment — and **filesystem substitution behind an
# unchanged assignment is a declared non-goal**. Do not add a heuristic for it
# (content overlap, path overlap, a mount identifier, a filesystem UUID): its
# failure direction is a silent keep on two vaults that merely resemble each
# other, and three review rounds rejected exactly that escalation.

# Verdicts. Total over every combination of inputs; see `classify_provenance`.
PROVENANCE_KEEP = "same_assignment"
PROVENANCE_REDERIVE = "provenance_unresolved"
PROVENANCE_DISCARD = "reassigned"
PROVENANCE_INDETERMINATE = "indeterminate"

# `struct file_handle`'s payload bound (linux/fs.h). A handle is at most this
# many opaque bytes; on ext4 and xfs it is eight — the inode number plus the
# inode's generation counter, which the kernel bumps precisely so a reused
# inode is not mistaken for the old one.
MAX_HANDLE_SZ = 128
AT_EMPTY_PATH = 0x1000

# The width of `users.indexed_vault_handle`. A token that will not fit is
# treated as unobtainable — recorded NULL — never truncated: a truncated token
# compared by byte equality is a signal that can produce a spurious match.
HANDLE_COLUMN_CHARS = 320

# How many offending paths an incomplete re-derive names before it summarises
# the remainder. 013's and 015's offender-report shape.
SKIP_REPORT_LIMIT = 20


class _FileHandle(ctypes.Structure):
    _fields_ = [
        ("handle_bytes", ctypes.c_uint),
        ("handle_type", ctypes.c_int),
        ("f_handle", ctypes.c_ubyte * MAX_HANDLE_SZ),
    ]


def _resolve_name_to_handle_at():
    """The glibc `name_to_handle_at` wrapper, or None if there is none.

    **Wrapper-first and wrapper-only**, unlike `vault_fs`'s `renameat2` shim:
    glibc has exported `name_to_handle_at` since 2.14 and it resolves on every
    version this project runs on, so there is no raw-syscall fallback and no
    architecture number table. A missing symbol is simply "no handle
    available" — not an error, not a guess, and not a degraded mode.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:  # pragma: no cover - no libc to bind against
        return None
    try:
        fn = libc.name_to_handle_at
    except AttributeError:  # pragma: no cover - glibc < 2.14
        return None
    fn.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.POINTER(_FileHandle),
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_int,
    ]
    fn.restype = ctypes.c_int
    return fn


_name_to_handle_at_cache: tuple | None = None


def _name_to_handle_at_fn():
    """Cached `_resolve_name_to_handle_at`; the lookup is per-process."""
    global _name_to_handle_at_cache
    if _name_to_handle_at_cache is None:
        _name_to_handle_at_cache = (_resolve_name_to_handle_at(),)
    return _name_to_handle_at_cache[0]


def read_dir_handle(fd: int) -> str | None:
    """An opaque `"<handle_type>:<hex>"` token for the pinned directory, or None.

    **Best-effort hardening in the refusing direction only.** Where a handle is
    recorded for a user *and* one can be read now, and the two differ, a
    verdict that would otherwise be *keep* is demoted to *re-derive*. A
    matching handle grants nothing. Every "no": `EOPNOTSUPP` (procfs, sysfs,
    overlayfs and some FUSE mounts), `ENOSYS`, a missing symbol, an oversized
    payload — all return None, record NULL, log nothing and change no verdict.

    The token is **opaque**: compared by byte equality, never parsed, and
    **never fed to `open_by_handle_at`**, which needs `CAP_DAC_READ_SEARCH`
    that the container does not have. A handle is a value to compare, never a
    door to open.

    `mount_id` is deliberately ignored. It is not stable across a remount, and
    the handle bytes for one directory are identical on the host and inside a
    bind-mounting container whose `mount_id` differs.
    """
    fn = _name_to_handle_at_fn()
    if fn is None:  # pragma: no cover - glibc always has it in practice
        return None
    fh = _FileHandle()
    fh.handle_bytes = MAX_HANDLE_SZ
    mount_id = ctypes.c_int()
    ctypes.set_errno(0)
    rc = fn(fd, b"", ctypes.byref(fh), ctypes.byref(mount_id), AT_EMPTY_PATH)
    if rc != 0:
        # The errno is read the way `vault_fs._renameat2_raw` reads it, and
        # then deliberately **not** branched on. `EOPNOTSUPP`, `ENOSYS`,
        # `EOVERFLOW` and anything else all mean the same thing here — no
        # hardening signal for this root — and distinguishing them would only
        # invite a degraded mode that the design does not have. Bound so a
        # future reader sees the value was considered rather than dropped.
        _errno = ctypes.get_errno()
        del _errno
        return None
    size = int(fh.handle_bytes)
    if size < 0 or size > MAX_HANDLE_SZ:  # pragma: no cover - kernel contract
        return None
    token = f"{int(fh.handle_type)}:{bytes(fh.f_handle[:size]).hex()}"
    if len(token) > HANDLE_COLUMN_CHARS:  # pragma: no cover - 320 > any real handle
        return None
    return token


def encode_realpath(realpath: str) -> str:
    """`os.fsencode(realpath).hex()` — the form the record stores and compares.

    A POSIX pathname is an arbitrary sequence of non-NUL bytes under no
    obligation to be valid UTF-8, and Python decodes such a component with
    `surrogateescape`, so `os.path.realpath` can return a string carrying a
    lone surrogate like `'\\udcff'` that asyncpg cannot UTF-8-encode. The
    discard branch writes this value *and* the delete in **one** transaction,
    so an encode failure here would roll the delete back on every later pass
    and serve the former vault's index forever — #91's own symptom, produced by
    a value domain. Hex has no unrepresentable input, so the column is total
    over the fact by construction rather than by a bound.

    Comparison is **encode-then-compare on both sides**: never decode the
    stored value in order to compare it. `decode_realpath` exists only to
    render it in a log.
    """
    return os.fsencode(realpath).hex()


def decode_realpath(stored: str) -> str:
    """The inverse of `encode_realpath`, for **log rendering only**.

    `os.fsdecode(bytes.fromhex(stored))` returns the observed string exactly,
    surrogates included. Never call this to compare two provenances.
    """
    return os.fsdecode(bytes.fromhex(stored))


@dataclass(frozen=True)
class RootFacts:
    """The three provenance facts, all observed at one moment from one pinned
    descriptor.

    `realpath` is kept beside `realpath_hex` for logging; only `realpath_hex`
    is ever compared or stored.
    """

    assignment: str
    realpath: str
    realpath_hex: str
    handle: str | None


@dataclass(frozen=True)
class Classification:
    """One of the four verdicts, plus the human-readable reason for the log."""

    verdict: str
    reason: str


@contextlib.contextmanager
def pinned_root(vault: Path) -> Iterator[int]:
    """Open the assigned root once and hold it for the pass.

    **What the pin buys is deliberately narrow.** Within one pass, the facts
    observed, the files discovered and the bytes read all come from **one
    inode**, so a pass cannot record provenance describing a directory it did
    not scan. It does **not** prove the pinned directory is the one earlier
    rows came from; nothing proves that.

    Observing facts through a pathname and then scanning that pathname is
    check-then-act, and the interval is exploitable in both directions: an
    assignment naming a symbolic link can be retargeted after the observation
    and before the scan, so the pass indexes one directory and records another,
    and retargeting it back before the following pass leaves that record
    standing over rows the pass never derived from it. A directory descriptor
    keeps naming the same directory however its pathname is later renamed or
    relinked — which is why the mutation path is already anchored this way
    (#59).

    An unopenable root raises, which is the **indeterminate** verdict's
    "nothing at all, and the pass fails": no delete, no record. That is a
    change from the pathname-based scan, where `Path.rglob` on a missing
    directory silently yielded nothing and the ordinary prune then deleted
    every row the user had.
    """
    fd = os.open(vault, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        yield fd
    finally:
        os.close(fd)


def observe_root_facts(vault: Path, root_fd: int) -> RootFacts | None:
    """The three facts for the pinned root, or None when they are indeterminate.

    The realpath is bound to the descriptor the way #59's
    `_require_same_directory` binds its root: `os.stat(os.path.realpath(vault))`
    must report the same `(st_dev, st_ino)` as `os.fstat(root_fd)`. **That is
    the only use of device and inode numbers in this design** — a
    within-one-moment check that the realpath being recorded describes the
    inode being pinned. They are never stored and never compared across passes,
    because a reused inode makes two different directories agree.

    A disagreement is *indeterminate*, not a mismatch: the root's pathname is
    moving under the pass, and nothing observed can be trusted to describe what
    was scanned.
    """
    try:
        pinned = os.fstat(root_fd)
        realpath = os.path.realpath(vault)
        named = os.stat(realpath)
    except OSError:
        return None
    if (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino):
        return None
    return RootFacts(
        assignment=canonical_vault_root(vault),
        realpath=realpath,
        realpath_hex=encode_realpath(realpath),
        handle=read_dir_handle(root_fd),
    )


def classify_provenance(
    recorded_assignment: str | None,
    recorded_realpath_hex: str | None,
    recorded_handle: str | None,
    facts: RootFacts | None,
) -> Classification:
    """The six-row classification, total over every combination of inputs.

    | observed vs. recorded | verdict |
    | --- | --- |
    | the root cannot be opened, or its realpath no longer names the pinned inode | **indeterminate** — nothing at all |
    | no record present (both null, or a half-set record) | **re-derive** |
    | assignment equal and realpath equal, no observable handle mismatch | **keep** |
    | assignment equal and realpath equal, but the recorded and observed handles differ | **re-derive** |
    | assignment differs **and** realpath differs | **discard** |
    | exactly one of assignment and realpath differs | **re-derive** |

    A record counts as **present** only when both the assignment string and the
    realpath are non-null. Both are always observable for a root the pass could
    pin, so a half-set record is drift rather than a state this code writes —
    and the safe reading of drift is that nothing is known, not that the half
    that is set may be trusted.

    A handle mismatch is **observable** only when a handle is recorded *and*
    one was read now. Either being absent means there is nothing to observe,
    **not** a degraded mode: the pass decides on the other two facts, does not
    re-derive on that account, and says nothing to the operator.

    Which error this prefers, said plainly. Ambiguity never resolves toward
    *keeping*, because silently wrong search results are the failure this
    product ranks highest — an agent acts on them without a human seeing the
    query. Ambiguity never resolves toward *discarding* either, because a
    discard costs a full re-embed of the vault. Everything between goes to a
    branch that asserts nothing and destroys nothing, and only **unanimous**
    disagreement destroys.

    This is the one function that computes "settled", so the scan and the gated
    ancillary passes cannot come to mean two different things by it.
    """
    if facts is None:
        return Classification(
            PROVENANCE_INDETERMINATE,
            "the assigned root could not be pinned, or its real path no longer "
            "names the directory that was pinned",
        )

    present = recorded_assignment is not None and recorded_realpath_hex is not None
    if not present:
        half = recorded_assignment is not None or recorded_realpath_hex is not None
        return Classification(
            PROVENANCE_REDERIVE,
            "a half-set provenance record is no record at all"
            if half
            else "no provenance is recorded for this user",
        )

    assignment_equal = recorded_assignment == facts.assignment
    realpath_equal = recorded_realpath_hex == facts.realpath_hex

    if assignment_equal and realpath_equal:
        # The handle can refuse a keep. It can never establish one, and it can
        # never establish a discard.
        if (
            recorded_handle is not None
            and facts.handle is not None
            and recorded_handle != facts.handle
        ):
            return Classification(
                PROVENANCE_REDERIVE,
                "the assignment and the real path agree but the recorded file "
                "handle does not match the one read now — the directory was "
                "probably replaced at the same path",
            )
        return Classification(
            PROVENANCE_KEEP, "the assignment and the real path are unchanged"
        )

    if not assignment_equal and not realpath_equal:
        return Classification(
            PROVENANCE_DISCARD,
            f"the assignment changed from {recorded_assignment!r} to "
            f"{facts.assignment!r} and the real path changed with it",
        )

    if assignment_equal:
        return Classification(
            PROVENANCE_REDERIVE,
            "the assignment is unchanged but the real path it names differs "
            "from the one recorded",
        )
    return Classification(
        PROVENANCE_REDERIVE,
        f"the assignment changed from {recorded_assignment!r} to "
        f"{facts.assignment!r} while the real path it names is unchanged",
    )


async def _read_recorded_provenance(session, user_id: int):
    """`(assignment, realpath_hex, handle)` for a user, all None if no row."""
    row = (
        await session.execute(
            select(
                User.indexed_vault_assignment,
                User.indexed_vault_realpath,
                User.indexed_vault_handle,
            ).where(User.id == user_id)
        )
    ).first()
    if row is None:
        return None, None, None
    return row[0], row[1], row[2]


async def _stamp_provenance(session, user_id: int, facts: RootFacts) -> int:
    """Write **all three** facts, NULL for anything not observed.

    There is no partial stamp. No branch may update one column and leave
    another describing a root it does not describe — that single rule is what
    makes a later observation safe to compare, because it can never be measured
    against a root the stamp did not cover. In particular a stamp taken with no
    handle available NULLs a previously recorded handle rather than leaving it
    beside a freshly observed pathname pair.

    Returns the number of rows the UPDATE touched. **Every caller checks it is
    exactly one**: a stamp that wrote no row is a provenance record that does
    not exist, and on the discard path the delete beside it must not stand
    without it. Does not commit: the caller decides which transaction this
    belongs to, and on the discard path that is emphatically the same one as
    the delete.
    """
    result = await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            indexed_vault_assignment=facts.assignment,
            indexed_vault_realpath=facts.realpath_hex,
            indexed_vault_handle=facts.handle,
        )
    )
    return result.rowcount


class ProvenanceRaceAborted(RuntimeError):
    """The assignment moved between the classification and the act.

    Raised by `_assert_still_assigned` when the locked, freshly read
    `users.vault_path` no longer equals the assignment the classification was
    computed against. Nothing has been deleted and nothing stamped; the pass
    aborts and the next one reclassifies against whatever the row says then.
    """


class ProvenanceLockUnavailable(RuntimeError):
    """Somebody else holds the `users` row, and we refused to wait for it.

    Raised by `_assert_still_assigned(nowait=True)`. The tail stamp cannot
    *wait* for that lock: it already holds this pass's `notes_metadata` row
    locks, and a permanent user delete takes the parent first and then cascades
    to exactly those children, so waiting closes a cycle and PostgreSQL aborts
    one of the two — possibly the operator's delete. Withholding the stamp
    costs one more re-derive; taking the deadlock costs an aborted pass or an
    aborted panel action.
    """


# PostgreSQL SQLSTATE for `lock_not_available` — what `FOR UPDATE NOWAIT`
# raises when the row is already locked. Matched on the code rather than on
# asyncpg's exception class so the driver stays swappable.
LOCK_NOT_AVAILABLE = "55P03"


def _is_lock_not_available(exc: BaseException) -> bool:
    """Whether `exc` (however many layers it is wrapped in) is 55P03.

    The error arrives wrapped twice and the layers carry different things, the
    same way `_log_usage`'s foreign-key recovery has to walk them: SQLAlchemy's
    `.orig` is the asyncpg *dialect's* error and its `__cause__` is asyncpg's
    own. Walk both chains and take the first SQLSTATE we find.
    """
    seen = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        code = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if code == LOCK_NOT_AVAILABLE:
            return True
        stack.extend(
            [
                getattr(current, "orig", None),
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            ]
        )
    return False


async def _assert_still_assigned(
    session, user_id: int, facts: RootFacts, *, nowait: bool = False
) -> None:
    """Lock the user row and prove it still names the assignment we classified.

    **The classification is not, by itself, a licence to act on it.** `facts`
    is computed from a `vault` path that came out of the process cache, and the
    branches that act on it — the discard's delete, and either provenance stamp
    — run in a *later* transaction. In between, an administrator can reassign,
    or correct a reassignment back to the root the index was actually built
    from. The reviewer's failing input is the second one: the pass classifies A
    against B as a discard, the administrator puts the assignment back to A,
    and the discard then deletes a complete, valid A index and records
    provenance B beside an empty one.

    So the act is bound to the assignment that produced it. `SELECT … FOR
    UPDATE` takes the row, and under READ COMMITTED the lock wait re-reads the
    latest committed version, so what comes back is the assignment as of the
    moment we hold it — not a snapshot taken before a concurrent commit. It is
    held for the rest of the transaction, so the delete and the stamp beside it
    cannot straddle a change either.

    An inactive user, a deleted row, a cleared `vault_path` or a different one
    all abort: none of them is the state the classification described.

    **`nowait` is a lock-ordering requirement, not a tuning knob.** The discard
    takes this lock in its own transaction *before* it touches a single child
    row, which is the panel's own parent-then-child direction and therefore
    safe to wait on. The re-derive's tail stamp cannot: it runs at the end of
    the pass's transaction, holding `notes_metadata` row locks, and a permanent
    user delete locks `users` first and then waits on exactly those children —
    a cycle PostgreSQL resolves by aborting one side, possibly the operator's
    delete. The tail therefore asks with `NOWAIT` inside a savepoint and treats
    contention as "withhold the stamp", a state that branch already knows how
    to be in.
    """
    statement = (
        select(User.vault_path, User.is_active)
        .where(User.id == user_id)
        .with_for_update(nowait=nowait)
    )
    try:
        row = (await session.execute(statement)).first()
    except Exception as exc:
        if nowait and _is_lock_not_available(exc):
            raise ProvenanceLockUnavailable(
                f"the users row for user_id={user_id} is locked by another "
                "transaction, and this one already holds notes_metadata row "
                "locks, so waiting for it could deadlock with a user deletion"
            ) from None
        raise
    if row is None:
        raise ProvenanceRaceAborted(
            f"user_id={user_id} no longer exists"
        )
    if not row.is_active:
        raise ProvenanceRaceAborted(
            f"user_id={user_id} is no longer active"
        )
    if row.vault_path is None:
        raise ProvenanceRaceAborted(
            f"the vault assignment for user_id={user_id} has been cleared"
        )
    current = canonical_vault_root(row.vault_path)
    if current != facts.assignment:
        raise ProvenanceRaceAborted(
            f"the vault assignment for user_id={user_id} is now "
            f"{current!r}, not the {facts.assignment!r} this pass classified"
        )


async def classify_for_pass(session, user_id: int, vault: Path, root_fd: int):
    """`(Classification, RootFacts | None, recorded_triple)` for a pinned root.

    The one entry point the scan and both gated ancillary passes use, so
    "settled" cannot come to mean two different things in two places. The
    recorded triple comes back with it so a discard can log what it is
    replacing without issuing a second SELECT.
    """
    facts = observe_root_facts(vault, root_fd)
    recorded = await _read_recorded_provenance(session, user_id)
    return classify_provenance(*recorded, facts), facts, recorded


def describe_recorded(recorded) -> str:
    """The recorded provenance as an operator reads it, in a log line.

    The realpath is stored hex-encoded and is **decoded only here** — never in
    order to compare it. `decode_realpath` is lossless, surrogates included, so
    a pathname that cannot be spelled in UTF-8 still renders.
    """
    assignment, realpath_hex, handle = recorded
    if assignment is None and realpath_hex is None:
        return "no record"
    try:
        realpath = repr(decode_realpath(realpath_hex)) if realpath_hex else "none"
    except ValueError:  # pragma: no cover - a hand-edited column
        realpath = f"<undecodable: {realpath_hex!r}>"
    return (
        f"assignment={assignment!r} realpath={realpath} "
        f"handle={handle if handle is not None else 'none'}"
    )


def _format_skips(skips: list[str]) -> str:
    """013's and 015's offender-report shape: the first N, then a count.

    What is *not* in here: a note whose link extraction was truncated at
    `MAX_LINKS_PER_NOTE`. A skip withholds a re-derive's certification, and a
    capped note is not a skip — see the carve-out at the `skips` declaration in
    `_index_vault_pinned`. Its degradation is carried on the row
    (`notes_metadata.links_truncated`) and in an ERROR line, not here.
    """
    shown = skips[:SKIP_REPORT_LIMIT]
    suffix = (
        f", and {len(skips) - len(shown)} more"
        if len(skips) > len(shown)
        else ""
    )
    return ", ".join(shown) + suffix


# ══════════════════════════════════════════════════════════════════════════
# The anchored, read-only walk
# ══════════════════════════════════════════════════════════════════════════
#
# Deliberately **not** a `vault_fs` helper, and that is a design decision
# rather than an ownership one. `vault_fs` is the *mutation* primitive module:
# every helper in it writes or refuses, and its containment contract forbids a
# symbolic link anywhere in the path, ever. The indexer needs the opposite leaf
# policy and must keep it — a markdown file reached through a symbolic link is
# indexed today. A shared helper would have to fork its symlink policy per
# caller, and a future editor unifying the two forks would silently change
# either what the index contains or what a transfer may write. Two walks with
# two policies, in the two modules that own those policies.


@dataclass(frozen=True)
class DiscoveredFile:
    """One discovered note, addressed by its **parent descriptor** and name.

    The parent descriptor is open only while the consumer is being handed this
    entry: the walk closes each directory once its children are done, so it
    costs one descriptor per level of depth rather than one per file. Read the
    file *now*, through `read_note_at`, or not at all.
    """

    rel: str
    parent_fd: int
    name: str


def discover_markdown_files_at(
    root_fd: int, *, skips: list[str] | None = None
) -> Iterator[DiscoveredFile]:
    """Walk the pinned root depth-first, yielding every indexable note.

    The same rule `Path.rglob` applied, now enforced by the kernel per descent
    rather than by a library's traversal habit:

    - dot-directories are skipped (`.obsidian`, `.git`, `.trash`, `.smart-env`);
    - directory symbolic links are **not** descended — `O_DIRECTORY |
      O_NOFOLLOW` per descent, and the resulting `ELOOP`/`ENOTDIR` is a
      deliberate non-descent, **not** a skip;
    - a symbolic link at a discovered `.md` file is left alone here and read as
      it is today (see `read_note_at`), because anchoring is about *which
      directory is scanned* and must not change what the index contains.

    A directory that could not be opened for any *other* reason is a genuine
    skip and is appended to `skips` when one is supplied — a re-derive that
    could not visit a subtree has not visited the root it is about to certify.
    """

    def walk(parent_fd: int, prefix: str) -> Iterator[DiscoveredFile]:
        try:
            with os.scandir(parent_fd) as entries:
                children = list(entries)
        except OSError as e:
            if skips is not None:
                skips.append(f"{prefix or '.'} (directory: {e})")
            return
        # Sorted so a pass's discovery order is stable, which keeps the
        # offender report and the move-detection pairing reproducible.
        for entry in sorted(children, key=lambda e: e.name):
            name = entry.name
            if name.startswith("."):
                continue
            rel = f"{prefix}/{name}" if prefix else name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as e:  # pragma: no cover - dirent type is cached
                if skips is not None:
                    skips.append(f"{rel} ({e})")
                continue
            if is_dir:
                try:
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=parent_fd,
                    )
                except OSError as e:
                    # ELOOP/ENOTDIR: a directory symbolic link, or a directory
                    # that vanished. `rglob` declines to descend the former and
                    # silently drops the latter; neither is a skip.
                    if (
                        e.errno not in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT)
                        and skips is not None
                    ):
                        skips.append(f"{rel} (directory: {e})")
                    continue
                try:
                    yield from walk(child_fd, rel)
                finally:
                    os.close(child_fd)
                continue
            if not name.endswith(".md"):
                continue
            yield DiscoveredFile(rel=rel, parent_fd=parent_fd, name=name)

    yield from walk(root_fd, "")


def discover_markdown_files(vault: Path) -> dict[str, Path]:
    """Every indexable note under `vault`, as `vault-relative str -> abs Path`.

    A thin pathname-taking wrapper over `discover_markdown_files_at`: it opens
    the root, drains the walk and closes. Kept callable this way so
    `tests/test_symlink_mutation_guard.py` — which asserts what discovery finds
    under a symlinked folder — passes **unchanged**, which is how we know the
    anchoring did not change what the index contains.

    This is the single definition of "what the index contains", so it also
    decides what `notes_metadata.file_path` holds — which the write tools must
    agree with. Two properties matter:

    - dot-directories are skipped (`.obsidian`, `.git`, `.trash`, …);
    - directory symbolic links are **not** descended, so a note under a
      symlinked folder is discovered once, at its real path (`Real/A.md`),
      never at the alias (`Shared/A.md`). `open_mutable` reports that same real
      path as the target's `rel`, which is why `move_note` keys its DB updates
      on it.
    """
    with pinned_root(vault) as root_fd:
        return {
            found.rel: vault / found.rel
            for found in discover_markdown_files_at(root_fd)
        }


def read_note_at(parent_fd: int, name: str) -> tuple[str, os.stat_result]:
    """`(text, stat)` for one note, both from **one** open descriptor.

    Deliberately **no** `O_NOFOLLOW` on the leaf: a symlinked `.md` is read
    today and this change must not alter what the index contains. Containment
    at the leaf was never claimed here and `open_mutable` remains the guard
    that matters for writes.

    The size and modification time come from `os.fstat` on the descriptor whose
    bytes were just read, replacing a second, independent pathname resolution
    that could describe a different file from the one that was hashed. Not the
    reason for the anchoring; a free consequence of it.

    Text mode with the default universal-newline translation, exactly as the
    `Path.read_text` it replaces — a binary read would leave `\\r\\n` intact and
    silently change every CRLF note's `content_hash`, forcing a re-embed.
    """
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        stat = os.fstat(fd)
        with open(fd, "r", encoding="utf-8", errors="strict", closefd=False) as handle:
            return handle.read(), stat
    finally:
        os.close(fd)


def open_beneath(root_fd: int, rel_path: str) -> tuple[int, str]:
    """`(parent_fd, name)` for a vault-relative path beneath the pinned root.

    For the passes that read a note the database already named rather than one
    a walk just discovered. Descends with the walk's rule — `O_DIRECTORY |
    O_NOFOLLOW` per component — and leaves the leaf alone, so a symlinked `.md`
    still reads. The caller owns `parent_fd` and must close it.
    """
    parts = [p for p in rel_path.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        raise OSError(f"not a vault-relative path: {rel_path!r}")
    fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def read_note_beneath(root_fd: int, rel_path: str) -> tuple[str, os.stat_result]:
    """`read_note_at` for a path the caller names rather than one it walked."""
    parent_fd, name = open_beneath(root_fd, rel_path)
    try:
        return read_note_at(parent_fd, name)
    finally:
        os.close(parent_fd)


async def _reconcile_provenance(
    user_id: int, vault: Path, root_fd: int, log_suffix: str
) -> tuple[bool, RootFacts | None]:
    """Classify the pinned root and act, **before any file under it is read**.

    Returns `(re_derive, facts)`. `facts` is what a later stamp must write; it
    is None only when there is nothing to stamp.

    - **keep** — nothing at all, and nothing to stamp.
    - **discard** — delete the user's `notes_metadata` (embeddings cascade,
      links cascade on `source_note_id` and null out on `target_note_id`) and
      stamp the new provenance **in one committed transaction**, so no pass can
      leave rows from one vault beside a record naming another. That
      transaction first locks the `users` row and proves it still names the
      assignment this verdict was computed against (`_assert_still_assigned`),
      because the classification ran earlier, against a cached root, and an
      administrator correcting a reassignment back in between would otherwise
      have a valid index deleted underneath them. The pass then indexes the new
      root ordinarily: the index is empty, so there is nothing to re-derive,
      and a pass that fails after this retries cleanly because the next one
      finds both facts in agreement.
    - **re-derive** — no delete and no stamp here; the stamp is withheld until
      the pass has finished, and only if it raised nothing *and* skipped
      nothing.
    - **indeterminate** — nothing at all, and the pass fails, because an index
      cannot be re-derived from a directory that cannot be read and destroying
      one because a bind mount was briefly unavailable buys nothing and costs
      the full re-embed.
    """
    async with async_session() as session:
        classification, facts, recorded = await classify_for_pass(
            session, user_id, vault, root_fd
        )

    if classification.verdict == PROVENANCE_INDETERMINATE:
        raise RuntimeError(
            f"Index provenance indeterminate{log_suffix}: {classification.reason}. "
            "Nothing was deleted and no provenance was recorded."
        )

    if classification.verdict == PROVENANCE_KEEP:
        return False, None

    assert facts is not None  # every non-indeterminate verdict observed them

    if classification.verdict == PROVENANCE_DISCARD:
        async with async_session() as session:
            # **The delete is bound to the assignment that produced the
            # verdict, not merely to the user.** The classification was
            # computed in an earlier transaction against a root that came out
            # of the process cache; between then and here an administrator can
            # have corrected the reassignment back, and the delete would then
            # destroy a complete, valid index for the assignment the row
            # currently names. Lock the row, re-read it, and abort on any
            # disagreement — nothing deleted, nothing stamped, and the next
            # pass reclassifies against whatever the row says then.
            try:
                await _assert_still_assigned(session, user_id, facts)
            except ProvenanceRaceAborted as exc:
                await session.rollback()
                logger.warning(
                    "Discard aborted%s: %s. Nothing was deleted and no "
                    "provenance was recorded; the next pass will reclassify.",
                    log_suffix,
                    exc,
                )
                raise RuntimeError(
                    f"Index provenance discard aborted{log_suffix}: {exc}. "
                    "Nothing was deleted and no provenance was recorded."
                ) from None
            result = await session.execute(
                delete(NoteMetadata).where(NoteMetadata.user_id == user_id)
            )
            stamped = await _stamp_provenance(session, user_id, facts)
            if stamped != 1:
                # The stamp must land on exactly the row we locked. Zero rows
                # means the record does not exist, and a delete standing beside
                # a missing record is the "rows from one vault beside a record
                # naming another" this branch exists to make impossible.
                await session.rollback()
                raise RuntimeError(
                    f"Index provenance discard aborted{log_suffix}: the "
                    f"provenance stamp for user_id={user_id} matched "
                    f"{stamped} row(s), not exactly one. Nothing was deleted "
                    "and no provenance was recorded."
                )
            await session.commit()
        logger.warning(
            "Vault reassignment detected%s: %s. Discarded %s notes_metadata "
            "row(s) (embeddings and links cascade). Was [%s]; now recorded "
            "[assignment=%r realpath=%r handle=%s].",
            log_suffix,
            classification.reason,
            result.rowcount,
            describe_recorded(recorded),
            facts.assignment,
            facts.realpath,
            facts.handle if facts.handle is not None else "none",
        )
        return False, facts

    logger.info(
        "Re-deriving index%s: %s. Every discovered file will be re-parsed and "
        "every link row re-extracted; note_embeddings are kept.",
        log_suffix,
        classification.reason,
    )
    return True, facts


async def index_vault(user_id: int | None = None):
    """Scan vault, upsert notes_metadata with tsvector, remove deleted files.

    Single-user mode (`user_id is None`) keeps the legacy behavior: queries
    and inserts do not filter by `user_id` (NULL passes through every guard),
    and the index-provenance record is neither read nor written — single-user
    mode has no `users` row. Multi-user mode (`user_id` int) scopes
    existing-row lookups, stamps `user_id` on every upserted row, and
    reconciles the provenance record at the head of the pass.

    **The whole pass runs beneath one pinned root descriptor** (`pinned_root`):
    the facts observed, the files discovered and the bytes read all come from
    one inode, so a pass cannot record provenance describing a directory it did
    not scan. The reconciliation lives here rather than in any one caller, so
    the startup pass, the periodic tick and an operator-triggered reindex all
    inherit it.

    Returns `(notes_scanned, notes_indexed)` for the pass recorder (#160):
    every markdown file the walk discovered, and the subset whose row this pass
    wrote — the upserts plus the moves it repaired in place. Callers that do
    not record a run may ignore it.

    Refuses outright for a user the published overlap snapshot names — see
    `_refuse_quarantined_pass`. Nothing is read, written, pruned or
    provenance-stamped for such a user; the refusal happens before the root is
    resolved.
    """
    _refuse_quarantined_pass(user_id, "index")
    vault = _vault_root(user_id)
    log_suffix = f" (user_id={user_id})" if user_id is not None else ""
    logger.info(f"Starting vault index scan...{log_suffix}")

    with pinned_root(vault) as root_fd:
        return await _index_vault_pinned(user_id, vault, root_fd, log_suffix)


async def _index_vault_pinned(
    user_id: int | None, vault: Path, root_fd: int, log_suffix: str
) -> tuple[int, int]:
    re_derive = False
    facts: RootFacts | None = None
    if user_id is not None:
        re_derive, facts = await _reconcile_provenance(
            user_id, vault, root_fd, log_suffix
        )

    # Anything the pass discovered but could not fully process. **A non-empty
    # list makes a re-derive incomplete and withholds the stamp** (A.7a): the
    # re-derive's whole claim is that every surviving row was written by this
    # pass from a file under the assigned root, and one skipped path falsifies
    # it — the ordinary prune keeps a row whose relative path exists under the
    # new root, which is exactly the row a re-derive exists to replace. The
    # repairs are still performed; only the certification is withheld.
    #
    # **Carve-out: a note whose link extraction was truncated at
    # `MAX_LINKS_PER_NOTE` is NOT a skip** (#203, D4). The claim A.7a makes is
    # structural — every surviving link row was written by this pass — and a
    # capped note does not falsify it: the truncation is deterministic, and the
    # rows written are exactly the rows derived. Treating it as a skip would
    # instead hold a tenant with one generated MOC in re-derive mode
    # indefinitely, with no repair that could ever end it: a self-inflicted DoS
    # on the very machinery that exists to detect foreign rows. The degradation
    # is declared elsewhere and durably — `notes_metadata.links_truncated` on
    # the row, one ERROR line per capped note per pass, and `truncated: true`
    # from `get_links` — so it is visible without being fatal.
    skips: list[str] = []

    async with async_session() as session:
        # ── The generation lock, at the HEAD of this transaction ───────────
        # **Not at the tsvector write, and this is a deadlock regression, not
        # a matter of taste** (D7c3). This pass is one transaction and it takes
        # row locks long before it reaches the keyword vector: the id-preserving
        # move UPDATE and its `note_links.target_path` rewrite, the changed-note
        # upsert, the grammar-invalidation `UPDATE … SET
        # embedded_content_hash = NULL`, the prune DELETE and the
        # `note_links` delete-and-insert. Acquiring the advisory lock at the
        # tsvector write would leave this pass holding those row locks while it
        # waited for the lock, and the rebuild driver holding the lock while it
        # waited for those rows — a cycle the database resolves by killing one
        # side, and a direct violation of "the advisory lock before any row or
        # table lock".
        #
        # The rule is therefore a property of the **transaction**: a
        # transaction that will write any configuration-dependent derived row
        # takes the lock and re-validates the fingerprint before its first
        # row-locking mutation. Anyone adding a mutation to this function must
        # keep it below this line — auditing what the transaction touches, not
        # reasoning backwards from the write that consumes the fingerprint.
        #
        # `_reconcile_provenance` above is not in scope: it runs in its own
        # session and its transaction has committed before this one opens.
        #
        # The consequence is accepted and documented (L5b): the pass holds the
        # lock for its whole transaction — minutes on a large vault — so
        # `make reset-embeddings` and `make rebuild-tsvectors` *wait* for an
        # in-flight pass instead of interleaving with it. That is the required
        # behaviour, and those paths deliberately do not defeat it with a short
        # `lock_timeout`.
        #
        # The *unbounded* form, and it is the same argument in the other
        # direction: this pass is on the waiting side whenever a rebuild or a
        # reset already holds the lock, and under the engine's 60s
        # `statement_timeout` the wait was cancelled rather than served. That
        # failure direction was safe — the pass aborts, commits nothing and
        # retries next tick — but "waits" is the documented contract on both
        # sides of this lock, and a pass that abandons every tick for the
        # duration of a long rebuild writes an `indexer_runs` error row per
        # tick for a database that is merely busy. The raise is a `SET LOCAL`,
        # which takes no row or table lock, so it sits ahead of the acquisition
        # without touching the ordering rule; the timeout is restored before
        # the first mutation below.
        await acquire_generation_lock_unbounded(session)
        await _assert_fts_generation_current(session)

        # Get existing hashes (scoped to this user when set)
        existing_stmt = select(
            NoteMetadata.file_path,
            NoteMetadata.content_hash,
            NoteMetadata.extraction_version,
        )
        if user_id is None:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id.is_(None))
        else:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id == user_id)
        existing_rows = (await session.execute(existing_stmt)).fetchall()
        existing = {row.file_path: row.content_hash for row in existing_rows}
        # Kept beside `existing` rather than folded into it: that dict is the
        # move-detection input, keyed and reverse-keyed by content hash alone.
        stamped_version = {
            row.file_path: row.extraction_version for row in existing_rows
        }

        # Determine changes
        to_upsert = []
        # Notes whose vectors this grammar change invalidates: their marker was
        # stale AND their recognised fence spans differ between the stamped
        # grammar and the current one. `embedded_content_hash` is cleared for
        # exactly these, in the same transaction as the stamp.
        grammar_invalidated: list[str] = []
        # Body text parsed during this scan, keyed by rel_path. The tsvector
        # loop and the link rebuild below both reuse these instead of
        # re-reading from disk — a concurrent delete between the passes would
        # otherwise raise FileNotFoundError and leave the just-committed row's
        # content_tsvector null/stale, or silently drop that note's links while
        # the row the scan wrote stands.
        #
        # Memory shape, because re-derive mode changes it: an ordinary pass
        # buffers only the *changed* notes' parsed bodies, while a re-deriving
        # pass treats every note as changed and therefore holds the whole
        # vault's parsed bodies for the duration of the pass.
        path_to_content: dict[str, str] = {}
        # The set of discovered relative paths, accumulated as the walk yields
        # them. Discovery is a generator rather than a dict so the walk can
        # close each directory once its children are done — one descriptor per
        # level of depth, not one per file — which means each file must be read
        # *now*, while its parent descriptor is open.
        seen: set[str] = set()
        walk = discover_markdown_files_at(root_fd, skips=skips)
        with contextlib.closing(walk):
            for found in walk:
                rel_path = found.rel
                seen.add(rel_path)
                try:
                    raw, stat = read_note_at(found.parent_fd, found.name)
                except UnicodeDecodeError:
                    logger.warning(f"Skipping non-UTF8 file: {rel_path}")
                    skips.append(f"{rel_path} (not valid UTF-8)")
                    continue
                except Exception as e:
                    logger.warning(f"Failed to read {rel_path}: {e}")
                    skips.append(f"{rel_path} ({e})")
                    continue

                h = _content_hash(raw)
                # A stale extraction marker makes a note changed even when its
                # bytes are not: the derived state (links, tags, vectors) came
                # out of a fence grammar this build no longer uses, and nothing
                # else on the row can see that.
                marker_stale = (
                    stamped_version.get(rel_path, CURRENT_EXTRACTION_VERSION)
                    != CURRENT_EXTRACTION_VERSION
                )
                # **Content-hash change detection is disabled under a
                # re-derive**, so every discovered file is parsed and upserted
                # regardless of its hash — which is also what makes every note
                # "changed" for the link rebuild below, and therefore what
                # deletes and re-extracts every one of this user's link rows.
                if (
                    not re_derive
                    and not marker_stale
                    and rel_path in existing
                    and existing[rel_path] == h
                ):
                    continue  # No change

                try:
                    frontmatter, content = parse_frontmatter(raw)
                    # Off the loop (#180, D3). `extract_tags` is a pure
                    # function of the body that runs regexes over the whole of
                    # it, and this branch runs once per *changed* note — every
                    # note of every user on the pass that follows an extraction
                    # bump. A thread only yields between `re` calls, never
                    # inside one, so this bounds the stall to the longest
                    # single scan step rather than to zero; that step is short
                    # because the grammars are linear.
                    tags = await asyncio.to_thread(extract_tags, content, frontmatter)
                except Exception as e:
                    logger.warning(f"Failed to parse {rel_path}: {e}")
                    skips.append(f"{rel_path} (parse: {e})")
                    continue
                path_to_content[rel_path] = content
                title = _note_title(frontmatter, found.name)

                # Grammar-attributable embedding invalidation, scoped to notes
                # whose recognised spans actually moved. Only asked where it
                # can be the deciding factor: a new path has no vectors, and a
                # changed hash already invalidates through the ordinary
                # predicate. Clearing is additive — it never suppresses an
                # invalidation another rule mandates.
                #
                # Off the loop for the same reason `extract_tags` above is: it
                # runs both versions' whole cleaning function over the body,
                # and the v0 cleaner is a Python line scanner — which, unlike a
                # single `re` step, yields the GIL as it goes, so the thread
                # actually buys concurrency here rather than merely bounding
                # the stall. The `await` sits last in the chain, so the three
                # cheap predicates still short-circuit before any dispatch.
                if (
                    marker_stale
                    and rel_path in existing
                    and existing[rel_path] == h
                    and await asyncio.to_thread(
                        _grammar_changed_the_embedding_text,
                        stamped_version[rel_path],
                        content,
                    )
                ):
                    grammar_invalidated.append(rel_path)

                to_upsert.append({
                    "user_id": user_id,
                    "file_path": rel_path,
                    "title": title,
                    "tags": tags,
                    "frontmatter": _sanitize_frontmatter(frontmatter),
                    "content_hash": h,
                    "extraction_version": CURRENT_EXTRACTION_VERSION,
                    "file_size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                })

        logger.info(f"Found {len(seen)} markdown files{log_suffix}")

        # Compute deleted paths up front so the move-detection block can
        # repair them before the delete/insert pipeline tears them apart.
        deleted_paths = set(existing.keys()) - seen

        # ── Move detection ────────────────────────────────────────────────
        # An external move (file dragged in Obsidian) looks like
        # "delete old + insert new" from a path-only POV. Pair deleted paths
        # with genuinely-new paths sharing the same content_hash and update
        # `file_path` in place — preserving the row id keeps embeddings,
        # incoming `note_links.target_note_id` refs, and avoids a dangling-
        # link window. Falls back to delete+insert when a hash matches more
        # than one path on either side (ambiguous — could be a real duplicate).
        new_by_hash: dict[str, list[str]] = {}
        for e in to_upsert:
            if e["file_path"] not in existing:
                new_by_hash.setdefault(e["content_hash"], []).append(e["file_path"])
        deleted_by_hash: dict[str, list[str]] = {}
        for p in deleted_paths:
            deleted_by_hash.setdefault(existing[p], []).append(p)

        moves: list[tuple[str, str]] = []
        for h, olds in deleted_by_hash.items():
            news = new_by_hash.get(h, [])
            if len(olds) == 1 and len(news) == 1:
                moves.append((olds[0], news[0]))

        moved_new_paths: set[str] = set()
        if moves:
            user_clause = "user_id IS NULL" if user_id is None else "user_id = :uid"
            # `embedded_content_hash = NULL` for the same reason `move_note`
            # clears it: the certification records that this row's *current
            # content* has been dealt with, and the exclusion branch decides
            # how by matching `embedding_exclude_patterns` against
            # `file_path`. A move changes that answer without changing any
            # content, so carrying the stamp across it freezes the old decision
            # forever — the embedding pass selects on
            # `embedded_content_hash != content_hash`, which a preserved stamp
            # makes false. A note moved out of an excluded folder would stay
            # included with no vectors and never be selected again; one moved
            # into an excluded folder would stay searchable. NULL means
            # "re-evaluate at the next pass", which is the whole repair and
            # costs an embedding call only for a note that is then included.
            #
            # This is the *id-preserving* move path, and that is exactly why it
            # needs the clause: the ordinary prune-and-upsert path replaces the
            # row, so a moved note reached through it starts from NULL anyway.
            # `title` moves with the path for the same reason `file_path`
            # does: it is *derived from the path* when the frontmatter does not
            # set one, so a row that keeps the old title after a rename reports
            # `Alpha` for a note called `Beta.md` in every index-backed tool,
            # for ever — the scan never revisits it because the content hash is
            # unchanged by definition on this branch (that is what identified
            # the move). The value is bound from the entry this pass already
            # parsed for the *new* path, so it is exactly what a fresh index
            # would write, frontmatter title included (#127, D3).
            #
            # **`tags` and `extraction_version` move with it, for the same
            # reason (#150).** The first draft of this branch left both alone
            # and let the *next* pass re-derive them, on the argument that
            # stamping a marker over tags nobody re-extracted is a false
            # certification. That argument is right about the marker and wrong
            # about the remedy: this branch is the only writer that touches a
            # note during the remediation window, and deferring left a moved
            # note with old-grammar tags for a whole tick — the "the next index
            # pass refreshes links and tags" promise, broken by a rename. So
            # the tags are re-derived here instead, bound from the entry this
            # pass already parsed for the new path under the CURRENT grammar
            # (`extract_tags` in the scan loop above), the links are re-derived
            # because `moved_new_paths` feeds `_update_links_for_changed`, and
            # the marker is stamped in the same statement — one transaction, no
            # window in which the stamp outruns the derivation.
            #
            # `embedded_content_hash = NULL` stays unconditional and is not the
            # grammar rule: it is the path-change invalidation (#127), which
            # applies to every move whatever the marker says.
            move_upd_sql = (
                "UPDATE notes_metadata "
                "SET file_path = :new, title = :title, tags = :tags, "
                "file_size = :size, modified_at = :mtime, indexed_at = now(), "
                "extraction_version = :xver, "
                "embedded_content_hash = NULL "
                f"WHERE file_path = :old AND {user_clause}"
            )
            # `tags` is `varchar[]`; a bare `:tags` leaves the driver to guess
            # the parameter's type from a Python list and asyncpg will not.
            move_upd_stmt = text(move_upd_sql).bindparams(
                bindparam("tags", type_=ARRAY(String))
            )
            # Rewrite stored `target_path` strings that referenced the old
            # path. Two forms: the full path (`Folder/Old.md`) and the
            # extension-stripped form (`Folder/Old`) the extractor stores for
            # markdown links. Bare-name `[[noteName]]` references survive
            # untouched — their `target_note_id` is preserved via id reuse
            # and the stem doesn't change on a folder-only move.
            move_tp_sql = (
                "UPDATE note_links SET target_path = :new "
                "WHERE target_path = :old "
                f"AND source_note_id IN (SELECT id FROM notes_metadata WHERE {user_clause})"
            )

            entry_by_path = {e["file_path"]: e for e in to_upsert}
            for old, new in moves:
                e = entry_by_path[new]
                params: dict = {
                    "new": new, "old": old, "title": e["title"],
                    "tags": e["tags"], "xver": CURRENT_EXTRACTION_VERSION,
                    "size": e["file_size"], "mtime": e["modified_at"],
                }
                if user_id is not None:
                    params["uid"] = user_id
                await session.execute(move_upd_stmt, params)

                old_no_ext = old[:-3] if old.endswith(".md") else old
                new_no_ext = new[:-3] if new.endswith(".md") else new
                for o, n in [(old, new), (old_no_ext, new_no_ext)]:
                    tp_params: dict = {"new": n, "old": o}
                    if user_id is not None:
                        tp_params["uid"] = user_id
                    await session.execute(text(move_tp_sql), tp_params)

                moved_new_paths.add(new)

            logger.info(
                f"Detected {len(moves)} file move(s) — preserved ids{log_suffix}"
            )

            # The existing delete+insert pipeline below mustn't touch the
            # paths we just repaired in place.
            to_upsert = [e for e in to_upsert if e["file_path"] not in moved_new_paths]
            deleted_paths -= {old for old, _ in moves}

        # Upsert changed files
        if to_upsert:
            for batch_start in range(0, len(to_upsert), 100):
                batch = to_upsert[batch_start:batch_start + 100]
                stmt = insert(NoteMetadata).values(batch)
                stmt = stmt.on_conflict_do_update(
                    # Match the composite UNIQUE(user_id, file_path) on
                    # notes_metadata (migration 009). The constraint is
                    # declared NULLS NOT DISTINCT so single-user-mode
                    # rows (user_id IS NULL) still collide and upsert
                    # correctly. Without NULLS NOT DISTINCT, PG 15+ would
                    # treat each NULL user_id as distinct and silently
                    # duplicate rows on every indexer pass.
                    index_elements=["user_id", "file_path"],
                    set_={
                        "title": stmt.excluded.title,
                        "tags": stmt.excluded.tags,
                        "frontmatter": stmt.excluded.frontmatter,
                        "content_hash": stmt.excluded.content_hash,
                        # Stamped in the same statement that writes the state
                        # it certifies, inside the pass's one transaction: a
                        # pass that fails anywhere commits neither, so a stale
                        # marker never survives beside re-derived rows and a
                        # current marker never survives beside stale ones.
                        "extraction_version": stmt.excluded.extraction_version,
                        "file_size": stmt.excluded.file_size,
                        "modified_at": stmt.excluded.modified_at,
                        "indexed_at": text("now()"),
                    },
                )
                await session.execute(stmt)
            logger.info(f"Upserted {len(to_upsert)} notes")

        # Grammar-attributable embedding invalidation. A separate statement
        # because the upsert deliberately does NOT carry
        # `embedded_content_hash` — it must stay untouched for every note the
        # grammar did not affect, or a marker bump would re-embed the vault.
        # Owner-scoped like every other write in this pass: `user_id IS NULL`
        # in single-user mode, `user_id = :uid` in multi-user mode, so one
        # user's grammar sweep can never clear another's certification.
        if grammar_invalidated:
            user_clause = "user_id IS NULL" if user_id is None else "user_id = :uid"
            inval_params: dict = {"paths": grammar_invalidated}
            if user_id is not None:
                inval_params["uid"] = user_id
            await session.execute(
                text(
                    "UPDATE notes_metadata SET embedded_content_hash = NULL "
                    f"WHERE file_path = ANY(:paths) AND {user_clause}"
                ),
                inval_params,
            )
            logger.info(
                "Fence-grammar change invalidated embeddings for %d note(s)%s",
                len(grammar_invalidated),
                log_suffix,
            )

        # Update tsvectors for changed notes
        if to_upsert:
            paths = [n["file_path"] for n in to_upsert]
            # In multi-user mode the same `file_path` can exist for multiple
            # users, so the UPDATE scopes by user: `user_id IS NULL` in
            # single-user mode, `user_id = :uid` (never NULL) in multi-user
            # mode. The tsvector expression is built from `settings.fts_configs`
            # (see `src/services/fts.py`) so index-time configs match the
            # query-time configs in `search.py`.
            tsv_frag, tsv_params = index_tsvector_sql("content")
            if user_id is None:
                tsv_sql = f"""
                    UPDATE notes_metadata
                    SET content_tsvector = {tsv_frag}
                    WHERE file_path = :path
                      AND user_id IS NULL
                """
            else:
                tsv_sql = f"""
                    UPDATE notes_metadata
                    SET content_tsvector = {tsv_frag}
                    WHERE file_path = :path
                      AND user_id = :uid
                """
            for path in paths:
                # Reuse the body parsed during the scan loop above instead of
                # re-reading from disk; a concurrent delete between the passes
                # would otherwise leave content_tsvector null/stale (issue #18).
                # A changed path with no buffered body is a skip, not a silent
                # `continue`: it leaves a row whose keyword vector this pass did
                # not write, which a re-derive must not certify.
                if path not in path_to_content:
                    skips.append(f"{path} (no buffered body for the keyword vector)")
                    continue
                content = path_to_content[path]
                params: dict = {"path": path, **tsv_params}
                if user_id is not None:
                    params["uid"] = user_id
                # Full body first, halving retreat per note, floor failure
                # re-raised — which aborts the pass with nothing committed,
                # exactly as the unconditional `content[:100000]` did when it
                # failed. See `write_tsvector_bounded`.
                await write_tsvector_bounded(
                    session, text(tsv_sql), content, params, label=path
                )
            logger.info(f"Updated tsvectors for {len(paths)} notes{log_suffix}")

        # Remove deleted files (scoped to this user when set). `deleted_paths`
        # was computed earlier and any entries that turned out to be moves
        # have already been stripped out by the move-detection block above.
        if deleted_paths:
            del_stmt = delete(NoteMetadata).where(
                NoteMetadata.file_path.in_(deleted_paths)
            )
            if user_id is None:
                del_stmt = del_stmt.where(NoteMetadata.user_id.is_(None))
            else:
                del_stmt = del_stmt.where(NoteMetadata.user_id == user_id)
            await session.execute(del_stmt)
            logger.info(f"Removed {len(deleted_paths)} deleted notes{log_suffix}")

        # ── Link extraction for changed notes ───────────────────────────
        # We rebuild the vault_index here (post-commit), then for each
        # changed note delete-and-reinsert its rows in `note_links`. New or
        # renamed notes also get a re-resolution pass that updates any
        # previously-dangling rows now matching their path.
        # Moved notes need outgoing-link re-extraction too: same-folder
        # resolution can change once the note sits in a different directory.
        if to_upsert or deleted_paths or moved_new_paths:
            await _update_links_for_changed(
                session,
                vault,
                [n["file_path"] for n in to_upsert] + list(moved_new_paths),
                user_id=user_id,
                path_to_content=path_to_content,
                skips=skips,
            )

        # ── The tail stamp ────────────────────────────────────────────────
        # Written where the state it describes is established. On the re-derive
        # branch that state is "every surviving row was derived from this
        # root", which is not true until the pass has finished — so the stamp
        # is issued after the pass's last write and **only if it skipped
        # nothing**. Head-stamping a re-derive would be exactly the false
        # provenance this record exists to prevent, written by our own code
        # instead of by a migration.
        #
        # Committing it with the pass's own writes rather than afterwards makes
        # a crash mid-repair leave the previous record untouched, so the next
        # pass repairs again: bounded, idempotent, and never a stamp over a
        # half-repaired index.
        if re_derive and facts is not None:
            if skips:
                logger.warning(
                    "Re-derive incomplete%s: %d discovered path(s) were not "
                    "fully processed, so no provenance was recorded and the "
                    "next pass will re-derive again. Offenders: %s",
                    log_suffix,
                    len(skips),
                    _format_skips(skips),
                )
            else:
                # The same binding as the discard's, for the same reason: this
                # stamp is provenance too, and a record written under an
                # assignment the row no longer names is exactly the false
                # provenance the record exists to prevent. It is withheld
                # rather than fatal — the re-derive's repairs are still
                # correct for the root they were read from, nothing was
                # destroyed, and an unrecorded provenance simply makes the next
                # pass re-derive again.
                # **Inside a savepoint, and with `NOWAIT`.** This transaction
                # already holds `notes_metadata` row locks, and a permanent
                # user delete locks `users` first and then cascades onto
                # exactly those rows — so *waiting* here closes a deadlock
                # cycle that PostgreSQL breaks by aborting one side, possibly
                # the operator's delete. The savepoint is what makes the
                # refusal survivable: a failed statement poisons its
                # transaction, so without one the pass would lose every repair
                # it had just made rather than merely its stamp.
                stamped = None
                try:
                    async with session.begin_nested():
                        await _assert_still_assigned(
                            session, user_id, facts, nowait=True
                        )
                        stamped = await _stamp_provenance(session, user_id, facts)
                except ProvenanceLockUnavailable as exc:
                    logger.warning(
                        "Re-derive complete but not recorded%s: %s. The "
                        "repairs are committed; the next pass will re-derive "
                        "again and stamp then.",
                        log_suffix,
                        exc,
                    )
                except ProvenanceRaceAborted as exc:
                    logger.warning(
                        "Re-derive complete but not recorded%s: %s. The next "
                        "pass will reclassify and re-derive again.",
                        log_suffix,
                        exc,
                    )
                else:
                    if stamped != 1:
                        raise RuntimeError(
                            f"Re-derive stamp{log_suffix} matched {stamped} "
                            f"row(s) for user_id={user_id}, not exactly one."
                        )
                    logger.info(
                        "Re-derive complete%s: recorded provenance "
                        "assignment=%r realpath=%r handle=%s",
                        log_suffix,
                        facts.assignment,
                        facts.realpath,
                        facts.handle if facts.handle is not None else "none",
                    )
        elif skips:
            logger.warning(
                "%d discovered path(s) were not fully processed%s: %s",
                len(skips),
                log_suffix,
                _format_skips(skips),
            )

        # Metadata hashes, keyword vectors, deletions, and link rows describe
        # one filesystem snapshot. Commit them together so a failure in a
        # later stage cannot leave a new hash paired with stale search data
        # (which would make the next scan incorrectly skip the note).
        await session.commit()

    logger.info(f"Vault index scan complete{log_suffix}")
    # For the run recorder: what the walk saw, and what this pass wrote. The
    # moved paths count as indexed — the id-preserving branch rewrote those
    # rows — and they were removed from `to_upsert` above, so the two sets are
    # disjoint and nothing is double-counted.
    return len(seen), len(to_upsert) + len(moved_new_paths)


async def _update_links_for_changed(
    session,
    vault: Path,
    changed_paths: list[str],
    user_id: int | None = None,
    path_to_content: dict[str, str] | None = None,
    skips: list[str] | None = None,
):
    """Re-extract and upsert links for the given changed paths.

    Builds a fresh `vault_index` from `notes_metadata`, then for every changed
    note: deletes existing rows, extracts links, resolves targets, inserts.
    Finally, runs a re-resolution pass to attach previously-dangling rows
    whose `target_path` matches any of the changed notes.

    In multi-user mode the vault_index is scoped to `user_id` so a user's
    wikilinks cannot resolve to another user's note (they share the same
    `file_path` string but live in distinct `notes_metadata.id`s).

    **This reads no file.** It used to re-read each changed note from disk,
    which was both a second read of bytes the scan had already parsed and a
    second window in which the file could change or vanish — a disappearance
    between the scan and the rebuild silently dropped that note's links while
    the row the scan wrote stood. It now extracts from `path_to_content`, the
    buffer the scan already fills for the tsvector loop, which holds exactly
    the post-frontmatter body `extract_links` consumes. A changed path missing
    from the buffer is recorded in `skips` rather than silently passed over: it
    means a link row this pass was supposed to write is absent.

    **It writes per note.** Each changed note's rows are inserted before the
    next note's links are extracted, so peak link-row memory is one note's
    worth plus one insert batch rather than the whole pass's (#203). A note
    over `MAX_LINKS_PER_NOTE` keeps its first N links in document order, has
    `notes_metadata.links_truncated` set, and is logged at ERROR — it is
    **not** a skip, so it does not withhold a re-derive's certification
    (`index-integrity`, "A capped note does not withhold the record").

    `vault` is retained in the signature and is deliberately unused for reads.
    """
    bodies = path_to_content if path_to_content is not None else {}
    # Build vault_index once for the entire pass — scoped to this user when set.
    vi_stmt = select(NoteMetadata.file_path, NoteMetadata.id)
    if user_id is None:
        vi_stmt = vi_stmt.where(NoteMetadata.user_id.is_(None))
    else:
        vi_stmt = vi_stmt.where(NoteMetadata.user_id == user_id)
    rows = (await session.execute(vi_stmt)).all()
    vault_index = build_vault_index([(r.file_path, r.id) for r in rows])
    paths_to_id: dict[str, int] = vault_index["paths"]

    if changed_paths:
        # Process changed notes' outgoing links.
        change_ids = [paths_to_id[p] for p in changed_paths if p in paths_to_id]
        if change_ids:
            await session.execute(
                delete(NoteLink).where(NoteLink.source_note_id.in_(change_ids))
            )
            # **Per note, not per pass** (#203, D4). This used to accumulate
            # every changed note's rows into one `new_rows` list and insert the
            # lot at the end, so peak link-row memory was the whole pass's
            # derived links — unbounded in the number of changed notes, and a
            # re-derive makes *every* note changed. Each note's rows are now
            # inserted before the next note's are extracted, so the peak is one
            # note's worth (at most `MAX_LINKS_PER_NOTE`) plus one insert
            # batch, whatever the vault's size.
            #
            # The *body* buffer (`path_to_content`) is deliberately not bounded
            # here: the scan fills it for the tsvector loop, it is bounded by
            # the write-side note cap times the number of changed notes, and
            # narrowing it is a separate change (accepted residual on #203).
            # This bounds link rows, which were the unbounded term.
            total_rows = 0
            truncated_ids: list[int] = []
            complete_ids: list[int] = []
            for path in changed_paths:
                src_id = paths_to_id.get(path)
                if src_id is None:
                    # Practically unreachable — the index is selected after the
                    # upsert, in this same transaction — but it is a link
                    # extraction that did not happen, and A.7a's rule is that
                    # *any* such skip withholds the re-derive's certification.
                    # Its sibling below records one; recording only one of the
                    # two would leave the one branch that can drop a link row
                    # while still stamping "every link row was written by this
                    # pass".
                    if skips is not None:
                        skips.append(
                            f"{path} (no index row for the link rebuild)"
                        )
                    continue
                content = bodies.get(path)
                if content is None:
                    if skips is not None:
                        skips.append(f"{path} (no buffered body for the link rebuild)")
                    continue
                # Off the loop (#180, D3) and bounded (#203, D4). A thread only
                # yields between `re` calls, never inside one, so this bounds
                # the stall to the longest single scan step rather than to
                # zero; the linear grammars are what make that step short.
                links, truncated = await asyncio.to_thread(
                    extract_links_bounded, content, max_links=MAX_LINKS_PER_NOTE
                )
                note_rows = [
                    {
                        "source_note_id": src_id,
                        "target_note_id": resolve_target(
                            link.target, path, vault_index
                        ),
                        "target_path": link.target[:1024],
                        "link_text": link.link_text,
                        "kind": link.kind,
                        "position": link.position,
                    }
                    for link in links
                ]
                if truncated:
                    truncated_ids.append(src_id)
                    # ERROR, not WARNING, so it also reaches the ops-health
                    # error buffer. One line per capped note per pass.
                    #
                    # The note's *true* link count is deliberately not named:
                    # counting it requires the unbounded extraction the cap
                    # exists to avoid. What is named is what is true and
                    # bounded — the cap, and the rows this pass persisted.
                    logger.error(
                        f"Link extraction truncated for {path}: the note holds "
                        f"more than MAX_LINKS_PER_NOTE={MAX_LINKS_PER_NOTE} "
                        f"links; the first {len(note_rows)} in document order "
                        "were persisted and notes_metadata.links_truncated was "
                        "set, so `get_links` reports this note's link set as "
                        "incomplete. This is a declared degradation, not a "
                        "skip: the pass stays complete."
                    )
                else:
                    complete_ids.append(src_id)
                for batch_start in range(0, len(note_rows), 1000):
                    await session.execute(
                        insert(NoteLink).values(
                            note_rows[batch_start:batch_start + 1000]
                        )
                    )
                total_rows += len(note_rows)
                # Explicit, so the peak this block exists to bound is not held
                # across the next note's extraction by a stale binding.
                note_rows = []

            # The marker is derived state, exactly like the rows: set where
            # this pass capped, cleared where it did not, in the same
            # transaction as the rows it describes. Both statements are
            # predicated on the value actually changing, so the ordinary pass —
            # nothing truncated, nothing previously truncated — writes no
            # `notes_metadata` rows at all rather than rewriting one per
            # changed note.
            for ids, value in ((truncated_ids, True), (complete_ids, False)):
                for start in range(0, len(ids), 1000):
                    chunk = ids[start:start + 1000]
                    await session.execute(
                        update(NoteMetadata)
                        .where(
                            NoteMetadata.id.in_(chunk),
                            NoteMetadata.links_truncated.is_(not value),
                        )
                        .values(links_truncated=value)
                    )

            logger.info(
                f"Re-extracted links for {len(change_ids)} notes "
                f"({total_rows} link rows)"
                + (
                    f", {len(truncated_ids)} truncated at {MAX_LINKS_PER_NOTE}"
                    if truncated_ids
                    else ""
                )
            )

    # Re-resolution pass: any newly-arrived note may resolve previously
    # dangling rows. We patch `target_note_id` for rows whose `target_path`
    # matches one of the changed paths in a few canonical forms.
    #
    # In multi-user mode we restrict the UPDATE to rows whose source note
    # belongs to the same user — otherwise alice's newly-created `foo.md`
    # would silently get attached as the target of bob's dangling
    # `[[foo]]` link.
    #
    # The bare-stem form (`[[Foo]]`) is only safe to match when exactly one
    # note in the vault carries that stem. With a shared stem the resolver
    # (`resolve_target`) uses same-folder preference and an alphabetical
    # tie-break, so a blind `target_path = stem` match here would mis-attach
    # dangling rows that belong to a *different* note. Ambiguous stems stay
    # dangling and resolve later when their own source note is reindexed.
    stems: dict[str, list[tuple[str, int]]] = vault_index["stems"]
    for path in changed_paths:
        nid = paths_to_id.get(path)
        if nid is None:
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        path_no_ext = path[:-3] if path.endswith(".md") else path
        # Always-safe canonical forms keyed to the exact stored path.
        params: dict = {
            "nid": nid,
            "full": path,
            "no_ext": path_no_ext,
        }
        # Only fold in the bare stem when it maps to a single note.
        if len(stems.get(stem, [])) == 1:
            params["stem"] = stem
        in_clause = ", ".join(f":{p}" for p in ("full", "no_ext", "stem")
                              if p in params)
        where_extra = ""
        if user_id is None:
            where_extra = (
                " AND source_note_id IN ("
                "SELECT id FROM notes_metadata WHERE user_id IS NULL)"
            )
        else:
            params["uid"] = user_id
            where_extra = (
                " AND source_note_id IN ("
                "SELECT id FROM notes_metadata WHERE user_id = :uid)"
            )
        reresolve_sql = (
            "UPDATE note_links "
            "SET target_note_id = :nid "
            "WHERE target_note_id IS NULL "
            f"AND target_path IN ({in_clause})"
            f"{where_extra}"
        )
        await session.execute(text(reresolve_sql), params)


async def _ancillary_pass_is_permitted(
    session, user_id: int | None, vault: Path, root_fd: int, label: str
) -> bool:
    """May this unverified ancillary pass write rows for this user?

    **Only on the `same assignment` verdict.** The one-shot link backfill and
    the keyword-vector rebuild both read `vault / file_path` and write rows the
    provenance is a claim about — `note_links`, `content_tsvector` — with **no
    verification of any kind** that the bytes they read belong to the row they
    write against. And neither may assume the scan settled that claim a moment
    ago: a user whose notes contain no links leaves the link backfill eligible
    on *every* startup, and a reassignment can commit between the scan and
    either of them. Allowing them to write under an unresolved provenance is
    exactly what lets a link row extracted from one root be committed against a
    metadata row from another.

    Verification is not merely unimplemented in those two. A link row's
    *resolution* is a function of the whole set of notes under a root rather
    than of one file's bytes, so no per-file check could license the backfill.
    The keyword rebuild could in principle be verified the way the embedding
    pass is, and is still gated, because nothing records what a tsvector was
    built from — there is no keyword analogue of `embedded_content_hash`, so a
    vector built from foreign bytes leaves no evidence a later pass could act
    on.

    Skipping costs them nothing even for a user whose provenance never settles:
    the re-derive branch does both of their jobs itself on every pass. So this
    is a delay, never a loss.

    **`embed_vault` is deliberately not gated** — see its own docstring. Its
    hash verification makes it safe by construction, and gating it composes
    with the completeness rule into indefinite staleness.

    The skip is **per user**: one unsettled user must not stop the pass for
    everybody else. Single-user mode has no `users` row and is ungated, exactly
    as it behaves today.
    """
    if user_id is None:
        return True
    classification, _facts, _recorded = await classify_for_pass(
        session, user_id, vault, root_fd
    )
    if classification.verdict == PROVENANCE_KEEP:
        return True
    logger.info(
        "%s skipped for user_id=%s: provenance is not settled (%s). No row was "
        "written for this user; the next index pass will settle it.",
        label,
        user_id,
        classification.reason,
    )
    return False


async def link_backfill_pass(user_id: int | None = None):
    """One-shot backfill that populates `note_links` for every note.

    Runs on startup when this user's graph has no rows. Rebuilds the graph in one transaction so a
    restart after any batch rolls back cleanly instead of mistaking a partial
    graph for a completed backfill.

    In multi-user mode each user's pass scopes its scan + vault_index to its
    own `notes_metadata` rows and replaces only links sourced by those rows.
    """
    global link_backfill_in_progress
    # Ahead of the root resolution and ahead of the run row: a quarantined user
    # gets no backfill row, because the pass that called us records the refusal
    # on the row it already opened.
    _refuse_quarantined_pass(user_id, "link backfill")
    vault = _vault_root(user_id)
    # Its own `indexer_runs` row, under the `backfill` trigger: it is a
    # distinct kind of pass with distinct timings, and folding it into the
    # startup row it usually runs beside would hide a slow one-shot rebuild
    # inside a slow-looking startup. The recorder wraps the pinned body, so its
    # write happens after that body's session has closed — never two pooled
    # connections at once for one task.
    async with record_indexer_run("backfill", user_id) as stats:
        with pinned_root(vault) as root_fd:
            await _link_backfill_pinned(user_id, vault, root_fd, stats)


async def _link_backfill_pinned(
    user_id: int | None, vault: Path, root_fd: int, stats: "PassStats | None" = None
):
    global link_backfill_in_progress
    stats = stats if stats is not None else PassStats()
    # Every path out of the guards below is "this scope needed no backfill",
    # which is not a pass and must not spend a row.
    stats.skipped = True
    async with async_session() as session:
        if not await _ancillary_pass_is_permitted(
            session, user_id, vault, root_fd, "Link backfill"
        ):
            return

        # Completion is inferred per user, never from the global table. The
        # rebuild itself commits atomically below, so any visible row proves a
        # prior pass for this scope completed (a zero-link vault is harmlessly
        # rescanned on the next startup).
        existing_stmt = (
            select(func.count(NoteLink.id))
            .join(NoteMetadata, NoteLink.source_note_id == NoteMetadata.id)
        )
        if user_id is None:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id.is_(None))
        else:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id == user_id)
        existing = (await session.execute(existing_stmt)).scalar() or 0
        if existing > 0:
            return

        rows_stmt = select(NoteMetadata.id, NoteMetadata.file_path)
        if user_id is None:
            rows_stmt = rows_stmt.where(NoteMetadata.user_id.is_(None))
        else:
            rows_stmt = rows_stmt.where(NoteMetadata.user_id == user_id)
        rows = (await session.execute(rows_stmt)).all()
        if not rows:
            return

        # Past every guard: this is a real pass and it records one.
        stats.skipped = False
        stats.notes_scanned = len(rows)
        link_backfill_in_progress = True
        log_suffix = f" (user_id={user_id})" if user_id is not None else ""
        logger.info(f"Starting link backfill across {len(rows)} notes{log_suffix}")

        vault_index = build_vault_index([(r.file_path, r.id) for r in rows])

        try:
            note_ids = [r.id for r in rows]
            await session.execute(
                delete(NoteLink).where(NoteLink.source_note_id.in_(note_ids))
            )
            # Same two rules as the changed-path rebuild (#180/#203): bounded
            # extraction, dispatched off the event loop, and a buffer that
            # never grows past one note's rows plus one insert batch — the
            # flush below is checked after every note, so the peak is
            # `MAX_LINKS_PER_NOTE + 999` and not the vault's total link count.
            buffer: list[dict] = []
            truncated_ids: list[int] = []
            complete_ids: list[int] = []
            for i, row in enumerate(rows, start=1):
                try:
                    raw, _stat = read_note_beneath(root_fd, row.file_path)
                except (UnicodeDecodeError, OSError):
                    continue
                # Counted as indexed only once its bytes were read: an
                # unreadable note is scanned and not rebuilt, and the two
                # numbers differing is exactly how an operator sees that.
                stats.notes_indexed += 1
                _, content = parse_frontmatter(raw)
                links, truncated = await asyncio.to_thread(
                    extract_links_bounded, content, max_links=MAX_LINKS_PER_NOTE
                )
                for link in links:
                    target_id = resolve_target(link.target, row.file_path, vault_index)
                    buffer.append({
                        "source_note_id": row.id,
                        "target_note_id": target_id,
                        "target_path": link.target[:1024],
                        "link_text": link.link_text,
                        "kind": link.kind,
                        "position": link.position,
                    })
                if truncated:
                    truncated_ids.append(row.id)
                    logger.error(
                        f"Link extraction truncated for {row.file_path}: the "
                        f"note holds more than "
                        f"MAX_LINKS_PER_NOTE={MAX_LINKS_PER_NOTE} links; the "
                        f"first {len(links)} in document order were persisted "
                        "and notes_metadata.links_truncated was set, so "
                        "`get_links` reports this note's link set as "
                        "incomplete."
                    )
                else:
                    complete_ids.append(row.id)
                while len(buffer) >= 1000:
                    await session.execute(insert(NoteLink).values(buffer[:1000]))
                    del buffer[:1000]
                if i % 500 == 0:
                    logger.info(f"Link backfill: {i}/{len(rows)} notes")

            if buffer:
                await session.execute(insert(NoteLink).values(buffer))

            # Set where this backfill capped, cleared where it did not —
            # predicated on the value changing, so an ordinary backfill writes
            # no `notes_metadata` rows.
            for ids, value in ((truncated_ids, True), (complete_ids, False)):
                for start in range(0, len(ids), 1000):
                    chunk = ids[start:start + 1000]
                    await session.execute(
                        update(NoteMetadata)
                        .where(
                            NoteMetadata.id.in_(chunk),
                            NoteMetadata.links_truncated.is_(not value),
                        )
                        .values(links_truncated=value)
                    )

            await session.commit()

            logger.info(
                f"Link backfill complete: {len(rows)} notes scanned"
                + (
                    f", {len(truncated_ids)} truncated at {MAX_LINKS_PER_NOTE}"
                    if truncated_ids
                    else ""
                )
            )
        finally:
            link_backfill_in_progress = False


class GenerationMismatch(RuntimeError):
    """A stored settings fingerprint no longer describes this process.

    Raised by the incremental index pass when the keyword fingerprint it
    re-read under the generation lock is not the one this build would write.
    Deliberately fatal to the pass rather than a skip: a keyword vector is only
    ever rewritten when a note's `content_hash` changes, so a row this pass
    wrote under the previous `FTS_CONFIGS` would keep that vector for ever
    behind a fingerprint claiming otherwise — `'running'` stored as the english
    stem `run`, matching a `simple` query for `run` on a note that does not
    contain the word. The pass aborts with nothing committed, exactly as a
    tsvector floor failure does, and retries on the next tick under whichever
    configuration is then current.
    """


async def _fingerprint_verdict(session, key: str, current: str):
    """Compare the fingerprint stored under `key` against `current`.

    Returns `None` when there is nothing to compare — `indexer_state` has not
    been migrated in yet. `to_regclass` answers without raising, which is what
    lets this run as the first statement of a transaction that must go on: a
    `SELECT` against a missing table aborts the transaction outright.
    """
    if not await state_table_exists(session):
        return None
    return compare_fingerprint(await get_state(session, key), current)


async def _assert_fts_generation_current(session) -> None:
    """Re-read the keyword fingerprint under the generation lock, or abort.

    Acquiring the lock buys nothing on its own: the stored value may have
    changed while this transaction waited for it, so the read has to happen
    *after* the acquisition and inside the same transaction. `ABSENT` proceeds
    — startup adopts, and a database that has never recorded a fingerprint has
    no claim for this pass to contradict.
    """
    verdict = await _fingerprint_verdict(
        session, KEY_FTS_FINGERPRINT, fts_fingerprint()
    )
    if verdict is None or verdict.status in (
        FingerprintStatus.ABSENT,
        FingerprintStatus.MATCH,
    ):
        return
    if verdict.status is FingerprintStatus.UNREADABLE:
        detail = f"the stored value could not be interpreted: {verdict.reason}"
    else:
        detail = (
            "the stored value differs in "
            f"{', '.join(verdict.fields) or 'an unnamed field'}"
        )
    raise GenerationMismatch(
        "index pass aborted before its first write: the stored keyword "
        f"fingerprint does not describe this process's FTS_CONFIGS — {detail}. "
        f"stored={verdict.stored!r} current={verdict.current!r}. Nothing has "
        "been committed. Run `make rebuild-tsvectors`, or restore the "
        "FTS_CONFIGS the stored fingerprint names."
    )


async def _embedding_generation_current(session, log_suffix: str) -> bool:
    """Whether this process's embedding configuration is the stored one.

    A per-user-stage read, and **an optimisation rather than the guarantee**:
    `embed_note` takes the generation lock and re-reads this fingerprint
    between its provider call and its certification, which is the only window
    where the check and the act are not separated by a network round trip. This
    one simply stops an old container from working through a backlog whose
    every certification the lock is going to refuse.

    Anything other than a difference proceeds. `ABSENT` is startup's to adopt,
    and a database with no recorded fingerprint makes no claim this stage could
    contradict.
    """
    verdict = await _fingerprint_verdict(
        session, KEY_EMBEDDING_FINGERPRINT, embedding_fingerprint()
    )
    if verdict is None or verdict.status in (
        FingerprintStatus.ABSENT,
        FingerprintStatus.MATCH,
    ):
        return True
    logger.error(
        "Embedding stage skipped%s: the stored embedding fingerprint does not "
        "describe this process (%s). stored=%r current=%r. Nothing was "
        "attempted and nothing recorded — every certification this stage could "
        "reach would be refused under the generation lock anyway. Run "
        "`make reset-embeddings`, or restore the configuration the stored "
        "fingerprint names.",
        log_suffix,
        (
            verdict.reason
            if verdict.status is FingerprintStatus.UNREADABLE
            else "differs in " + (", ".join(verdict.fields) or "an unnamed field")
        ),
        verdict.stored,
        verdict.current,
    )
    return False


async def _clear_chunks_truncated(session, row) -> None:
    """Drop a chunk-cap marker from a row that is about to have no vectors.

    Written as a conditional UPDATE rather than an unconditional one so a pass
    over an untruncated vault dirties no rows: the marker's lifecycle is
    `links_truncated`'s, and the overwhelmingly common value is `false`.

    `getattr` with a default because the callers' rows come from raw SQL that
    the offline test fakes answer with hand-built namespaces; a row without the
    column has no marker to clear.
    """
    if not getattr(row, "chunks_truncated", False):
        return
    await session.execute(
        update(NoteMetadata)
        .where(NoteMetadata.id == row.id)
        .values(chunks_truncated=False)
        .execution_options(synchronize_session=False)
    )


@dataclass
class EmbedBudget:
    """One tenant's embed-stage allowance for one pass (#202, D5).

    **Checked only at a note boundary, and never inside a note.** `embed_note`
    refuses partial certification, so a note abandoned between chunks is left
    uncertified, re-selected by the backlog on the next tick, and re-performs
    every provider call it already made — #127's permanent burn arriving by a
    new route. Bounding at the boundary means the overrun is at most one note,
    which `MAX_CHUNKS_PER_NOTE` has already bounded.

    **It debits chunks *submitted*, never chunks stored.** A failing provider
    stores nothing, so a budget debited by stored chunks is not debited at all
    by an outage — and a tenant whose every note fails would burn the whole
    pass, every pass, without ever reaching its own bound: the starvation this
    exists to stop, surviving inside the fix for it. A raise and a cardinality
    mismatch therefore debit exactly as a success does.

    **At least one note, always.** The check runs only after a note of this
    user's pass has completed, so a tenant whose very first note exceeds the
    whole budget still advances by one note per pass instead of zero for ever.

    **Only when the pass serves more than one scope.** With one active scope
    there is no other tenant to be fair to, and a budget there would spread a
    first index of a few thousand notes over several five-minute-spaced passes
    for no benefit. That clause is what keeps the default deployment's
    behaviour identical to today's.

    A stop is **not** a failure: it writes nothing to `indexer_runs.error` and
    logs once per user per pass, the same class of event as a pause. The
    operator-visible signal for a tenant permanently over budget is the
    dashboard's pending count, which is a property of the index rather than of
    one pass.
    """

    chunk_budget: int = 0
    time_budget: float = 0.0
    enforced: bool = False
    chunks_submitted: int = 0
    notes_completed: int = 0
    started_at: float = field(default_factory=time.monotonic)
    #: One WARNING per user per pass, shared by the backlog loop and the sweep.
    announced: bool = False

    @property
    def active(self) -> bool:
        """Whether this budget can ever stop anything."""
        return self.enforced and bool(self.chunk_budget or self.time_budget)

    def debit(self, chunks_submitted: int) -> None:
        self.chunks_submitted += chunks_submitted

    def note_finished(self) -> None:
        """One note of this user's embed stage reached its boundary."""
        self.notes_completed += 1

    def exhausted(self) -> bool:
        if not self.active or self.notes_completed < 1:
            return False
        if self.chunk_budget and self.chunks_submitted >= self.chunk_budget:
            return True
        if (
            self.time_budget
            and (time.monotonic() - self.started_at) >= self.time_budget
        ):
            return True
        return False

    def stop(self, log_suffix: str, stage: str) -> None:
        """Log the stop once per user per pass, whichever stage reached it."""
        if self.announced:
            return
        self.announced = True
        logger.warning(
            "Embed budget exhausted%s during %s: %d chunk(s) submitted against "
            "a budget of %s, %.1fs elapsed against %ss. Stopping this user at a "
            "note boundary so the next tenant is served in this pass; the "
            "remaining backlog stays visible as the dashboard's pending count. "
            "This is not a failure and is not recorded in the run row's error.",
            log_suffix,
            stage,
            self.chunks_submitted,
            self.chunk_budget or "off",
            time.monotonic() - self.started_at,
            self.time_budget or "off",
        )


class _ProviderCallAccounting:
    """One provider call, counted once, from whichever side reports it first.

    The two embed loops used to reconstruct the attempt and the chunk debit
    from the returned `EmbedNoteResult.chunks_submitted`. That is correct for
    every path that *returns* and silently wrong for every path that **raises
    after the provider call** — `certify_embedded` raising `StaleCertification`
    on a row that moved is the ordinary one, and a database error anywhere
    below it is the rest. The call had been made and the provider's time spent,
    but nothing counted it as an attempt and nothing debited the tenant's chunk
    budget. A tenant losing that race on every note could therefore issue
    provider calls indefinitely without ever becoming budget-exhaustible: the
    starvation #202 exists to bound, surviving inside the fix for it.

    So the authoritative signal is `issued()`, handed to `embed_note` as
    `on_provider_call` and invoked by it **at the moment of issuance**, before
    the await — which is the only point that is on every subsequent path,
    including the ones that never produce a result.

    `reconcile()` is the backstop for the opposite risk: a return that reports
    a call nothing announced. It exists because "the caller reads
    `chunks_submitted`" was the contract for a while, and a future
    provider-calling path that forgets to pass the callback should
    under-report nothing rather than silently stop debiting. `_counted` makes
    the pair idempotent per note, so the ordinary path — announce, then
    return, then reconcile — counts exactly once.

    `begin()` starts a note. The backlog loop and the reconciliation sweep each
    build one over the *same* `outcome` and `budget`, so the rule they must not
    drift on lives here once rather than in two hand-written copies.
    """

    def __init__(self, outcome: "EmbedPassResult", budget: "EmbedBudget"):
        self._outcome = outcome
        self._budget = budget
        self._counted = False

    def begin(self) -> None:
        """A new note; nothing has been counted for it yet."""
        self._counted = False

    def issued(self, chunks_submitted: int) -> None:
        """`embed_note`'s `on_provider_call`: a provider call went out."""
        if self._counted or not chunks_submitted:
            return
        self._counted = True
        self._outcome.record_attempt()
        self._budget.debit(chunks_submitted)

    def reconcile(self, result) -> None:
        """A result came back; count it if its call was never announced."""
        self.issued(result.chunks_submitted)


async def _active_scope_count(session) -> int:
    """How many scopes this pass is serving — the budget's on/off switch.

    Counted from `users` rather than threaded down from the loop so that the
    panel's reindex, the startup pass and the periodic tick all get the same
    answer without three call sites agreeing about it. A failure to count
    returns 1, which leaves the pass **unbudgeted** — today's behaviour, and
    the safe direction: a bookkeeping query that fails must not start stopping
    tenants short.
    """
    if not settings.multi_user_mode:
        return 1
    try:
        result = await session.execute(
            text(
                "SELECT count(*) FROM users "
                "WHERE is_active = true AND vault_path IS NOT NULL"
            )
        )
        return int(result.scalar() or 0)
    except Exception as e:
        logger.warning(
            "Could not count active scopes for the embed budget (%s); this "
            "pass runs unbudgeted",
            e,
        )
        return 1


async def _budget_for_pass(session, log_suffix: str) -> EmbedBudget:
    chunk_budget = int(settings.embed_chunk_budget_per_user or 0)
    time_budget = float(settings.embed_time_budget_seconds_per_user or 0)
    if not (chunk_budget or time_budget):
        # Both settings at 0 disables the machinery entirely — no scope count,
        # no clock, nothing to reason about.
        return EmbedBudget()
    scopes = await _active_scope_count(session)
    return EmbedBudget(
        chunk_budget=chunk_budget,
        time_budget=time_budget,
        enforced=scopes > 1,
    )


async def embed_vault(user_id: int | None = None):
    """Embed notes that don't have embeddings yet or have changed.

    Multi-user mode: only embeds notes belonging to `user_id`. Each note's
    embeddings go into `note_embeddings`, which inherits user scope via its
    `note_id` FK back to `notes_metadata`. No `user_id` column on
    `note_embeddings` itself.

    **This pass is deliberately NOT gated on settled provenance, and it
    verifies every hash it certifies. The two halves are one decision.**

    Gating it was specified first and was wrong, because the two rules it would
    sit between compose into indefinite staleness. A permanently unreadable
    file withholds the provenance record forever — by design, so that nothing
    certifies a root the pass could not fully visit — and the gate would turn
    that withheld record into a permanent refusal to embed *anything* for that
    user. Meanwhile the scan keeps working: a readable note the user edits gets
    a fresh `content_hash` on every pass while its `note_embeddings` still hold
    the chunk text of the content it used to have, and `semantic_search` reads
    `chunk_text` with **no** `embedded_content_hash = content_hash` guard. One
    unreadable file would have converted that user's semantic search into a
    silently wrong one, indefinitely, for an agent consumer that acts on the
    result without a human ever seeing the query.

    Running ungated is sound only because of the verification below, and the
    argument is exact. The gate existed to stop a pass writing a row derived
    from one root against a metadata row derived from another. An embedding is
    a **pure function of content**, and the verification refuses to embed any
    bytes that do not hash to the `content_hash` the selected row records — so
    a chunk vector is written against a row only when the bytes it was built
    from are the bytes that row describes, and which directory supplied them is
    not a fact the vector depends on. Under a wrong root the hashes disagree
    and the pass skips.

    Reads are anchored beneath a root this pass pins itself, for the same
    within-pass-consistency reason the scan pins one.

    Returns an `EmbedPassResult` for the pass recorder (#160): the number of
    notes it actually embedded, and the per-note failures it swallowed. Notes
    skipped by an exclude pattern, skipped because their bytes no longer hash to
    their row, or left behind by a pause are **not** counted as embedded: the
    figure answers "how much embedding work did this pass do", and a
    reconciliation stamp is not embedding work. Nor are they counted as
    failures — each is a deliberate decision, not something that went wrong.

    Refuses outright for a quarantined user, before any row is selected and
    before any vector is written or deleted.
    """
    _refuse_quarantined_pass(user_id, "embed")
    vault = _vault_root(user_id)
    log_suffix = f" (user_id={user_id})" if user_id is not None else ""
    logger.info(f"Starting embedding pass...{log_suffix}")

    with pinned_root(vault) as root_fd:
        return await _embed_vault_pinned(user_id, root_fd, log_suffix)


async def _embed_vault_pinned(
    user_id: int | None, root_fd: int, log_suffix: str
) -> EmbedPassResult:
    async with async_session() as session:
        # ── The cheap early exit, and it is only that (D7c, task 5.7a) ─────
        # A stage-head fingerprint read cannot bind a certification that
        # happens after a provider call — the check and the act are separated
        # by a network round trip, which is exactly why the enforcement is the
        # generation lock inside `embed_note`. What this buys is that a
        # container running the previous configuration abandons the stage
        # within one tick instead of grinding through a backlog whose every
        # certification the lock will refuse. It records nothing: a skipped
        # stage did no work and had no failure.
        if not await _embedding_generation_current(session, log_suffix):
            return EmbedPassResult()

        # Find notes without embeddings or with stale embeddings, scoped
        # to this user when set. We bind the user_id parameter even in
        # single-user mode and compare with `IS NOT DISTINCT FROM` so the
        # NULL case still selects all rows without a separate branch.
        #
        # `chunks_truncated` rides along so the marker is only *written* when
        # it has to change: the certifying branch below sets or clears it from
        # `EmbedNoteResult.truncated`, and re-stamping an unchanged boolean on
        # every note of every pass would dirty every row for nothing.
        if user_id is None:
            sql = """
                SELECT nm.id, nm.file_path, nm.content_hash, nm.chunks_truncated
                FROM notes_metadata nm
                WHERE nm.user_id IS NULL
                  AND (nm.embedded_content_hash IS NULL
                       OR nm.embedded_content_hash != nm.content_hash)
                ORDER BY nm.modified_at DESC
            """
            params: dict = {}
        else:
            sql = """
                SELECT nm.id, nm.file_path, nm.content_hash, nm.chunks_truncated
                FROM notes_metadata nm
                WHERE nm.user_id = :uid
                  AND (nm.embedded_content_hash IS NULL
                       OR nm.embedded_content_hash != nm.content_hash)
                ORDER BY nm.modified_at DESC
            """
            params = {"uid": user_id}
        result = await session.execute(text(sql), params)
        unembedded = result.fetchall()
        budget = await _budget_for_pass(session, log_suffix)

        exclude_patterns = settings.embedding_exclude_patterns or []
        if not unembedded:
            logger.info(f"All notes already embedded{log_suffix}")
        else:
            logger.info(f"Embedding {len(unembedded)} notes...{log_suffix}")
        total_chunks = 0
        skipped_excluded = 0
        # Notes this pass embedded and certified, plus the failures it
        # swallowed. `embedded` is incremented only after the per-note commit
        # lands, so a note whose certification was rolled back is not reported
        # as embedded. `attempted` starts at zero and rises with each provider
        # call — never from `len(unembedded)`, which counts work contemplated.
        outcome = EmbedPassResult()
        # One accountant for this user's whole embed stage — the backlog loop
        # below and the reconciliation sweep after it — so the attempt and the
        # chunk debit are recorded where the provider call is *issued* rather
        # than where its result is read.
        accounting = _ProviderCallAccounting(outcome, budget)
        for i, row in enumerate(unembedded):
            entered_embed = False
            accounting.begin()
            # Re-check the pause flag every iteration so a panel-driven pause
            # (e.g. reset-embeddings) stops an in-flight embed pass promptly
            # instead of grinding through the whole backlog first (issue #19).
            if _is_paused():
                logger.info(f"Embedding pass paused, stopping early{log_suffix}")
                break
            # The budget sits beside the pause and is exactly as much of a
            # decision: between notes, never inside one, and never before this
            # user's pass has completed a note. A stop is not a failure and
            # moves neither counter.
            if budget.exhausted():
                budget.stop(log_suffix, "the hash-mismatch backlog")
                break
            try:
                # Skip files matching exclude patterns. Drop any pre-existing
                # embeddings (in case the file was indexed before exclusion was
                # configured) and stamp embedded_content_hash so the indexer
                # doesn't keep re-checking it.
                if any(fnmatch.fnmatch(row.file_path, pat) for pat in exclude_patterns):
                    # **Certified exactly like the embedding path, and for the
                    # same reason.** This branch used to stamp by `id` alone,
                    # which made it the one way to mark a row embedded without
                    # proving the row still describes what was decided about:
                    # a `move_note` out of an excluded folder commits a new
                    # `file_path` with an unchanged `content_hash`, so a stale
                    # decision taken against `Private/A.md` deleted the vectors
                    # and stamped `Public/A.md` as embedded with none. Included,
                    # hash-equal, and therefore never selected again —
                    # permanently absent from `semantic_search`. The predicate
                    # includes the path, so the moved row matches nothing and
                    # `StaleCertification` rolls the whole note back.
                    #
                    # Stamp first, delete second: the conditional UPDATE is what
                    # takes the row lock, so nothing is dropped on the strength
                    # of a row that has since moved.
                    #
                    # **No generation lock here, deliberately — do not "fix"
                    # the asymmetry** (D7c). This branch issues no provider
                    # call and writes no vector; it stamps a row to record that
                    # an *excluded* note has been dealt with, and "the correct
                    # vector set for an excluded note is the empty one" is true
                    # under every embedding configuration. There is nothing a
                    # generation change could invalidate, so there is nothing
                    # for the lock to protect.
                    await certify_embedded(
                        session, row.id, row.content_hash, row.file_path
                    )
                    await session.execute(
                        delete(NoteEmbedding).where(NoteEmbedding.note_id == row.id)
                    )
                    # The note ends this branch with no vectors at all, so a
                    # chunk-cap marker left from a previous embed would claim a
                    # truncation of a set that no longer exists.
                    await _clear_chunks_truncated(session, row)
                    await session.commit()
                    skipped_excluded += 1
                    continue

                try:
                    raw, _stat = read_note_beneath(root_fd, row.file_path)
                except UnicodeDecodeError:
                    logger.warning(f"Skipping non-UTF8 file: {row.file_path}")
                    continue

                # ── Verify the hash before certifying it ──────────────────
                # `embed_note` marks a row embedded by copying the **row's**
                # `content_hash`, not a hash of the bytes it just embedded. So
                # a file that differs from its row at embedding time would be
                # embedded and then permanently marked as embedded for a hash
                # it does not have, and nothing would ever re-embed it.
                #
                # This check does two load-bearing jobs. It is what makes the
                # re-derive branch's retention of `note_embeddings` sound —
                # that branch keeps a vector *because* a matching content hash
                # proves it is the right vector for that file. And it is the
                # **entire licence** for this pass running ungated on
                # provenance (see the docstring): refusing bytes that do not
                # hash to the selected row's `content_hash` means the vector
                # and the row describe the same content whatever directory
                # supplied the bytes.
                #
                # Anyone removing this must re-gate `embed_vault` on settled
                # provenance in the same change.
                if _content_hash(raw) != row.content_hash:
                    logger.info(
                        "Skipping %s: its bytes no longer hash to the indexed "
                        "content_hash, so nothing may be certified against that "
                        "row. A later pass will embed it once the scan has "
                        "refreshed the row.",
                        row.file_path,
                    )
                    continue

                _, content = parse_frontmatter(raw)

                # Get the NoteMetadata object
                note_result = await session.execute(
                    select(NoteMetadata).where(NoteMetadata.id == row.id)
                )
                note = note_result.scalar_one()

                # ── Certify against what was verified, not what was re-read ──
                # The `select` above is a *second* database read, in a later
                # transaction than the one that produced `row`, so it can
                # return a hash another pass has committed since the bytes were
                # verified. Copying that value onto vectors built from `row`'s
                # content marked the row embedded for content it does not have,
                # and the resulting equality then blocked every later repair —
                # permanently wrong semantic results for an agent that acts on
                # them unseen. So the hash and path the bytes were verified
                # against are handed down explicitly: `embed_note` stamps them
                # with a conditional, row-locking UPDATE before it replaces a
                # single vector, and raises `StaleCertification` if the row has
                # moved.
                was_truncated = bool(getattr(note, "chunks_truncated", False))
                # Past this line the note has reached the provider path, so it
                # has reached a note *boundary* whatever happens next — that is
                # what the `finally` below keys on.
                entered_embed = True
                result = await embed_note(
                    session,
                    note,
                    content,
                    certified_hash=row.content_hash,
                    certified_path=row.file_path,
                    # The attempt and the budget debit are recorded by
                    # `embed_note` at the moment it issues the provider call,
                    # not reconstructed here from a result this `try` may never
                    # receive. See `_ProviderCallAccounting`.
                    on_provider_call=accounting.issued,
                )
                accounting.reconcile(result)

                # ── Read the result by field, never as a number ────────────
                # `embed_note` used to return an int and `0` meant three
                # unrelated things — a zero-chunk certification, a swallowed
                # provider exception and a cardinality mismatch — with
                # `outcome.embedded += 1` running after all three. A total
                # provider outage therefore wrote `notes_embedded = N,
                # error = NULL`: the row a healthy pass writes, with a
                # *positive* count (#201).
                if result.outcome in (
                    NoteEmbedOutcome.EMBEDDED,
                    NoteEmbedOutcome.CERTIFIED_EMPTY,
                ):
                    total_chunks += result.chunks_embedded
                    # `CERTIFIED_EMPTY` leaves the note with no vectors at all,
                    # so its marker is cleared for the exclusion branch's
                    # reason; `EMBEDDED` sets it from what the chunker actually
                    # did. Written in the certifying transaction, so the marker
                    # and the vectors it describes land or roll back together.
                    if bool(result.truncated) != was_truncated:
                        note.chunks_truncated = bool(result.truncated)
                    await session.commit()
                    outcome.embedded += 1
                    if result.truncated:
                        # **After the commit, never before** (D3). Logging it
                        # first would leave a permanent ERROR in a bounded,
                        # process-lifetime buffer for a truncation that then
                        # rolled back on a `StaleCertification`, sending an
                        # operator after a note that was never stored that way.
                        # The line cannot name the note's true chunk count —
                        # obtaining it means the unbounded chunking the cap
                        # exists to prevent.
                        logger.error(
                            "Chunking truncated at MAX_CHUNKS_PER_NOTE=%d for "
                            "%s: its first %d chunks are embedded and the note "
                            "is certified, but the tail of it is not "
                            "semantically searchable. notes_metadata."
                            "chunks_truncated is set and every vector-search "
                            "row for this note reports the truncation.",
                            MAX_CHUNKS_PER_NOTE,
                            row.file_path,
                            MAX_CHUNKS_PER_NOTE,
                        )
                elif result.outcome is NoteEmbedOutcome.GENERATION_MISMATCH:
                    # Neither embedded nor a failure: nothing went wrong with
                    # the provider, the configuration moved under the call and
                    # the interlock refused the certification. Nothing was
                    # written, so the note is simply left for a later pass
                    # running whichever configuration is then current.
                    logger.error(
                        "Refused to certify %s: the embedding configuration "
                        "changed while its provider call was in flight, so "
                        "these vectors describe a generation the database no "
                        "longer records. Nothing was written; a later pass "
                        "will embed it.%s",
                        row.file_path,
                        log_suffix,
                    )
                    await session.rollback()
                else:
                    # `PROVIDER_FAILED` / `PROVIDER_CARDINALITY_MISMATCH`.
                    # `embed_note` swallowed the exception, so the pass's own
                    # record is built from the structured failure and from
                    # nothing else.
                    outcome.record_failure_detail(result.failure)
                    await session.rollback()

                if (i + 1) % 50 == 0:
                    logger.info(f"Embedded {i + 1}/{len(unembedded)} notes ({total_chunks} chunks)")
            except StaleCertification as e:
                # Not a failure of this note, and not a hole in the index: the
                # row moved between the byte verification and the
                # certification, so the vectors in hand describe content the
                # row no longer claims. Discard them, leave the row unmarked,
                # and let a later pass embed it as it then stands.
                logger.info("Skipping %s: %s", row.file_path, e)
                await session.rollback()
            except Exception as e:
                # Swallowed on purpose — one poisoned note must not stop the
                # backlog — but **counted**, because the log line is not a
                # record. A provider outage fails every note here and used to
                # produce a run row identical to "nothing to embed": zero
                # embedded, no error. The count and the first message go into
                # the row so the history can tell the two apart.
                outcome.record_failure(e)
                logger.warning(f"Failed to embed {row.file_path}: {e}")
                await session.rollback()
            finally:
                # A note that reached `embed_note` reached a note boundary,
                # including the ones that got there and then raised. Leaving
                # this at the end of the success path meant a note whose
                # certification lost its race never advanced the budget's
                # note counter, so `exhausted()`'s "at least one note
                # completed" guard could never be satisfied by exactly the
                # notes that were burning the provider time.
                if entered_embed:
                    budget.note_finished()

        if outcome.failures:
            logger.error(
                "Embedding pass%s swallowed %s of %s notes: %s",
                log_suffix,
                outcome.failures,
                outcome.attempted,
                outcome.first_error,
            )

        if unembedded:
            logger.info(
                f"Embedding complete{log_suffix}: {len(unembedded)} notes, "
                f"{total_chunks} chunks"
                + (
                    f", {skipped_excluded} skipped by exclude patterns"
                    if skipped_excluded else ""
                )
            )

        # The backlog only ever sees rows whose content changed. Editing
        # `EMBEDDING_EXCLUDE_PATTERNS` changes no content, so without this the
        # new configuration reached only notes that happened to be edited
        # afterwards — see `_reconcile_exclusions`.
        await _reconcile_exclusions(
            session, user_id, root_fd, exclude_patterns, log_suffix,
            outcome, budget,
        )

    return outcome


async def _reconcile_exclusions(
    session,
    user_id: int | None,
    root_fd: int,
    exclude_patterns: list[str],
    log_suffix: str,
    outcome: EmbedPassResult,
    budget: EmbedBudget,
) -> None:
    """Make the stored vectors agree with the *current* exclusion patterns
    (#127, D2).

    The backlog selects on `embedded_content_hash IS NULL OR != content_hash`,
    so it is driven entirely by content changes. `EMBEDDING_EXCLUDE_PATTERNS`
    is configuration: editing it changes no note's content and therefore
    selected nothing. Both directions were permanent:

    * **adding** a pattern left the matching notes' vectors in place, so an
      excluded note kept answering `semantic_search` for ever;
    * **removing** one left the stamp the exclusion branch wrote beside zero
      vectors, so a now-included note stayed silently absent from
      `semantic_search` — hash-equal, never re-selected, with nothing to
      indicate the hole.

    This sweep therefore looks at the rows the backlog *cannot* see: those
    whose certification is current. It writes only where the configuration and
    the stored vectors disagree, and every write goes through the same
    certified `id + content_hash + file_path` predicate as the backlog — never
    a delete by id. A row that moved between the decision and the write fails
    certification, is rolled back, and is left for a later pass.

    **Convergence is defined for a completed sweep.** After one that visited
    every selected row without pause or error, every certification-current row
    has vectors iff the configuration includes it, with three defined
    exceptions: a note whose cleaned content produces zero chunks (correct with
    zero vectors, and deliberately not rewritten — that is the one probe this
    sweep repeats per pass), a row whose bytes no longer hash to it (the
    backlog owns it next pass), and a row whose provider call failed (left
    unstamped, retried). A pause stops it between notes; the next pass runs a
    fresh sweep from the start, and the per-note commits make re-visiting an
    already-repaired row a no-op.

    **It reports into the pass's own accumulator** (#201, D9). On a fully
    indexed vault the backlog is empty and this sweep is the only stage making
    provider calls, so a sweep that swallowed its failures into a log line
    reproduced the falsely-clean run row in the one code path a backlog-only
    fix does not touch. It calls `record_attempt` for the rows it actually
    sends to the provider and **never for the rows it decides about without
    one** — it scans every certification-current row in the scope (~16,700 on
    the production vault), and counting those would render three failures out
    of three calls as "3 of 16,700".

    It draws on the **same per-user budget** as the backlog, since both call
    the provider, and stops at a note boundary exactly as a pause does.
    """
    owner_clause = "nm.user_id IS NULL" if user_id is None else "nm.user_id = :uid"
    params: dict = {} if user_id is None else {"uid": user_id}
    rows = (await session.execute(text(f"""
        SELECT nm.id, nm.file_path, nm.content_hash, nm.chunks_truncated,
               EXISTS (
                   SELECT 1 FROM note_embeddings ne WHERE ne.note_id = nm.id
               ) AS has_vectors
        FROM notes_metadata nm
        WHERE {owner_clause}
          AND nm.embedded_content_hash IS NOT DISTINCT FROM nm.content_hash
        ORDER BY nm.modified_at DESC
    """), params)).fetchall()

    if not rows:
        return

    removed = 0
    restored = 0
    # The same accounting rule as the backlog's, over the same `outcome` and
    # the same `budget`.
    accounting = _ProviderCallAccounting(outcome, budget)
    for row in rows:
        entered_embed = False
        accounting.begin()
        # Between notes only, exactly as the backlog checks it: a partially
        # applied *note* is what the certified predicate exists to prevent.
        if _is_paused():
            logger.info(
                f"Exclusion reconciliation paused, stopping early{log_suffix}"
            )
            break
        # Same budget, same boundary. A sweep stopped here behaves exactly as
        # one stopped by the pause: already-repaired rows stay repaired and the
        # next unexhausted pass runs a fresh, idempotent sweep.
        if budget.exhausted():
            budget.stop(log_suffix, "the exclusion-reconciliation sweep")
            break

        excluded = any(
            fnmatch.fnmatch(row.file_path, pat) for pat in exclude_patterns
        )
        # Agreement, in both directions: excluded with no vectors, or included
        # with vectors. Nothing to decide and nothing written — which is the
        # overwhelmingly common case, so it is the cheap one.
        if excluded != bool(row.has_vectors):
            continue

        try:
            if excluded:
                # Byte-for-byte the backlog's exclusion branch: stamp through
                # the conditional, row-locking UPDATE *first* — that is what
                # takes the lock — then delete. A concurrent move changes
                # `file_path` without touching `content_hash`, so the predicate
                # misses and `StaleCertification` rolls the note back rather
                # than deleting the vectors of a row that is now included.
                # No generation lock, for the backlog exclusion branch's
                # reason: no provider call, no vector written, and a claim that
                # holds under every configuration.
                await certify_embedded(
                    session, row.id, row.content_hash, row.file_path
                )
                await session.execute(
                    delete(NoteEmbedding).where(NoteEmbedding.note_id == row.id)
                )
                await _clear_chunks_truncated(session, row)
                await session.commit()
                removed += 1
                continue

            # Included, and no vectors. The stamp is either a stale exclusion
            # stamp (repair it) or a genuinely empty note (leave it alone).
            try:
                raw, _stat = read_note_beneath(root_fd, row.file_path)
            except UnicodeDecodeError:
                logger.warning(
                    "Skipping non-UTF8 file during reconciliation: %s",
                    row.file_path,
                )
                continue

            # The same verification the backlog runs, and for the same reason:
            # nothing may be certified against a row whose content the bytes do
            # not describe. A mismatch means the scan has not caught up, so the
            # ordinary backlog owns this row on a later pass.
            if _content_hash(raw) != row.content_hash:
                continue

            _, content = parse_frontmatter(raw)
            # The **bounded** chunker, so "this note produces no chunks" means
            # the same thing here as it does in `embed_note`, and so the probe
            # stops at the first chunk instead of chunking a 10 MiB note to
            # find out it is non-empty.
            probe_chunks, _probe_truncated = chunk_text_bounded(
                clean_for_embedding(content),
                chunk_size=settings.chunk_size,
                overlap=settings.chunk_overlap,
                max_chunks=MAX_CHUNKS_PER_NOTE,
            )
            if not probe_chunks:
                # Zero chunks: the row is already exactly right — current
                # certification, zero vectors. Writing anything here would
                # re-stamp an unchanged row on every single pass.
                continue

            note = (await session.execute(
                select(NoteMetadata).where(NoteMetadata.id == row.id)
            )).scalar_one()
            was_truncated = bool(getattr(note, "chunks_truncated", False))
            # Past this line the note has reached the provider path, so it has
            # reached a note boundary whatever happens next.
            entered_embed = True
            result = await embed_note(
                session,
                note,
                content,
                certified_hash=row.content_hash,
                certified_path=row.file_path,
                on_provider_call=accounting.issued,
            )
            accounting.reconcile(result)

            if result.outcome in (
                NoteEmbedOutcome.EMBEDDED,
                NoteEmbedOutcome.CERTIFIED_EMPTY,
            ):
                if bool(result.truncated) != was_truncated:
                    note.chunks_truncated = bool(result.truncated)
                await session.commit()
                if result.truncated:
                    logger.error(
                        "Chunking truncated at MAX_CHUNKS_PER_NOTE=%d for %s "
                        "during exclusion reconciliation: its first %d chunks "
                        "are embedded and the note is certified, but the tail "
                        "of it is not semantically searchable.",
                        MAX_CHUNKS_PER_NOTE,
                        row.file_path,
                        MAX_CHUNKS_PER_NOTE,
                    )
                # **Counted as embedded, like the backlog's** (adversarial
                # review). This sweep commits vectors through the same
                # `certify_embedded` predicate, and on a fully indexed vault it
                # is the *only* stage making provider calls — so leaving
                # `embedded` alone here made `indexer_runs.notes_embedded`
                # under-report exactly the pass whose whole output was the
                # sweep's. Incremented after the commit, so a certification
                # that rolled back is not reported as embedded.
                outcome.embedded += 1
                if result.chunks_embedded:
                    restored += 1
            elif result.outcome is NoteEmbedOutcome.GENERATION_MISMATCH:
                logger.error(
                    "Reconciliation refused to certify %s: the embedding "
                    "configuration changed while its provider call was in "
                    "flight. Nothing was written.",
                    row.file_path,
                )
                await session.rollback()
            else:
                # The sweep's own provider failures ride back on the pass's
                # accumulator rather than dying in a log line — the whole point
                # of D9.
                outcome.record_failure_detail(result.failure)
                await session.rollback()
        except StaleCertification as e:
            logger.info("Reconciliation skipped %s: %s", row.file_path, e)
            await session.rollback()
        except Exception as e:
            # Counted, for the same reason the backlog counts its own: the log
            # line scrolls away and the run row is what survives a redeploy.
            outcome.record_failure(e)
            logger.warning(
                "Reconciliation failed for %s: %s", row.file_path, e
            )
            await session.rollback()
        finally:
            # Same rule as the backlog's: a note that reached `embed_note`
            # reached a note boundary, including one that got there and then
            # raised.
            if entered_embed:
                budget.note_finished()

    if removed or restored:
        logger.info(
            "Exclusion reconciliation%s: %d note(s) had vectors removed, "
            "%d re-embedded",
            log_suffix, removed, restored,
        )


class RebuildSkip(enum.Enum):
    """Why one owner scope's keyword rebuild wrote nothing."""

    #: `_ancillary_pass_is_permitted` refused: that owner's index provenance is
    #: not settled, so an unverified writer of rows the provenance is a claim
    #: about may not run for them.
    PROVENANCE_UNSETTLED = "provenance unsettled"
    #: The owner has no assigned `vault_path`, or the assigned path could not
    #: be pinned.
    ROOT_UNPINNABLE = "root unpinnable"
    #: The published overlap snapshot names this owner's root, so no pass may
    #: read or write for them at all. A skip like any other here: the scope
    #: keeps its previous-configuration vectors, so the coverage claim cannot
    #: be made.
    ROOT_QUARANTINED = "root quarantined"
    #: The pre-lock survey found this scope's root identical to, inside, or
    #: containing another root in the **maintenance** population — which
    #: includes the retained scopes of *inactive* users, whom the serving
    #: snapshot never observes because nothing serves them. Distinct from
    #: `ROOT_QUARANTINED`, which is the serving snapshot's verdict about an
    #: active tenant.
    ROOT_OVERLAPS = "root overlaps another scope"
    #: The pre-lock survey could not observe this scope's root within the
    #: bounded deadline, or could not open it at all. "We could not look" is
    #: not evidence of safety, and it is emphatically not a completed rebuild:
    #: it aborts the coverage claim exactly as the other skips do.
    ROOT_UNEXAMINABLE = "root unexaminable"


@dataclass(frozen=True)
class RebuildOutcome:
    """What one owner scope's rebuild did — **typed, never a row count**.

    The per-owner rebuild returned an `int`, and `0` meant two irreconcilable
    things: "this scope had nothing to do" and "this scope was **skipped**
    because its provenance is not settled". A driver reading `0` as success
    would record a global fingerprint certifying a scope the rebuild
    deliberately declined to touch — precisely the false claim the coverage
    proof exists to remove, and invisible because a skip and an empty scope
    look identical from the outside.
    """

    #: `None` on a completed rebuild; the reason otherwise.
    skip: RebuildSkip | None = None
    #: Rows written. Meaningful only when `completed`.
    rows: int = 0
    #: Free text for the operator — which path could not be pinned, and why.
    detail: str | None = None

    @property
    def completed(self) -> bool:
        return self.skip is None

    def describe(self) -> str:
        if self.completed:
            return f"completed ({self.rows} row(s))"
        return self.skip.value + (f": {self.detail}" if self.detail else "")


class RebuildCoverageAborted(RuntimeError):
    """The all-scopes rebuild could not prove it rebuilt every retained row.

    Raised instead of recording the keyword fingerprint. A fingerprint is one
    stored row asserting something about *every* retained row in the database,
    so "all scopes rebuilt" is not a fact that can be established one user at a
    time: a scope that was skipped, or one the driver could not reach at all,
    falsifies the claim, and a startup that fails closed on that fingerprint
    would then pass while keyword search was exactly as wrong as before.

    The whole operation is one transaction, so raising rolls every scope's
    rebuilt vectors back with it. Three remedies, and the message names them:
    settle the scope (assign or delete the user, or let an in-progress
    re-derive finish), delete or reassign ownerless rows, or put `FTS_CONFIGS`
    back to the value the stored fingerprint names — which clears the refusal
    immediately with no rebuild at all.
    """


async def _scope_vault_path(session, owner: int | None) -> Path | None:
    """The vault root for one retained owner scope, resolved by the driver.

    **Read directly from `users.vault_path`, read-only, inside the operation**
    (D7b, round 3). `_vault_root` and `warm_user_vault_cache` are keyed to
    *active* users because that is whom the periodic pass serves; the coverage
    proof asks a different question — which rows **exist** — and an inactive
    user's rows are as retained, and as returnable by `keyword_search`, as
    anyone's. So an inactive owner with an assigned vault is rebuilt rather
    than skipped, and nothing about the active-user machinery is widened to do
    it: this read is local to the maintenance command and changes which users
    the periodic pass serves not at all.

    `None` means "no assigned path", which the caller turns into
    `ROOT_UNPINNABLE`.
    """
    if owner is None:
        # Only reachable in single-user mode: the driver aborts on a NULL-owned
        # retained scope while `MULTI_USER_MODE` is on, because
        # `_vault_root(None)` refuses there and substituting
        # `settings.vault_path` would read one tenant's notes under an unowned
        # scope.
        return Path(settings.vault_path)
    row = (await session.execute(
        text("SELECT vault_path FROM users WHERE id = :id"), {"id": owner}
    )).first()
    if row is None or not row.vault_path:
        return None
    return Path(row.vault_path)


async def _retained_scopes(session) -> list[int | None]:
    """Every owner scope holding `notes_metadata` rows. **One statement, two
    readers** — the pre-lock survey and the locked driver both enumerate
    through this, so "the roots that were surveyed" and "the roots that are
    opened" are the same question asked twice rather than two questions."""
    return [
        row[0]
        for row in (await session.execute(text(
            "SELECT DISTINCT user_id FROM notes_metadata ORDER BY user_id"
        ))).all()
    ]


@dataclass(frozen=True)
class _RootParticipant:
    """One root the all-scopes rebuild must reason about before it opens any.

    `is_scope` is the half that matters. A **scope** is a root this driver
    would open and read; a **peer** is an active tenant's root it would not,
    present only so that a scope can be found to overlap it.
    """

    owner: int | None
    label: str
    assignment: str
    is_scope: bool


@dataclass
class RebuildRootSurvey:
    """What one bounded look at every root the rebuild would open established.

    `observations` is the **descriptor-bound verdict** — `(st_dev, st_ino)` and
    the canonical real path taken from an opened directory descriptor — and
    `descriptors` holds **that descriptor, still open**, so the locked rebuild
    reads through the object the survey examined rather than through a pathname
    it reopens.

    Reopening was the defect. `pinned_root` calls `os.open` synchronously, and
    doing that after the generation lock is taken means one hung NFS or FUSE mount
    holds the index generation lock for as long as the kernel likes with every
    pass in the process queued behind it — and it is a *second* lookup, so the
    inode it lands on need not be the inode that was checked. Carrying the
    descriptor answers both: no pathname resolution happens under the lock at
    all, and the identity check is a plain `fstat` of the thing about to be
    read.

    **The owner of this object owns the descriptors** and must call `close()`
    on every path — success, abort, and exception. `close()` is idempotent.
    """

    #: Per participant owner, the observation taken before the lock.
    observations: dict[int | None, vault_overlap.RootObservation]
    #: Per participant owner, the canonical assignment that was observed.
    assignments: dict[int | None, str]
    #: Per **scope**, why it may not be rebuilt. Empty means the survey found
    #: nothing; any entry aborts the whole operation.
    failures: dict[int | None, RebuildOutcome]
    #: Canonical assignment → the still-open directory descriptor. Keyed by
    #: **assignment and not by owner** so that two scopes naming one directory
    #: share one descriptor and it is closed exactly once. (That pair is an
    #: `identical` overlap and aborts, but the bookkeeping must not depend on
    #: the check that makes it unreachable.)
    descriptors: dict[str, int] = field(default_factory=dict)

    def descriptor_for(self, owner: int | None) -> int | None:
        """The retained descriptor for this scope's root, or None."""
        assignment = self.assignments.get(owner)
        if assignment is None:
            return None
        return self.descriptors.get(assignment)

    def close(self) -> None:
        """Close every retained descriptor. Idempotent, and it never raises."""
        while self.descriptors:
            _assignment, fd = self.descriptors.popitem()
            vault_overlap.close_root_descriptor(fd)


def _close_late_root_descriptor(task) -> None:
    """Close a descriptor an abandoned observation opened after we gave up.

    The deadline abandons the *wait*, not the syscall (L4): the thread stays
    parked in `open(2)` until the filesystem answers, and when it does it comes
    back holding an open directory nobody is waiting for any more. Without this
    callback that descriptor lives as long as the process — one per stalled
    root per detection, which is a slow leak on exactly the pathological mount
    the deadline exists to survive.
    """
    if task.cancelled():
        return
    if task.exception() is not None:
        return
    _observation, fd = task.result()
    vault_overlap.close_root_descriptor(fd)


async def _observe_root_retaining(
    assignment: str, *, timeout: float | None = None
) -> tuple[vault_overlap.RootObservation, int | None]:
    """One root observed off the loop under the deadline, descriptor retained.

    The maintenance twin of `vault_overlap.observe_root`: same bound, same
    per-root verdict, and it hands back the open directory instead of closing
    it. The caller owns the descriptor.

    Expiry returns `(RootUnexaminable(timeout), None)` exactly as the serving
    observation does, and arms `_close_late_root_descriptor` so a thread that
    answers afterwards does not leave the directory open. The same arming
    covers an outer cancellation, which is the other way this coroutine can
    stop caring about a task that is still running.
    """
    if timeout is None:
        timeout = float(settings.vault_root_observe_timeout_seconds)
    task = asyncio.ensure_future(
        asyncio.to_thread(
            vault_overlap.observe_root_blocking_retaining, assignment
        )
    )
    handed_off = False
    try:
        observation, fd = await asyncio.wait_for(asyncio.shield(task), timeout)
        handed_off = True
        return observation, fd
    except (asyncio.TimeoutError, TimeoutError):
        return vault_overlap.RootObservation(
            assignment=vault_overlap.canonical_assignment(assignment),
            cause=vault_overlap.CAUSE_TIMEOUT,
        ), None
    finally:
        if not handed_off:
            task.add_done_callback(_close_late_root_descriptor)


def _survey_overlap_detail(
    subject: _RootParticipant, peer: _RootParticipant, relation: str
) -> str:
    """The abort text for one surveyed pair. Names both sides and the relation."""
    return (
        f"{subject.label} at '{subject.assignment}' "
        f"{vault_overlap.relation_text(relation)} {peer.label} at "
        f"'{peer.assignment}'"
    )


async def survey_rebuild_roots(
    participants: list[_RootParticipant],
) -> RebuildRootSurvey:
    """Observe every root the rebuild would open, **before** the lock is taken.

    Two defects this closes, and they are the same defect seen from two sides.

    **The population.** `vault_overlap.detect_and_publish` observes *active*
    users holding an assignment, because that is exactly whom the server serves
    and indexes — and the all-scopes rebuild does not serve, it enumerates the
    scopes that hold **rows** (`SELECT DISTINCT user_id FROM notes_metadata`)
    and opens an *inactive* owner's retained root along with everyone else's
    (D7b: an inactive user's rows are as retained, and as returnable by
    `keyword_search`, as anyone's). So the serving snapshot cannot speak for
    this driver: an inactive user retaining `/vaults/team` beside an active
    tenant at `/vaults/team/private` is named by nothing, and the rebuild would
    read that tenant's notes under the inactive owner's scope and write their
    keyword vectors there — under a fingerprint certifying the result. The
    survey therefore runs the same two checks over the population **this
    command** will open: every retained scope, active or not, plus every active
    assigned root as a peer it could collide with.

    **The moment.** The observation is bounded and off the event loop
    (`vault_overlap.observe_root`, `VAULT_ROOT_OBSERVE_TIMEOUT_SECONDS`) and it
    happens **before the generation lock is acquired** — and the descriptor it
    opens is **kept**, so the locked rebuild reads through it and resolves no
    pathname at all. Opening a root synchronously after the lock, which is what
    `pinned_root` did, means one hung NFS or FUSE mount holds the index
    generation lock for as long as the kernel likes with every index pass in
    the process queued behind it, and it is a second lookup that can land on an
    inode nobody examined.

    **This changes the serving snapshot not at all.** It publishes nothing,
    quarantines nobody, and no tool call consults it: it is a maintenance
    verdict about one command's read set, computed and discarded within it. The
    serving population must stay "active users holding an assignment", because
    quarantining an inactive account would refuse nothing (nothing serves it)
    while making an active peer look implicated.

    A verdict per scope, never a global one:

    * a **scope** whose root cannot be observed is `ROOT_UNEXAMINABLE` — "we
      could not look" is not a completed rebuild, and it aborts like any other
      non-completed outcome;
    * a **scope** in any relation (identical, contains, contained by) with any
      other participant is `ROOT_OVERLAPS`, naming the pair;
    * a **peer** that cannot be observed is *not* an abort. Nothing was
      observed to relate it to anything, which is limitation **L2**'s class
      exactly — and failing the whole maintenance command because one unrelated
      tenant's mount is down is the false-positive direction this codebase
      treats as the expensive error.
    """
    distinct = sorted({p.assignment for p in participants})
    observed = await asyncio.gather(
        *(_observe_root_retaining(assignment) for assignment in distinct)
    )
    by_assignment = {a: o for a, (o, _fd) in zip(distinct, observed)}

    # **Only a scope's descriptor is retained.** A peer is present so that a
    # scope can be found to overlap it; nothing reads a peer's directory, so
    # holding its descriptor open across the rebuild would be a plain leak.
    scope_assignments = {p.assignment for p in participants if p.is_scope}
    descriptors: dict[str, int] = {}
    for assignment, (_observation, fd) in zip(distinct, observed):
        if fd is None:
            continue
        if assignment in scope_assignments:
            descriptors[assignment] = fd
        else:
            vault_overlap.close_root_descriptor(fd)

    observations = {p.owner: by_assignment[p.assignment] for p in participants}
    assignments = {p.owner: p.assignment for p in participants}
    failures: dict[int | None, RebuildOutcome] = {}

    for participant in participants:
        if not participant.is_scope:
            continue
        observation = by_assignment[participant.assignment]
        if observation.examinable:
            continue
        failures[participant.owner] = RebuildOutcome(
            skip=RebuildSkip.ROOT_UNEXAMINABLE,
            detail=(
                f"{participant.label} at '{participant.assignment}' could not "
                f"be examined: {vault_overlap.cause_text(observation.cause)}"
            ),
        )

    for i, a in enumerate(participants):
        for b in participants[i + 1 :]:
            if not (a.is_scope or b.is_scope):
                continue
            relation = vault_overlap.relation_between(
                by_assignment[a.assignment], by_assignment[b.assignment]
            )
            if relation is None:
                continue
            inverse = vault_overlap.inverse_relation(relation)
            for subject, peer, rel in ((a, b, relation), (b, a, inverse)):
                if subject.is_scope and subject.owner not in failures:
                    failures[subject.owner] = RebuildOutcome(
                        skip=RebuildSkip.ROOT_OVERLAPS,
                        detail=_survey_overlap_detail(subject, peer, rel),
                    )

    return RebuildRootSurvey(
        observations=observations,
        assignments=assignments,
        failures=failures,
        descriptors=descriptors,
    )


async def _rebuild_root_participants(session) -> list[_RootParticipant]:
    """Every root the all-scopes rebuild would open, plus the active roots it
    could collide with.

    Scopes come from the rows that exist, which is the driver's own population
    and includes inactive owners. Peers are added **only in multi-user mode**
    and only for a user who is not already a scope: in single-user mode
    `users.vault_path` is not the tenancy source at all, and the one root is
    already in the list as the ownerless scope — pairing it against the same
    directory read out of a `users` row would abort a healthy rebuild against
    itself.

    A scope with no assignment contributes nothing to observe. It is left to
    `_rebuild_scope`, which reports it as `ROOT_UNPINNABLE` with the wording an
    operator already knows.
    """
    participants: list[_RootParticipant] = []
    scope_owners: set[int | None] = set()

    for owner in await _retained_scopes(session):
        if owner is None and settings.multi_user_mode:
            # The ownerless coverage abort below owns this case, and it must
            # keep owning it: there is no root to pin for an unowned scope, and
            # substituting `settings.vault_path` here would observe one
            # tenant's directory on behalf of nobody.
            continue
        try:
            vault = await _scope_vault_path(session, owner)
        except Exception:  # noqa: BLE001 - reported by `_rebuild_scope`
            continue
        if vault is None:
            continue
        scope_owners.add(owner)
        participants.append(_RootParticipant(
            owner=owner,
            label=(
                "the single-user scope" if owner is None
                else f"retained scope user_id={owner}"
            ),
            assignment=vault_overlap.canonical_assignment(vault),
            is_scope=True,
        ))

    if settings.multi_user_mode:
        peers = (await session.execute(text(
            "SELECT id, username, vault_path FROM users "
            "WHERE is_active AND vault_path IS NOT NULL ORDER BY id"
        ))).all()
        for peer in peers:
            if peer.id in scope_owners:
                continue
            participants.append(_RootParticipant(
                owner=peer.id,
                label=f"active user '{peer.username}' (user_id={peer.id})",
                assignment=vault_overlap.canonical_assignment(peer.vault_path),
                is_scope=False,
            ))

    return participants


async def _rebuild_scope(
    session,
    owner: int | None,
    survey: RebuildRootSurvey,
) -> RebuildOutcome:
    """Rebuild one retained owner scope. Does **not** commit.

    `survey` carries the descriptor-bound verdict taken **before** the
    generation lock, and it carries the descriptors themselves. When it is
    present this function performs **no pathname lookup at all**: it reads
    through the directory the survey opened, and the only filesystem call it
    makes on the root is `fstat` of that descriptor.

    That is stronger than re-checking the assignment, and it has to be. A
    pathname reopened after the lock is a second lookup — unbounded, so a hung
    mount holds the index generation lock while it waits, and *racy*, so it can
    land on an inode nobody examined. The descriptor is the only object that
    survives the wait still naming the same directory.

    The assignment is still compared, because a scope reassigned between the
    survey and the lock is a scope whose retained descriptor is now the *wrong*
    directory to rebuild — correct in identity, wrong in tenancy — and the
    coverage claim is about the root the user holds now.

    **`survey` is required, and there is deliberately no fallback.** It had a
    `None` default and a `pinned_root` branch behind it; with one caller, which
    always passes a survey, that branch was unreachable in production and was
    an `os.open` of a vault root sitting inside the locked section waiting for
    somebody to reach it. A guarantee with a bypass parameter is a guarantee
    until the next caller.
    """
    log_suffix = f" (user_id={owner})" if owner is not None else ""
    try:
        # The same guard `_rebuild_tsvectors_single_scope_for_tests` applies, applied here because the
        # driver calls the pinned rebuild directly. A quarantined root is a
        # skip like the others, not an exception the driver may step over: the
        # scope keeps its previous-configuration vectors, and the fingerprint
        # would certify them.
        _refuse_quarantined_pass(owner, "tsvector rebuild")
    except VaultRootQuarantined as exc:
        return RebuildOutcome(
            skip=RebuildSkip.ROOT_QUARANTINED, detail=str(exc)
        )
    try:
        vault = await _scope_vault_path(session, owner)
    except Exception as exc:
        return RebuildOutcome(
            skip=RebuildSkip.ROOT_UNPINNABLE,
            detail=f"could not resolve a vault path for user_id={owner}: {exc}",
        )
    if vault is None:
        return RebuildOutcome(
            skip=RebuildSkip.ROOT_UNPINNABLE,
            detail=(
                f"user_id={owner} holds notes_metadata rows but has no "
                "assigned vault_path"
            ),
        )
    observed = survey.observations.get(owner)
    surveyed_assignment = survey.assignments.get(owner)
    retained = survey.descriptor_for(owner)
    canonical = vault_overlap.canonical_assignment(vault)
    if observed is None or surveyed_assignment is None:
        return RebuildOutcome(
            skip=RebuildSkip.ROOT_UNEXAMINABLE,
            detail=(
                f"user_id={owner} was not in the pre-lock root survey, so "
                "its root has not been examined; nothing may be opened "
                "under the generation lock on that basis. Re-run."
            ),
        )
    if canonical != surveyed_assignment:
        return RebuildOutcome(
            skip=RebuildSkip.ROOT_UNEXAMINABLE,
            detail=(
                f"user_id={owner} was assigned '{surveyed_assignment}' "
                f"when the roots were surveyed and '{canonical}' now, so "
                "the verdict does not describe the root about to be read. "
                "Re-run."
            ),
        )
    if retained is None:
        # The survey examined this scope but is not holding its directory
        # open — a timed-out or unopenable root, which is already a
        # failure, or a survey that was closed. Reopening the pathname is
        # exactly what must not happen here.
        return RebuildOutcome(
            skip=RebuildSkip.ROOT_UNEXAMINABLE,
            detail=(
                f"user_id={owner} has no retained descriptor from the "
                "pre-lock survey, and this command does not reopen a "
                "pathname under the generation lock. Re-run."
            ),
        )
    try:
        # The only filesystem call this makes on the root, and it is on the
        # descriptor rather than the name. It cannot fail the way a second
        # `open` can — there is no lookup — so what it actually guards is
        # the bookkeeping: that the fd handed over is the fd whose facts
        # the survey recorded.
        pinned = os.fstat(retained)
    except OSError as exc:
        return RebuildOutcome(
            skip=RebuildSkip.ROOT_UNEXAMINABLE,
            detail=f"{vault}: the retained root descriptor is unusable: {exc}",
        )
    if (pinned.st_dev, pinned.st_ino) != (observed.st_dev, observed.st_ino):
        return RebuildOutcome(
            skip=RebuildSkip.ROOT_UNEXAMINABLE,
            detail=(
                f"{vault}: the retained descriptor does not report the "
                "facts the survey recorded for it, so the examined root is "
                "not the one about to be read. Re-run."
            ),
        )
    return await _rebuild_tsvectors_pinned(
        session, owner, vault, retained, log_suffix
    )


async def rebuild_tsvectors_all_scopes(session) -> dict:
    """Rebuild **every retained owner scope** and record the keyword
    fingerprint, in one transaction. Returns `{owner: RebuildOutcome}`.

    This is the operation `make rebuild-tsvectors` runs, and — beside startup's
    adoption — the only writer of `fts_fingerprint`.

    **Why the driver and not the per-owner rebuild writes it.** A fingerprint
    written inside a per-owner rebuild claims something a per-owner rebuild
    cannot establish: that *every retained row* was rebuilt. Two ordinary
    shapes falsify it — user B's rebuild raising after user A's already wrote
    it, and a scope holding rows the loop never visits at all (an inactive or
    unassigned user; the ownerless scope in a database that also holds named
    users). Either way the stored value certifies rows still carrying the
    previous configuration, and the startup guard that now fails closed on it
    would pass while keyword search was exactly as wrong as before.

    So: take the **account-administration guard** first, survey every root this
    command would open, take the generation lock before reading the first row,
    enumerate the scopes from the rows that exist (`SELECT DISTINCT user_id
    FROM notes_metadata` — not `_active_user_ids()`), rebuild each, and write
    the fingerprint in the same transaction as all of them. Any retained scope
    whose outcome is not completed aborts the whole thing.

    ## Lock order, and why the account guard is here at all

    | # | Lock | Taken |
    | --- | --- | --- |
    | 1 | `ACCOUNT_GUARD_LOCK_KEY` (`lock_account_guard`) | first statement, before the participant enumeration |
    | 2 | `INDEX_GENERATION_LOCK_KEY` (`acquire_generation_lock_unbounded`) | after the survey, before the first row read |
    | 3 | row locks | inside the per-scope rebuild |

    **One direction, everywhere.** No other path in this codebase takes both:
    the account guard is taken alone by the admin handlers
    (`users._lock_admin_guard`), the self-service password change and every
    session mint; the generation lock is taken alone by the index pass, the
    embed certification, the panel's Danger-zone resets and
    `scripts/reset_embeddings.py`. This driver is the only holder of the pair
    and it takes them in the order above, so no cycle exists.

    Without the guard the survey is check-then-act **across processes**. The
    survey accepts a nested pair because the conflicting user is *inactive*
    — inactive users are not peers, correctly, since nothing serves them — and
    an administrator may then reactivate or reassign that user in the panel
    while this command is still running. The reads that follow are exactly the
    cross-tenant read the survey exists to prevent, and no amount of
    re-checking inside this transaction closes it: the edit is a different
    connection committing between the check and the read. The guard is the
    mechanism the panel already uses to serialize precisely those edits, so
    holding it across the whole rebuild makes an edit wait for the rebuild, or
    land before the survey and be seen by it.

    **The cost, stated:** an operator running `make rebuild-tsvectors` blocks
    panel account edits and session mints for the duration — including the wait
    for the generation lock, which is a wait for an in-flight index pass to
    commit. That is accepted. This is a one-off maintenance command an operator
    runs deliberately, the alternative is a cross-tenant read, and the guard
    is transaction-scoped so a crash releases it with no operator action.

    **The survey is this command's own overlap check, and it must be.** The
    published quarantine snapshot answers for the users the server *serves*;
    the scope list here is the users whose rows *exist*, which is a strictly
    larger set. `survey_rebuild_roots` has the reasoning and the population.

    The cost is that a multi-tenant rebuild is all-or-nothing rather than per
    tenant (L5). That is the price of the fingerprint meaning what it says, and
    this is the cheap maintenance path — keyword index only, no provider calls,
    seconds for a few thousand notes.
    """
    # ── Lock 1 of 3: the account-administration guard, before anything is
    # enumerated. See the docstring's lock-order table.
    #
    # The survey below is check-then-act across processes without it. It
    # accepts a nested pair whose conflicting user is *inactive* — correctly,
    # since nothing serves an inactive user — and an administrator may then
    # reactivate or reassign that user in the panel while this command runs,
    # turning the accepted layout into the cross-tenant read the survey exists
    # to prevent. No re-check inside this transaction closes that: the edit is
    # another connection committing between the check and the read. This is
    # the same key `users._lock_admin_guard` takes, so those edits serialize
    # behind this command — they either wait for it, or land before the survey
    # and are seen by it.
    #
    # It is `pg_advisory_xact_lock`, so this transaction's commit or rollback
    # releases it and there is no unlock path to forget.
    await lock_account_guard(session)

    # **Every root this command would open is observed here, before the lock.**
    # `detect_and_publish` speaks only for *active* assigned users, because
    # that is whom the server serves; this driver opens the retained scope of
    # an **inactive** owner too, and an inactive owner retaining `/vaults/team`
    # beside an active tenant at `/vaults/team/private` is named by nothing the
    # serving snapshot publishes. Reading it would file that tenant's notes'
    # keyword vectors under the inactive owner's scope, under a fingerprint
    # certifying the result. So the same two checks run over *this command's*
    # read set — and they run before the generation lock is taken, because a
    # bounded observation that happens after the lock has already let one hung
    # mount hold the index generation lock for as long as the kernel likes.
    #
    # The survey **retains** each scope's directory descriptor, and this
    # function owns them from here: every path below closes them, which is what
    # the `try`/`finally` is for. They are what the locked rebuild reads
    # through, so no pathname is resolved after the generation lock at all.
    #
    # Nothing about the serving snapshot changes: this publishes nothing and
    # quarantines nobody. See `survey_rebuild_roots`.
    participants = await _rebuild_root_participants(session)
    survey = await survey_rebuild_roots(participants)
    try:
        return await _rebuild_all_scopes_locked(session, survey)
    finally:
        # Success, abort and exception alike. A descriptor per tenant leaked
        # once per run of a command an operator may run in a loop is a slow
        # exhaustion of the process's file-descriptor budget.
        survey.close()


async def _rebuild_all_scopes_locked(session, survey: RebuildRootSurvey) -> dict:
    """The body of `rebuild_tsvectors_all_scopes`, with the survey in hand.

    Split out so that the descriptors the survey retains have exactly one
    owner and exactly one closing path — the caller's `finally` — rather than
    a `close()` repeated down every `raise` in here.
    """
    if survey.failures:
        owner, outcome = next(iter(sorted(
            survey.failures.items(), key=lambda item: (item[0] is not None, item[0])
        )))
        raise RebuildCoverageAborted(
            f"keyword rebuild aborted before the generation lock at scope "
            f"user_id={owner!r}: {outcome.describe()}. This command opens "
            "every scope that holds rows — including the retained root of an "
            "inactive user, whom the serving overlap snapshot never observes "
            "because nothing serves them — so the roots it will read are "
            "checked against each other here. Nothing has been read, nothing "
            "has been rebuilt and no fingerprint was recorded. Correct the "
            "overlapping assignment or restore the root, then re-run."
        )

    # Before the first row is read, so nothing can commit a keyword vector
    # between this snapshot and the record. A wait here is a wait for an
    # in-flight index pass to commit, which is the intended behaviour — and
    # the *unbounded* form, because the engine caps a statement at 60s and the
    # pass holds this lock for its whole transaction (L5b). Capped, this
    # command did not wait for a pass at all: it was cancelled after a minute
    # with a query-cancelled error that reads as a broken command rather than
    # as a busy index. The raise is a `SET LOCAL`, which takes no row or table
    # lock, so it precedes the acquisition without disturbing the ordering
    # rule, and it is restored the moment the lock is ours.
    await acquire_generation_lock_unbounded(session)

    # Re-enumerated under the lock, and deliberately not assumed equal to the
    # surveyed set: the survey ran before the lock, so a scope could have
    # appeared in between. Such a scope is a root **nobody examined**, and
    # `_rebuild_scope` refuses it (`ROOT_UNEXAMINABLE`) rather than opening it
    # on the strength of a survey that did not include it. Re-running is cheap;
    # guessing is not.
    scopes = await _retained_scopes(session)

    if settings.multi_user_mode and None in scopes:
        # **Aborts by decision** (L6). `_vault_root(None)` refuses in this mode
        # by design, and substituting `settings.vault_path` would read one
        # tenant's notes under an unowned scope — a tenancy violation performed
        # to satisfy a bookkeeping row. Nor may they be quietly excluded from
        # the coverage proof: they are retained rows `keyword_search` can still
        # return.
        ownerless = (await session.execute(text(
            "SELECT count(*) FROM notes_metadata WHERE user_id IS NULL"
        ))).scalar() or 0
        raise RebuildCoverageAborted(
            f"keyword rebuild aborted: {ownerless} notes_metadata row(s) have "
            "user_id IS NULL while MULTI_USER_MODE is on. They cannot be "
            "rebuilt — there is no vault root to pin for an unowned scope — "
            "and they must not be excluded from the coverage proof, because "
            "keyword_search can still return them. Nothing has been rebuilt "
            "and no fingerprint was recorded. Delete or reassign those rows "
            "and re-run, or restore the FTS_CONFIGS the stored fingerprint "
            "names, which clears the startup refusal with no rebuild at all."
        )

    outcomes: dict = {}
    for owner in scopes:
        outcome = await _rebuild_scope(session, owner, survey)
        outcomes[owner] = outcome
        if not outcome.completed:
            raise RebuildCoverageAborted(
                "keyword rebuild aborted at scope "
                f"user_id={owner!r}: {outcome.describe()}. The fingerprint "
                "asserts that every retained row was rebuilt under the "
                "current FTS_CONFIGS, so one scope it could not rebuild means "
                "the claim cannot be made at all — a skip's zero row count is "
                "not a completed rebuild. Nothing has been committed: every "
                "scope rebuilt so far is rolled back and no fingerprint was "
                "recorded. Settle the scope (assign or delete that user, or "
                "let an in-progress re-derive finish), resolve the root "
                "overlap if it is quarantined, delete or reassign its rows, or "
                "restore the FTS_CONFIGS the stored fingerprint names, which "
                "clears the startup refusal with no rebuild at all."
            )

    # Same transaction as every row above. A failure here is **not** swallowed
    # (D7d): a fingerprint is not instrumentation but the claim a later startup
    # refuses on, and a rebuild that rebuilt everything and lost its write
    # would refuse at every subsequent startup over a database that is actually
    # correct.
    await set_state(session, KEY_FTS_FINGERPRINT, fts_fingerprint())
    await session.commit()
    logger.info(
        "Keyword rebuild complete across %d scope(s); fts_fingerprint recorded "
        "as %s",
        len(outcomes),
        fts_fingerprint(),
    )
    return outcomes


async def _rebuild_tsvectors_single_scope_for_tests(
    session, user_id: int | None = None
) -> int:
    """Recompute `content_tsvector` for one owner scope under the currently
    configured `FTS_CONFIGS` (see `src/services/fts.py`). Returns the count of
    notes updated.

    **Private, and the name says who it is for** (adversarial review). This
    was `rebuild_tsvectors`, a public export — and it commits keyword vectors
    **without taking the generation lock and without re-reading the keyword
    fingerprint under it**, which is precisely the interlock every other
    `content_tsvector` writer has. It had no production caller, so nothing was
    wrong in the tree; but "no caller today" is a fact about today, and a
    plausible-looking public function that quietly writes outside the interlock
    is an invitation. A keyword vector is only ever rewritten when a note's
    content hash changes, so one row written here under a superseded
    `FTS_CONFIGS` keeps that vector indefinitely behind a fingerprint claiming
    otherwise — a keyword search that matches a note not containing the word,
    handed to an agent that acts on it unseen.

    So the operational entry point is `rebuild_tsvectors_all_scopes`, which
    takes the lock before its first read and writes the fingerprint for every
    retained scope in one transaction. This one survives because the tests hold
    its `int` contract over a single scope — atomicity, the certified UPDATE
    predicate, the provenance gate — facts about `_rebuild_tsvectors_pinned`
    that the driver's dict-of-outcomes return would obscure. Anything that
    gives it a production caller must take the generation lock at the head of
    that transaction first, and should almost certainly call the driver
    instead.

    **It records no fingerprint.** A per-owner rebuild cannot establish the
    global claim a fingerprint makes, so that is
    `rebuild_tsvectors_all_scopes`'s job and only its job.

    Run this after changing `FTS_CONFIGS`, since `notes_metadata` stores no raw
    body column — the tsvector must be rebuilt by re-reading each note's file.
    This rebuilds the KEYWORD index only: it does NOT touch embeddings and makes
    NO API calls, so it's cheap (seconds for a few thousand notes), unlike
    `reset-embeddings`.

    Scoped to `user_id` when set (multi-user mode); single-user mode passes
    `None` and rebuilds every note. Reuses `index_tsvector_sql` so the rebuilt
    tsvector is byte-identical to what the indexer would write for the same
    config(s).

    **Gated on settled provenance, per user**, and anchored beneath a root it
    pins itself — see `_ancillary_pass_is_permitted` for why an unverified
    writer of rows the provenance is a claim about may not run under an
    unresolved one. A skipped user is logged once and returns zero.

    Refuses outright for a quarantined user — the standalone rebuild process
    reaches a pass without touching the indexer loop, so the guard has to be
    here rather than in any caller.
    """
    _refuse_quarantined_pass(user_id, "tsvector rebuild")
    vault = _vault_root(user_id)
    log_suffix = f" (user_id={user_id})" if user_id is not None else ""
    with pinned_root(vault) as root_fd:
        outcome = await _rebuild_tsvectors_pinned(
            session, user_id, vault, root_fd, log_suffix
        )
    # The per-owner rebuild no longer commits — the coverage driver needs every
    # scope and the fingerprint in one transaction — so this entry point owns
    # the commit for the single scope it rebuilt.
    await session.commit()
    return outcome.rows


class TsvectorRebuildAborted(RuntimeError):
    """`_rebuild_tsvectors_single_scope_for_tests` met a row it could not certify, and rolled back.

    Deliberately fatal rather than a skip. A rebuild exists to move every row
    onto the *current* `FTS_CONFIGS`, and a row it silently steps over keeps its
    old-config vector with nothing that would ever repair it — the ordinary scan
    only rewrites a note's vector when its `content_hash` changes, and the two
    states that produce this error (a file the rebuild cannot read, and bytes
    that do not hash to the row) are exactly the states where that will not
    happen or cannot be relied on. The rebuild is one transaction, so raising
    rolls the whole thing back: the operator sees an error and re-runs, rather
    than a keyword index silently half-migrated between two configurations.
    """


# How many times one row may be re-read after the snapshot turns out stale. A
# move or a concurrent pass is repaired on the first retry; a row being rewritten
# in a tight loop is not something a rebuild should chase for ever.
MAX_REBUILD_REREADS = 3


async def _reread_rebuild_target(session, note_id: int, owner: int | None):
    """The row as it stands now, scoped to the owner the snapshot recorded.

    Owner-scoped on purpose: a row that is no longer this rebuild's to write is
    indistinguishable from a deleted one *for this rebuild*, and both mean
    "safely absent" — there is nothing left in scope to leave stale.
    """
    return (await session.execute(text(
        "SELECT id, user_id, file_path, content_hash FROM notes_metadata "
        "WHERE id = :id AND user_id IS NOT DISTINCT FROM :uid"
    ), {"id": note_id, "uid": owner})).first()


async def _rebuild_tsvectors_pinned(
    session, user_id: int | None, vault: Path, root_fd: int, log_suffix: str
) -> RebuildOutcome:
    """One owner scope's rebuild. **Does not commit** — the caller owns the
    transaction, which is what lets the coverage driver put every scope and the
    fingerprint in one.

    Returns a `RebuildOutcome` rather than a row count. `0` used to mean both
    "nothing to do" and "skipped, provenance unsettled", and the driver's whole
    job is to tell those apart: recording a global fingerprint over a scope the
    rebuild declined to touch is the false claim the coverage proof exists to
    remove.
    """
    if not await _ancillary_pass_is_permitted(
        session, user_id, vault, root_fd, "Keyword-vector rebuild"
    ):
        return RebuildOutcome(
            skip=RebuildSkip.PROVENANCE_UNSETTLED,
            detail=(
                f"user_id={user_id}: the index provenance for the assigned "
                "root is not settled, so this pass wrote nothing for that "
                "scope"
            ),
        )

    tsv_frag, tsv_params = index_tsvector_sql("content")
    # ── The rebuild certifies what it writes ──────────────────────────────
    # It used to address the UPDATE by `id` alone, over a snapshot holding only
    # `(id, file_path)`, and to treat a read failure as a silent `continue`.
    # Both were unsound for the same underlying reason: the snapshot is taken
    # once and the vault and the table both keep moving underneath it, while a
    # keyword vector is only ever rewritten again when a note's `content_hash`
    # changes.
    #
    # * A note **moved** after the snapshot (either move path, or externally)
    #   fails its old-path read. Silently continuing committed every other note
    #   and left that row on the old configuration for ever: both move paths
    #   preserve `content_tsvector`, and the scan skips a row whose hash has not
    #   changed, so nothing would ever revisit it. `'running'` stays stored as
    #   the english stem `run` and never matches a `simple` query again.
    # * A concurrent index pass that commits `content_hash(C2)` and
    #   `tsvector(C2)` between the rebuild's read of `C1` and its UPDATE had
    #   `tsvector(C1)` written over the top of it while the hash stayed `C2`.
    #   Every later scan skips the row, so the vector describes content the note
    #   does not have: false negatives for C2's terms and false positives for
    #   C1's, permanently.
    #
    # So the snapshot retains owner, path and hash; the bytes are verified
    # against that hash before anything is written; and the UPDATE names all
    # four, requiring exactly one row. Anything else is staleness, and staleness
    # is re-read and retried against the fresh row — or aborts the whole
    # transaction. It is never committed around.
    upd_sql = text(
        f"UPDATE notes_metadata SET content_tsvector = {tsv_frag} "
        "WHERE id = :id AND user_id IS NOT DISTINCT FROM :uid "
        "  AND file_path = :path AND content_hash = :hash"
    )

    rows_stmt = select(
        NoteMetadata.id,
        NoteMetadata.user_id,
        NoteMetadata.file_path,
        NoteMetadata.content_hash,
    )
    if user_id is None:
        rows_stmt = rows_stmt.where(NoteMetadata.user_id.is_(None))
    else:
        rows_stmt = rows_stmt.where(NoteMetadata.user_id == user_id)
    rows = (await session.execute(rows_stmt)).all()
    logger.info(f"Rebuilding tsvectors for {len(rows)} notes{log_suffix}")

    updated = 0
    vanished = 0
    for snapshot in rows:
        owner = snapshot.user_id
        path, chash = snapshot.file_path, snapshot.content_hash
        wrote = False

        for _attempt in range(MAX_REBUILD_REREADS + 1):
            stale_reason: str | None = None
            try:
                raw, _stat = read_note_beneath(root_fd, path)
            except (UnicodeDecodeError, OSError) as exc:
                stale_reason = f"could not be read at {path!r} ({exc})"
            else:
                _, content = parse_frontmatter(raw)
                if _content_hash(raw) != chash:
                    stale_reason = (
                        f"the bytes at {path!r} no longer hash to the "
                        "content_hash the rebuild selected"
                    )
                else:
                    _used, rowcount = await write_tsvector_bounded(
                        session, upd_sql, content,
                        {"id": snapshot.id, "uid": owner, "path": path,
                         "hash": chash, **tsv_params},
                        label=path,
                    )
                    if rowcount == 1:
                        wrote = True
                        break
                    # Zero rows: the row moved, its hash advanced, or it was
                    # deleted, between the read and the write. Nothing was
                    # written — and this must not reach the helper's halving
                    # retreat, which addresses a *size* failure and cannot fix
                    # a stale target.
                    stale_reason = (
                        "the certified UPDATE matched no row, so it moved or "
                        "its content advanced during the rebuild"
                    )

            fresh = await _reread_rebuild_target(session, snapshot.id, owner)
            if fresh is None:
                # Deleted, or no longer owned by the identity this rebuild is
                # running for: nothing in scope is left to leave stale.
                vanished += 1
                break
            if (fresh.file_path, fresh.content_hash) == (path, chash):
                # The row still claims exactly what we tried to write against,
                # so this is not a race we can win by looking again: the file
                # itself is unreadable, or the vault has bytes the scan has not
                # caught up with. Either way nothing will revisit this row —
                # the scan rewrites a keyword vector only when the hash changes,
                # and a note edited and then reverted before the next pass never
                # changes it. Abort, and roll the whole rebuild back.
                raise TsvectorRebuildAborted(
                    f"keyword-vector rebuild aborted: {path!r} {stale_reason}, "
                    "and the row still records that path and content hash. "
                    "Nothing has been committed — the whole rebuild is rolled "
                    "back. Re-run it once the index pass has caught up with "
                    "the vault."
                )
            logger.info(
                "rebuild_tsvectors: %r moved or changed during the rebuild "
                "(%s); retrying against the current row %r",
                path, stale_reason, fresh.file_path,
            )
            path, chash = fresh.file_path, fresh.content_hash
        else:
            raise TsvectorRebuildAborted(
                f"keyword-vector rebuild aborted: {snapshot.file_path!r} kept "
                f"changing across {MAX_REBUILD_REREADS} re-reads, so nothing "
                "could be certified for it. Nothing has been committed — the "
                "whole rebuild is rolled back."
            )

        if not wrote:
            continue
        updated += 1
        if updated % 500 == 0:
            # Progress only — **no intermediate commit** (#127, D4). It used to
            # commit here, so a floor failure a thousand notes in left the
            # keyword index half-rebuilt: the first N notes under the new
            # `FTS_CONFIGS`, the rest under the old one, with no periodic pass
            # that would ever repair them (an unchanged tsvector is never
            # re-selected). Atomic is the only shape an operator can act on —
            # either the rebuild happened or it did not.
            logger.info(f"rebuild_tsvectors: {updated}/{len(rows)} notes{log_suffix}")
    # **No commit here.** It used to commit at the end of each scope, which
    # made one user's index the unit of atomicity; the coverage driver needs a
    # wider one — every retained scope and the fingerprint that describes them,
    # or nothing. The single-scope entry point (`_rebuild_tsvectors_single_scope_for_tests`) commits on
    # this function's behalf. The every-500 intermediate commits are still
    # gone, for #127's reason: a floor failure must roll the whole rebuild back
    # rather than leave a keyword index half-migrated between two
    # configurations that no periodic pass would ever repair.
    logger.info(
        f"rebuild_tsvectors complete: {updated} notes{log_suffix}"
        + (f", {vanished} gone before they could be written" if vanished else "")
    )
    return RebuildOutcome(rows=updated)


async def cleanup_expired_tokens():
    """Delete OAuth codes and tokens that are more than 7 days dead.

    The token half used to read ``expires_at < cutoff OR revoked``, and that
    second disjunct carried **no age condition at all** despite this
    docstring's "older than 7 days" — every revoked token was deleted on the
    next pass, i.e. within `INDEX_INTERVAL_SECONDS` (5 minutes by default).

    That is wrong now that the panel lists revoked tokens as a grant's
    revocation history (issue #64). Filtering revoked rows *out of the page*
    is what made a Revoke that did nothing read as success — the row simply
    disappeared. Deleting them out of the table five minutes later reproduces
    the same blank space, just with a delay.

    **The 7-day window is measured from `expires_at`, not `created_at`, and
    that choice is load-bearing.** Revocation time is not stored anywhere, so
    the window has to be derived from a column we do have:

    * A token can only be revoked while it exists, so its revocation time R
      satisfies ``R <= expires_at`` for every revocation the panel or the token
      endpoint performs. Deleting only when ``expires_at < now - 7d`` therefore
      *guarantees* ``R < now - 7d`` — every revoked row is visible for at least
      seven days after it was revoked.
    * `created_at` gives no such guarantee and gets it backwards: a refresh
      token minted 30 days ago and revoked one minute ago is already 30 days
      past `created_at`, so it would be purged immediately — precisely the case
      the operator most needs to see.

    Age-gating the revoked branch makes it a strict subset of the expiry
    branch, so the correct implementation is the single predicate below rather
    than a redundant `or_`. Revoked tokens are still deleted; they are deleted
    seven days after they would have expired anyway, which is the same
    retention their unrevoked siblings get. The auth-code half is unchanged: a
    used code is spent immediately and has no history value.

    Edge case, stated rather than hidden: a family revocation also flips
    `revoked` on tokens that had *already* expired, so for those R can exceed
    `expires_at`. Such a row is deleted on the ordinary expiry schedule and can
    therefore disappear sooner than seven days after that flip — but it was
    already dead and already displayed as "Expired" before the revocation
    touched it, so no revocation the operator performed becomes invisible.

    **Panel session rows are purged here too, on a different predicate (#198).**
    They are dead credential rows on the same schedule, which is why they share
    this function and why it keeps its name. What they do not share is the
    single `expires_at` comparison above, and the difference is not cosmetic:
    `user_sessions` *does* record a revocation time, and an administrative
    password reset, a deactivation or a soft delete revokes **every** unrevoked
    row of that user — including rows that had already expired. Such a row is
    immediately past `expires_at`, so the expiry-only rule would delete the
    record of a revocation minutes after an operator performed it, which is the
    #64 blank space in a new table. The predicate therefore takes the *later*
    of the two timestamps: `expires_at < cutoff AND (revoked_at IS NULL OR
    revoked_at < cutoff)`. A revoked row is readable for the full retention
    window after the revocation, whenever the revocation happened.

    The session window is `SESSION_PURGE_RETAIN_DAYS` (7, `ge=1` — a zero
    window would delete a revocation the moment it was made) rather than the
    literal above, so an operator can lengthen session retention without
    touching OAuth retention.

    **This runs in both modes.** Single-user mode never creates or validates a
    session row, but a deployment that flipped from multi-user to single-user
    still has rows, and maintenance that skipped them would strand them
    forever.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    session_cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.session_purge_retain_days
    )

    async with async_session() as session:
        # Clean up expired/used auth codes
        result = await session.execute(
            delete(OAuthCode).where(
                or_(
                    OAuthCode.expires_at < cutoff,
                    OAuthCode.used == True,
                )
            )
        )
        codes_deleted = result.rowcount

        # Clean up tokens more than 7 days past their expiry — revoked ones
        # included, and on the same schedule. See the docstring: an unqualified
        # `OR revoked` deleted a revocation's evidence within five minutes of
        # the operator creating it.
        result = await session.execute(
            delete(OAuthToken).where(OAuthToken.expires_at < cutoff)
        )
        tokens_deleted = result.rowcount

        # Panel browser sessions, on the later of expiry and revocation. See
        # the docstring for why the token half's single comparison is wrong
        # here.
        result = await session.execute(
            delete(UserSession).where(
                UserSession.expires_at < session_cutoff,
                or_(
                    UserSession.revoked_at.is_(None),
                    UserSession.revoked_at < session_cutoff,
                ),
            )
        )
        sessions_deleted = result.rowcount

        await session.commit()

        if codes_deleted or tokens_deleted or sessions_deleted:
            logger.info(
                f"Token cleanup: {codes_deleted} codes, {tokens_deleted} tokens, "
                f"{sessions_deleted} panel sessions removed"
            )


def _is_paused() -> bool:
    """Check if a panel-driven action has paused the indexer."""
    try:
        from src.control_panel import routes as panel_routes
        return bool(getattr(panel_routes, "indexer_paused", False))
    except Exception:
        return False


async def _active_user_ids() -> list[int]:
    """Active users with a non-null `vault_path`, **in ascending id order**.

    Empty list in single-user mode (the caller already takes the legacy
    NULL-user path).

    The `ORDER BY` is what makes the order a fact rather than the planner's
    opinion. Without it the same tenant went first on every cycle in practice
    and nothing could be asserted about it in principle — and a rotation over
    an unspecified order is not a rotation.
    """
    async with async_session() as session:
        # Warm the in-process vault-path cache for every active user before
        # the indexer kicks off — saves a per-user lookup later.
        await warm_user_vault_cache(session)
        result = await session.execute(
            select(User.id)
            .where(
                User.is_active.is_(True),
                User.vault_path.isnot(None),
            )
            .order_by(User.id)
        )
        return [row[0] for row in result.all()]


async def _read_rotation_cursor() -> int | None:
    """The persisted `embed_rotation_cursor`, or `None` to start at the first.

    Never raises for the pass's benefit: a read that fails, a table that has
    not been migrated in yet and a value that cannot be parsed all resolve to
    "start at the first tenant", which is a complete and correct pass and is
    precisely today's behaviour. Logged once at WARNING when the stored value
    was there and unusable, so drift is visible without being fatal.
    """
    try:
        async with async_session() as session:
            if not await state_table_exists(session):
                return None
            raw = await get_state(session, KEY_ROTATION_CURSOR)
    except Exception as e:
        logger.warning(
            "Could not read the embed rotation cursor (%s); this cycle starts "
            "at the first tenant",
            e,
        )
        return None
    cursor = parse_rotation_cursor(raw)
    if raw is not None and cursor is None:
        logger.warning(
            "Ignoring the stored embed rotation cursor %r: it is not a usable "
            "user id. This cycle starts at the first tenant in id order. A "
            "cursor is scheduling state — its worst consequence is an order, "
            "so it is never allowed to stop a tenant's indexing.",
            raw,
        )
    return cursor


async def _rotated_user_ids() -> list[int]:
    """`_active_user_ids()` rotated to begin after the persisted cursor.

    Used by `run_indexer_loop` only — the startup pass and the periodic tick.
    `_reindex_background` and the keyword rebuild keep the unrotated list: an
    operator-triggered reindex is not the starvation vector, and letting a
    panel click move the periodic pass's rotation would make the schedule a
    function of who clicked what.

    **The cursor stores a user id, never a positional offset.** The active list
    changes when a user is added, deactivated or deleted, so an offset points
    somewhere else on the next cycle; "resume after id 7" is well defined
    whether or not user 7 still exists, because "the smallest id strictly
    greater than 7" does not require it to. An out-of-range value needs no
    special case either — it selects nothing and wraps to the first, which is
    the same outcome by the ordinary rule.
    """
    ordered = await _active_user_ids()
    if len(ordered) < 2:
        return ordered
    cursor = await _read_rotation_cursor()
    if cursor is None:
        return ordered
    for index, uid in enumerate(ordered):
        if uid > cursor:
            return ordered[index:] + ordered[:index]
    # Nothing is greater: the cycle wraps to the first tenant.
    return ordered


async def _advance_rotation_cursor(user_id: int) -> None:
    """Record that this user's per-user sequence has finished.

    **Its own short session, opened by the holder of `index_pass_lock` after
    the wrapped body's session has closed** — `_write_indexer_run`'s discipline,
    so one task never holds two pooled connections while another waits for the
    lock.

    **Swallow on failure, and this is the one write in this change that does.**
    A lost cursor costs an order; the fingerprints are claims a later startup
    refuses on, and they abort their operation instead.
    """
    try:
        async with async_session() as session:
            if not await state_table_exists(session):
                return
            await set_state(session, KEY_ROTATION_CURSOR, str(user_id))
            await session.commit()
    except Exception as e:
        logger.warning(
            "Could not record the embed rotation cursor at user_id=%s (%s); "
            "the next cycle starts where the previous stored value points",
            user_id,
            e,
        )


# One wall-clock bound for the whole pre-warm. It runs while holding
# `index_pass_lock`, so an unbounded hang would block the panel's reindex and
# reset-embeddings actions indefinitely.
PREWARM_TIMEOUT_SECONDS = 15.0

_HNSW_INDEX_NAME = "ix_note_embeddings_embedding_hnsw"

# Tri-state cache for "does an HNSW index exist on note_embeddings.embedding".
# None = not yet looked up. Deployments with EMBEDDING_DIMENSIONS > 2000 have
# no such index (pgvector's HNSW limit) and must not pay a sequential scan
# every five minutes just to warm a cache. `reset_embeddings` drops and
# recreates the index, so it invalidates this via `invalidate_hnsw_index_cache`.
_hnsw_index_present: bool | None = None


def invalidate_hnsw_index_cache() -> None:
    """Forget the cached `pg_indexes` lookup.

    Called by the panel's reset-embeddings action, which drops the HNSW index
    and only recreates it when the configured dimension allows one — so the
    cached answer can go stale in either direction.
    """
    global _hnsw_index_present
    _hnsw_index_present = None


async def _hnsw_index_exists(session) -> bool:
    global _hnsw_index_present
    if _hnsw_index_present is None:
        result = await session.execute(
            text(
                "SELECT 1 FROM pg_indexes "
                "WHERE tablename = 'note_embeddings' AND indexname = :name"
            ),
            {"name": _HNSW_INDEX_NAME},
        )
        _hnsw_index_present = result.first() is not None
    return _hnsw_index_present


def _probe_vector() -> list[float]:
    """A deterministic non-zero unit vector of `EMBEDDING_DIMENSIONS`.

    Non-zero matters: a zero vector has no cosine direction, so
    `embedding <=> '[0,...]'` is undefined and the scan would not traverse the
    graph — it would warm nothing.
    """
    dim = int(settings.embedding_dimensions)
    return [1.0] + [0.0] * (dim - 1)


# The planner hint the search path uses. Named so the probe and the test that
# EXPLAINs it cannot drift apart from each other.
PROBE_PLANNER_SETTING = "SET LOCAL random_page_cost = 1.1"


def probe_statement():
    """The HNSW probe statement, exactly as `_prewarm_once` issues it.

    Factored out so `tests/integration/test_prewarm_probe.py` can EXPLAIN the
    statement production runs (under `PROBE_PLANNER_SETTING`) instead of a
    hand-copied lookalike: the whole point of the probe is that it walks the
    HNSW index, and only the plan of *this* statement can show that.
    """
    return (
        select(literal(1))
        .select_from(NoteEmbedding)
        .order_by(NoteEmbedding.embedding.cosine_distance(_probe_vector()))
        .limit(1)
    )


async def _prewarm_once() -> tuple[float | None, float | None]:
    """The body of the pre-warm. Returns `(embed_ms, probe_ms)`, either None
    when that half was skipped. Raises freely — the caller contains it."""
    embed_ms: float | None = None
    probe_ms: float | None = None

    # Only local providers have warm state worth keeping. A remote API would
    # just be billed once per tick for nothing.
    if settings.embedding_provider == "ollama":
        from src.services.embeddings import get_embedding

        start = time.monotonic()
        await get_embedding("warmup")
        embed_ms = (time.monotonic() - start) * 1000

    async with async_session() as session:
        if not await _hnsw_index_exists(session):
            logger.info(
                "Pre-warm: HNSW probe skipped (no %s index; "
                "embedding_dimensions=%s exceeds pgvector's 2000-dim limit?)",
                _HNSW_INDEX_NAME,
                settings.embedding_dimensions,
            )
            return embed_ms, None

        # Same planner hint the search path uses, so the probe walks the index
        # and pulls the pages a real search would need — a seq scan here would
        # warm the heap instead, which is not what goes cold.
        await session.execute(text(PROBE_PLANNER_SETTING))
        stmt = probe_statement()
        start = time.monotonic()
        await session.execute(stmt)
        probe_ms = (time.monotonic() - start) * 1000

    return embed_ms, probe_ms


async def prewarm_search_caches() -> None:
    """Keep the embedding model resident and the HNSW hot pages cached.

    `semantic_search` latency is bimodal: ~0.47 s warm, ~17.5 s cold — 14 s of
    that is Ollama reloading bge-m3 after eviction, ~3 s is HNSW index pages
    missing from a 128 MB `shared_buffers` shared with another tenant. As the
    median gap between calls grew from 135 s to 1,676 s, more calls paid the
    cold price (p50 1.2 s → 4.8 s over five weeks). One warm-up per indexer
    tick costs ≈ 0.4 s + 6 ms per five minutes and removes both.

    Runs under `index_pass_lock` (the caller holds it), so it can never overlap
    an index pass, a panel reindex, or a reset-embeddings. Never raises for an
    ordinary failure: a broken embedding provider is the indexer's business,
    not the pre-warm's, and the loop's failure counter must not react to it.
    `CancelledError` is re-raised so lifespan shutdown still stops the loop.
    """
    if settings.mcp_sandbox_mode:
        return
    # Re-checked here, not just before the pass: a panel action can set the
    # pause flag *during* a long index pass, and it does so precisely because
    # it is about to run destructive statements.
    if _is_paused():
        logger.info("Pre-warm skipped (paused)")
        return

    try:
        embed_ms, probe_ms = await asyncio.wait_for(
            _prewarm_once(), timeout=PREWARM_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        # Lifespan shutdown. Must propagate, or the indexer task outlives the
        # app and keeps holding DB sessions.
        raise
    except TimeoutError:
        logger.warning(
            "Pre-warm exceeded %.0fs and was abandoned", PREWARM_TIMEOUT_SECONDS
        )
    except Exception as e:  # noqa: BLE001 - pre-warm must never break the loop
        logger.warning("Pre-warm failed (non-fatal): %s", e)
    else:
        logger.info(
            "Pre-warm complete (embed_ms=%s, probe_ms=%s)",
            "skipped" if embed_ms is None else f"{embed_ms:.0f}",
            "skipped" if probe_ms is None else f"{probe_ms:.0f}",
        )


async def _index_pass_once(user_id: int | None, trigger: str = "scheduled") -> bool:
    """One full index + embed pass for a single user (or single-user mode).

    Returns True only if both stages completed. Failures are swallowed per
    stage so one user's broken vault cannot stop every other user's pass, but
    the caller has to *know* they happened: a tick that logged two failures
    must not stamp the heartbeat as a healthy run (#78). Swallowing and
    returning True is exactly the "reports fine, is not" defect the heartbeat
    exists to remove.

    The swallowed exceptions are also written to the run row's `error` (#160).
    A pass that swallowed a failure and recorded a clean row would reproduce
    the same defect one layer down: the log line scrolls away, the row is what
    survives a redeploy.
    """
    ok = True
    async with record_indexer_run(trigger, user_id) as stats:
        try:
            stats.record_index(await index_vault(user_id=user_id))
        except Exception as e:
            ok = False
            stats.record_error("index", e)
            logger.error(f"Index failed (user_id={user_id}): {e}")
        try:
            stats.record_embedded(await embed_vault(user_id=user_id))
        except Exception as e:
            ok = False
            stats.record_error("embed", e)
            logger.error(f"Embedding failed (user_id={user_id}): {e}")
    return ok


async def run_indexer_loop():
    """Run indexer on startup and then periodically.

    Multi-user mode iterates active users sequentially per pass (v1 simplicity;
    parallelism can come later). Single-user mode runs one legacy pass with
    `user_id=None`.
    """
    # E2 — the startup pass. Before `index_pass_lock`, so the check does not
    # queue behind the pass it gates. The lifespan (E1) has normally published
    # one already; this is not redundant, because `run_indexer_loop` is started
    # by paths that are not the lifespan in tests and could be in future, and a
    # second detection here costs N bounded root observations.
    await detect_root_overlaps("startup")
    # Hold `index_pass_lock` for the initial pass too, so a panel-triggered
    # `_reindex_background` fired during startup is serialized against it.
    startup_ok = True
    async with index_pass_lock:
        if settings.multi_user_mode:
            # One `indexer_runs` row per user (#160), which is why the three
            # stages are one per-user sequence rather than three loops over
            # every user: a run row spanning two loops would have to be held
            # open across them, and a pass's start and finish would then
            # describe the whole startup rather than that user's pass.
            #
            # Per-user ordering — index, then link backfill, then embed — is
            # unchanged and is the ordering that matters (the backfill reads
            # `notes_metadata`, the embed pass reads the hashes the scan
            # wrote). What changes is that one user's failed backfill no longer
            # aborts the backfill of every user after them; it is now isolated
            # exactly like the index and embed stages already were.
            # Rotated, so a startup that a deploy truncated resumes where the
            # previous one stopped rather than sending the tail of the order to
            # the tail again — which is exactly the case an in-process cursor
            # would reset in.
            for uid in await _rotated_user_ids():
                async with record_indexer_run("startup", uid) as stats:
                    try:
                        stats.record_index(await index_vault(user_id=uid))
                    except Exception as e:
                        startup_ok = False
                        stats.record_error("index", e)
                        logger.error(f"Initial index failed (user_id={uid}): {e}")
                    try:
                        # The backfill writes its own `backfill` row; the note
                        # here is so an operator reading a startup row sees
                        # that something in that startup failed.
                        await link_backfill_pass(user_id=uid)
                    except Exception as e:
                        startup_ok = False
                        stats.record_error("link backfill", e)
                        logger.error(f"Link backfill failed (user_id={uid}): {e}")
                    try:
                        stats.record_embedded(await embed_vault(user_id=uid))
                    except Exception as e:
                        startup_ok = False
                        stats.record_error("embed", e)
                        logger.error(f"Initial embedding failed (user_id={uid}): {e}")
                # After this user's whole sequence, success or failure, and
                # **outside** the recorder's context so its own short session
                # has closed first.
                await _advance_rotation_cursor(uid)
        else:
            async with record_indexer_run("startup", None) as stats:
                try:
                    stats.record_index(await index_vault())
                except Exception as e:
                    startup_ok = False
                    stats.record_error("index", e)
                    logger.error(f"Initial index failed: {e}")

                try:
                    await link_backfill_pass()
                except Exception as e:
                    startup_ok = False
                    stats.record_error("link backfill", e)
                    logger.error(f"Link backfill failed: {e}")

                try:
                    stats.record_embedded(await embed_vault())
                except Exception as e:
                    startup_ok = False
                    stats.record_error("embed", e)
                    logger.error(f"Initial embedding failed: {e}")

    # The startup pass counts as a run: it does the same work a tick does, and
    # without it the dashboard would read "Never" for a whole interval after
    # every restart.
    _record_index_run(startup_ok)

    consecutive_failures = 0
    logger.info(
        f"Periodic indexer loop armed (interval={settings.index_interval_seconds}s, "
        f"multi_user={settings.multi_user_mode})"
    )
    while True:
        await asyncio.sleep(settings.index_interval_seconds)
        logger.info("Periodic indexer tick")
        # E3 — every periodic iteration, **before** the pause check and before
        # `index_pass_lock`. Before the pause check because a pause suppresses
        # index and embed work and must not suppress detection: the pause is
        # entered precisely when an operator is doing something destructive and
        # watching the panel, which is the worst moment for a quarantine to go
        # unpublished and unrecorded.
        await detect_root_overlaps("periodic")
        if _is_paused():
            logger.info("Periodic tick skipped (paused)")
            # The snapshot has been published and the ERROR logged; the run rows
            # are the half that survives a restart, so a paused iteration writes
            # them before it returns. No index or embed work is performed.
            await record_quarantined_runs("scheduled")
            continue
        try:
            # Hold `index_pass_lock` for the whole index/embed pass so a
            # concurrent panel-triggered `_reindex_background` cannot run a
            # second index_vault/embed_vault over the same scope.
            tick_ok = True
            async with index_pass_lock:
                if settings.multi_user_mode:
                    # Re-fetch the user list every cycle so newly-added or
                    # newly-deactivated users are picked up without a restart.
                    # One user's failure does not abort the others, but it
                    # does make the whole tick a failed run.
                    for uid in await _rotated_user_ids():
                        if not await _index_pass_once(uid):
                            tick_ok = False
                        # Advanced whether the pass succeeded or failed: the
                        # cursor records where the cycle got to, not whether it
                        # went well, and a failing tenant that pinned it would
                        # starve every tenant after them.
                        await _advance_rotation_cursor(uid)
                else:
                    # Not routed through `_index_pass_once`: that helper
                    # swallows per-stage exceptions so one user cannot stop the
                    # others, and in single-user mode a raising pass must reach
                    # the outer handler so `consecutive_failures` sees it. The
                    # run row is still written — `record_indexer_run` records
                    # the exception on its way past.
                    async with record_indexer_run("scheduled", None) as stats:
                        stats.record_index(await index_vault())
                        stats.record_embedded(await embed_vault())
                # Still under the lock: serialised against a panel reindex and
                # against reset-embeddings, which also takes this lock. It
                # never raises, so `consecutive_failures` cannot react to it,
                # and it delays the next tick by at most PREWARM_TIMEOUT_SECONDS.
                await prewarm_search_caches()
            await cleanup_expired_tokens()
            # The refusal coalescer's standalone flush: any window that closed
            # since the last tick writes the row it owes, so a principal that
            # was refused in a burst and then went quiet still has its count
            # land — otherwise the last window of every burst would wait for
            # the *next* refusal, which by definition may never come.
            #
            # Housekeeping, so a failure here may never fail a pass — the
            # `quota_counters` prune precedent. Every row's own failure is
            # already swallowed and recorded by the usage writer; this guard
            # covers the flush itself.
            try:
                await flush_expired()
            except Exception as e:  # noqa: BLE001 - housekeeping, never fatal
                logger.error(f"Refusal flush failed: {e}")
            consecutive_failures = 0
            # Heartbeat: the tick completed. Recorded whether or not the pass
            # found anything to index — that is the whole point (#78) — but
            # `tick_ok` is False if any per-user pass swallowed an exception,
            # so a multi-user tick that failed for every user is not stamped
            # healthy just because the loop itself survived.
            _record_index_run(tick_ok)
        except Exception as e:
            consecutive_failures += 1
            # A tick that raised still *ran*; the dashboard says so and marks
            # it failed rather than showing a stale-looking success.
            # `CancelledError` is a BaseException and does not land here, so
            # lifespan shutdown is not recorded as a failed pass.
            _record_index_run(False)
            logger.error(f"Periodic task failed ({consecutive_failures} consecutive): {e}")
            if consecutive_failures >= 5:
                logger.critical("Indexer has failed 5+ consecutive times — manual intervention required")
