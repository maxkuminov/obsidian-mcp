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
import math
from dataclasses import dataclass

from sqlalchemy import text

from src.auth.session import current_user_id
from src.database import async_session
from src.models.db import DAILY_REQUEST_LIMIT_MAX, DAILY_REQUEST_LIMIT_MIN
from src.services import refusals, security_events
from src.services.usage_stats import OVER_QUOTA_PARAM

logger = logging.getLogger(__name__)

__all__ = [
    "ADMISSION_SQL",
    "DAILY_REQUEST_LIMIT_MAX",
    "DAILY_REQUEST_LIMIT_MIN",
    "OVER_QUOTA_PARAM",
    "PRUNE_AFTER_DAYS",
    "QUOTA_REFUSAL_SCOPE",
    "Admission",
    "admit",
    "apply_daily_request_limit",
    "as_utc",
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
#: contended path.
#:
#: **Its trigger is per-key; its scope is global.** One key's first call of the
#: day deletes *every* key's rows older than the cutoff, not just its own. That
#: is deliberate and is the only reason the table stays bounded: what
#: accumulates is rows belonging to keys nobody is calling any more, and a
#: per-key prune is by construction never run for those keys again.
#:
#: **Accepted cost:** the prune runs before `admit()` returns, so the request
#: that happens to be a key's first of the day pays for the housekeeping. It is
#: one indexed `DELETE` over a table holding at most one row per key per
#: retained day, it happens at most once per key per day, and its failure is
#: swallowed rather than charged to the caller. Moving it to a background task
#: would buy a few milliseconds for that one request at the cost of a scheduler
#: this server does not otherwise need.
PRUNE_SQL = text("DELETE FROM quota_counters WHERE day < :cutoff")

#: The `scope` an over-quota refusal reports: the ceiling belongs to one API
#: key, not to a user and not to an OAuth grant (which this quota does not
#: reach at all). Named here so this module's renderer and the decorator's own
#: fallback rendering cannot drift into two different words for one fact.
QUOTA_REFUSAL_SCOPE = "api_key"


def utc_day(now: _dt.datetime | None = None) -> _dt.date:
    """The UTC date a call falls in. The counter's `day`, always."""
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc).date()


def as_utc(moment: _dt.datetime) -> _dt.datetime:
    """One clock reading, normalised to UTC. Naive input is assumed UTC.

    The same normalisation `utc_day` applies before taking a date, factored out
    so the instant stored on an `Admission` and the day bound into the
    statement are demonstrably the *same* reading rather than two readings that
    usually agree.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc)


def reset_instant(day: _dt.date) -> _dt.datetime:
    """Midnight UTC at the end of `day` — when that day's counter starts again.

    Takes the **accounting day**, not a clock reading. That is the whole point:
    the reset an over-quota caller is told about must be derived from the day
    the admission statement actually decided against, never from a second look
    at the clock. See `Admission.reset_at`.
    """
    return _dt.datetime.combine(
        day + _dt.timedelta(days=1), _dt.time.min, tzinfo=_dt.timezone.utc
    )


@dataclass(frozen=True)
class Admission:
    """What one admission attempt decided, and the day it decided it for.

    **The day travels with the decision.** `admit()` reads the clock once to
    pick the accounting day, binds it into the statement, and returns it here;
    every consequence of the decision — most importantly the reset instant the
    refusal quotes — is derived from *this* `day` rather than from a fresh
    `datetime.now()`.

    That is not fastidiousness. A refusal that straddles UTC midnight reads the
    clock twice: the statement decides against day D (the counter for D is
    full) and a message built from a second clock read is already in day D+1,
    so it names D+2's midnight and tells an obedient agent to back off for
    nearly forty-eight hours instead of the few milliseconds actually left.
    An agent that believes its own error message would then have quota it could
    not spend — a self-inflicted outage, caused by the one thing on this path
    that was allowed to be non-deterministic.
    """

    #: The UTC date the admission statement was bound to.
    day: _dt.date
    #: The day's admission count including this call, or None when refused.
    count: int | None
    #: The instant `admit()` read the clock to pick `day` — the *same*
    #: reading, not a second one taken alongside it. Everything a refusal
    #: quotes about time is derived from this pair (`day`, `decided_at`) and
    #: from nothing else, which is what makes the refusal reproducible: the
    #: message is a pure function of the decision, so however long the caller
    #: takes to render it, it cannot describe a different decision than the
    #: one the statement made.
    decided_at: _dt.datetime

    @property
    def admitted(self) -> bool:
        return self.count is not None

    @property
    def reset_at(self) -> _dt.datetime:
        """When this decision stops applying. Derived from `day`, not the
        clock, so it is correct however long the caller takes to read it."""
        return reset_instant(self.day)

    @property
    def retry_after_seconds(self) -> int:
        """Whole seconds from the decision to the reset, never below one.

        The interval a refused caller is told to wait, derived here so that
        **no** downstream renderer has to read the clock a second time (D10).
        Both halves of the arithmetic are the decision's own: `reset_at` comes
        from the day the statement was bound to, `decided_at` from the reading
        that picked it.

        `max(1, …)` is what keeps the UTC-midnight case honest. A decision made
        a hundred milliseconds before midnight quotes one second — not zero,
        which invites a retry that arrives before the counter has rolled, and
        not a negative number, which `Refusal` rejects outright. A decision
        whose reset has already passed by the time the message is built (the
        caller was slow, not the clock wrong) quotes one second for the same
        reason.
        """
        return max(1, math.ceil((self.reset_at - self.decided_at).total_seconds()))


def quota_refusal_message(
    limit: int,
    reset_at: _dt.datetime,
    retry_after_seconds: int | None = None,
) -> str:
    """The refusal an over-quota caller receives, in place of its tool result.

    It names the limit and the reset instant because the reader is an agent:
    "quota exceeded" gives it nothing to act on, while a number and a timestamp
    let it either wait or tell its operator exactly what to raise.

    `reset_at` is **passed in**, from `Admission.reset_at`, and is never
    recomputed here. A message that re-read the clock could name the wrong
    midnight for a call that crossed one between the statement and the string.
    The prose is unchanged from #162 — the machine-readable line is *appended*
    (#194), so every `in` / `startswith` assertion written against the wording
    still holds.

    `retry_after_seconds` is `Admission.retry_after_seconds` — the interval the
    decision itself derived, never one measured here. Given it, this function
    appends the sentinel line through `refusals.render`; without it the caller
    holds only a reset instant and cannot honestly quote an interval, so the
    prose is returned bare for that caller to render with the interval it does
    hold. The over-quota code is **not** one of the futile ones: a refusal that
    omitted the retry field would tell an agent that waiting cannot help, which
    is exactly wrong for a ceiling that resets at midnight. So the sentinel is
    appended only where the number is known, never with the field dropped.

    `refusals.render` is idempotent, so a caller that renders again over this
    message gets it back untouched — the two altitudes cannot stack two
    sentinel lines on one refusal.
    """
    prose = (
        f"Error: this API key has used its daily request limit of {limit} "
        f"tool calls for the current UTC day. No further calls will run until "
        f"the limit resets at {reset_at.strftime('%Y-%m-%dT%H:%M:%SZ')} (the "
        "next UTC midnight). Wait for the reset, or ask an administrator to "
        "raise this key's daily request limit in the control panel."
    )
    if retry_after_seconds is None:
        return prose
    return refusals.render(
        prose,
        refusals.Refusal(
            code=refusals.OVER_QUOTA,
            scope=QUOTA_REFUSAL_SCOPE,
            limit=limit,
            limit_unit=refusals.CALLS_PER_DAY,
            retry_after_seconds=retry_after_seconds,
        ),
    )


async def admit(key_id: int, limit: int, now: _dt.datetime | None = None) -> Admission:
    """Consume one slot for `key_id` today, or report that none is left.

    Returns an `Admission` carrying the accounting day and the day's count
    including this call (None when refused). Commits in its own transaction and
    releases the connection before returning, so the caller's tool body never
    waits on it.

    **The clock is read exactly once**, here, to pick `day`; the returned
    `Admission` carries both the day and that very instant (`decided_at`), so
    neither the refusal's reset instant nor the interval it quotes can drift
    from the decision that produced them. Nothing downstream reads the clock
    again — the retry interval is arithmetic on the two values recorded here
    (D10, #194).

    A failure of the admission statement itself is logged and re-raised: the
    call does not run (fail closed, deliberately — see the module docstring),
    but it also does not vanish. The exception would otherwise propagate past a
    `_tracked` that never reached `_log_usage`, leaving an enforcement outage
    with no line anywhere naming the key it happened to.

    The prune runs only on the INSERT branch — a `RETURNING count` of 1 is the
    row having just been created, since the `DO UPDATE` adds to a count that is
    at least 1 — and its failure is swallowed: a call that has already been
    admitted must not be turned into an error by housekeeping.
    """
    # The one reading. `day` is taken *from it* rather than beside it, so the
    # statement's day and the instant the refusal measures against are the same
    # observation by construction and not by two calls happening to agree.
    decided_at = as_utc(now or _dt.datetime.now(_dt.timezone.utc))
    day = utc_day(decided_at)
    async with async_session() as session:
        try:
            count = (
                await session.execute(
                    ADMISSION_SQL, {"key_id": key_id, "day": day, "limit": limit}
                )
            ).scalar()
            await session.commit()
        except Exception as exc:
            # Fail closed *and* visible. Re-raised unchanged so the behaviour
            # is exactly what it was; logged because a quota that has stopped
            # deciding is an incident, and this is the only place that knows
            # which key and which day it stopped deciding for.
            security_events.emit(
                "quota_admission_failed",
                level=logging.ERROR,
                subject=security_events.subject_for(user_id=current_user_id.get()),
                key_id=key_id,
                day=day.isoformat(),
                error_type=type(exc).__name__,
            )
            raise

        if count == 1:
            try:
                await session.execute(
                    PRUNE_SQL, {"cutoff": day - _dt.timedelta(days=PRUNE_AFTER_DAYS)}
                )
                await session.commit()
            except Exception as exc:  # pragma: no cover - housekeeping only
                await session.rollback()
                security_events.emit(
                    "quota_counter_prune_failed",
                    subject=security_events.subject_for(user_id=current_user_id.get()),
                    error_type=type(exc).__name__,
                )
        return Admission(day=day, count=count, decided_at=decided_at)


#: Deletes the current day's counter for one key. Issued only on the
#: NULL-to-limited transition, in the same transaction as the limit write.
_CLEAR_TODAY_SQL = text(
    "DELETE FROM quota_counters WHERE key_id = :key_id AND day = :day"
)


async def apply_daily_request_limit(
    session, api_key, limit: int | None, now: _dt.datetime | None = None
) -> bool:
    """Write a key's limit, resetting the day's counter iff this enables one.

    **The one implementation of the enable-reset rule**, shared by the panel
    form and the JSON API. Two copies of "did this transition go NULL to a
    value" is how the two surfaces start disagreeing about whether an operator
    is charged for traffic that was unlimited when it happened — and the
    disagreement would be invisible, because both would look like they worked.

    Returns whether the counter was reset, for the caller's own reporting.

    The rule, both halves deliberate:

    * **NULL to a value resets.** Consumption is defined as admissions *since
      the limit was enabled*, so the current UTC day's counter row is deleted
      in the same transaction as the write. A key that made forty calls this
      morning with no limit set reads 0/100 the moment a limit of 100 is set.
      Charging an operator for traffic that was explicitly unlimited when it
      happened is the surprise that gets the feature turned off again.
    * **A value to another value keeps.** Those calls *were* admitted under a
      quota, and forgiving them would make "lower the limit" a way to grant
      more calls.

    Clearing to NULL keeps nothing meaningful either way — an unlimited key
    performs no accounting — so the row is left to the ordinary prune.

    **One transaction for both statements.** Split, a crash between them leaves
    a key limited with this morning's unlimited traffic already charged
    against it.
    """
    enabling = api_key.daily_request_limit is None and limit is not None
    api_key.daily_request_limit = limit
    if enabling:
        await session.execute(
            _CLEAR_TODAY_SQL, {"key_id": api_key.id, "day": utc_day(now)}
        )
    await session.commit()
    return enabling


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
