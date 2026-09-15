"""Cancel-on-stale policy for resting orders.

Part of the live-ready execution architecture. A resting order must never
remain logically valid forever. This module evaluates invalidation triggers
and recommends cancel / hold / escalate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

D = Decimal


class CancelAction(str, Enum):
    HOLD = "HOLD"
    CANCEL = "CANCEL"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class RestingOrder:
    order_id: str
    token_id: str
    side: str
    limit_price: Decimal
    shares: Decimal
    model_probability_at_placement: Decimal
    book_hash_at_placement: str
    placed_at_seconds_ago: float


@dataclass(frozen=True)
class CancelDecision:
    order_id: str
    action: CancelAction
    triggers: list[str]

    def as_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "action": self.action.value,
            "triggers": self.triggers,
        }


@dataclass
class CancelPolicy:
    max_age_seconds: float = 3600.0
    model_probability_tolerance: Decimal = D("0.05")
    price_tolerance: Decimal = D("0.03")

    def evaluate(
        self,
        order: RestingOrder,
        current_model_probability: Decimal | None,
        current_book_hash: str | None,
        resolution_changed: bool = False,
        external_shock: bool = False,
        feed_stale: bool = False,
        ws_disconnected: bool = False,
        reconciliation_failed: bool = False,
        risk_cap_reached: bool = False,
        portfolio_uncertain: bool = False,
        kill_switch_active: bool = False,
    ) -> CancelDecision:
        triggers: list[str] = []
        if current_model_probability is not None and abs(
            current_model_probability - order.model_probability_at_placement
        ) > self.model_probability_tolerance:
            triggers.append("model_probability_moved")
        if current_book_hash is not None and current_book_hash != order.book_hash_at_placement:
            triggers.append("book_hash_changed")
        if resolution_changed:
            triggers.append("resolution_metadata_changed")
        if external_shock:
            triggers.append("external_information_shock")
        if feed_stale:
            triggers.append("stale_market_feed")
        if ws_disconnected:
            triggers.append("ws_disconnect")
        if reconciliation_failed:
            triggers.append("reconciliation_failure")
        if risk_cap_reached:
            triggers.append("risk_cap_reached_elsewhere")
        if portfolio_uncertain:
            triggers.append("portfolio_state_uncertain")
        if kill_switch_active:
            triggers.append("kill_switch_active")
        if order.placed_at_seconds_ago > self.max_age_seconds:
            triggers.append("order_age_exceeded")

        if not triggers:
            return CancelDecision(order.order_id, CancelAction.HOLD, [])
        # Escalate (do not auto-cancel) when the portfolio/account state is
        # uncertain — cancelling blindly could also be wrong.
        if portfolio_uncertain or reconciliation_failed or kill_switch_active:
            return CancelDecision(order.order_id, CancelAction.ESCALATE, triggers)
        return CancelDecision(order.order_id, CancelAction.CANCEL, triggers)