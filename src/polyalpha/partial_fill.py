"""Partial fill stress testing — simulate incomplete order execution.

Models the impact of partial fills, random fill fractions, and
disappearing order book depth before execution. Measures degradation
in PnL and edge realization under realistic fill scenarios.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class FractionMetrics:
    """Performance metrics for a specific fill fraction scenario."""

    fill_fraction: Decimal
    label: str
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    partial_fills_count: int
    full_fills_count: int
    depth_disappeared_count: int
    avg_slippage: Decimal
    edge_realization: Decimal
    total_signals: int

    def summary(self) -> dict:
        return {
            "fill_fraction": str(self.fill_fraction),
            "label": self.label,
            "total_pnl": str(self.total_pnl),
            "edge_realization": str(self.edge_realization),
            "partial_fills": self.partial_fills_count,
            "depth_disappeared": self.depth_disappeared_count,
        }


@dataclass(frozen=True)
class PartialFillResult:
    """Full partial fill stress test results."""

    fraction_results: list[FractionMetrics] = field(default_factory=list)
    worst_fraction: Decimal | None = None
    best_fraction: Decimal | None = None
    pnl_sensitivity: Decimal | None = None  # max - min PnL across fractions

    def summary(self) -> dict:
        return {
            "worst_fraction": str(self.worst_fraction) if self.worst_fraction else None,
            "best_fraction": str(self.best_fraction) if self.best_fraction else None,
            "pnl_sensitivity": str(self.pnl_sensitivity) if self.pnl_sensitivity else None,
            "fractions": [f.summary() for f in self.fraction_results],
        }


def _simulate_depth_vanish(
    order_size: Decimal,
    book_depth: Decimal,
    vanish_probability: Decimal = D("0.10"),
) -> tuple[Decimal, bool]:
    """Simulate depth disappearing before execution.

    Returns (actual_fill_size, depth_vanished).
    """
    if book_depth >= order_size:
        return order_size, False
    # Partial fill due to insufficient depth
    if random.random() < float(vanish_probability):
        return book_depth, True
    return book_depth, False


def _simulate_single_fraction(
    snapshots: list[dict],
    target_fraction: Decimal | None,
    randomize: bool = False,
) -> FractionMetrics:
    """Run simulation for a single fill fraction scenario."""
    total_pnl = D(0)
    realized = D(0)
    unrealized = D(0)
    partial_count = 0
    full_count = 0
    depth_gone = 0
    total_slippage = D(0)
    edge_sum = D(0)
    n = 0

    for snap in snapshots:
        signal_side = snap.get("signal_side", "YES")
        signal_prob = snap.get("signal_probability", D("0.5"))
        exec_price = snap.get("exec_price", D("0.5"))
        outcome_price = snap.get("outcome_price")
        order_size = snap.get("size", D(1))
        book_depth = snap.get("book_depth", D(10))
        midpoint = snap.get("midpoint", D("0.5"))
        n += 1

        # Determine fill fraction
        if randomize:
            frac = D(str(random.uniform(0.1, 1.0))).quantize(D("0.01"))
        elif target_fraction is not None:
            frac = target_fraction
        else:
            frac = D(1)

        # Simulate fill
        if frac < 0:
            frac = D(str(random.uniform(0.1, 1.0))).quantize(D("0.01"))

        requested_size = order_size * frac
        actual_fill, vanished = _simulate_depth_vanish(requested_size, book_depth)
        if vanished:
            depth_gone += 1

        if actual_fill <= 0:
            continue

        # Edge realized: compare forecast vs execution price
        if signal_side == "YES":
            edge = signal_prob - exec_price
        else:
            edge = exec_price - (D(1) - signal_prob)

        # PnL calculation
        if outcome_price is not None:
            if signal_side == "YES":
                pnl = (outcome_price - exec_price) * actual_fill
            else:
                pnl = (exec_price - outcome_price) * actual_fill
        else:
            pnl = edge * actual_fill

        # Slippage vs midpoint
        if signal_side == "YES":
            slip = exec_price - midpoint
        else:
            slip = midpoint - exec_price
        total_slippage += max(D(0), slip)

        if actual_fill < order_size:
            partial_count += 1
        else:
            full_count += 1

        if pnl > 0:
            realized += pnl
        else:
            unrealized += pnl

        total_pnl += pnl
        edge_sum += edge

    avg_slip = total_slippage / D(n) if n > 0 else D(0)
    avg_edge = edge_sum / D(n) if n > 0 else D(0)

    label = "random" if randomize else str(target_fraction)

    return FractionMetrics(
        fill_fraction=target_fraction if target_fraction else D(-1),
        label=label,
        realized_pnl=realized.quantize(D("0.01")),
        unrealized_pnl=unrealized.quantize(D("0.01")),
        total_pnl=total_pnl.quantize(D("0.01")),
        partial_fills_count=partial_count,
        full_fills_count=full_count,
        depth_disappeared_count=depth_gone,
        avg_slippage=avg_slip.quantize(D("0.0001")),
        edge_realization=avg_edge.quantize(D("0.0001")),
        total_signals=n,
    )


def simulate_partial_fills(
    snapshots: list[dict],
    fill_fractions: list[int | float] | None = None,
) -> PartialFillResult:
    """Simulate partial fill stress across multiple fraction scenarios.

    Args:
        snapshots: Signal snapshots with execution context.
        fill_fractions: List of fill fractions to test. Use -1 for random.
            Default: [1.0, 0.75, 0.5, 0.25, -1]

    Returns:
        PartialFillResult with per-fraction metrics and sensitivity analysis.
    """
    if fill_fractions is None:
        fill_fractions = [1.0, 0.75, 0.5, 0.25, -1]

    results: list[FractionMetrics] = []

    for frac in fill_fractions:
        is_random = frac == -1 or frac == -1.0
        target = None if is_random else D(str(frac))
        metrics = _simulate_single_fraction(snapshots, target, randomize=is_random)
        results.append(metrics)

    # Analyze sensitivity
    pnl_values = [r.total_pnl for r in results if r.fill_fraction != -1]
    worst = min(results, key=lambda r: r.total_pnl) if results else None
    best = max(results, key=lambda r: r.total_pnl) if results else None

    sensitivity = None
    if len(pnl_values) >= 2:
        sensitivity = max(pnl_values) - min(pnl_values)

    return PartialFillResult(
        fraction_results=results,
        worst_fraction=worst.fill_fraction if worst else None,
        best_fraction=best.fill_fraction if best else None,
        pnl_sensitivity=sensitivity.quantize(D("0.01")) if sensitivity else None,
    )
