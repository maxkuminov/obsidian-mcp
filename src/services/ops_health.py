"""Read-only backing for the panel's health page (#163).

Three questions, three sources, and only one of them is here:

* **Pass history** — `indexer_runs`, read through
  `src.services.usage_stats.recent_indexer_runs`, which the performance page
  already uses. The health page asks the same function for a longer window
  rather than growing a second query with its own scoping rule.
* **Recent errors** — `src.services.error_log`, an in-process ring buffer. No
  schema, process lifetime only.
* **Backup recency** — this module. `backups_log` (migration 021) is written
  host-side by `make db-backup`, because the container cannot see the backups
  directory and giving it a mount was the alternative rejected.

## The table may legitimately not exist

`make deploy` backs up **before** it migrates, and a database can sit at any
revision below 021 for other reasons (a downgrade, a restore from an older
dump). Reading a table that is not there is a 500 on the whole page, so the
read probes `to_regclass` first and returns None — which the page renders as
"no backup recorded yet", the same state a fresh install is in. Failure posture
throughout: an absent record is a state, never an error.

## What "stale" means, and what it does not

`STALE_AFTER_DAYS = 8`. The threshold is deliberately a day clear of a weekly
cadence: a backup taken every Sunday is at most 7 days old on a good week, and a
7-day threshold would page an operator every Saturday evening for a schedule
that is working. Eight days means one missed run, not one late one.

The signal is **age and nothing else**. Nobody here checks that the file still
exists, that it restores, or that its contents are the database — the panel
cannot see the filesystem it is reporting on, which is the whole reason this
table exists. "The last backup this server knows about was taken N days ago" is
the entire promise, and the page's copy says so rather than implying a verified
backup.
"""
from __future__ import annotations

import datetime

from sqlalchemy import text

#: How many passes the health page's run history shows. The performance page
#: shows 20 as a summary; this is the fuller view the #160 change deferred here,
#: and the table itself keeps 500 rows.
HEALTH_RUNS_LIMIT = 50

#: Older than this and the newest recorded backup is called stale. See the
#: module docstring for why it is 8 and not 7.
STALE_AFTER_DAYS = 8


async def latest_backup(session) -> dict | None:
    """The newest `backups_log` row as a display dict, or None.

    None covers both "no row" and "no table": a fresh install and a pre-021
    database are the same state as far as this page is concerned — nothing has
    been recorded — and distinguishing them on an operator's screen would be
    distinguishing two things they would act on identically.
    """
    present = (
        await session.execute(text("SELECT to_regclass('public.backups_log')"))
    ).scalar()
    if present is None:
        return None

    row = (
        await session.execute(
            text(
                "SELECT created_at, filename, size_bytes FROM backups_log "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            )
        )
    ).first()
    if row is None:
        return None

    created_at = row.created_at
    if created_at is not None and created_at.tzinfo is None:
        # The column is `timestamptz`, so this should not arise; a naive value
        # would otherwise make the subtraction below raise and take the page
        # down over a timezone.
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)

    age = _age(created_at)
    return {
        "created_at": created_at,
        "created_at_iso": created_at.isoformat() if created_at else None,
        "filename": row.filename,
        "size_bytes": int(row.size_bytes) if row.size_bytes is not None else None,
        "size_human": human_size(row.size_bytes),
        "age": age,
        "age_days": None if age is None else age.days,
        "stale": is_stale(created_at),
    }


def _age(created_at: datetime.datetime | None) -> datetime.timedelta | None:
    if created_at is None:
        return None
    return datetime.datetime.now(datetime.timezone.utc) - created_at


def is_stale(created_at: datetime.datetime | None) -> bool:
    """True when the newest recorded backup is older than the threshold.

    A missing record is **not** stale — it is unknown, and the page says so
    with its own copy. Reporting "stale" for a fresh install would be a warning
    about a backup nobody has failed to take yet.
    """
    age = _age(created_at)
    if age is None:
        return False
    return age > datetime.timedelta(days=STALE_AFTER_DAYS)


def human_size(size_bytes) -> str | None:
    """A dump size for display. Base-1024, one decimal above KiB."""
    if size_bytes is None:
        return None
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return None  # pragma: no cover - the loop always returns
