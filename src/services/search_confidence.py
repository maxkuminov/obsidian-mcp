"""The `confidence: ok | low` rule shared by `semantic_search` and
`hybrid_search` (#389, design D1).

One pure function, no I/O. `low` means all three of:

- the threshold (`SEARCH_LOW_CONFIDENCE_SIMILARITY`) is above 0 — 0 is a
  hard off, so a negative cosine can never fire it;
- no keyword note matched all terms (`keyword_all == 0`): a `match: any`
  row is disjunctive filler, not evidence, and `semantic_search` has no
  keyword arm, so it passes 0;
- the best similarity of the call's actual candidate set is `None` (no
  candidate, or the semantic arm did not run) or strictly below the
  threshold — a similarity exactly at the threshold reads `ok`.

The token never filters, reorders or changes a candidate. The hint lines
below are the only text a `low` page adds, and the renderers withdraw them
before they could cost a row (D4).
"""
from __future__ import annotations

from typing import Literal

Confidence = Literal["ok", "low"]

#: The upper bound on either hint line (D4): the hybrid line is exactly 140,
#: the semantic line 132.
CONFIDENCE_HINT_MAX = 140

#: `hybrid_search`'s hint, after the header and any `index_transition:` line.
HYBRID_LOW_CONFIDENCE_HINT = (
    "low confidence: no all-terms keyword match, weak similarity; try "
    "`keyword_search` with an identifier or `get_neighborhood` from a known note"
)

#: `semantic_search`'s sibling hint (it has no keyword arm).
SEMANTIC_LOW_CONFIDENCE_HINT = (
    "low confidence: weak similarity and no keyword arm; try `keyword_search` "
    "with an identifier, or `get_neighborhood` from a known note"
)

assert len(HYBRID_LOW_CONFIDENCE_HINT) <= CONFIDENCE_HINT_MAX
assert len(SEMANTIC_LOW_CONFIDENCE_HINT) <= CONFIDENCE_HINT_MAX


def confidence(keyword_all: int, top_similarity: float | None,
               threshold: float) -> Confidence:
    """`"low"` exactly when `threshold > 0`, `keyword_all == 0`, and
    `top_similarity` is None or strictly below `threshold`; else `"ok"`."""
    if not threshold > 0:
        return "ok"
    if keyword_all:
        return "ok"
    if top_similarity is None or top_similarity < threshold:
        return "low"
    return "ok"


def token(value: Confidence) -> str:
    """The header token, `confidence: ok` or `confidence: low`."""
    return f"confidence: {value}"
