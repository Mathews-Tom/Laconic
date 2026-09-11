"""Read the last written avoided-cost estimate, with its age.

``laconic status`` is instant because it reads only runtime ledgers.
Producing an estimate means joining every session transcript against them,
which takes tens of seconds on a real corpus -- too slow to run on a
command whose value is that it answers immediately.

So ``status`` reads the last report off disk instead of recomputing one. A
stale figure that says how stale it is beats no figure at all, and it beats
making the fast command twenty times slower. Age is always reported, never
inferred by the reader, and the estimate is never silently refreshed.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: How old a band may be before the caller suggests recomputing it. An hour
#: is long enough that a session's worth of new decisions has probably
#: landed, and short enough that a figure quoted from it is still about
#: today's corpus.
STALE_AFTER_SECONDS: Final = 3600.0


@dataclass(frozen=True, slots=True)
class CachedEstimate:
    """One previously written avoided-cost band and how old it is."""

    low_usd: float
    high_usd: float
    low_pct: float
    high_pct: float
    denominator_usd: float
    age_seconds: float

    @property
    def is_stale(self) -> bool:
        """Whether the figure is old enough that rerunning would change it.

        A freshly computed band does not need a "rerun" hint attached to
        it: printing one unconditionally makes the command look as though
        it had no effect, which is exactly the confusion it should remove.
        """
        return self.age_seconds >= STALE_AFTER_SECONDS

    @property
    def age_text(self) -> str:
        """Coarse, honest age. Precision here would imply freshness."""
        minutes = self.age_seconds / 60.0
        if minutes < 1.0:
            return "just now"
        if minutes < 60.0:
            return f"{minutes:.0f}m ago"
        hours = minutes / 60.0
        if hours < 24.0:
            return f"{hours:.0f}h ago"
        return f"{hours / 24.0:.0f}d ago"


def read_cached_estimate(report_json: Path, *, now: float | None = None) -> CachedEstimate | None:
    """Return the last written estimate, or ``None`` if there is not one.

    ``None`` covers every reason a band cannot be shown -- no report has
    been generated, the file is unreadable, it predates this field, or the
    corpus could not support an estimate. The caller prints how to produce
    one rather than printing a zero.
    """
    try:
        payload: Any = json.loads(report_json.read_text(encoding="utf-8"))
        modified = report_json.stat().st_mtime
    except (OSError, ValueError):
        # `ValueError` rather than `json.JSONDecodeError`: a report holding
        # non-UTF8 bytes raises `UnicodeDecodeError`, which is a `ValueError`
        # and not a `JSONDecodeError`. Catching only the narrower type let it
        # escape into `status`, which has no handler of its own, aborting the
        # command with a traceback after it had already printed half its
        # output. `ValueError` subsumes both.
        return None
    if not isinstance(payload, dict):
        return None
    estimate = payload.get("estimate")
    if not isinstance(estimate, dict):
        return None
    try:
        values = [
            float(estimate["avoided_cost_usd_low"]),
            float(estimate["avoided_cost_usd_high"]),
            float(estimate["avoided_share_pct_low"]),
            float(estimate["avoided_share_pct_high"]),
            float(estimate["denominator_usd"]),
        ]
    except (KeyError, TypeError, ValueError):
        return None
    # `json.loads` accepts bare `NaN` and `Infinity`, and `float()` passes
    # them through, so a corrupt report would render as "$nan to $inf". The
    # write path already refuses non-finite values before serializing; this
    # is the same standard applied on the way back in.
    if not all(math.isfinite(value) for value in values):
        return None
    low_usd, high_usd, low_pct, high_pct, denominator = values
    return CachedEstimate(
        low_usd=low_usd,
        high_usd=high_usd,
        low_pct=low_pct,
        high_pct=high_pct,
        denominator_usd=denominator,
        age_seconds=max(0.0, (time.time() if now is None else now) - modified),
    )
