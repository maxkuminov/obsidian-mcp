"""One caller-visible shape for every refusal a tool call can receive.

`usage_logs.params["error"]` is an *operator's* field — nothing the caller ever
sees — so "typed, actionable refusal" has to mean something the agent on the
other end can parse. That is this module: a `Refusal` value, a closed set of
codes, and one renderer that appends a single machine-readable final line to
the human prose a tool already returns:

```
MCP-REFUSAL {"code":"rate_limited","scope":"principal","limit":120,"limit_unit":"calls_per_minute","retry_after_seconds":3}
```

The sentinel is line-initial and the JSON is one line, so the pair survives
being quoted into a transcript, and appending it is **additive**: every
existing refusal keeps its wording, so every `in` / `startswith` assertion that
was written against that wording still holds.

**This module imports nothing from the application.** `tools.py`, `quotas.py`,
`embeddings.py` and `rate_limits.py` all render refusals, and a shared
vocabulary that imported any of them could not be used by the others without a
cycle. That is also why `ProviderInputTooLarge` — raised by the embedding
providers, handled by the search tools — is declared *here* rather than in
`src/services/embeddings.py`: the code that raises it and the code that handles
it then share one dependency-free contract and neither depends on the other's
module.

**`retry_after_seconds` is present only where retrying can succeed.** The
whole seconds until a token is available for a bucket refusal, the interval to
the next UTC reset for a quota refusal — and **absent** for a refusal a retry
cannot fix, so that no refusal invites a loop that cannot end. `Refusal`
enforces the two the spec names outright (`no_vault_assigned`,
`argument_not_encodable`) and permits every other code to omit it; where it is
present it must be a whole number of at least one second. The codes that
deliberately omit it are named at their definitions below.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

#: The line-initial token that introduces the machine-readable line. Fixed, and
#: deliberately not a word an ordinary sentence of refusal prose starts with.
SENTINEL = "MCP-REFUSAL"

# ── The closed code set ─────────────────────────────────────────────────────
#
# A *caller-facing* vocabulary, which is not the same thing as the
# operator-facing `usage_logs` marker register in
# `docs/architecture/usage-attribution.md`: the two answer different questions
# and are permitted to differ. The clearest case is the provider input-limit
# rejection, whose caller-facing code is `argument_too_long` (the caller's
# query was too big — one actionable failure mode) while its marker is
# `provider_input_rejected` and post-body (the body ran and made a network
# call, so the row belongs inside the latency percentiles).

RATE_LIMITED = "rate_limited"
ARGUMENT_TOO_LONG = "argument_too_long"
OVER_QUOTA = "over_quota"
NO_VAULT_ASSIGNED = "no_vault_assigned"
ARGUMENT_NOT_ENCODABLE = "argument_not_encodable"
VAULT_ROOT_OVERLAP = "vault_root_overlap"
VAULT_ROOT_UNEXAMINABLE = "vault_root_unexaminable"
VAULT_ROOT_NOT_READY = "vault_root_not_ready"

CODES = frozenset(
    {
        RATE_LIMITED,
        ARGUMENT_TOO_LONG,
        OVER_QUOTA,
        NO_VAULT_ASSIGNED,
        ARGUMENT_NOT_ENCODABLE,
        VAULT_ROOT_OVERLAP,
        VAULT_ROOT_UNEXAMINABLE,
        VAULT_ROOT_NOT_READY,
    }
)

#: The two codes for which a retry interval is **forbidden**, not merely
#: omitted: an unassigned vault and an unencodable argument are facts about the
#: caller's account and the caller's own bytes, and neither changes because
#: time passed. Quoting any number there would tell an obedient agent to sleep
#: and try again forever.
FUTILE_CODES = frozenset({NO_VAULT_ASSIGNED, ARGUMENT_NOT_ENCODABLE})

#: The scope strings the two token buckets refuse under. `principal_write` and
#: `principal` are different facts about the same tool, so they are never
#: merged — in the refusal the caller reads, and (see `rate_limits.py`) in the
#: coalescing key the operator's row is written under.
SCOPE_PRINCIPAL = "principal"
SCOPE_PRINCIPAL_WRITE = "principal_write"

#: `limit_unit` values. A limit without its unit is a number an agent cannot
#: act on: 120 what, per what?
CALLS_PER_MINUTE = "calls_per_minute"
CALLS_PER_DAY = "calls_per_day"
CHARACTERS = "characters"


class RefusalShapeError(ValueError):
    """A `Refusal` that could not be honestly rendered.

    Raised at construction, so a bad refusal fails where it is built rather
    than reaching an agent as a line it cannot parse.
    """


@dataclass(frozen=True)
class Refusal:
    """The machine-readable half of one refusal.

    Every field except `code` is optional and rendered as JSON `null` when the
    gate has nothing to say — `retry_after_seconds` alone is *omitted* rather
    than nulled, because "absent" is the contract's own way of saying "do not
    retry this".
    """

    code: str
    scope: str | None = None
    limit: int | None = None
    limit_unit: str | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.code not in CODES:
            raise RefusalShapeError(
                f"{self.code!r} is not one of the declared refusal codes: "
                + ", ".join(sorted(CODES))
            )
        retry = self.retry_after_seconds
        if retry is not None:
            if self.code in FUTILE_CODES:
                raise RefusalShapeError(
                    f"{self.code!r} must not carry retry_after_seconds: "
                    "retrying it can never succeed."
                )
            if isinstance(retry, bool) or not isinstance(retry, int) or retry < 1:
                raise RefusalShapeError(
                    "retry_after_seconds must be a whole number of at least 1 "
                    f"second, not {retry!r}."
                )

    def as_payload(self) -> dict:
        """The JSON object the sentinel line carries."""
        payload: dict = {
            "code": self.code,
            "scope": self.scope,
            "limit": self.limit,
            "limit_unit": self.limit_unit,
        }
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        return payload


def sentinel_line(refusal: Refusal) -> str:
    """The single final line, rendered.

    `separators` without spaces and `ensure_ascii=False` keep it to one compact
    line; `sort_keys` is deliberately **not** used, so the field order is the
    declaration order a reader of this module sees.
    """
    return f"{SENTINEL} " + json.dumps(
        refusal.as_payload(), ensure_ascii=False, separators=(",", ":")
    )


def has_sentinel(text: str) -> bool:
    """Does `text` already end with a rendered sentinel line?"""
    if not text:
        return False
    return text.rsplit("\n", 1)[-1].startswith(f"{SENTINEL} ")


def render(prose: str, refusal: Refusal) -> str:
    """`prose`, unchanged, with the sentinel line appended.

    **Idempotent.** A refusal message is built at one altitude and rendered at
    another — `quotas.quota_refusal_message` composes the over-quota prose, the
    decorator decides the retry interval — and the two must not be able to
    stack two sentinel lines on one message by both doing their job. A message
    that already ends in a rendered line is returned untouched.
    """
    if has_sentinel(prose):
        return prose
    return f"{prose}\n{sentinel_line(refusal)}"


class ProviderInputTooLarge(Exception):
    """An embedding provider refused the input as too large for *its* limit.

    Declared here, and not in `src/services/embeddings.py`, so the code that
    raises it and the code that handles it share a module that imports neither.

    A character cap is necessary and not sufficient: 8,192 characters of a
    densely-tokenizing script can still exceed a provider's token limit, so the
    cap cannot promise the provider will accept the input. The search tools
    translate this exception into the ordinary `argument_too_long` refusal
    carrying `reason`, so an agent sees one actionable failure mode for "the
    query was too large" whichever limit actually applied.
    """

    def __init__(self, reason: str, *, provider: str | None = None) -> None:
        super().__init__(reason)
        #: The provider's own stated reason, passed through to the caller.
        self.reason = reason
        #: Which provider said so, for the operator-facing log. Optional: the
        #: caller-facing message is the same either way.
        self.provider = provider
