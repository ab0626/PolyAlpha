"""ExecutionGateway — mode-agnostic order execution abstraction.

Part of the live-ready execution architecture. The strategy/risk pipeline
produces the same OrderIntent regardless of mode; the gateway decides what
happens to it:

    PaperExecutionGateway  -> simulates depth/fees/partial fills (no venue)
    ShadowExecutionGateway -> records the intent and observed book, never executes
    LiveReadyExecutionGateway -> GATED SCAFFOLD; fails closed, transmits nothing
                                  without an explicit operator-approved adapter

This module deliberately contains NO venue client and NO credentials. The
LiveReady gateway is an architecture boundary: it runs the full pre-trade
gate and idempotency checks, then stops at APPROVAL_PENDING. Actual
transmission requires a venue adapter supplied by an operator, which is out
of scope for this phase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Protocol

from .execution_modes import ExecutionMode
from .order_intent import IntentLedger, IntentState, OrderIntent
from .pre_trade_gate import LivePreTradeGate, PreTradeResult

D = Decimal
FEE_QUANT = D("0.00001")


def taker_fee(shares: Decimal, rate: Decimal, price: Decimal) -> Decimal:
    """Platform fee: shares * rate * price * (1-price), 5dp, per level."""
    return (shares * rate * price * (1 - price)).quantize(FEE_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ExecutionResult:
    intent_id: str
    execution_attempt_id: str
    mode: ExecutionMode
    state: IntentState
    submitted: bool
    filled_shares: Decimal = D(0)
    vwap: Decimal | None = None
    fees: Decimal = D(0)
    slippage: Decimal = D(0)
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "execution_attempt_id": self.execution_attempt_id,
            "mode": self.mode.value,
            "state": self.state.value,
            "submitted": self.submitted,
            "filled_shares": str(self.filled_shares),
            "vwap": str(self.vwap) if self.vwap is not None else None,
            "fees": str(self.fees),
            "slippage": str(self.slippage),
            "notes": self.notes,
        }


class DepthWalkSimulator:
    """Conservative depth-walk fill model: no replenishment is inferred."""

    def __init__(self, fee_rate: Decimal = D("0"), allow_partial: bool = True):
        self.fee_rate = fee_rate
        self.allow_partial = allow_partial

    def fill(self, intent: OrderIntent, book: dict) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Return (filled_shares, vwap, fees, slippage)."""
        levels = book.get("asks" if intent.side == "BUY" else "bids", [])
        remaining = intent.requested_shares
        legs: list[tuple[Decimal, Decimal]] = []
        for level in levels:
            price = D(str(level["price"]))
            if intent.side == "BUY" and price > intent.limit_price:
                break
            if intent.side == "SELL" and price < intent.limit_price:
                break
            available = D(str(level["size"]))
            qty = min(remaining, available)
            if qty > 0:
                legs.append((price, qty))
                remaining -= qty
            if remaining <= 0:
                break
        if remaining > 0 and not self.allow_partial:
            return D(0), None, D(0), D(0)
        filled = intent.requested_shares - remaining
        if filled == 0:
            return D(0), None, D(0), D(0)
        notional = sum((p * q for p, q in legs), D(0))
        vwap = notional / filled
        fees = sum((taker_fee(q, self.fee_rate, p) for p, q in legs), D(0))
        best = D(str(levels[0]["price"])) if levels else vwap
        slippage = abs(vwap - best)
        return filled, vwap, fees, slippage


class ExecutionGateway(Protocol):
    mode: ExecutionMode

    def submit(self, intent: OrderIntent, execution_book: dict | None = None) -> ExecutionResult: ...


class PaperExecutionGateway:
    """Simulates execution against a provided book. Never touches a venue."""

    mode = ExecutionMode.PAPER

    def __init__(self, fee_rate: Decimal = D("0")):
        self.simulator = DepthWalkSimulator(fee_rate=fee_rate)

    def submit(self, intent: OrderIntent, execution_book: dict | None = None) -> ExecutionResult:
        if execution_book is None:
            return ExecutionResult(
                intent.intent_id, intent.execution_attempt_id, self.mode,
                IntentState.FAILED, submitted=False, notes="no execution book",
            )
        filled, vwap, fees, slippage = self.simulator.fill(intent, execution_book)
        if filled == 0:
            return ExecutionResult(
                intent.intent_id, intent.execution_attempt_id, self.mode,
                IntentState.REJECTED, submitted=False, notes="no fill",
            )
        state = IntentState.FILLED if filled == intent.requested_shares else IntentState.PARTIALLY_FILLED
        return ExecutionResult(
            intent.intent_id, intent.execution_attempt_id, self.mode, state,
            submitted=False, filled_shares=filled, vwap=vwap, fees=fees, slippage=slippage,
            notes="paper simulation",
        )


class ShadowExecutionGateway:
    """Records intended orders and observed books; never executes.

    Shadow mode answers: "What exactly would the live system have attempted?"
    It is distinct from historical backtesting.
    """

    mode = ExecutionMode.SHADOW

    def __init__(self, log_path: str | Path | None = None):
        self.log_path = Path(log_path) if log_path else None
        self.records: list[dict] = []

    def submit(self, intent: OrderIntent, execution_book: dict | None = None) -> ExecutionResult:
        record = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "intent": intent.to_dict(),
            "observed_book": execution_book or {},
        }
        self.records.append(record)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
        return ExecutionResult(
            intent.intent_id, intent.execution_attempt_id, self.mode,
            IntentState.APPROVAL_PENDING, submitted=False,
            notes="shadow: intent recorded, not executed",
        )


class OperatorApprovalRequired(RuntimeError):
    """Raised when a live-ready intent has no operator approval."""


class LiveReadyExecutionGateway:
    """GATED SCAFFOLD — architecture boundary, transmits nothing here.

    Flow: kill switch clear -> pre-trade gate -> idempotency -> APPROVAL_PENDING.
    Actual submission requires an operator-supplied `venue_adapter` AND an
    approval token. Without them, this gateway never transmits; it returns an
    APPROVAL_PENDING result (or raises if an adapter is missing).

    There is intentionally no venue client or credential handling in this
    module. Wiring a real adapter is a separate, operator-approved step.
    """

    mode = ExecutionMode.LIVE_READY

    def __init__(
        self,
        gate: LivePreTradeGate,
        kill_switch,
        ledger: IntentLedger | None = None,
        venue_adapter=None,
    ):
        self.gate = gate
        self.kill_switch = kill_switch
        self.ledger = ledger or IntentLedger()
        self.venue_adapter = venue_adapter

    def submit(
        self,
        intent: OrderIntent,
        execution_book: dict | None = None,
        world: dict | None = None,
        operator_approval: str | None = None,
    ) -> ExecutionResult:
        # 1. Kill switch must be clear.
        if self.kill_switch.active:
            return ExecutionResult(
                intent.intent_id, intent.execution_attempt_id, self.mode,
                IntentState.REJECTED, submitted=False,
                notes=f"kill switch active: {self.kill_switch.state().reason}",
            )
        # 2. Idempotency.
        if not self.ledger.register(intent):
            return ExecutionResult(
                intent.intent_id, intent.execution_attempt_id, self.mode,
                IntentState.RECONCILIATION_REQUIRED, submitted=False,
                notes="duplicate intent/attempt (idempotency guard)",
            )
        # 3. Pre-trade gate revalidates the world.
        world = world or {}
        result: PreTradeResult = self.gate.evaluate(intent, world, execution_book)
        if not result.approved:
            failed = [c.name for c in result.checks if not c.passed]
            return ExecutionResult(
                intent.intent_id, intent.execution_attempt_id, self.mode,
                IntentState.REJECTED, submitted=False,
                notes=f"pre-trade gate rejected: {failed}",
            )
        # 4. Human approval boundary. Without it, stop at APPROVAL_PENDING.
        if operator_approval is None:
            return ExecutionResult(
                intent.intent_id, intent.execution_attempt_id, self.mode,
                IntentState.APPROVAL_PENDING, submitted=False,
                notes="awaiting explicit operator approval",
            )
        # 5. Transmission requires a real venue adapter. This scaffold has none.
        if self.venue_adapter is None:
            return ExecutionResult(
                intent.intent_id, intent.execution_attempt_id, self.mode,
                IntentState.APPROVAL_PENDING, submitted=False,
                notes="no venue adapter configured; live transmission not implemented",
            )
        # A real adapter would be invoked here by an operator-approved deployment.
        return ExecutionResult(
            intent.intent_id, intent.execution_attempt_id, self.mode,
            IntentState.SUBMISSION_PENDING, submitted=False,
            notes="operator-approved adapter path (not implemented in this phase)",
        )