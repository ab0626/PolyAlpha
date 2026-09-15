"""Latency simulation — PnL degradation under realistic execution delays.

Simulates how different latency levels affect strategy performance by
applying entry delays using actual future book snapshots, without
peeking into future model forecasts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class DelayMetrics:
    """Performance metrics at a specific latency level."""

    delay_seconds: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    fill_rate: Decimal
    slippage: Decimal
    adverse_selection: Decimal
    missed_opportunities: int
    total_signals: int
    avg_entry_slippage: Decimal
    avg_spread_at_entry: Decimal

    def summary(self) -> dict:
        return {
            "delay_seconds": self.delay_seconds,
            "total_pnl": str(self.total_pnl),
            "fill_rate": str(self.fill_rate),
            "slippage": str(self.slippage),
            "adverse_selection": str(self.adverse_selection),
            "missed_opportunities": self.missed_opportunities,
        }


@dataclass(frozen=True)
class LatencyResult:
    """Full latency simulation results across all delay levels."""

    baseline_metrics: DelayMetrics | None = None
    delay_results: list[DelayMetrics] = field(default_factory=list)
    optimal_delay_seconds: int | None = None
    pnl_degradation_rate: Decimal | None = None  # PnL loss per second of latency

    def summary(self) -> dict:
        return {
            "baseline_pnl": str(self.baseline_metrics.total_pnl) if self.baseline_metrics else None,
            "optimal_delay": self.optimal_delay_seconds,
            "degradation_rate": (
                str(self.pnl_degradation_rate)
                if self.pnl_degradation_rate
                else None
            ),
            "delays": [d.summary() for d in self.delay_results],
        }


def _get_book_at_time(
    book_snapshots: list[dict],
    target_time: datetime,
) -> dict | None:
    """Find the book snapshot closest to target_time (no lookahead)."""
    best = None
    best_delta = timedelta(days=365)
    for snap in book_snapshots:
        snap_time = snap.get("timestamp", datetime.min)
        delta = abs(snap_time - target_time)
        if snap_time <= target_time and delta < best_delta:
            best = snap
            best_delta = delta
    return best


def _compute_slippage(
    intended_price: Decimal,
    actual_fill_price: Decimal,
    side: str,
) -> Decimal:
    """Compute slippage as positive value (worse fill = positive)."""
    if side == "BUY":
        return actual_fill_price - intended_price
    return intended_price - actual_fill_price


def _simulate_strategy(
    snapshots: list[dict],
    delay_seconds: int,
    book_snapshots: list[dict] | None,
    model_forecast_fn,
) -> DelayMetrics:
    """Simulate strategy execution with a fixed delay applied to entries."""
    total_signals = 0
    missed = 0
    fills = 0
    total_pnl = D(0)
    total_slippage = D(0)
    total_adverse = D(0)
    total_spread = D(0)
    realized = D(0)
    unrealized = D(0)
    delay = timedelta(seconds=delay_seconds)

    for snap in snapshots:
        signal_time = snap.get("signal_time", datetime.min)
        delayed_time = signal_time + delay
        side = snap.get("signal_side", "YES")
        signal_prob = snap.get("signal_probability", D("0.5"))
        original_mid = snap.get("midpoint", D("0.5"))
        total_signals += 1

        # Model forecast must use data available at signal_time, NOT at delayed_time
        forecast = model_forecast_fn(snap)
        if forecast is None:
            continue

        # Book state at the delayed execution time (actual future data)
        if book_snapshots:
            exec_book = _get_book_at_time(book_snapshots, delayed_time)
        else:
            exec_book = snap.get("book_at_delay", snap.get("book"))

        if exec_book is None:
            missed += 1
            continue

        bid = exec_book.get("bid", D("0"))
        ask = exec_book.get("ask", D("1"))
        spread = ask - bid

        # Determine executable price
        if side == "YES":
            exec_price = ask
        else:
            exec_price = bid

        # Check if price moved against us (adverse selection)
        price_move = exec_price - original_mid
        if side == "YES" and price_move < 0:
            adverse = abs(price_move)
        elif side == "NO" and price_move > 0:
            adverse = price_move
        else:
            adverse = D(0)

        # Slippage relative to signal-time midpoint
        slippage = _compute_slippage(original_mid, exec_price, side)

        # Simulate PnL: outcome depends on market resolution
        outcome_price = snap.get("outcome_price", D("0"))
        if outcome_price is not None:
            if side == "YES":
                pnl = (outcome_price - exec_price) * snap.get("size", D(1))
            else:
                pnl = (exec_price - outcome_price) * snap.get("size", D(1))
        else:
            # Use forecast to estimate PnL
            edge = signal_prob - D("0.5") if side == "YES" else D("0.5") - signal_prob
            pnl = edge * snap.get("size", D(1))

        if pnl > 0:
            realized += pnl
        else:
            unrealized += pnl

        total_pnl += pnl
        total_slippage += slippage
        total_adverse += adverse
        total_spread += spread
        fills += 1

    fill_rate = D(fills) / D(total_signals) if total_signals > 0 else D(0)
    avg_slip = total_slippage / D(fills) if fills > 0 else D(0)
    avg_adverse = total_adverse / D(fills) if fills > 0 else D(0)
    avg_spread = total_spread / D(fills) if fills > 0 else D(0)

    return DelayMetrics(
        delay_seconds=delay_seconds,
        realized_pnl=realized.quantize(D("0.01")),
        unrealized_pnl=unrealized.quantize(D("0.01")),
        total_pnl=total_pnl.quantize(D("0.01")),
        fill_rate=fill_rate.quantize(D("0.0001")),
        slippage=avg_slip.quantize(D("0.0001")),
        adverse_selection=avg_adverse.quantize(D("0.0001")),
        missed_opportunities=missed,
        total_signals=total_signals,
        avg_entry_slippage=avg_slip.quantize(D("0.0001")),
        avg_spread_at_entry=avg_spread.quantize(D("0.0001")),
    )


def simulate_latency(
    snapshots: list[dict],
    delays_seconds: list[int] | None = None,
    book_snapshots: list[dict] | None = None,
    model_forecast_fn=None,
) -> LatencyResult:
    """Simulate PnL degradation across multiple latency levels.

    For each delay, applies entry delay using actual future book snapshots.
    Does NOT alter model forecast using future data — the forecast is always
    based on information available at signal generation time.

    Args:
        snapshots: Signal snapshots with timing and market data.
        delays_seconds: Latency levels to test in seconds.
        book_snapshots: Historical book states for realistic fill simulation.
        model_forecast_fn: Callable(signal_snapshot) -> forecast probability.
            Must not access data beyond signal_time.

    Returns:
        LatencyResult with per-delay metrics and degradation analysis.
    """
    if delays_seconds is None:
        delays_seconds = [0, 1, 5, 15, 30, 60, 300]

    if model_forecast_fn is None:
        def model_forecast_fn(s):
            return s.get("signal_probability", D("0.5"))

    results: list[DelayMetrics] = []
    for delay in delays_seconds:
        metrics = _simulate_strategy(snapshots, delay, book_snapshots, model_forecast_fn)
        results.append(metrics)

    # Find optimal delay (highest PnL)
    best = max(results, key=lambda m: m.total_pnl) if results else None
    optimal = best.delay_seconds if best else None

    # Compute degradation rate: PnL loss per second of latency
    degradation_rate = None
    if len(results) >= 2:
        baseline = results[0]
        worst = min(results, key=lambda m: m.total_pnl)
        if worst.delay_seconds > baseline.delay_seconds:
            pnl_diff = baseline.total_pnl - worst.total_pnl
            time_diff = D(worst.delay_seconds - baseline.delay_seconds)
            if time_diff > 0:
                degradation_rate = (pnl_diff / time_diff).quantize(D("0.0001"))

    return LatencyResult(
        baseline_metrics=results[0] if results else None,
        delay_results=results,
        optimal_delay_seconds=optimal,
        pnl_degradation_rate=degradation_rate,
    )
