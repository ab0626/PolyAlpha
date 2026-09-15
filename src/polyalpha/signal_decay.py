"""Signal decay extended — midpoint and executable price decay analysis.

Measures how signal alpha degrades over time by tracking both midpoint
movement and executable bid/ask movement. Computes half-life of alpha
for realistic decay estimation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class HorizonDecay:
    """Decay metrics for a single time horizon."""

    horizon_seconds: int
    midpoint_decay: Decimal
    executable_decay: Decimal
    sample_size: int
    midpoint_mean_change: Decimal
    executable_mean_change: Decimal
    directional_accuracy: float


@dataclass(frozen=True)
class DecayResult:
    """Full signal decay analysis across all horizons."""

    horizon_results: list[HorizonDecay] = field(default_factory=list)
    half_life_seconds: float | None = None
    midpoint_alpha_at_half: Decimal = D(0)
    executable_alpha_at_half: Decimal = D(0)
    decay_rate_per_second: float | None = None

    def summary(self) -> dict:
        return {
            "half_life_seconds": self.half_life_seconds,
            "decay_rate_per_second": self.decay_rate_per_second,
            "horizons": [
                {
                    "horizon": h.horizon_seconds,
                    "midpoint_decay": str(h.midpoint_decay),
                    "executable_decay": str(h.executable_decay),
                    "sample_size": h.sample_size,
                }
                for h in self.horizon_results
            ],
        }


def _compute_half_life(
    decay_points: list[tuple[int, float]],
) -> float | None:
    """Fit exponential decay: alpha(t) = alpha_0 * exp(-lambda * t).

    Returns half-life in seconds if fit is possible.
    """
    if len(decay_points) < 2:
        return None
    valid = [(t, a) for t, a in decay_points if a > 0]
    if len(valid) < 2:
        return None
    # Least-squares on log(alpha) = log(alpha_0) - lambda * t
    log_vals = [math.log(a) for _, a in valid]
    t_vals = [float(t) for t, _ in valid]
    n = len(valid)
    sum_t = sum(t_vals)
    sum_log = sum(log_vals)
    sum_t2 = sum(t * t for t in t_vals)
    sum_t_log = sum(t * val for t, val in zip(t_vals, log_vals))
    denom = n * sum_t2 - sum_t * sum_t
    if denom == 0:
        return None
    lam = (sum_t * sum_log - n * sum_t_log) / denom
    if lam <= 0:
        return None
    return math.log(2) / lam


def analyze_signal_decay(
    snapshots: list[dict],
    horizons_seconds: list[int] | None = None,
) -> DecayResult:
    """Analyze signal decay across multiple time horizons.

    Each snapshot must contain:
        - signal_time: datetime
        - signal_side: str ("YES" or "NO")
        - signal_probability: Decimal
        - midpoints: dict[int, Decimal]  # horizon -> midpoint price at that time
        - executables: dict[int, dict]   # horizon -> {"bid": Decimal, "ask": Decimal}

    For each horizon, measures:
        - midpoint_decay: how much the midpoint moved toward the predicted direction
        - executable_decay: how much the executable price moved toward predicted direction
        - directional_accuracy: fraction of signals that were directionally correct

    Then fits exponential decay to estimate half-life of alpha.
    """
    if horizons_seconds is None:
        horizons_seconds = [10, 30, 60, 120, 300, 900, 1800, 3600, 21600, 86400]

    horizon_results: list[HorizonDecay] = []

    for horizon in horizons_seconds:
        mid_changes: list[Decimal] = []
        exec_changes: list[Decimal] = []
        directional: list[bool] = []

        for snap in snapshots:
            midpoints = snap.get("midpoints", {})
            executables = snap.get("executables", {})
            if horizon not in midpoints or horizon not in executables:
                continue
            signal_side = snap["signal_side"]
            entry_mid = snap.get("entry_midpoint", midpoints.get(0, D("0.5")))
            future_mid = midpoints[horizon]
            exec_info = executables[horizon]

            # Midpoint change in predicted direction
            if signal_side == "YES":
                mid_change = future_mid - entry_mid
            else:
                mid_change = entry_mid - future_mid

            # Executable change: compare entry executable vs future executable
            entry_exec = snap.get("entry_executable")
            if entry_exec is None:
                if signal_side == "YES":
                    entry_exec = snap.get("entry_bid", D("0.5"))
                else:
                    entry_exec = snap.get("entry_ask", D("0.5"))

            if signal_side == "YES":
                future_exec = exec_info.get("bid", D("0.5"))
            else:
                future_exec = exec_info.get("ask", D("0.5"))

            exec_change = (
                future_exec - entry_exec
                if signal_side == "YES"
                else entry_exec - future_exec
            )

            mid_changes.append(mid_change)
            exec_changes.append(exec_change)
            directional.append(mid_change > 0)

        if not mid_changes:
            continue

        n = len(mid_changes)
        avg_mid = sum(mid_changes, D(0)) / D(n)
        avg_exec = sum(exec_changes, D(0)) / D(n)
        dir_acc = sum(directional) / n

        # Decay ratio: how much of original edge remains at this horizon
        initial_edge = D(0)
        for snap in snapshots:
            prob = snap.get("signal_probability", D("0.5"))
            edge = abs(prob - D("0.5"))
            if edge > initial_edge:
                initial_edge = edge
        if initial_edge <= 0:
            initial_edge = D(0.01)

        midpoint_decay = D(1) - (avg_mid / initial_edge if initial_edge else D(0))
        executable_decay = D(1) - (avg_exec / initial_edge if initial_edge else D(0))
        midpoint_decay = max(D(0), min(D(1), midpoint_decay))
        executable_decay = max(D(0), min(D(1), executable_decay))

        horizon_results.append(
            HorizonDecay(
                horizon_seconds=horizon,
                midpoint_decay=midpoint_decay.quantize(D("0.0001")),
                executable_decay=executable_decay.quantize(D("0.0001")),
                sample_size=n,
                midpoint_mean_change=avg_mid.quantize(D("0.0001")),
                executable_mean_change=avg_exec.quantize(D("0.0001")),
                directional_accuracy=round(dir_acc, 4),
            )
        )

    # Compute half-life using executable decay points
    decay_points = []
    for hr in horizon_results:
        fraction_remaining = float(D(1) - hr.executable_decay)
        if fraction_remaining > 0:
            decay_points.append((hr.horizon_seconds, fraction_remaining))

    half_life = _compute_half_life(decay_points)

    return DecayResult(
        horizon_results=horizon_results,
        half_life_seconds=round(half_life, 2) if half_life else None,
        midpoint_alpha_at_half=D(0.5),
        executable_alpha_at_half=D(0.5),
        decay_rate_per_second=round(
            math.log(2) / half_life, 10
        )
        if half_life
        else None,
    )
