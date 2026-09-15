"""Regime detection — identify market regimes using heuristic-based methods.

Detects regimes like low volatility, high volatility, election events,
macro announcements, low liquidity, and information shocks using rolling
statistics, volume spikes, and spread widening. Evaluates strategy
performance per regime.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class RegimeLabel:
    """A labeled regime for a specific time period."""

    regime: str
    start_time: datetime
    end_time: datetime
    confidence: Decimal
    trigger_metric: str
    trigger_value: Decimal


@dataclass(frozen=True)
class RegimePerformance:
    """Strategy performance within a specific regime."""

    regime: str
    total_signals: int
    accepted_signals: int
    total_pnl: Decimal
    avg_pnl_per_signal: Decimal
    win_rate: Decimal
    avg_edge: Decimal
    max_drawdown: Decimal
    sharpe_ratio: Decimal

    def summary(self) -> dict:
        return {
            "regime": self.regime,
            "total_signals": self.total_signals,
            "total_pnl": str(self.total_pnl),
            "win_rate": str(self.win_rate),
            "sharpe_ratio": str(self.sharpe_ratio),
        }


@dataclass(frozen=True)
class RegimeResult:
    """Full regime detection and performance analysis."""

    regimes: list[RegimeLabel] = field(default_factory=list)
    regime_performance: list[RegimePerformance] = field(default_factory=list)
    regime_distribution: dict[str, Decimal] = field(default_factory=dict)
    current_regime: str | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "current_regime": self.current_regime,
            "regime_distribution": {k: str(v) for k, v in self.regime_distribution.items()},
            "performance": [p.summary() for p in self.regime_performance],
            "warnings": self.warnings,
        }


def _rolling_stats(
    values: list[Decimal],
    window: int,
) -> list[tuple[Decimal, Decimal]]:
    """Compute rolling mean and std for a series."""
    results: list[tuple[Decimal, Decimal]] = []
    for i in range(len(values)):
        start = max(0, i - window + 1)
        window_vals = values[start : i + 1]
        n = len(window_vals)
        if n < 2:
            results.append((D(0), D(0)))
            continue
        mean = sum(window_vals) / D(n)
        variance = sum((v - mean) ** 2 for v in window_vals) / D(n)
        # Decimal sqrt approximation
        std_approx = D(str(math.sqrt(float(variance)))) if variance > 0 else D(0)
        results.append((mean, std_approx))
    return results


def _detect_volatility_regime(
    midpoints: list[Decimal],
    timestamps: list[datetime],
    vol_window: int = 20,
    low_vol_threshold: Decimal = D("0.005"),
    high_vol_threshold: Decimal = D("0.02"),
) -> list[tuple[str, datetime, datetime, Decimal]]:
    """Detect low and high volatility regimes from price changes."""
    if len(midpoints) < 2:
        return []

    # Compute returns
    returns = [D(0)]
    for i in range(1, len(midpoints)):
        if midpoints[i - 1] > 0:
            ret = (midpoints[i] - midpoints[i - 1]) / midpoints[i - 1]
        else:
            ret = D(0)
        returns.append(ret)

    rolling_stats = _rolling_stats(returns, vol_window)
    rolling_std = [s for _, s in rolling_stats]

    regimes: list[tuple[str, datetime, datetime, Decimal]] = []
    current_regime = None
    regime_start = timestamps[0] if timestamps else datetime.min

    for i, (std_val, ts) in enumerate(zip(rolling_std, timestamps)):
        if std_val < low_vol_threshold:
            new_regime = "low_volatility"
        elif std_val > high_vol_threshold:
            new_regime = "high_volatility"
        else:
            new_regime = "normal"

        if new_regime != current_regime:
            if current_regime and current_regime != "normal":
                confidence = min(
                    D(1),
                    std_val / high_vol_threshold
                    if current_regime == "high_volatility"
                    else low_vol_threshold / max(std_val, D("0.001"))
                )
                regimes.append((current_regime, regime_start, ts, confidence.quantize(D("0.01"))))
            if new_regime != "normal":
                regime_start = ts
            current_regime = new_regime

    return regimes


def _detect_volume_spike(
    volumes: list[Decimal],
    timestamps: list[datetime],
    window: int = 20,
    spike_threshold: Decimal = D("3.0"),
) -> list[tuple[str, datetime, datetime, Decimal]]:
    """Detect information shocks from volume spikes."""
    if len(volumes) < window:
        return []

    regimes: list[tuple[str, datetime, datetime, Decimal]] = []
    rolling_mean = [m for m, _ in _rolling_stats(volumes, window)]

    in_shock = False
    shock_start = datetime.min

    for i, (mean_val, ts) in enumerate(zip(rolling_mean, timestamps)):
        vol = volumes[i]
        if mean_val > 0 and vol > mean_val * spike_threshold:
            if not in_shock:
                in_shock = True
                shock_start = ts
            confidence = min(D(1), vol / (mean_val * spike_threshold))
        else:
            if in_shock:
                regimes.append(
                    ("information_shock", shock_start, ts, confidence.quantize(D("0.01")))
                )
                in_shock = False

    return regimes


def _detect_liquidity_regime(
    spreads: list[Decimal],
    timestamps: list[datetime],
    window: int = 20,
    low_liq_threshold: Decimal = D("0.05"),
) -> list[tuple[str, datetime, datetime, Decimal]]:
    """Detect low liquidity from spread widening."""
    if len(spreads) < window:
        return []

    regimes: list[tuple[str, datetime, datetime, Decimal]] = []
    rolling_mean = [m for m, _ in _rolling_stats(spreads, window)]

    in_low = False
    low_start = datetime.min

    for i, (mean_val, ts) in enumerate(zip(rolling_mean, timestamps)):
        spread = spreads[i]
        if spread > low_liq_threshold or (mean_val > 0 and spread > mean_val * D("2")):
            if not in_low:
                in_low = True
                low_start = ts
            confidence = min(D(1), spread / low_liq_threshold)
        else:
            if in_low:
                regimes.append(
                    ("low_liquidity", low_start, ts, confidence.quantize(D("0.01")))
                )
                in_low = False

    return regimes


def _detect_event_regime(
    snapshots: list[dict],
) -> list[tuple[str, datetime, datetime, Decimal]]:
    """Detect election/macro events from snapshot metadata."""
    regimes: list[tuple[str, datetime, datetime, Decimal]] = []

    for snap in snapshots:
        event_type = snap.get("event_type")
        if event_type in ("election", "macro_announcement"):
            ts = snap.get("timestamp", datetime.min)
            confidence = D(str(snap.get("event_confidence", "0.8")))
            regimes.append(
                (event_type, ts, ts + timedelta(hours=1), confidence)
            )

    return regimes


def detect_regimes(snapshots: list[dict]) -> RegimeResult:
    """Detect market regimes from snapshot data.

    Uses heuristic-based detection:
    - Rolling volatility for low/high volatility regimes
    - Volume spikes for information shocks
    - Spread widening for low liquidity
    - Metadata for election/macro events

    Args:
        snapshots: List of market snapshots with keys:
            - timestamp: datetime
            - midpoint: Decimal
            - spread: Decimal
            - volume: Decimal
            - event_type: str (optional)
            - event_confidence: Decimal (optional)

    Returns:
        RegimeResult with detected regimes and their timestamps.
    """
    if not snapshots:
        return RegimeResult()

    timestamps = [s.get("timestamp", datetime.min) for s in snapshots]
    midpoints = [s.get("midpoint", D("0.5")) for s in snapshots]
    spreads = [s.get("spread", D("0.02")) for s in snapshots]
    volumes = [s.get("volume", D(100)) for s in snapshots]

    # Detect regimes
    vol_regimes = _detect_volatility_regime(midpoints, timestamps)
    shock_regimes = _detect_volume_spike(volumes, timestamps)
    liq_regimes = _detect_liquidity_regime(spreads, timestamps)
    event_regimes = _detect_event_regime(snapshots)

    all_regime_tuples = vol_regimes + shock_regimes + liq_regimes + event_regimes

    # Convert to RegimeLabel objects
    regime_labels = [
        RegimeLabel(
            regime=r[0],
            start_time=r[1],
            end_time=r[2],
            confidence=r[3],
            trigger_metric=(
                "rolling_std" if "volatility" in r[0]
                else "volume" if "shock" in r[0]
                else "spread" if "liquidity" in r[0]
                else "metadata"
            ),
            trigger_value=D(0),
        )
        for r in all_regime_tuples
    ]

    # Compute regime distribution
    total_time = (timestamps[-1] - timestamps[0]).total_seconds() if len(timestamps) > 1 else 1
    regime_seconds: dict[str, float] = defaultdict(float)
    for rl in regime_labels:
        duration = (rl.end_time - rl.start_time).total_seconds()
        regime_seconds[rl.regime] += duration

    regime_dist: dict[str, Decimal] = {}
    for regime, secs in regime_seconds.items():
        regime_dist[regime] = D(str(round(secs / total_time * 100, 1))) if total_time > 0 else D(0)

    # Current regime is the last detected regime
    current = regime_labels[-1].regime if regime_labels else "normal"

    warnings: list[str] = []
    if not regime_labels:
        warnings.append("No distinct regimes detected. Market may be in normal state.")
    if len(regime_labels) > len(snapshots) * 0.3:
        warnings.append("High regime switching frequency detected. Consider longer windows.")

    return RegimeResult(
        regimes=regime_labels,
        regime_distribution=regime_dist,
        current_regime=current,
        warnings=warnings,
    )


def evaluate_by_regime(
    results: list[dict],
    regimes: RegimeResult,
) -> list[RegimePerformance]:
    """Evaluate strategy performance segmented by regime.

    Args:
        results: List of trade result dicts with keys:
            - timestamp: datetime
            - pnl: Decimal
            - edge: Decimal
            - accepted: bool
            - signal_id: str
        regimes: RegimeResult from detect_regimes.

    Returns:
        List of RegimePerformance for each detected regime.
    """
    if not results or not regimes.regimes:
        return []

    # Classify each result into a regime
    regime_results: dict[str, list[dict]] = defaultdict(list)

    for result in results:
        ts = result.get("timestamp", datetime.min)
        matched_regime = "normal"

        for rl in regimes.regimes:
            if rl.start_time <= ts <= rl.end_time:
                matched_regime = rl.regime
                break

        regime_results[matched_regime].append(result)

    performances: list[RegimePerformance] = []

    for regime, regime_trades in regime_results.items():
        total = len(regime_trades)
        accepted = sum(1 for t in regime_trades if t.get("accepted", False))
        pnls = [D(str(t.get("pnl", "0"))) for t in regime_trades]
        edges = [D(str(t.get("edge", "0"))) for t in regime_trades]

        total_pnl = sum(pnls)
        avg_pnl = total_pnl / D(total) if total > 0 else D(0)
        wins = sum(1 for p in pnls if p > 0)
        win_rate = D(wins) / D(total) if total > 0 else D(0)
        avg_edge = sum(edges) / D(total) if total > 0 else D(0)

        # Max drawdown
        cumulative = D(0)
        peak = D(0)
        max_dd = D(0)
        for p in pnls:
            cumulative += p
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)

        # Sharpe ratio (simplified)
        if len(pnls) > 1:
            mean_pnl = sum(pnls) / D(len(pnls))
            var = sum((p - mean_pnl) ** 2 for p in pnls) / D(len(pnls))
            std = D(str(math.sqrt(float(var)))) if var > 0 else D(1)
            sharpe = (mean_pnl / std) * D(str(math.sqrt(252))) if std > 0 else D(0)
        else:
            sharpe = D(0)

        performances.append(
            RegimePerformance(
                regime=regime,
                total_signals=total,
                accepted_signals=accepted,
                total_pnl=total_pnl.quantize(D("0.01")),
                avg_pnl_per_signal=avg_pnl.quantize(D("0.0001")),
                win_rate=win_rate.quantize(D("0.0001")),
                avg_edge=avg_edge.quantize(D("0.0001")),
                max_drawdown=max_dd.quantize(D("0.01")),
                sharpe_ratio=sharpe.quantize(D("0.01")),
            )
        )

    return performances
