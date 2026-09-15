"""Alpha decay analysis.

Measures how signal quality degrades over time after signal generation.
Evaluates whether the model has immediate predictive value, slow
information diffusion, or no genuine predictive value.

For each signal, measure the market's subsequent price movement at:
- 30 seconds, 1 minute, 5 minutes, 15 minutes, 1 hour, 6 hours, 24 hours
"""

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

D = Decimal

HORIZONS_SECONDS = (30, 60, 300, 900, 3600, 21600, 86400)


@dataclass(frozen=True)
class AlphaDecayObservation:
    """Single observation of signal followed by price changes."""

    market_id: str
    signal_time: datetime
    signal_side: str  # "YES" or "NO"
    signal_probability: Decimal
    entry_price: Decimal
    price_observations: dict[int, Decimal]  # horizon_seconds -> price


@dataclass(frozen=True)
class AlphaDecayResult:
    """Aggregated alpha decay analysis."""

    horizon_seconds: int
    mean_price_change: float
    median_price_change: float
    directional_accuracy: float
    sample_size: int
    t_statistic: float | None
    p_value: float | None


def analyze_alpha_decay(
    observations: list[AlphaDecayObservation],
    horizons: tuple[int, ...] = HORIZONS_SECONDS,
) -> list[AlphaDecayResult]:
    """Analyze alpha decay across multiple time horizons.

    For each horizon, compute:
    - Mean price change from entry
    - Median price change
    - Directional accuracy (did price move in predicted direction?)
    - Statistical significance (t-test against zero)
    """
    from statistics import mean, median, stdev

    results = []

    for horizon in horizons:
        changes = []
        directional = []

        for obs in observations:
            if horizon not in obs.price_observations:
                continue

            future_price = obs.price_observations[horizon]
            change = float(future_price - obs.entry_price)

            # Direction: for YES signal, positive change is correct
            if obs.signal_side == "YES":
                correct = change > 0
            else:
                correct = change < 0

            changes.append(change)
            directional.append(correct)

        if not changes:
            continue

        mean_change = mean(changes)
        median_change = median(changes)
        dir_accuracy = sum(directional) / len(directional)

        # t-test against zero
        t_stat = None
        p_val = None
        if len(changes) >= 2:
            s = stdev(changes)
            if s > 0:
                t_stat = mean_change / (s / len(changes) ** 0.5)
                # Two-tailed p-value approximation using t-distribution
                # For large n, approximate with normal
                n = len(changes)
                if n > 30:
                    # Normal approximation
                    z = abs(t_stat)
                    p_val = 2 * (1 - _normal_cdf(z))
                else:
                    # Rough t-distribution approximation
                    p_val = 2 * (1 - _t_cdf(abs(t_stat), n - 1))

        results.append(
            AlphaDecayResult(
                horizon_seconds=horizon,
                mean_price_change=mean_change,
                median_price_change=median_change,
                directional_accuracy=dir_accuracy,
                sample_size=len(changes),
                t_statistic=t_stat,
                p_value=p_val,
            )
        )

    return results


def _normal_cdf(x: float) -> float:
    """Approximate standard normal CDF using error function."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _t_cdf(t: float, df: int) -> float:
    """Rough approximation of t-distribution CDF for moderate df."""
    # For df > 30, t converges to normal
    if df > 30:
        return _normal_cdf(t)
    # Rough approximation using beta function relation
    x = df / (df + t * t)
    # Incomplete beta function approximation
    if t >= 0:
        return 1 - 0.5 * _incomplete_beta(df / 2, 0.5, x)
    else:
        return 0.5 * _incomplete_beta(df / 2, 0.5, x)


def _incomplete_beta(a: float, b: float, x: float) -> float:
    """Rough incomplete beta function approximation."""
    # Simple numerical integration for small parameters
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    # Use series expansion for small x
    result = 0.0
    term = 1.0
    for n in range(100):
        result += term / (a + n)
        term *= x * (a + n) / (a + b + n)
        if abs(term) < 1e-10:
            break
    return result * x**a * (1 - x) ** b / _beta(a, b)


def _beta(a: float, b: float) -> float:
    """Beta function via gamma function."""
    import math

    return math.gamma(a) * math.gamma(b) / math.gamma(a + b)


def decay_summary(results: list[AlphaDecayResult]) -> dict:
    """Human-readable summary of alpha decay analysis."""
    if not results:
        return {"status": "no_data"}

    best_horizon = max(results, key=lambda r: r.directional_accuracy)
    worst_horizon = min(results, key=lambda r: r.directional_accuracy)

    return {
        "total_horizons": len(results),
        "best_horizon_seconds": best_horizon.horizon_seconds,
        "best_directional_accuracy": best_horizon.directional_accuracy,
        "worst_horizon_seconds": worst_horizon.horizon_seconds,
        "worst_directional_accuracy": worst_horizon.directional_accuracy,
        "has_immediate_signal": any(
            r.horizon_seconds <= 60 and r.directional_accuracy > 0.55 for r in results
        ),
        "has_slow_diffusion": any(
            r.horizon_seconds >= 3600 and r.directional_accuracy > 0.55 for r in results
        ),
        "significant_results": [
            {
                "horizon_seconds": r.horizon_seconds,
                "directional_accuracy": r.directional_accuracy,
                "p_value": r.p_value,
                "sample_size": r.sample_size,
            }
            for r in results
            if r.p_value is not None and r.p_value < 0.05
        ],
    }
