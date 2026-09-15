"""Execution-model calibration — predicted vs observed execution error.

Part of the live-ready execution architecture. Once forward observations
exist, compare predicted execution against observed forward execution
conditions to determine whether historical assumptions are systematically
optimistic. Results are conditioned on spread, liquidity, depth, volatility,
category, trade size, and time-to-resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class ExecutionObservation:
    market_id: str
    predicted_vwap: Decimal
    observed_vwap: Decimal
    predicted_slippage: Decimal
    observed_slippage: Decimal
    predicted_fill_fraction: Decimal
    observed_fill_fraction: Decimal
    predicted_latency_ms: int
    observed_latency_ms: int
    spread: Decimal = D("0")
    liquidity: Decimal = D("0")
    depth: Decimal = D("0")
    volatility: Decimal = D("0")
    category: str = "unknown"
    trade_size: Decimal = D("0")
    hours_to_resolution: float = 0.0

    @property
    def vwap_error(self) -> Decimal:
        return self.observed_vwap - self.predicted_vwap

    @property
    def slippage_error(self) -> Decimal:
        return self.observed_slippage - self.predicted_slippage

    @property
    def fill_error(self) -> Decimal:
        return self.observed_fill_fraction - self.predicted_fill_fraction

    @property
    def latency_error_ms(self) -> int:
        return self.observed_latency_ms - self.predicted_latency_ms


@dataclass(frozen=True)
class CalibrationBucket:
    key: str
    count: int
    mean_vwap_error: Decimal
    mean_slippage_error: Decimal
    mean_fill_error: Decimal
    mean_latency_error_ms: float
    systematic_optimism: bool  # True if observed is worse than predicted on average

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "count": self.count,
            "mean_vwap_error": str(self.mean_vwap_error),
            "mean_slippage_error": str(self.mean_slippage_error),
            "mean_fill_error": str(self.mean_fill_error),
            "mean_latency_error_ms": round(self.mean_latency_error_ms, 2),
            "systematic_optimism": self.systematic_optimism,
        }


@dataclass(frozen=True)
class CalibrationReport:
    total: int
    overall: CalibrationBucket
    by_spread: list[CalibrationBucket]
    by_liquidity: list[CalibrationBucket]
    by_category: list[CalibrationBucket]
    by_trade_size: list[CalibrationBucket]
    by_time_to_resolution: list[CalibrationBucket]

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "overall": self.overall.as_dict(),
            "by_spread": [b.as_dict() for b in self.by_spread],
            "by_liquidity": [b.as_dict() for b in self.by_liquidity],
            "by_category": [b.as_dict() for b in self.by_category],
            "by_trade_size": [b.as_dict() for b in self.by_trade_size],
            "by_time_to_resolution": [b.as_dict() for b in self.by_time_to_resolution],
        }


def _bucket(obs: list[ExecutionObservation], key: str) -> CalibrationBucket:
    n = len(obs)
    if n == 0:
        return CalibrationBucket(key, 0, D(0), D(0), D(0), 0.0, False)
    mean_vwap = sum((o.vwap_error for o in obs), D(0)) / n
    mean_slip = sum((o.slippage_error for o in obs), D(0)) / n
    mean_fill = sum((o.fill_error for o in obs), D(0)) / n
    mean_lat = sum((o.latency_error_ms for o in obs)) / n
    # Optimism: observed slippage higher or fill lower than predicted.
    optimism = mean_slip > 0 or mean_fill < 0
    return CalibrationBucket(key, n, mean_vwap, mean_slip, mean_fill, mean_lat, optimism)


def _group(obs: list[ExecutionObservation], keyfn) -> list[CalibrationBucket]:
    groups: dict[str, list[ExecutionObservation]] = {}
    for o in obs:
        groups.setdefault(keyfn(o), []).append(o)
    return [_bucket(v, k) for k, v in sorted(groups.items())]


def calibrate(observations: list[ExecutionObservation]) -> CalibrationReport:
    """Compute predicted-vs-observed execution error, conditioned on context."""
    def spread_bucket(o: ExecutionObservation) -> str:
        s = float(o.spread)
        return "tight(<0.01)" if s < 0.01 else ("mid(0.01-0.03)" if s < 0.03 else "wide(>=0.03)")

    def liq_bucket(o: ExecutionObservation) -> str:
        liq = float(o.liquidity)
        return "low(<5k)" if liq < 5000 else ("mid(5k-50k)" if liq < 50000 else "high(>=50k)")

    def size_bucket(o: ExecutionObservation) -> str:
        s = float(o.trade_size)
        return "small(<100)" if s < 100 else ("mid(100-1000)" if s < 1000 else "large(>=1000)")

    def ttr_bucket(o: ExecutionObservation) -> str:
        h = o.hours_to_resolution
        return "<24h" if h < 24 else ("1-7d" if h < 168 else ">7d")

    return CalibrationReport(
        total=len(observations),
        overall=_bucket(observations, "overall"),
        by_spread=_group(observations, spread_bucket),
        by_liquidity=_group(observations, liq_bucket),
        by_category=_group(observations, lambda o: o.category),
        by_trade_size=_group(observations, size_bucket),
        by_time_to_resolution=_group(observations, ttr_bucket),
    )