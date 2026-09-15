"""Signal explanations — human-readable rationale for each signal.

Section 17 of the v0.3 spec: every signal must be explainable.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SignalExplanation:
    """Human-readable explanation of why a signal was generated."""

    market_id: str
    side: str
    fair_probability: Decimal
    execution_price: Decimal
    gross_edge: Decimal
    net_edge: Decimal

    # Cost breakdown
    fee_cost: Decimal
    slippage_cost: Decimal
    uncertainty_cost: Decimal
    resolution_cost: Decimal
    stale_cost: Decimal
    liquidity_cost: Decimal

    # Key drivers
    primary_driver: str  # "orderbook_imbalance", "cross_market_spread", etc.
    secondary_drivers: list[str]
    risk_notes: list[str]

    # Confidence
    confidence_level: str  # "high", "medium", "low"
    confidence_reason: str

    # Cluster and market context
    event_cluster: str = ""
    cluster_exposure: float = 0.0
    vwap: Decimal = Decimal("0")
    spread: Decimal = Decimal("0")

    def to_text(self) -> str:
        """Generate human-readable explanation."""
        lines = [
            f"Signal: {self.side} {self.market_id}",
            f"Fair value: {self.fair_probability}, Market price: {self.execution_price}",
            f"Gross edge: {self.gross_edge}, Net edge: {self.net_edge}",
            "",
            "Cost breakdown:",
            f"  Fees: {self.fee_cost}",
            f"  Slippage: {self.slippage_cost}",
            f"  Uncertainty: {self.uncertainty_cost}",
            f"  Resolution: {self.resolution_cost}",
            f"  Stale data: {self.stale_cost}",
            f"  Liquidity: {self.liquidity_cost}",
            "",
            f"Primary driver: {self.primary_driver}",
        ]
        if self.secondary_drivers:
            lines.append(f"Secondary drivers: {', '.join(self.secondary_drivers)}")
        if self.risk_notes:
            lines.append(f"Risk notes: {'; '.join(self.risk_notes)}")
        lines.append(f"Confidence: {self.confidence_level} ({self.confidence_reason})")
        if self.event_cluster:
            lines.append(f"Event cluster: {self.event_cluster}")
        if self.cluster_exposure:
            lines.append(f"Cluster exposure: {self.cluster_exposure:.2%}")
        if self.vwap:
            lines.append(f"VWAP: {self.vwap}")
        if self.spread:
            lines.append(f"Spread: {self.spread}")
        return "\n".join(lines)


def explain_signal(
    market_id: str,
    side: str,
    fair_probability: Decimal,
    execution_price: Decimal,
    gross_edge: Decimal,
    net_edge: Decimal,
    fee_cost: Decimal = Decimal("0"),
    slippage_cost: Decimal = Decimal("0"),
    uncertainty_cost: Decimal = Decimal("0"),
    resolution_cost: Decimal = Decimal("0"),
    stale_cost: Decimal = Decimal("0"),
    liquidity_cost: Decimal = Decimal("0"),
    primary_driver: str = "unknown",
    secondary_drivers: list[str] | None = None,
    risk_notes: list[str] | None = None,
    confidence_level: str = "medium",
    confidence_reason: str = "",
    event_cluster: str = "",
    cluster_exposure: float = 0.0,
    vwap: Decimal = Decimal("0"),
    spread: Decimal = Decimal("0"),
) -> SignalExplanation:
    """Build a SignalExplanation."""
    return SignalExplanation(
        market_id=market_id,
        side=side,
        fair_probability=fair_probability,
        execution_price=execution_price,
        gross_edge=gross_edge,
        net_edge=net_edge,
        fee_cost=fee_cost,
        slippage_cost=slippage_cost,
        uncertainty_cost=uncertainty_cost,
        resolution_cost=resolution_cost,
        stale_cost=stale_cost,
        liquidity_cost=liquidity_cost,
        primary_driver=primary_driver,
        secondary_drivers=secondary_drivers or [],
        risk_notes=risk_notes or [],
        confidence_level=confidence_level,
        confidence_reason=confidence_reason,
        event_cluster=event_cluster,
        cluster_exposure=cluster_exposure,
        vwap=vwap,
        spread=spread,
    )
