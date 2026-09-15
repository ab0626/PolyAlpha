"""Maker strategy stress testing — realistic maker order scenarios.

Tests maker strategies under queue priority issues, partial fills,
adverse selection, cancellations, stale quotes, and information shocks.
Never counts fill merely because historical price crossed the level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class ScenarioResult:
    """Result for a single stress scenario."""

    scenario: str
    total_orders: int
    fills: int
    fill_rate: Decimal
    total_pnl: Decimal
    avg_fill_price: Decimal
    adverse_selection_count: int
    adverse_selection_loss: Decimal
    queue_position_avg: Decimal
    cancellation_rate: Decimal

    def summary(self) -> dict:
        return {
            "scenario": self.scenario,
            "fill_rate": str(self.fill_rate),
            "total_pnl": str(self.total_pnl),
            "adverse_selection_loss": str(self.adverse_selection_loss),
            "cancellation_rate": str(self.cancellation_rate),
        }


@dataclass(frozen=True)
class MakerStressResult:
    """Full maker stress test results across all scenarios."""

    scenario_results: list[ScenarioResult] = field(default_factory=list)
    baseline_fill_rate: Decimal = D(0)
    baseline_pnl: Decimal = D(0)
    worst_scenario: str | None = None
    worst_pnl: Decimal | None = None
    scenario_sensitivity: Decimal | None = None

    def summary(self) -> dict:
        return {
            "baseline_fill_rate": str(self.baseline_fill_rate),
            "baseline_pnl": str(self.baseline_pnl),
            "worst_scenario": self.worst_scenario,
            "worst_pnl": str(self.worst_pnl) if self.worst_pnl else None,
            "scenarios": [s.summary() for s in self.scenario_results],
        }


def _simulate_queue_priority(
    orders: list[dict],
    book_snapshots: list[dict],
) -> ScenarioResult:
    """Simulate maker orders where queue position affects fill probability."""
    fills = 0
    total_pnl = D(0)
    adverse = 0
    adverse_loss = D(0)
    queue_positions = []

    for order in orders:
        side = order.get("side", "BUY")
        price = order.get("price", D("0.5"))
        size = order.get("size", D(1))
        queue_ahead = order.get("queue_ahead", D(0))
        queue_positions.append(queue_ahead)

        # Only fill if aggressive volume reaches our queue position
        agg_volume = order.get("aggressive_volume_reaching_price", D(0))
        fill_amount = max(D(0), agg_volume - queue_ahead)
        fill_amount = min(fill_amount, size)

        if fill_amount <= 0:
            continue

        fills += 1
        # Check if price moved against us after fill
        post_fill_mid = order.get("post_fill_midpoint", price)
        if side == "BUY" and post_fill_mid < price:
            adverse += 1
            adverse_loss += (price - post_fill_mid) * fill_amount
        elif side == "SELL" and post_fill_mid > price:
            adverse += 1
            adverse_loss += (post_fill_mid - price) * fill_amount

        if side == "BUY":
            pnl = (order.get("exit_price", post_fill_mid) - price) * fill_amount
        else:
            pnl = (price - order.get("exit_price", post_fill_mid)) * fill_amount
        total_pnl += pnl

    n = len(orders)
    avg_queue = sum(queue_positions) / D(n) if n > 0 else D(0)

    return ScenarioResult(
        scenario="queue_priority",
        total_orders=n,
        fills=fills,
        fill_rate=(D(fills) / D(n)).quantize(D("0.0001")) if n > 0 else D(0),
        total_pnl=total_pnl.quantize(D("0.01")),
        avg_fill_price=D(0),
        adverse_selection_count=adverse,
        adverse_selection_loss=adverse_loss.quantize(D("0.01")),
        queue_position_avg=avg_queue.quantize(D("0.01")),
        cancellation_rate=D(0),
    )


def _simulate_adverse_selection(
    orders: list[dict],
) -> ScenarioResult:
    """Simulate maker orders where informed traders pick off quotes."""
    fills = 0
    total_pnl = D(0)
    adverse = 0
    adverse_loss = D(0)

    for order in orders:
        side = order.get("side", "BUY")
        price = order.get("price", D("0.5"))
        size = order.get("size", D(1))
        # Only count as fill if aggressive trade actually executes at our level
        aggressive_trade = order.get("aggressive_trade_at_level", False)
        trade_price = order.get("trade_price")
        trade_size = order.get("trade_size", D(0))

        if not aggressive_trade or trade_price != price:
            continue

        fill_amount = min(size, trade_size)
        if fill_amount <= 0:
            continue

        fills += 1
        # Adverse selection: did informed trader know something?
        information_move = order.get("information_move", D(0))
        if abs(information_move) > D("0.01"):
            adverse += 1
            adverse_loss += abs(information_move) * fill_amount

        future_mid = order.get("future_midpoint", price)
        if side == "BUY":
            pnl = (future_mid - price) * fill_amount
        else:
            pnl = (price - future_mid) * fill_amount
        total_pnl += pnl

    n = len(orders)
    return ScenarioResult(
        scenario="adverse_selection",
        total_orders=n,
        fills=fills,
        fill_rate=(D(fills) / D(n)).quantize(D("0.0001")) if n > 0 else D(0),
        total_pnl=total_pnl.quantize(D("0.01")),
        avg_fill_price=D(0),
        adverse_selection_count=adverse,
        adverse_selection_loss=adverse_loss.quantize(D("0.01")),
        queue_position_avg=D(0),
        cancellation_rate=D(0),
    )


def _simulate_cancellation(
    orders: list[dict],
    cancel_probability: Decimal = D("0.30"),
) -> ScenarioResult:
    """Simulate orders canceled before fill."""
    import random

    fills = 0
    canceled = 0
    total_pnl = D(0)

    for order in orders:
        if random.random() < float(cancel_probability):
            canceled += 1
            continue

        # Check if aggressive volume reaches price
        agg_volume = order.get("aggressive_volume_reaching_price", D(0))
        queue_ahead = order.get("queue_ahead", D(0))
        size = order.get("size", D(1))
        fill_amount = min(size, max(D(0), agg_volume - queue_ahead))

        if fill_amount > 0:
            fills += 1
            side = order.get("side", "BUY")
            price = order.get("price", D("0.5"))
            future_mid = order.get("future_midpoint", price)
            if side == "BUY":
                pnl = (future_mid - price) * fill_amount
            else:
                pnl = (price - future_mid) * fill_amount
            total_pnl += pnl

    n = len(orders)
    return ScenarioResult(
        scenario="canceled_before_fill",
        total_orders=n,
        fills=fills,
        fill_rate=(D(fills) / D(n)).quantize(D("0.0001")) if n > 0 else D(0),
        total_pnl=total_pnl.quantize(D("0.01")),
        avg_fill_price=D(0),
        adverse_selection_count=0,
        adverse_selection_loss=D(0),
        queue_position_avg=D(0),
        cancellation_rate=(D(canceled) / D(n)).quantize(D("0.0001")) if n > 0 else D(0),
    )


def _simulate_stale_quote(
    orders: list[dict],
    staleness_threshold_seconds: int = 30,
) -> ScenarioResult:
    """Simulate orders that become stale due to delayed updates."""
    fills = 0
    stale_fills = 0
    total_pnl = D(0)
    adverse_loss = D(0)

    for order in orders:
        side = order.get("side", "BUY")
        price = order.get("price", D("0.5"))
        size = order.get("size", D(1))
        age_seconds = order.get("quote_age_seconds", 0)
        agg_volume = order.get("aggressive_volume_reaching_price", D(0))
        queue_ahead = order.get("queue_ahead", D(0))
        fill_amount = min(size, max(D(0), agg_volume - queue_ahead))

        if fill_amount <= 0:
            continue

        fills += 1
        is_stale = age_seconds > staleness_threshold_seconds
        if is_stale:
            stale_fills += 1

        # Price moved away from stale quote
        current_mid = order.get("current_midpoint", price)
        if side == "BUY" and current_mid < price:
            loss = (price - current_mid) * fill_amount
            adverse_loss += loss
        elif side == "SELL" and current_mid > price:
            loss = (current_mid - price) * fill_amount
            adverse_loss += loss

        future_mid = order.get("future_midpoint", current_mid)
        if side == "BUY":
            pnl = (future_mid - price) * fill_amount
        else:
            pnl = (price - future_mid) * fill_amount
        total_pnl += pnl

    n = len(orders)
    stale_rate = D(stale_fills) / D(fills) if fills > 0 else D(0)

    return ScenarioResult(
        scenario="stale_quote",
        total_orders=n,
        fills=fills,
        fill_rate=(D(fills) / D(n)).quantize(D("0.0001")) if n > 0 else D(0),
        total_pnl=total_pnl.quantize(D("0.01")),
        avg_fill_price=D(0),
        adverse_selection_count=stale_fills,
        adverse_selection_loss=adverse_loss.quantize(D("0.01")),
        queue_position_avg=D(0),
        cancellation_rate=stale_rate.quantize(D("0.0001")),
    )


def _simulate_price_touches_no_fill(
    orders: list[dict],
) -> ScenarioResult:
    """Price touches the level but no fill occurs (no aggressive volume)."""
    touches = 0
    fills = 0
    total_pnl = D(0)

    for order in orders:
        price_touched = order.get("price_touched", False)
        agg_volume = order.get("aggressive_volume_reaching_price", D(0))
        size = order.get("size", D(1))
        queue_ahead = order.get("queue_ahead", D(0))
        side = order.get("side", "BUY")

        if price_touched:
            touches += 1

        fill_amount = min(size, max(D(0), agg_volume - queue_ahead))
        if fill_amount > 0:
            fills += 1
            future_mid = order.get(
                "future_midpoint",
                price_touched and order.get("price", D("0.5"))
            )
            price = order.get("price", D("0.5"))
            if side == "BUY":
                pnl = (future_mid - price) * fill_amount
            else:
                pnl = (price - future_mid) * fill_amount
            total_pnl += pnl

    n = len(orders)
    return ScenarioResult(
        scenario="price_touches_no_fill",
        total_orders=n,
        fills=fills,
        fill_rate=(D(fills) / D(n)).quantize(D("0.0001")) if n > 0 else D(0),
        total_pnl=total_pnl.quantize(D("0.01")),
        avg_fill_price=D(0),
        adverse_selection_count=0,
        adverse_selection_loss=D(0),
        queue_position_avg=D(0),
        cancellation_rate=D(0),
    )


def _simulate_information_shock(
    orders: list[dict],
) -> ScenarioResult:
    """Sudden information causes price jump through resting orders."""
    fills = 0
    total_pnl = D(0)
    shock_loss = D(0)

    for order in orders:
        info_shock = order.get("information_shock", False)
        shock_magnitude = order.get("shock_magnitude", D(0))
        side = order.get("side", "BUY")
        price = order.get("price", D("0.5"))
        size = order.get("size", D(1))
        agg_volume = order.get("aggressive_volume_reaching_price", D(0))
        queue_ahead = order.get("queue_ahead", D(0))
        fill_amount = min(size, max(D(0), agg_volume - queue_ahead))

        if fill_amount <= 0:
            continue

        fills += 1
        if info_shock:
            # Order gets swept at unfavorable price
            shock_loss += abs(shock_magnitude) * fill_amount

        future_mid = order.get("future_midpoint", price)
        if side == "BUY":
            pnl = (future_mid - price) * fill_amount
        else:
            pnl = (price - future_mid) * fill_amount
        total_pnl += pnl

    n = len(orders)
    return ScenarioResult(
        scenario="sudden_information",
        total_orders=n,
        fills=fills,
        fill_rate=(D(fills) / D(n)).quantize(D("0.0001")) if n > 0 else D(0),
        total_pnl=total_pnl.quantize(D("0.01")),
        avg_fill_price=D(0),
        adverse_selection_count=fills,
        adverse_selection_loss=shock_loss.quantize(D("0.01")),
        queue_position_avg=D(0),
        cancellation_rate=D(0),
    )


_SCENARIO_FNS = {
    "queue_priority": _simulate_queue_priority,
    "adverse_selection": _simulate_adverse_selection,
    "canceled_before_fill": lambda o, b: _simulate_cancellation(o),
    "price_touches_no_fill": lambda o, b: _simulate_price_touches_no_fill(o),
    "stale_quote": lambda o, b: _simulate_stale_quote(o),
    "sudden_information": lambda o, b: _simulate_information_shock(o),
    "partial_fills": lambda o, b: _simulate_queue_priority(o, b),
    "price_moves_through": lambda o, b: _simulate_adverse_selection(o),
}


def stress_test_maker(
    signals: list[dict],
    book_snapshots: list[dict],
    scenarios: list[str] | None = None,
) -> MakerStressResult:
    """Stress test maker strategy across multiple adverse scenarios.

    Never counts fill merely because historical price crossed the level.
    Requires actual aggressive volume reaching the order's price level.

    Args:
        signals: Maker order signals with execution context.
        book_snapshots: Historical book states.
        scenarios: Scenario names to test. Default: all built-in scenarios.

    Returns:
        MakerStressResult with per-scenario metrics and sensitivity analysis.
    """
    if scenarios is None:
        scenarios = list(_SCENARIO_FNS.keys())

    # Baseline: simple fill simulation
    baseline_fills = 0
    baseline_pnl = D(0)
    for order in signals:
        agg = order.get("aggressive_volume_reaching_price", D(0))
        qa = order.get("queue_ahead", D(0))
        size = order.get("size", D(1))
        fill = min(size, max(D(0), agg - qa))
        if fill > 0:
            baseline_fills += 1
            side = order.get("side", "BUY")
            price = order.get("price", D("0.5"))
            future = order.get("future_midpoint", price)
            if side == "BUY":
                baseline_pnl += (future - price) * fill
            else:
                baseline_pnl += (price - future) * fill

    baseline_rate = D(baseline_fills) / D(len(signals)) if signals else D(0)

    results: list[ScenarioResult] = []
    for scenario in scenarios:
        fn = _SCENARIO_FNS.get(scenario)
        if fn is None:
            continue
        result = fn(signals, book_snapshots)
        results.append(result)

    worst = min(results, key=lambda r: r.total_pnl) if results else None
    pnl_values = [r.total_pnl for r in results]
    sensitivity = max(pnl_values) - min(pnl_values) if len(pnl_values) >= 2 else None

    return MakerStressResult(
        scenario_results=results,
        baseline_fill_rate=baseline_rate.quantize(D("0.0001")),
        baseline_pnl=baseline_pnl.quantize(D("0.01")),
        worst_scenario=worst.scenario if worst else None,
        worst_pnl=worst.total_pnl if worst else None,
        scenario_sensitivity=sensitivity.quantize(D("0.01")) if sensitivity else None,
    )
