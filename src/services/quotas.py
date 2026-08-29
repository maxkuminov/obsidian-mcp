"""Per-key daily request quotas (#162): admission, display, validation.

The whole feature is opt-in and per API key. `api_keys.daily_request_limit` is
NULL for every key until an operator sets one, and a key with a NULL limit
issues **zero** quota statements — `_tracked` never calls into this module for
it, so the hot path is byte-for-byte what it was before the feature existed.

## The admission is one statement, and that is the design

    INSERT INTO quota_counters (key_id, day, count) VALUES (:k, :d, 1)
    ON CONFLICT (key_id, day) DO UPDATE SET count = quota_counters.count + 1
    WHERE quota_counters.count < :limit
    RETURNING count

A returned row is the admission. No row means the ceiling is already reached
and the call is refused before its tool body runs.

What this buys, and what the obvious alternatives do not:

* **It is atomic under concurrency.** `SELECT count(*) FROM usage_logs …` and
  then a decision is raceable in the way that matters — two calls both read "99
  used, limit 100" and both run. PostgreSQL evaluates the `DO UPDATE … WHERE`
  while holding the conflicting row's lock, so exactly `limit` calls are
  admitted per UTC day however many arrive at once.
  `tests/integration/test_issue_162_quotas_pg.py` proves that with real
  concurrent statements against a real database rather than by argument.
* **It survives a restart and spans workers.** An in-memory counter does
  neither, and "the quota resets when the container is redeployed" is not a
  quota.
* **It does not serialize the key.** An advisory lock around a COUNT would put
  every one of a key's calls through one lock; this contends only on the single
  counter row, and only for the duration of one upsert.

## Refusals never consume

The guarded UPDATE *declines* when the count is already at the limit, so a
refused call leaves the counter exactly where it was. An agent looping on the
refusal cannot push the number past the ceiling, and at the next UTC midnight
exactly `limit` new calls are admitted. The refusal is still written to
`usage_logs` — with `over_quota: true`, the marker
`src/services/usage_stats.py` already enumerates as a pre-body refusal — so the
pressure is visible on `/admin/performance` without being folded into a latency
percentile.

## An admitted call that then fails has still consumed

The admission commits in its **own** transaction before the tool body starts,
and nothing releases the slot afterwards. That direction is deliberate in both
halves: incrementing on completion instead would admit unboundedly many
concurrent calls (the whole point is to decide before the work), and refunding
a failed call makes a tool that always fails free — which is precisely the
runaway agent a quota exists to stop.

## The transaction is short and holds nothing the body needs

`admit()` opens its own `async_session()`, runs the statement, commits and
exits — the same shape `_log_usage` uses — so the pooled connection is back in
the pool before the tool body asks for one. Holding a connection across the
body would turn a five-connection pool into a five-concurrent-call server.

## A database failure is not silently "unlimited"

If the admission statement raises, the exception propagates: the call does not
run. This is the same failure every database-touching tool already has, and it
is the honest one — swallowing it would mean a database blip quietly disables
every configured ceiling, which is the one failure mode nobody would notice.
Keys with no limit are unaffected, because they never reach this code.

## UTC, everywhere

`day` is a UTC date computed here and bound as a parameter — never
`now()::date`, which is the server's timezone. A limit that resets at an hour
nobody administering it can name is not a limit anybody can reason about, and
the reset instant is quoted verbatim in the refusal so an agent can back off
rather than spin.
"""
from __future__ import annotations

import datetime as _dt
import logging

from sqlalchemy import text

from src.database import async_session
from src.models.db import DAILY_REQUEST_LIMIT_MAX, DAILY_REQUEST_LIMIT_MIN
from src.services.usage_stats import OVER_QUOTA_PARAM

logger = logging.getLogger(__name__)

__all__ = [
    "ADMISSION_SQL",
    "DAILY_REQUEST_LIMIT_MAX",
    "DAILY_REQUEST_LIMIT_MIN",
    "OVER_QUOTA_PARAM",
    "PRUNE_AFTER_DAYS",
    "admit",
    "consumed_today",
    "limit_value_error",
    "parse_limit_form_value",
    "quota_refusal_message",
    "reset_instant",
    "utc_day",
]

#: How long a counter row outlives its day before the next day's first
#: admission prunes it. Two days rather than one: "yesterday" must still be
#: readable while a day rolls over across a fleet of workers whose clocks agree
#: only to within a second, and the row is twelve bytes.
PRUNE_AFTER_DAYS = 2

#: The admission statement, verbatim and in one place. Written as SQL rather
#: than assembled through the ORM's `on_conflict_do_update`, because the
#: guarded `WHERE` on the `DO UPDATE` — the clause the whole atomicity argument
#: rests on — is the part a reviewer must be able to read without expanding a
#: builder in their head.
ADMISSION_SQL = text(
    "INSERT INTO quota_counters (key_id, day, count) VALUES (:key_id, :day, 1) "
    "ON CONFLICT (key_id, day) DO UPDATE SET count = quota_counters.count + 1 "
    "WHERE quota_counters.count < :limit "
    "RETURNING count"
)

#: The opportunistic prune. Runs only after an admission whose `RETURNING
#: count` is 1 — the INSERT branch, i.e. the first admission of a new UTC day
#: for that key — so it happens at most once per key per day and never on the
#: contended path. It is global rather than per-key on purpose: what bounds the
#: table is old rows belonging to keys nobody is calling any more, and a
#: per-key prune never reaches those.
PRUNE_SQL = text("DELETE FROM quota_counters WHERE day < :cutoff")


def utc_day(now: _dt.datetime | None = None) -> _dt.date:
    """The UTC date a call falls in. The counter's `day`, always."""
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc).date()


def reset_instant(now: _dt.datetime | None = None) -> _dt.datetime:
    """The next UTC midnight — when this key's counter starts again.

    Quoted in the refusal so an agent that reads its own error can back off
    until then instead of retrying in a loop.
    """
    day = utc_day(now)
    return _dt.datetime.combine(
        day + _dt.timedelta(days=1), _dt.time.min, tzinfo=_dt.timezone.utc
    )


def quota_refusal_message(limit: int, now: _dt.datetime | None = None) -> str:
    """The refusal an over-quota caller receives, in place of its tool result.

    It names the limit and the reset instant because the reader is an agent:
    "quota exceeded" gives it nothing to act on, while a number and a timestamp
    let it either wait or tell its operator exactly what to raise.
    """
    reset = reset_instant(now)
    return (
        f"Error: this API key has used its daily request limit of {limit} "
        f"tool calls for the current UTC day. No further calls will run until "
        f"the limit resets at {reset.strftime('%Y-%m-%dT%H:%M:%SZ')} (the next "
        "UTC midnight). Wait for the reset, or ask an administrator to raise "
        "this key's daily request limit in the control panel."
    )


async def admit(key_id: int, limit: int, now: _dt.datetime | None = None) -> int | None:
    """Consume one slot for `key_id` today, or return None if none is left.

    Returns the day's admission count including this one, or None when the
    ceiling is already reached. Commits in its own transaction and releases the
    connection before returning, so the caller's tool body never waits on it.

    The prune runs only on the INSERT branch — a `RETURNING count` of 1 is the
    row having just been created, since the `DO UPDATE` adds to a count that is
    at least 1 — and its failure is swallowed: a call that has already been
    admitted must not be turned into an error by housekeeping.
    """
    day = utc_day(now)
    async with async_session() as session:
        admitted = (
            await session.execute(
                ADMISSION_SQL, {"key_id": key_id, "day": day, "limit": limit}
            )
        ).scalar()
        await session.commit()

        if admitted == 1:
            try:
                await session.execute(
                    PRUNE_SQL, {"cutoff": day - _dt.timedelta(days=PRUNE_AFTER_DAYS)}
                )
                await session.commit()
            except Exception as exc:  # pragma: no cover - housekeeping only
                await session.rollback()
                logger.warning("quota_counter_prune_failed", extra={"error": str(exc)})
        return admitted


async def consumed_today(session, key_ids, now: _dt.datetime | None = None) -> dict:
    """`{key_id: admissions today}` for the keys that have a counter row.

    A key with no row is absent from the mapping, and the caller renders 0 —
    which is the truth twice over: it has admitted nothing today, and a limit
    enabled today deleted the row so consumption is counted from the
    enablement.

    Read from `quota_counters`, never as a `COUNT(*)` over `usage_logs`: the
    log counts every request including the refused ones and including traffic
    from before the limit existed, and neither is what an operator reading
    "43 / 100" is being told.
    """
    ids = [int(k) for k in key_ids]
    if not ids:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT key_id, count FROM quota_counters "
                "WHERE day = :day AND key_id = ANY(:ids)"
            ),
            {"day": utc_day(now), "ids": ids},
        )
    ).fetchall()
    return {row.key_id: int(row.count) for row in rows}


def limit_value_error(value: int) -> str | None:
    """The message for an out-of-domain limit, or None when it is acceptable.

    The database's CHECK says the same thing; this exists so an operator sees a
    sentence instead of a 500. Both layers, because a constraint is what makes
    the invariant true of the data and a message is what makes it fixable.
    """
    if value < DAILY_REQUEST_LIMIT_MIN:
        return (
            f"Daily request limit must be at least {DAILY_REQUEST_LIMIT_MIN}. "
            "Leave it empty for unlimited; to stop a key entirely, revoke it."
        )
    if value > DAILY_REQUEST_LIMIT_MAX:
        return (
            "Daily request limit must be at most "
            f"{DAILY_REQUEST_LIMIT_MAX:,}."
        )
    return None


def parse_limit_form_value(raw: str | None) -> tuple[int | None, str | None]:
    """A form field to `(limit, error)`. Empty means unlimited, not zero.

    `("", None)` and `(None, None)` both mean "no limit" — an operator clearing
    the box is clearing the limit, which is the documented way to return a key
    to unlimited. A value that is not an integer is an error rather than a
    silent clear, because silently discarding "1oo" would look exactly like
    success.
    """
    if raw is None:
        return None, None
    text_value = raw.strip()
    if not text_value:
        return None, None
    try:
        value = int(text_value)
    except ValueError:
        return None, (
            "Daily request limit must be a whole number, or empty for unlimited."
        )
    error = limit_value_error(value)
    if error is not None:
        return None, error
    return value, None
