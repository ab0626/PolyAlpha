"""OrderIntent — the execution-mode-agnostic order contract.

Part of the live-ready execution architecture. Every mode (backtest, replay,
paper, shadow, live-ready) consumes the SAME OrderIntent; no strategy contains
exchange-specific submission logic. The intent carries the full decision
context — pricing, edge, exposures before/after, freshness, and hash
provenance — so a pre-trade gate can revalidate the world at execution time.

Also defines the intent state machine used for idempotent execution tracking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from uuid import uuid4

D = Decimal


class IntentState(str, Enum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUBMISSION_PENDING = "SUBMISSION_PENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


# States that may legally be retried without re-creating a logical order.
# Idempotency: a retry re-uses intent_id / execution_attempt_id.
RETRYABLE_STATES = {
    IntentState.CREATED,
    IntentState.VALIDATING,
    IntentState.APPROVAL_PENDING,
    IntentState.SUBMISSION_PENDING,
    IntentState.RECONCILIATION_REQUIRED,
}
# States from which no further progression is allowed.
TERMINAL_STATES = {
    IntentState.REJECTED,
    IntentState.FILLED,
    IntentState.CANCELLED,
    IntentState.FAILED,
}


@dataclass(frozen=True)
class OrderIntent:
    """Complete, mode-agnostic order intent."""

    # Identity
    intent_id: str
    created_at: datetime
    decision_id: str
    strategy_version: str

    # Market identity
    market_id: str
    condition_id: str
    token_id: str
    outcome: str
    side: str  # "BUY" | "SELL"

    # Sizing / pricing
    requested_shares: Decimal
    requested_notional: Decimal
    limit_price: Decimal

    # Fair value
    fair_probability: Decimal
    conservative_probability: Decimal

    # Execution expectation
    expected_vwap: Decimal
    expected_fee: Decimal
    expected_slippage: Decimal

    # Edge
    gross_edge: Decimal
    predicted_net_edge: Decimal

    # Model / provenance
    model_version: str

    # Event taxonomy
    event_id: str
    event_cluster: str
    category: str

    # Exposures before the trade
    market_exposure_before: Decimal
    event_exposure_before: Decimal
    cluster_exposure_before: Decimal
    total_exposure_before: Decimal

    # Exposures after the trade (projected)
    market_exposure_after: Decimal
    event_exposure_after: Decimal
    cluster_exposure_after: Decimal
    total_exposure_after: Decimal

    # Freshness
    book_timestamp: datetime
    model_timestamp: datetime

    # Hash provenance
    research_logic_sha256: str
    config_sha256: str

    # TOCTOU: hash of the book the signal was generated against
    signal_book_hash: str = ""

    # Idempotency: a monotonic attempt counter scoped to the intent
    execution_attempt_id: str = ""
    state: IntentState = IntentState.CREATED

    def __post_init__(self) -> None:
        for ts in (self.created_at, self.book_timestamp, self.model_timestamp):
            if ts.tzinfo is None or ts.utcoffset() is None:
                raise ValueError("timestamps must be timezone-aware")
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if self.outcome not in ("YES", "NO"):
            raise ValueError("outcome must be YES or NO")
        for v in (
            self.requested_shares,
            self.requested_notional,
            self.limit_price,
            self.fair_probability,
            self.conservative_probability,
            self.expected_vwap,
            self.expected_fee,
            self.expected_slippage,
            self.gross_edge,
            self.predicted_net_edge,
        ):
            if not v.is_finite() or v < 0:
                raise ValueError("numeric fields must be finite and non-negative")
        for v in (
            self.market_exposure_before,
            self.event_exposure_before,
            self.cluster_exposure_before,
            self.total_exposure_before,
            self.market_exposure_after,
            self.event_exposure_after,
            self.cluster_exposure_after,
            self.total_exposure_after,
        ):
            if not v.is_finite():
                raise ValueError("exposure fields must be finite")

    def next_attempt(self) -> "OrderIntent":
        """Return a copy with a fresh execution_attempt_id (idempotency)."""
        return OrderIntent(
            **{
                **self.__dict__,
                "execution_attempt_id": f"{self.intent_id}:{uuid4().hex[:12]}",
            }
        )

    def with_state(self, state: IntentState) -> "OrderIntent":
        return OrderIntent(**{**self.__dict__, "state": state})

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "created_at": self.created_at.isoformat(),
            "decision_id": self.decision_id,
            "strategy_version": self.strategy_version,
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "outcome": self.outcome,
            "side": self.side,
            "requested_shares": str(self.requested_shares),
            "requested_notional": str(self.requested_notional),
            "limit_price": str(self.limit_price),
            "fair_probability": str(self.fair_probability),
            "conservative_probability": str(self.conservative_probability),
            "expected_vwap": str(self.expected_vwap),
            "expected_fee": str(self.expected_fee),
            "expected_slippage": str(self.expected_slippage),
            "gross_edge": str(self.gross_edge),
            "predicted_net_edge": str(self.predicted_net_edge),
            "model_version": self.model_version,
            "event_id": self.event_id,
            "event_cluster": self.event_cluster,
            "category": self.category,
            "market_exposure_before": str(self.market_exposure_before),
            "event_exposure_before": str(self.event_exposure_before),
            "cluster_exposure_before": str(self.cluster_exposure_before),
            "total_exposure_before": str(self.total_exposure_before),
            "market_exposure_after": str(self.market_exposure_after),
            "event_exposure_after": str(self.event_exposure_after),
            "cluster_exposure_after": str(self.cluster_exposure_after),
            "total_exposure_after": str(self.total_exposure_after),
            "book_timestamp": self.book_timestamp.isoformat(),
            "model_timestamp": self.model_timestamp.isoformat(),
            "research_logic_sha256": self.research_logic_sha256,
            "config_sha256": self.config_sha256,
            "signal_book_hash": self.signal_book_hash,
            "execution_attempt_id": self.execution_attempt_id,
            "state": self.state.value,
        }


def new_intent(
    decision_id: str,
    strategy_version: str,
    market_id: str,
    condition_id: str,
    token_id: str,
    outcome: str,
    side: str,
    requested_shares: Decimal,
    requested_notional: Decimal,
    limit_price: Decimal,
    fair_probability: Decimal,
    conservative_probability: Decimal,
    expected_vwap: Decimal,
    expected_fee: Decimal,
    expected_slippage: Decimal,
    gross_edge: Decimal,
    predicted_net_edge: Decimal,
    model_version: str,
    event_id: str,
    event_cluster: str,
    category: str,
    exposures_before: tuple[Decimal, Decimal, Decimal, Decimal],
    exposures_after: tuple[Decimal, Decimal, Decimal, Decimal],
    book_timestamp: datetime,
    model_timestamp: datetime,
    research_logic_sha256: str,
    config_sha256: str,
    signal_book_hash: str = "",
    created_at: datetime | None = None,
) -> OrderIntent:
    """Convenience constructor with a fresh intent_id and attempt id."""
    created = created_at or datetime.now(UTC)
    intent = OrderIntent(
        intent_id=uuid4().hex,
        created_at=created,
        decision_id=decision_id,
        strategy_version=strategy_version,
        market_id=market_id,
        condition_id=condition_id,
        token_id=token_id,
        outcome=outcome,
        side=side,
        requested_shares=requested_shares,
        requested_notional=requested_notional,
        limit_price=limit_price,
        fair_probability=fair_probability,
        conservative_probability=conservative_probability,
        expected_vwap=expected_vwap,
        expected_fee=expected_fee,
        expected_slippage=expected_slippage,
        gross_edge=gross_edge,
        predicted_net_edge=predicted_net_edge,
        model_version=model_version,
        event_id=event_id,
        event_cluster=event_cluster,
        category=category,
        market_exposure_before=exposures_before[0],
        event_exposure_before=exposures_before[1],
        cluster_exposure_before=exposures_before[2],
        total_exposure_before=exposures_before[3],
        market_exposure_after=exposures_after[0],
        event_exposure_after=exposures_after[1],
        cluster_exposure_after=exposures_after[2],
        total_exposure_after=exposures_after[3],
        book_timestamp=book_timestamp,
        model_timestamp=model_timestamp,
        research_logic_sha256=research_logic_sha256,
        config_sha256=config_sha256,
        signal_book_hash=signal_book_hash,
    )
    return intent.next_attempt()


class IntentLedger:
    """Idempotency ledger: repeated processing of the same intent_id or
    execution_attempt_id must not create a second logical order."""

    def __init__(self) -> None:
        self._by_id: dict[str, OrderIntent] = {}
        self._attempts: set[str] = set()

    def register(self, intent: OrderIntent) -> bool:
        """Return True if this intent is new; False if it's a duplicate."""
        if intent.intent_id in self._by_id:
            return False
        if intent.execution_attempt_id in self._attempts:
            return False
        self._by_id[intent.intent_id] = intent
        self._attempts.add(intent.execution_attempt_id)
        return True

    def get(self, intent_id: str) -> OrderIntent | None:
        return self._by_id.get(intent_id)

    def has_attempt(self, execution_attempt_id: str) -> bool:
        return execution_attempt_id in self._attempts

    def size(self) -> int:
        return len(self._by_id)