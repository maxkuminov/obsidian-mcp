"""Pure terminal body results; classification never inspects response text."""
from __future__ import annotations

from typing import Literal

from src.services.refusals import BODY_CODES, PRECONDITION_CODES, Refusal, render

BODY_MARKERS = BODY_CODES | PRECONDITION_CODES | {"provider_input_rejected"}
Disposition = Literal["refused", "partial"]


class BodyOutcome(str):
    """A wire-compatible string with explicit, non-serialized outcome metadata.

    Construction is pure: only the final value returned from a body is logged.
    Use with_prose when composing explanations, never interpolate this rendered
    string into another outcome (which would embed its generated sentinel).
    """

    def __new__(
        cls, prose: str, refusal: Refusal, *, marker: str | None = None,
        disposition: Disposition = "refused",
    ):
        marker = refusal.code if marker is None else marker
        if marker not in BODY_MARKERS:
            raise ValueError("Undeclared body outcome marker")
        if disposition not in {"refused", "partial"}:
            raise ValueError("Undeclared body outcome disposition")
        if disposition == "partial" and refusal.nothing_written is not None:
            raise ValueError("Partial work must not claim nothing_written")
        obj = super().__new__(cls, render(prose, refusal, authoritative=True))
        obj.prose = prose
        obj.refusal = refusal
        obj.marker = marker
        obj.disposition = disposition
        return obj

    def with_prose(self, prose: str) -> BodyOutcome:
        return BodyOutcome(prose, self.refusal, marker=self.marker, disposition=self.disposition)

    def __reduce__(self):
        # Pydantic model_copy(deep=True) must preserve the private typed value.
        return (_restore, (self.prose, self.refusal, self.marker, self.disposition))


def _restore(prose, refusal, marker, disposition):
    return BodyOutcome(prose, refusal, marker=marker, disposition=disposition)


def body_refusal(
    prose: str, code: str, *, disposition: Disposition = "refused",
) -> BodyOutcome:
    return BodyOutcome(prose, Refusal(code=code), disposition=disposition)
