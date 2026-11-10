"""The write-time task refresh (#428, design D10 R1).

A task tool (`complete_task`, `update_task`, `add_task`) publishes a note and
then calls `refresh_note_tasks`, so that a `list_tasks` right after the write
sees it (read-your-writes). It refreshes **only that note's `note_tasks`
rows** and records which bytes they describe. It does not index the note: the
row's content hash, stat, keyword vector, links, embeddings, `derived_under`
and `extraction_version` stay the pass's alone (the full single-note index was
rejected in D10 as a second writer of the hottest code in the server).

## R1, in order

1. Its own session and transaction, with `SET LOCAL lock_timeout` =
   `TASK_REFRESH_LOCK_WAIT_MS` (2 s).
2. The caller must still be bound to the root the tool ran under — the check
   `change_feed.record_tool_events` makes (`root_binding`); a reassigned
   account writes nothing (`root_changed`).
3. `SELECT id … FOR UPDATE` on the **owner-scoped** row at `rel`. No row (a
   note never indexed, or a quarantined one) writes nothing (`not_indexed`).
3a. **Still current?** Under that row lock the leaf is re-read through the
   call's anchored `target` (the same parent descriptor, `read_bytes_at`
   under `MAX_NOTE_BYTES`) and compared byte for byte with `published`. A
   difference or a failed read means another writer published since: nothing
   is written (`superseded`). The newer bytes are then covered either by
   that writer's own refresh (queued on the same row lock) or by the next
   pass (a changed stat, or R3's read of a task-dirty row).
4. Tasks are derived from **`published`**, held in memory, with
   `tasks.tasks_from_note_text` — never from the re-read, so a refresh can
   never launder a concurrent writer's edit into "tool-refreshed" state. The
   derivation runs off the loop **before** step 1, so neither the row lock nor
   a pooled connection is held while it runs; its result (or its failure) is
   applied only after 3a passes.
5. The note's task rows are deleted and re-inserted with `tasks.note_task_row`,
   the mapping the pass uses, so the two writers agree column for column.
6. `tasks_source_hash = canonical_note_hash(published)`,
   `tasks_refreshed_at = clock_timestamp()`, and `tasks_truncated` /
   `tasks_failed` per the result. **No other column is written.**

## Locks

The only lock taken is the note's row lock, with a bounded wait. **No
generation lock, ever** (`tests/test_note_tasks_extractor.py` pins it with an
AST guard): tasks do not depend on the index fingerprint, and the pass takes
the generation lock *before* row locks, so a writer that takes only one row
lock cannot form a cycle with it — at worst it waits its 2 s and lags.

## Never raises

Every outcome is a `RefreshResult`. A refresh that does not write leaves the
publication standing; the tool says `index: may lag (<code>)` and logs
`task_refresh = lagged:<code>`, and the next pass converges regardless (D10
"Convergence"). The codes:

- `lock_timeout` — the row lock was not granted within the wait;
- `root_changed` — the account is no longer bound to the root the tool ran
  under (or no longer exists);
- `not_indexed` — no index row at the path for this owner;
- `superseded` — step 3a: the file no longer holds the published bytes;
- `not_utf8` — the published bytes are not UTF-8 (the index would not index
  them either);
- `derivation_failed` — the extractor raised; the note's task rows are
  removed and `tasks_failed` set, exactly the pass's failure mark;
- `error` — anything else (a database error), with nothing written.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select, text, update

from src.config import MAX_NOTE_BYTES, TASK_REFRESH_LOCK_WAIT_MS
from src.models.db import NoteMetadata, NoteTask
from src.services.note_hash import canonical_note_hash
from src.services.tasks import note_task_row, tasks_from_note_text

logger = logging.getLogger(__name__)

REFRESHED = "refreshed"
LAGGED = "lagged"

LAG_LOCK_TIMEOUT = "lock_timeout"
LAG_ROOT_CHANGED = "root_changed"
LAG_NOT_INDEXED = "not_indexed"
LAG_SUPERSEDED = "superseded"
LAG_NOT_UTF8 = "not_utf8"
LAG_DERIVATION_FAILED = "derivation_failed"
LAG_ERROR = "error"
LAG_CODES = (
    LAG_LOCK_TIMEOUT,
    LAG_ROOT_CHANGED,
    LAG_NOT_INDEXED,
    LAG_SUPERSEDED,
    LAG_NOT_UTF8,
    LAG_DERIVATION_FAILED,
    LAG_ERROR,
)

#: PostgreSQL's `lock_not_available`, which a `lock_timeout` raises.
_LOCK_NOT_AVAILABLE = "55P03"
_INSERT_BATCH = 1_000


@dataclass(frozen=True)
class RefreshResult:
    """`status` is `refreshed` (code None) or `lagged` (code in `LAG_CODES`).

    `usage` is the D17 `task_refresh` value; `line` the response's D16 line.
    """

    status: str
    code: str | None = None

    @property
    def refreshed(self) -> bool:
        return self.status == REFRESHED

    @property
    def usage(self) -> str:
        return REFRESHED if self.refreshed else f"{LAGGED}:{self.code}"

    @property
    def line(self) -> str:
        return "index: refreshed" if self.refreshed else f"index: may lag ({self.code})"


_REFRESHED = RefreshResult(REFRESHED)


def _lagged(code: str) -> RefreshResult:
    return RefreshResult(LAGGED, code)


def _owner(uid: int | None):
    """The total owner mapping: None is IS NULL. The ONE implementation,
    `tools._note_owner_predicate` (imported at call time, as `task_query`
    does: `tools` imports this module), so tenancy has a single definition."""
    from src.mcp_server import tools

    return tools._note_owner_predicate(uid)


def _is_lock_not_available(exc: BaseException) -> bool:
    """Whether 55P03 is anywhere in `exc`'s wrapper (`.orig`) or cause chain."""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        code = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if code == _LOCK_NOT_AVAILABLE:
            return True
        stack.extend([getattr(current, "orig", None), current.__cause__])
    return False


class _Outcome(Exception):
    """Leave the transaction without committing, with this result."""

    def __init__(self, result: RefreshResult):
        super().__init__(result.code)
        self.result = result


async def refresh_note_tasks(
    *,
    uid: int | None,
    root,
    target,
    rel: str,
    published: bytes,
) -> RefreshResult:
    """Refresh one note's task rows from the bytes this call published (R1).

    `uid` is the caller's owner scope (None in single-user mode), `root` the
    vault root the tool ran under (what `change_feed.root_binding` digests),
    `target` the call's anchored `MutableTarget` (still open), `rel` the
    note's vault-relative path as the index stores it, and `published` the
    exact bytes `write_file_at` published. Call it only after a confirmed
    publication. Never raises.
    """
    try:
        source_hash = canonical_note_hash(published)
        if source_hash is None:
            return _lagged(LAG_NOT_UTF8)
        # 4 (hoisted). Derive from the published bytes, off the loop and
        # **before** the transaction: the derivation is a pure function of
        # `published`, so it never holds the row lock or a pooled connection
        # (a large note costs seconds, and the pass's R4 `FOR UPDATE` would
        # wait on that lock). A failure is recorded under the lock below,
        # exactly as before.
        try:
            derived = await asyncio.to_thread(tasks_from_note_text, published)
        except Exception as exc:  # noqa: BLE001 - recorded as the pass records it
            derived = exc
        from src import database

        async with database.async_session() as session:
            try:
                result = await _refresh_locked(
                    session,
                    uid=uid,
                    root=root,
                    target=target,
                    rel=rel,
                    published=published,
                    source_hash=source_hash,
                    derived=derived,
                )
            except _Outcome as outcome:
                await session.rollback()
                return outcome.result
            except BaseException:
                await session.rollback()
                raise
            await session.commit()
            return result
    except Exception as exc:  # noqa: BLE001 - the write stands either way
        code = LAG_LOCK_TIMEOUT if _is_lock_not_available(exc) else LAG_ERROR
        logger.warning(
            "Task refresh after a task write did not run (%s, %s); the write "
            "stands and the next index pass converges the task rows.",
            code,
            type(exc).__name__,
        )
        return _lagged(code)


async def _refresh_locked(
    session,
    *,
    uid: int | None,
    root,
    target,
    rel: str,
    published: bytes,
    source_hash: str,
    derived,
) -> RefreshResult:
    # 1. The bounded wait, for this transaction only.
    await session.execute(
        text(f"SET LOCAL lock_timeout = '{int(TASK_REFRESH_LOCK_WAIT_MS)}ms'")
    )

    # 2. Still bound to the root the tool ran under (change_feed's check).
    if uid is not None:
        from src.services.change_feed import root_binding

        row = (await session.execute(
            text("SELECT vault_path FROM users WHERE id = :uid"), {"uid": uid}
        )).first()
        if row is None or root_binding(row.vault_path) != root_binding(root):
            raise _Outcome(_lagged(LAG_ROOT_CHANGED))

    # 3. The owner-scoped row, locked.
    note_id = (await session.execute(
        select(NoteMetadata.id)
        .where(NoteMetadata.file_path == rel, _owner(uid))
        .with_for_update()
    )).scalar_one_or_none()
    if note_id is None:
        raise _Outcome(_lagged(LAG_NOT_INDEXED))

    # 3a. Still the bytes this call published?
    from src.services.vault import read_bytes_at

    try:
        current = await asyncio.to_thread(read_bytes_at, target, MAX_NOTE_BYTES)
    except Exception:  # noqa: BLE001 - gone, swapped or over the cap: superseded
        raise _Outcome(_lagged(LAG_SUPERSEDED)) from None
    if current != published:
        raise _Outcome(_lagged(LAG_SUPERSEDED))

    # 4. The tasks derived (before the lock) from the published bytes, never
    # from the re-read: `derived` is `(tasks, truncated)` or the exception.
    if isinstance(derived, BaseException):
        exc = derived
        await session.execute(delete(NoteTask).where(NoteTask.note_id == note_id))
        await _stamp(session, note_id, uid, source_hash, failed=True, truncated=None)
        logger.error(
            "Task derivation failed in a write-time refresh (%s): the note's "
            "task rows are absent and notes_metadata.tasks_failed is set.",
            type(exc).__name__,
        )
        return _lagged(LAG_DERIVATION_FAILED)
    extracted, truncated = derived

    # 5. Delete and insert, as the pass does.
    await session.execute(delete(NoteTask).where(NoteTask.note_id == note_id))
    rows = [note_task_row(note_id, t) for t in extracted]
    for start in range(0, len(rows), _INSERT_BATCH):
        await session.execute(insert(NoteTask).values(rows[start:start + _INSERT_BATCH]))

    # 6. The hash and the timestamp, and the two flags.
    await _stamp(session, note_id, uid, source_hash, failed=False, truncated=truncated)
    return _REFRESHED


async def _stamp(
    session,
    note_id: int,
    uid: int | None,
    source_hash: str,
    *,
    failed: bool,
    truncated: bool | None,
) -> None:
    """Exactly the R1 columns: never `content_hash`, the stat, `derived_under`,
    `extraction_version` or any other."""
    values: dict = {
        "tasks_source_hash": source_hash,
        "tasks_refreshed_at": func.clock_timestamp(),
        "tasks_failed": failed,
    }
    if truncated is not None:
        values["tasks_truncated"] = truncated
    await session.execute(
        update(NoteMetadata)
        .where(NoteMetadata.id == note_id, _owner(uid))
        .values(**values)
    )


__all__ = [
    "LAGGED",
    "LAG_CODES",
    "LAG_DERIVATION_FAILED",
    "LAG_ERROR",
    "LAG_LOCK_TIMEOUT",
    "LAG_NOT_INDEXED",
    "LAG_NOT_UTF8",
    "LAG_ROOT_CHANGED",
    "LAG_SUPERSEDED",
    "REFRESHED",
    "RefreshResult",
    "refresh_note_tasks",
]
