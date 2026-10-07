"""Point-in-time peak-load baselines.

The first (simplest) baseline is climatology/persistence: estimate
``P(M_d > K)`` from the empirical distribution of daily peak load over a rolling
window of recent operating days strictly before the target day. No lookahead:
future and same-day values are never used. A seasonal (multi-year month/day)
climatology is the natural upgrade once multiple years are backfilled.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

D = Decimal


def _norm_day(s: str) -> str:
    """Normalize ``MM/DD/YYYY`` (ERCOT CSV) to ``YYYY-MM-DD``."""
    s = s.strip()
    if "/" in s:
        m, d, y = s.split("/")
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return s


class ClimatologyPeakModel:
    """Empirical survival of recent daily peak load (rolling window)."""

    def __init__(self, peaks_by_day: dict[str, Decimal], window_days: int = 60,
                 min_samples: int = 20):
        self.by_day: dict[str, Decimal] = {
            _norm_day(k): v for k, v in peaks_by_day.items()
        }
        self.window_days = window_days
        self.min_samples = min_samples

    def _sample(self, target_day: str) -> list[Decimal]:
        try:
            target = date.fromisoformat(target_day)
        except ValueError:
            return []
        out = []
        for d, peak in self.by_day.items():
            try:
                dd = date.fromisoformat(d)
            except ValueError:
                continue
            if dd < target and (target - dd).days <= self.window_days:
                out.append(peak)
        return out

    def p_peak_gt(self, target_day: str, as_of: datetime, strike: Decimal) -> Decimal | None:
        sample = self._sample(target_day)
        if len(sample) < self.min_samples:
            return None
        exceed = sum(1 for p in sample if p > strike)
        return D(exceed) / D(len(sample))

    def peak_quantile(self, target_day: str, q: float) -> Decimal | None:
        """q-th quantile (0..1) of the recent peak distribution."""
        sample = sorted(self._sample(target_day))
        if len(sample) < self.min_samples:
            return None
        idx = min(int(q * len(sample)), len(sample) - 1)
        return sample[idx]
