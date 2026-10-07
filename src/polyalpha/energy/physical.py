"""Point-in-time peak-load baselines.

The first (simplest) baseline is climatology/persistence: estimate
``P(M_d > K)`` from the empirical distribution of daily peak load over a rolling
window of recent operating days strictly before the target day. No lookahead:
future and same-day values are never used. A seasonal (multi-year month/day)
climatology is the natural upgrade once multiple years are backfilled.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
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


class ForecastErrorModel:
    """Official-forecast peak + lead-matched additive error (ENERGY_E3_BASELINE_V0).

    Residuals ``ε = M_d - M̂_d`` are grouped by forecast lead (days). p_YES is the
    empirical survival of ε with a rank correction ``r/(N+1)`` so a modest sample
    never yields literal 0/1.
    """

    def __init__(self, actual_peaks: dict[str, Decimal],
                 forecast_peaks: dict[tuple[str, str], Decimal],
                 max_lead: int = 6, min_residuals: int = 5):
        self.forecast_peaks = forecast_peaks          # {(publish_day, delivery_day): peak}
        self.max_lead = max_lead
        self.min_residuals = min_residuals
        self.residuals: dict[int, list[Decimal]] = defaultdict(list)
        self._build_residuals(actual_peaks)

    def _build_residuals(self, actual_peaks: dict[str, Decimal]) -> None:
        for day, actual in actual_peaks.items():
            try:
                dd = date.fromisoformat(day)
            except ValueError:
                continue
            for lead in range(self.max_lead + 1):
                pub_day = (dd - timedelta(days=lead)).isoformat()
                fpeak = self.forecast_peaks.get((pub_day, day))
                if fpeak is not None:
                    self.residuals[lead].append(actual - fpeak)

    def _current_forecast(self, target_day: str, as_of: datetime) -> tuple[Decimal, int] | None:
        """Latest forecast (<= as_of) covering target_day, and its lead in days."""
        try:
            target = date.fromisoformat(target_day)
        except ValueError:
            return None
        best: tuple[date, Decimal] | None = None
        for (pub_day, day), peak in self.forecast_peaks.items():
            if day != target_day:
                continue
            try:
                pub = date.fromisoformat(pub_day)
            except ValueError:
                continue
            if pub <= as_of.date() and (best is None or pub > best[0]):
                best = (pub, peak)
        if best is None:
            return None
        lead = (target - best[0]).days
        return best[1], min(max(lead, 0), self.max_lead)

    def p_peak_gt(self, target_day: str, as_of: datetime, strike: Decimal) -> Decimal | None:
        fc = self._current_forecast(target_day, as_of)
        if fc is None:
            return None
        mhat, lead = fc
        eps = self.residuals.get(lead, [])
        if len(eps) < self.min_residuals:
            return None
        threshold = strike - mhat
        r = sum(1 for e in eps if e <= threshold)
        n = len(eps)
        return D(1) - D(r) / D(n + 1)
