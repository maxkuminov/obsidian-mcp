"""A load-tolerant linearity check for the scanner tests (#401).

The old checks timed one call at `n` and one at `2n` and required a ratio
under 4. Linear is 2 and quadratic is 4, so the margin was a factor of two on
a measurement of a millisecond or less, and a scheduler hiccup or a cache
miss on a loaded host flipped it.

This check keeps what those tests prove (growth is not superlinear) with a
margin that load cannot eat:

* **An 8x size step.** Linear growth reads about 8, `n log n` about 10,
  `n^1.5` about 23 and quadratic about 64; the bound of 20 sits well clear of
  linear and below every polynomial worse than `n^1.45`.
* **No sub-millisecond measurements.** The call is repeated until the small
  input costs at least `min_seconds`, and the large input is timed with the
  same repetition count, so timer granularity and one-off interrupts are a
  small fraction of either figure.
* **Interleaved rounds, best of each.** A burst of load lands on both sizes
  rather than on one, and the minimum discards it.
* **CPU time** (`time.process_time`), which does not count the time this
  process spends waiting for a core (#244).
"""
from __future__ import annotations

import math
import time

SIZE_FACTOR = 8
MAX_RATIO = 20


def _run(fn, arg, reps, clock):
    start = clock()
    for _ in range(reps):
        fn(arg)
    return clock() - start


def growth_ratio(fn, small, large, *, min_seconds=0.02, rounds=3,
                 clock=time.process_time):
    """`(ratio, per_call_small, per_call_large)` for `fn` over the two inputs.

    `large` should be `SIZE_FACTOR` times `small`; the caller asserts
    `ratio < MAX_RATIO`.
    """
    fn(small)  # warm the interpreter, not the measurement
    reps = 1
    while _run(fn, small, reps, clock) < min_seconds and reps < 1 << 16:
        reps *= 2
    best_small = best_large = math.inf
    for _ in range(rounds):
        best_small = min(best_small, _run(fn, small, reps, clock))
        best_large = min(best_large, _run(fn, large, reps, clock))
    ratio = best_large / max(best_small, 1e-9)
    return ratio, best_small / reps, best_large / reps
