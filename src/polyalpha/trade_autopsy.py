"""Trade autopsies — Part 19 structured failure classification."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

D = Decimal

FailureType = Literal[
    "forecast_error",
    "stale_external_information",
    "microstructure_false_signal",
    "execution_slippage",
    "fee_drag",
    "model_overconfidence",
    "correlation_concentration",
    "resolution_ambiguity",
    "late_information",
    "calibration_error",
    "unknown",
]

ALL_FAILURE_TYPES: tuple[str, ...] = tuple(FailureType.__args__)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class TradeAutopsy:
    market: str
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    model_probability: Decimal
    conservative_probability: Decimal
    final_resolution: str
    realized_pnl: Decimal
    predicted_edge: Decimal
    realized_edge: Decimal
    model_component_predictions: dict[str, Decimal] = field(default_factory=dict)
    market_movement_after_entry: Decimal = D("0")
    external_information_changes: list[str] = field(default_factory=list)
    resolution_risk_changes: list[str] = field(default_factory=list)
    failure_classification: FailureType = "unknown"


def classify_failure(
    trade: TradeAutopsy,
    spread_at_entry: Decimal | None = None,
    calibration_error_for_range: Decimal | None = None,
) -> FailureType:
    """Assign one of 11 failure classifications. Only call for losing trades."""
    forecast_correct = (
        trade.exit_price > trade.entry_price
        if trade.model_probability > trade.conservative_probability
        else trade.exit_price < trade.entry_price
    )
    if not forecast_correct:
        return "forecast_error"
    if trade.external_information_changes:
        return "stale_external_information"
    if spread_at_entry is not None and spread_at_entry > D("0.03"):
        return "microstructure_false_signal"
    if abs(trade.predicted_edge) > D("0"):
        edge_captured = abs(trade.realized_edge) / abs(trade.predicted_edge)
        if D("1") - edge_captured > D("0.5"):
            return "execution_slippage"
    if abs(trade.realized_pnl) < abs(trade.predicted_edge) * D("0.1"):
        return "fee_drag"
    if trade.model_probability > D("0.8") and not forecast_correct:
        return "model_overconfidence"
    if len(trade.model_component_predictions) >= 5:
        top = sorted(trade.model_component_predictions.values(), reverse=True)
        if len(top) >= 2 and top[1] > D("0.6"):
            return "correlation_concentration"
    for c in trade.resolution_risk_changes:
        if any(w in c.lower() for w in ("ambiguous", "unclear")):
            return "resolution_ambiguity"
    if trade.market_movement_after_entry != D("0") and trade.external_information_changes:
        return "late_information"
    if calibration_error_for_range is not None and calibration_error_for_range > D("0.1"):
        return "calibration_error"
    return "unknown"


def autopsy_trade(
    market: str,
    entry_time: datetime,
    exit_time: datetime,
    entry_price: Decimal,
    exit_price: Decimal,
    model_probability: Decimal,
    conservative_probability: Decimal,
    final_resolution: str,
    predicted_edge: Decimal,
    **kw: object,
) -> TradeAutopsy:
    """Build a TradeAutopsy and classify its failure if losing."""
    is_long = model_probability > conservative_probability
    realized_edge = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    realized_pnl = realized_edge * kw.pop("shares", D("1")) - kw.pop("fees", D("0"))
    trade = TradeAutopsy(
        market=market,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=entry_price,
        exit_price=exit_price,
        model_probability=model_probability,
        conservative_probability=conservative_probability,
        final_resolution=final_resolution,
        realized_pnl=realized_pnl,
        predicted_edge=predicted_edge,
        realized_edge=realized_edge,
        model_component_predictions=kw.pop("model_component_predictions", {}),
        market_movement_after_entry=kw.pop("market_movement_after_entry", D("0")),
        external_information_changes=kw.pop("external_information_changes", []),
        resolution_risk_changes=kw.pop("resolution_risk_changes", []),
    )
    if realized_pnl >= D("0"):
        return trade
    return TradeAutopsy(
        **{
            **trade.__dict__,
            "failure_classification": classify_failure(
                trade,
                kw.get("spread_at_entry"),
                kw.get("calibration_error_for_range"),
            ),
        }
    )


def aggregate_autopsies(autopsies: list[TradeAutopsy]) -> dict[FailureType, dict[str, object]]:
    """Group autopsies by failure type with summary statistics."""
    groups: dict[str, list[TradeAutopsy]] = defaultdict(list)
    for a in autopsies:
        groups[a.failure_classification].append(a)
    result: dict[str, dict[str, object]] = {}
    for ftype in ALL_FAILURE_TYPES:
        trades = groups.get(ftype, [])
        if not trades:
            continue
        pnls = [t.realized_pnl for t in trades]
        result[ftype] = {
            "count": len(trades),
            "total_pnl": sum(pnls, D("0")),
            "avg_pnl": sum(pnls, D("0")) / len(pnls),
            "win_rate": sum(1 for p in pnls if p > 0) / len(pnls),
            "markets": list({t.market for t in trades}),
        }
    return result
