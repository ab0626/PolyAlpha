"""Decision auditability — full reconstruction of every trading decision.

Records decision context (market state, features, models, forecasts,
costs, risk) and enables full reconstruction of any decision for
regulatory and debugging purposes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

D = Decimal


@dataclass(frozen=True)
class MarketStateSnapshot:
    """Market state at decision time."""

    market_id: str
    timestamp: datetime
    midpoint: Decimal
    bid: Decimal
    ask: Decimal
    spread: Decimal
    bid_depth: Decimal
    ask_depth: Decimal
    volume_24h: Decimal
    last_trade_price: Decimal
    last_trade_time: datetime | None = None


@dataclass(frozen=True)
class FeatureSnapshot:
    """Feature vector at decision time."""

    feature_names: list[str]
    feature_values: list[float]
    feature_importances: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelSnapshot:
    """Model state at decision time."""

    model_id: str
    model_version: str
    model_type: str
    training_samples: int
    training_date: datetime | None = None
    calibration_version: str = ""
    brier_score: float = 0.0


@dataclass(frozen=True)
class ForecastSnapshot:
    """Model forecast at decision time."""

    raw_forecast: Decimal
    calibrated_forecast: Decimal
    ensemble_forecasts: dict[str, Decimal] = field(default_factory=dict)
    uncertainty_estimate: Decimal = D(0)
    confidence_interval_lower: Decimal = D(0)
    confidence_interval_upper: Decimal = D(1)


@dataclass(frozen=True)
class CostSnapshot:
    """Transaction cost estimate at decision time."""

    spread_cost: Decimal
    slippage_estimate: Decimal
    fee_estimate: Decimal
    total_cost: Decimal
    maker_taker: str = "taker"


@dataclass(frozen=True)
class RiskSnapshot:
    """Risk assessment at decision time."""

    position_size: Decimal
    portfolio_heat: Decimal
    current_drawdown: Decimal
    daily_pnl: Decimal
    risk_budget_remaining: Decimal
    correlation_penalty: Decimal = D(0)
    kelly_fraction: Decimal = D(0)


@dataclass(frozen=True)
class DecisionRecord:
    """Complete audit record for a single decision."""

    decision_id: str
    timestamp: datetime
    market_id: str
    signal_id: str
    side: str
    size: Decimal
    target_price: Decimal
    market_state: MarketStateSnapshot
    features: FeatureSnapshot
    model: ModelSnapshot
    forecast: ForecastSnapshot
    costs: CostSnapshot
    risk: RiskSnapshot
    edge: Decimal
    expected_value: Decimal
    final_decision: str  # "accept", "reject", "modify"
    rejection_reason: str | None = None
    actual_fill_price: Decimal | None = None
    actual_fill_size: Decimal | None = None

    def summary(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "timestamp": self.timestamp.isoformat(),
            "market_id": self.market_id,
            "side": self.side,
            "size": str(self.size),
            "edge": str(self.edge),
            "final_decision": self.final_decision,
        }


class DecisionAuditor:
    """Full decision audit trail with reconstruction capability.

    Records every decision with complete context (market state, features,
    model state, forecast, costs, risk) and enables reconstruction
    of any decision for debugging and regulatory compliance.
    """

    def __init__(self):
        self._decisions: dict[str, DecisionRecord] = {}
        self._market_history: dict[str, list[MarketStateSnapshot]] = {}
        self._decision_index: list[str] = []

    def record_decision(
        self,
        market_id: str,
        signal_id: str,
        side: str,
        size: Decimal,
        target_price: Decimal,
        market_state: MarketStateSnapshot,
        features: FeatureSnapshot,
        model: ModelSnapshot,
        forecast: ForecastSnapshot,
        costs: CostSnapshot,
        risk: RiskSnapshot,
        edge: Decimal,
        expected_value: Decimal,
        final_decision: str,
        rejection_reason: str | None = None,
        actual_fill_price: Decimal | None = None,
        actual_fill_size: Decimal | None = None,
    ) -> str:
        """Record a complete decision with all context. Returns decision_id."""
        decision_id = str(uuid4())
        record = DecisionRecord(
            decision_id=decision_id,
            timestamp=datetime.now(timezone.utc),
            market_id=market_id,
            signal_id=signal_id,
            side=side,
            size=size,
            target_price=target_price,
            market_state=market_state,
            features=features,
            model=model,
            forecast=forecast,
            costs=costs,
            risk=risk,
            edge=edge,
            expected_value=expected_value,
            final_decision=final_decision,
            rejection_reason=rejection_reason,
            actual_fill_price=actual_fill_price,
            actual_fill_size=actual_fill_size,
        )

        self._decisions[decision_id] = record
        self._decision_index.append(decision_id)

        # Track market history for reconstruction
        self._market_history.setdefault(market_id, []).append(market_state)

        return decision_id

    def reconstruct(self, decision_id: str) -> dict | None:
        """Reconstruct the full context of a decision.

        Returns a dict with all decision context or None if not found.
        """
        record = self._decisions.get(decision_id)
        if record is None:
            return None

        # Get surrounding market state
        market_states = self._market_history.get(record.market_id, [])
        state_idx = None
        for i, ms in enumerate(market_states):
            if ms.timestamp == record.market_state.timestamp:
                state_idx = i
                break

        preceding_states = []
        following_states = []
        if state_idx is not None:
            preceding_states = market_states[max(0, state_idx - 5) : state_idx]
            following_states = market_states[state_idx + 1 : state_idx + 6]

        return {
            "decision": record.summary(),
            "full_context": {
                "market_state": {
                    "market_id": record.market_state.market_id,
                    "timestamp": record.market_state.timestamp.isoformat(),
                    "midpoint": str(record.market_state.midpoint),
                    "bid": str(record.market_state.bid),
                    "ask": str(record.market_state.ask),
                    "spread": str(record.market_state.spread),
                    "bid_depth": str(record.market_state.bid_depth),
                    "ask_depth": str(record.market_state.ask_depth),
                },
                "features": {
                    "names": record.features.feature_names,
                    "values": record.features.feature_values,
                    "importances": record.features.feature_importances,
                },
                "model": {
                    "id": record.model.model_id,
                    "version": record.model.model_version,
                    "type": record.model.model_type,
                    "training_samples": record.model.training_samples,
                    "calibration": record.model.calibration_version,
                },
                "forecast": {
                    "raw": str(record.forecast.raw_forecast),
                    "calibrated": str(record.forecast.calibrated_forecast),
                    "ensemble": {k: str(v) for k, v in record.forecast.ensemble_forecasts.items()},
                    "uncertainty": str(record.forecast.uncertainty_estimate),
                },
                "costs": {
                    "spread": str(record.costs.spread_cost),
                    "slippage": str(record.costs.slippage_estimate),
                    "fee": str(record.costs.fee_estimate),
                    "total": str(record.costs.total_cost),
                },
                "risk": {
                    "position_size": str(record.risk.position_size),
                    "portfolio_heat": str(record.risk.portfolio_heat),
                    "drawdown": str(record.risk.current_drawdown),
                    "daily_pnl": str(record.risk.daily_pnl),
                    "budget_remaining": str(record.risk.risk_budget_remaining),
                },
                "edge_analysis": {
                    "edge": str(record.edge),
                    "expected_value": str(record.expected_value),
                    "net_of_costs": str(record.edge - record.costs.total_cost),
                },
                "preceding_market_states": [
                    {
                        "timestamp": ms.timestamp.isoformat(),
                        "midpoint": str(ms.midpoint),
                        "spread": str(ms.spread),
                    }
                    for ms in preceding_states
                ],
                "following_market_states": [
                    {
                        "timestamp": ms.timestamp.isoformat(),
                        "midpoint": str(ms.midpoint),
                        "spread": str(ms.spread),
                    }
                    for ms in following_states
                ],
            },
            "execution": {
                "final_decision": record.final_decision,
                "rejection_reason": record.rejection_reason,
                "actual_fill_price": (
                    str(record.actual_fill_price)
                    if record.actual_fill_price
                    else None
                ),
                "actual_fill_size": (
                    str(record.actual_fill_size)
                    if record.actual_fill_size
                    else None
                ),
                "slippage": (
                    str(record.actual_fill_price - record.target_price)
                    if record.actual_fill_price
                    else None
                ),
            },
        }

    def export_audit_trail(
        self,
        market_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        decision_type: str | None = None,
    ) -> list[dict]:
        """Export audit trail with optional filtering.

        Args:
            market_id: Filter to specific market.
            start_time: Filter decisions after this time.
            end_time: Filter decisions before this time.
            decision_type: Filter by "accept", "reject", or "modify".

        Returns:
            List of full decision reconstructions.
        """
        results: list[dict] = []

        for decision_id in self._decision_index:
            record = self._decisions.get(decision_id)
            if record is None:
                continue

            # Apply filters
            if market_id and record.market_id != market_id:
                continue
            if start_time and record.timestamp < start_time:
                continue
            if end_time and record.timestamp > end_time:
                continue
            if decision_type and record.final_decision != decision_type:
                continue

            reconstruction = self.reconstruct(decision_id)
            if reconstruction:
                results.append(reconstruction)

        return results

    def get_decision_count(self) -> int:
        """Get total number of recorded decisions."""
        return len(self._decisions)

    def get_acceptance_rate(self) -> Decimal:
        """Get acceptance rate across all decisions."""
        if not self._decisions:
            return D(0)
        accepted = sum(
            1 for d in self._decisions.values() if d.final_decision == "accept"
        )
        return D(accepted) / D(len(self._decisions))

    def get_summary_stats(self) -> dict:
        """Get summary statistics of all recorded decisions."""
        if not self._decisions:
            return {"total_decisions": 0}

        edges = [d.edge for d in self._decisions.values()]
        sizes = [d.size for d in self._decisions.values()]
        accepted = sum(
            1 for d in self._decisions.values() if d.final_decision == "accept"
        )
        rejected = sum(
            1 for d in self._decisions.values() if d.final_decision == "reject"
        )

        return {
            "total_decisions": len(self._decisions),
            "accepted": accepted,
            "rejected": rejected,
            "acceptance_rate": str(
                (D(accepted) / D(len(self._decisions)) * D(100)).quantize(D("0.1"))
            )
            + "%",
            "avg_edge": str((sum(edges) / D(len(edges))).quantize(D("0.0001"))),
            "avg_size": str((sum(sizes) / D(len(sizes))).quantize(D("0.01"))),
            "markets_traded": len(set(d.market_id for d in self._decisions.values())),
        }
