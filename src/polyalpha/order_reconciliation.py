"""Order-state reconciliation — never assume submission success.

Part of the live-ready execution architecture. After a submission whose
outcome is unknown (connection failed, ambiguous ack), the system must NOT
blindly retry. It transitions to RECONCILIATION_REQUIRED and reconciles the
intended order against acknowledged/open/filled/cancelled/position state.

Distinguishes:
    definitely rejected
from:
    submission outcome unknown
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .order_intent import IntentState


class SubmissionOutcome(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # connection failed before outcome was learned


@dataclass(frozen=True)
class VenueView:
    """What the venue/account reports right now (authoritative)."""

    open_orders: dict[str, dict] = field(default_factory=dict)  # order_id -> details
    fills: dict[str, list[dict]] = field(default_factory=dict)  # order_id -> fills
    cancellations: set[str] = field(default_factory=set)
    positions: dict[str, dict] = field(default_factory=dict)  # token -> position
    balances: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationOutcome:
    intent_id: str
    execution_attempt_id: str
    submission_outcome: SubmissionOutcome
    next_state: IntentState
    order_exists_at_venue: bool
    filled_quantity: int = 0
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "execution_attempt_id": self.execution_attempt_id,
            "submission_outcome": self.submission_outcome.value,
            "next_state": self.next_state.value,
            "order_exists_at_venue": self.order_exists_at_venue,
            "filled_quantity": self.filled_quantity,
            "notes": self.notes,
        }


class OrderStateReconciler:
    """Reconciles intended orders against the authoritative venue view."""

    def __init__(self) -> None:
        self.history: list[ReconciliationOutcome] = []

    def reconcile(
        self,
        intent_id: str,
        execution_attempt_id: str,
        venue: VenueView,
        submission_outcome: SubmissionOutcome | None = None,
    ) -> ReconciliationOutcome:
        """Classify the true state of an attempt given the venue view.

        Rules:
          * order present in open_orders -> ACKNOWLEDGED (not yet filled)
          * order present in fills -> FILLED/PARTIALLY_FILLED
          * order in cancellations -> CANCELLED
          * submission_outcome == REJECTED and not present -> REJECTED
          * submission_outcome == UNKNOWN and not present -> RECONCILIATION_REQUIRED
        """
        present = intent_id in venue.open_orders
        cancelled = intent_id in venue.cancellations
        filled = venue.fills.get(intent_id)
        filled_qty = sum(f.get("shares", 0) for f in filled) if filled else 0

        if present and not cancelled:
            state = IntentState.ACKNOWLEDGED
            outcome = SubmissionOutcome.ACKNOWLEDGED
        elif filled:
            state = IntentState.FILLED if filled_qty > 0 else IntentState.ACKNOWLEDGED
            outcome = SubmissionOutcome.ACKNOWLEDGED
        elif cancelled:
            state = IntentState.CANCELLED
            outcome = SubmissionOutcome.REJECTED
        elif submission_outcome == SubmissionOutcome.REJECTED:
            state = IntentState.REJECTED
            outcome = SubmissionOutcome.REJECTED
        elif submission_outcome == SubmissionOutcome.UNKNOWN:
            state = IntentState.RECONCILIATION_REQUIRED
            outcome = SubmissionOutcome.UNKNOWN
        else:
            # No venue evidence and no explicit outcome: treat as unknown.
            state = IntentState.RECONCILIATION_REQUIRED
            outcome = SubmissionOutcome.UNKNOWN

        rec = ReconciliationOutcome(
            intent_id=intent_id,
            execution_attempt_id=execution_attempt_id,
            submission_outcome=outcome,
            next_state=state,
            order_exists_at_venue=present,
            filled_quantity=filled_qty,
            notes="reconciled against venue view",
        )
        self.history.append(rec)
        return rec

    def summary(self) -> dict:
        by_state: dict[str, int] = {}
        for r in self.history:
            by_state[r.next_state.value] = by_state.get(r.next_state.value, 0) + 1
        return {"total": len(self.history), "by_state": by_state}