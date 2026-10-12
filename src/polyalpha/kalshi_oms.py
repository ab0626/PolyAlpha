"""Demo automated execution control plane — state machine over the venue adapter.

This is the execution harness (NULL signal), NOT an OMS with pre-trade gating,
sizing, or a strategy signal. It drives ``KalshiExecClient`` through the
place -> rest -> cancel -> reconcile lifecycle with one hard invariant:

    UNKNOWN or unresolved state  ==>  no new intents.

Any ambiguous create/cancel (outcome unknown) enters a bounded
reconcile-with-backoff loop against the SAME ``client_order_id``. If an
authoritative state cannot be reached within the timeout, the harness arms the
kill switch and halts — it never proceeds to the next cycle while a prior
intent is unresolved.

The signal is NULL (a deterministic 1-contract post-only bid that is then
cancelled); it carries no edge and makes no alpha claim.

States: IDLE -> SUBMITTING -> WORKING -> CANCELING -> RECONCILING -> IDLE,
with HALTED as the terminal safety state.
"""

from __future__ import annotations

import time
from decimal import Decimal
from enum import Enum
from typing import Any, Callable

from .kalshi_exec import (
    ExecutionBlocked,
    KalshiConflict,
    NewOrder,
    OrderOutcomeUnknown,
    OrderRejected,
    OrderSide,
    ReconcileResult,
    new_client_order_id,
)


class HarnessState(str, Enum):
    IDLE = "idle"
    SUBMITTING = "submitting"
    WORKING = "working"
    CANCELING = "canceling"
    RECONCILING = "reconciling"
    HALTED = "halted"


AuditFn = Callable[[str, Any], None]  # (event, fields_dict) -> None


class DemoExecutionHarness:
    """State machine that places/cancels one NULL-signal order per cycle.

    ``client`` is any object exposing ``create_order`` / ``cancel_order`` /
    ``reconcile`` / ``disable`` with the same signatures as ``KalshiExecClient``
    (duck-typed so tests can inject a scripted fake).
    """

    def __init__(
        self,
        client,
        ticker: str,
        *,
        reconcile_timeout: float = 30.0,
        reconcile_poll: float = 1.0,
        audit: AuditFn | None = None,
    ) -> None:
        self.client = client
        self.ticker = ticker
        self.reconcile_timeout = reconcile_timeout
        self.reconcile_poll = reconcile_poll
        self.audit = audit or (lambda event, fields: None)
        self.state = HarnessState.IDLE
        self.halt_reason: str | None = None

    def _set(self, state: HarnessState, **fields) -> None:
        self.state = state
        self.audit(state.value, fields)

    def _reconcile_until_found(self, cid: str) -> ReconcileResult | None:
        """Poll reconcile(cid) until an authoritative view (found) or timeout.

        Returns the ReconcileResult once found; None if still absent at timeout
        (ambiguous — the eventually-consistent order list may not have caught up).
        """
        deadline = time.monotonic() + self.reconcile_timeout
        while True:
            try:
                rec = self.client.reconcile(cid)
            except OrderOutcomeUnknown:
                rec = None  # reconcile transport failure is itself unresolved
            if rec is not None and rec.found:
                return rec
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.reconcile_poll)

    def _halt(self, cid: str, reason: str) -> HarnessState:
        self.halt_reason = reason
        self._set(HarnessState.HALTED, client_order_id=cid, reason=reason)
        try:
            self.client.disable("ABNORMAL_RECONCILIATION", actor="oms-harness")
        except Exception:  # noqa: BLE001 — halt must not depend on the kill switch
            pass
        return HarnessState.HALTED

    def run_cycle(self) -> HarnessState:
        """Run one place -> rest -> cancel -> reconcile cycle."""
        if self.state == HarnessState.HALTED:
            return self.state

        # ── SUBMITTING ───────────────────────────────────────────────────
        self._set(HarnessState.SUBMITTING)
        cid = new_client_order_id()
        order = NewOrder(
            ticker=self.ticker,
            side=OrderSide.BID,
            count=Decimal("1"),
            price=Decimal("0.01"),
            client_order_id=cid,
            post_only=True,
        )
        order_id: str | None = None
        try:
            ack = self.client.create_order(order)
            order_id = ack.order_id
            self.audit("order_placed", {"client_order_id": cid, "order_id": order_id})
        except (OrderRejected, ExecutionBlocked) as exc:
            # Definitive: the order was NOT placed -> safe to idle.
            self.audit("order_place_rejected", {"error": f"{type(exc).__name__}: {exc}"})
            self._set(HarnessState.IDLE, client_order_id=cid)
            return self.state
        except (OrderOutcomeUnknown, KalshiConflict) as exc:
            # Ambiguous: the order may already exist (unknown outcome or 409).
            # Reconcile against the SAME client_order_id before anything else.
            self.audit("order_place_ambiguous", {"error": f"{type(exc).__name__}: {exc}"})
            self._set(HarnessState.RECONCILING, client_order_id=cid)
            rec = self._reconcile_until_found(cid)
            if rec is None:
                return self._halt(cid, "create outcome unresolved at timeout")
            order_id = rec.order_id
            self.audit("order_place_confirmed", {"client_order_id": cid, "order_id": order_id})

        if not order_id:
            # A create that neither rejected nor produced an order_id is not
            # safe to leave dangling.
            return self._halt(cid, "create ack missing order_id")

        # ── WORKING -> CANCELING ─────────────────────────────────────────
        self._set(HarnessState.WORKING, client_order_id=cid, order_id=order_id)
        self._set(HarnessState.CANCELING, client_order_id=cid, order_id=order_id)
        try:
            self.client.cancel_order(order_id, market_ticker=self.ticker)
            self.audit("order_canceled", {"client_order_id": cid, "order_id": order_id})
            self._set(HarnessState.IDLE, client_order_id=cid, order_id=order_id)
        except (OrderRejected, OrderOutcomeUnknown):
            # Ambiguous: the cancel may not have taken effect.
            self._set(HarnessState.RECONCILING, client_order_id=cid, order_id=order_id)
            if not self._resolve_cancel(cid, order_id):
                return self._halt(cid, "cancel outcome unresolved at timeout")
            self._set(HarnessState.IDLE, client_order_id=cid, order_id=order_id)

        return self.state

    def _resolve_cancel(self, cid: str, order_id: str) -> bool:
        """After an ambiguous cancel, reach an authoritative canceled state.

        Polls reconcile(cid); while the order is still resting, retries the
        cancel; returns True once the venue reports it canceled (or fully
        reduced), False if still ambiguous at timeout.
        """
        deadline = time.monotonic() + self.reconcile_timeout
        while True:
            try:
                rec = self.client.reconcile(cid)
            except OrderOutcomeUnknown:
                rec = None
            if rec is not None and rec.found:
                if rec.status == "canceled" or (
                    rec.remaining_count is not None and rec.remaining_count == 0
                ):
                    return True
                # Still resting -> retry cancel and keep polling.
                try:
                    self.client.cancel_order(order_id, market_ticker=self.ticker)
                    self.audit("order_cancel_retried", {"client_order_id": cid, "order_id": order_id})
                except (OrderRejected, OrderOutcomeUnknown):
                    pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.reconcile_poll)