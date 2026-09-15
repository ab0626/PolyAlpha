"""OrderIntent — the execution-mode-agnostic order contract.

Part of the live-ready execution architecture. Every mode (backtest, replay,
paper, shadow, live-ready) consumes the SAME OrderIntent; no strategy contains
exchange-specific submission logic. The intent carries the full decision
context — pricing, edge, exposures before/after, freshness, and hash
provenance — so a pre-trade gate can revalidate the world at execution time.

Also defines the intent state machine used for idempotent execution tracking.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
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
# States that move an intent toward submission/execution.
FORWARD_STATES = {
    IntentState.VALIDATING,
    IntentState.APPROVAL_PENDING,
    IntentState.APPROVED,
    IntentState.SUBMISSION_PENDING,
    IntentState.ACKNOWLEDGED,
    IntentState.PARTIALLY_FILLED,
    IntentState.FILLED,
}


def assert_transition_allowed(current: IntentState, target: IntentState) -> None:
    """Guard the permanent rule: once RECONCILIATION_REQUIRED, an intent must
    NOT move back toward submission through normal workflow. It requires a
    definitive reconciliation outcome first; if state cannot be established it
    remains blocked.

    Raises ValueError on an illegal transition.
    """
    if current == IntentState.RECONCILIATION_REQUIRED and target in FORWARD_STATES:
        raise ValueError(
            "RECONCILIATION_REQUIRED cannot progress toward submission without "
            "a definitive reconciliation outcome"
        )


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
    execution_attempt_id must not create a second logical order.

    The ledger is durable: when constructed with a `path`, every registered
    intent identity is persisted to an append-only JSONL file so that a
    duplicate after a process restart is still detected. Partial trailing
    lines from a crash are tolerated (skipped), matching the raw-store policy.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._by_id: dict[str, OrderIntent] = {}
        self._attempts: set[str] = set()
        if self.path is not None:
            self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue  # partial trailing line from a crash
                intent_id = data.get("intent_id")
                attempt_id = data.get("execution_attempt_id")
                if intent_id:
                    self._by_id.setdefault(intent_id, None)
                if attempt_id:
                    self._attempts.add(attempt_id)

    def _append(self, intent: OrderIntent) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "intent_id": intent.intent_id,
                        "execution_attempt_id": intent.execution_attempt_id,
                        "state": intent.state.value,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )

    def register(self, intent: OrderIntent) -> bool:
        """Return True if this intent is new; False if it's a duplicate.

        A duplicate is any intent_id already seen OR any execution_attempt_id
        already seen. After a restart, identities loaded from the ledger are
        still treated as known, so a retry cannot create a second order.
        """
        if intent.intent_id in self._by_id:
            return False
        if intent.execution_attempt_id in self._attempts:
            return False
        self._by_id[intent.intent_id] = intent
        self._attempts.add(intent.execution_attempt_id)
        self._append(intent)
        return True

    def get(self, intent_id: str) -> OrderIntent | None:
        return self._by_id.get(intent_id)

    def has_attempt(self, execution_attempt_id: str) -> bool:
        return execution_attempt_id in self._attempts

    def size(self) -> int:
        return len(self._by_id)


# ── Approval (state-bound, single-use) ─────────────────────────────────────


@dataclass(frozen=True)
class Approval:
    """Authorizes exactly one bounded action, never "turn live mode on".

    An approval is valid only for the exact:
      intent_id, execution_attempt_id, book_hash, research_logic_sha256,
      config_sha256, max_price, max_notional, and expires_at.

    So an approval for `BUY 100 shares @ <= 0.47` cannot later authorize
    `BUY 150 shares @ 0.51` — the market moved, and the bound holds.
    """

    approval_id: str
    intent_id: str
    execution_attempt_id: str
    book_hash: str
    research_logic_sha256: str
    config_sha256: str
    max_price: Decimal
    max_notional: Decimal
    expires_at: datetime
    issued_at: datetime
    used: bool = False

    def __post_init__(self) -> None:
        for ts in (self.expires_at, self.issued_at):
            if ts.tzinfo is None or ts.utcoffset() is None:
                raise ValueError("approval timestamps must be timezone-aware")
        for v in (self.max_price, self.max_notional):
            if not v.is_finite() or v < 0:
                raise ValueError("approval bounds must be finite and non-negative")

    def validate(self, intent: OrderIntent, now: datetime) -> tuple[bool, str]:
        """Check whether this approval authorizes the given intent right now."""
        if self.used:
            return False, "approval already used"
        if now > self.expires_at:
            return False, "approval expired"
        if self.intent_id != intent.intent_id:
            return False, "approval bound to a different intent"
        if self.execution_attempt_id != intent.execution_attempt_id:
            return False, "approval bound to a different execution attempt"
        if self.book_hash and intent.signal_book_hash != self.book_hash:
            return False, "approval book hash mismatch"
        if self.research_logic_sha256 != intent.research_logic_sha256:
            return False, "approval research hash mismatch"
        if self.config_sha256 != intent.config_sha256:
            return False, "approval config hash mismatch"
        if intent.requested_notional > self.max_notional:
            return False, f"notional {intent.requested_notional} exceeds approval {self.max_notional}"
        if intent.side == "BUY" and intent.limit_price > self.max_price:
            return False, f"price {intent.limit_price} exceeds approval max {self.max_price}"
        if intent.side == "SELL" and intent.limit_price < self.max_price:
            return False, f"price {intent.limit_price} below approval floor {self.max_price}"
        return True, "ok"

    def with_used(self) -> "Approval":
        return Approval(
            approval_id=self.approval_id,
            intent_id=self.intent_id,
            execution_attempt_id=self.execution_attempt_id,
            book_hash=self.book_hash,
            research_logic_sha256=self.research_logic_sha256,
            config_sha256=self.config_sha256,
            max_price=self.max_price,
            max_notional=self.max_notional,
            expires_at=self.expires_at,
            issued_at=self.issued_at,
            used=True,
        )

    def to_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "intent_id": self.intent_id,
            "execution_attempt_id": self.execution_attempt_id,
            "book_hash": self.book_hash,
            "research_logic_sha256": self.research_logic_sha256,
            "config_sha256": self.config_sha256,
            "max_price": str(self.max_price),
            "max_notional": str(self.max_notional),
            "expires_at": self.expires_at.isoformat(),
            "issued_at": self.issued_at.isoformat(),
            "used": self.used,
        }


class ApprovalLedger:
    """Tracks issued/used approvals; an approval is single-use.

    Persisted to a JSONL file so double-use survives a restart and so the
    "approval supplied twice" fault is detectable.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._by_id: dict[str, Approval] = {}
        if self.path is not None:
            self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    self._by_id[data["approval_id"]] = Approval(
                        approval_id=data["approval_id"],
                        intent_id=data["intent_id"],
                        execution_attempt_id=data["execution_attempt_id"],
                        book_hash=data["book_hash"],
                        research_logic_sha256=data["research_logic_sha256"],
                        config_sha256=data["config_sha256"],
                        max_price=D(data["max_price"]),
                        max_notional=D(data["max_notional"]),
                        expires_at=datetime.fromisoformat(data["expires_at"]),
                        issued_at=datetime.fromisoformat(data["issued_at"]),
                        used=bool(data.get("used", False)),
                    )
                except (KeyError, ValueError):
                    continue

    def _append(self, approval: Approval) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(approval.to_dict(), sort_keys=True) + "\n")

    def issue(self, approval: Approval) -> bool:
        """Register an approval. Returns False if the id is already known."""
        if approval.approval_id in self._by_id:
            return False
        self._by_id[approval.approval_id] = approval
        self._append(approval)
        return True

    def get(self, approval_id: str) -> Approval | None:
        return self._by_id.get(approval_id)

    def mark_used(self, approval_id: str) -> Approval | None:
        approval = self._by_id.get(approval_id)
        if approval is None or approval.used:
            return approval
        used = approval.with_used()
        self._by_id[approval_id] = used
        self._append(used)
        return used