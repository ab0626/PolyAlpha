"""Production execution control plane — LIVE_CALIBRATION_V0 scaffold.

Subclasses the frozen demo harness (``DemoExecutionHarness``) so the proven
durability + ambiguity machinery is REUSED, not reimplemented:

    UNKNOWN or unresolved state  ==>  no new intents.

On top of that it layers the LIVE_CALIBRATION_V0 preconditions that keep
production mutation fail-closed:

  - calibration protocol version must match the frozen runtime version,
  - operator arming must be set (strategy may observe, never relax),
  - the production kill switch must be clear,
  - the frozen daily-loss stop must not be breached,
  - the frozen bankroll cap must not be exceeded.

There is NO automatic signal in this pass: ``signal_fn`` is None by default, so
``run_cycle`` is a precondition-verifying no-op that cannot place an order until
the signal layer is explicitly wired in a later pass.

Important scaffold caveat: production orders REST (they are not auto-cancelled
like the demo NULL signal), so a recovered nonterminal WORKING order is, for
now, conservatively cancelled by the inherited recovery path. Full fill/settle
reconciliation is wired in a later pass.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

from .kalshi_exec import (
    ExecutionBlocked,
    KalshiConflict,
    NewOrder,
    OrderOutcomeUnknown,
    OrderRejected,
)
from .kalshi_exec_prod import (
    BANKROLL_CAP,
    CALIBRATION_PROTOCOL_VERSION,
    DAILY_LOSS_STOP,
)
from .kalshi_oms import DemoExecutionHarness, HarnessState

SignalFn = Callable[[], NewOrder | None]


class ProdExecutionHarness(DemoExecutionHarness):
    """Production harness: demo state machine + LIVE_CALIBRATION_V0 gates."""

    def __init__(
        self,
        client,
        ticker: str,
        *,
        frozen_version: str | None = None,
        daily_loss_stop: Decimal | str = DAILY_LOSS_STOP,
        bankroll_cap: Decimal | str = BANKROLL_CAP,
        signal_fn: SignalFn | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(client, ticker, **kwargs)
        self.frozen_version = frozen_version
        self.daily_loss_stop = Decimal(daily_loss_stop)
        self.bankroll_cap = Decimal(bankroll_cap)
        self.signal_fn = signal_fn
        self._daily_loss = Decimal("0")
        self._daily_loss_day: str | None = None
        self._committed_notional = Decimal("0")

    # ── LIVE_CALIBRATION_V0 preconditions ─────────────────────────────────

    def _kill_active(self) -> tuple[bool, str | None]:
        state = getattr(self.client, "kill_switch_state", None)
        if state is None:
            return True, "client missing kill_switch_state (fail-closed)"
        return bool(state.active), state.reason

    def _armed(self) -> tuple[bool, str | None]:
        state = getattr(self.client, "armed_state", None)
        if state is None:
            return False, "client missing armed_state (fail-closed)"
        return bool(state.armed), None

    def _daily_stop_breached(self) -> bool:
        return self._daily_loss >= self.daily_loss_stop

    def _precondition_failure(self) -> str | None:
        if self.frozen_version != CALIBRATION_PROTOCOL_VERSION:
            return "calibration protocol version unverified/mismatched"
        kill_active, kill_reason = self._kill_active()
        if kill_active:
            return f"kill switch active: {kill_reason}"
        armed, arm_reason = self._armed()
        if not armed:
            return arm_reason or "operator arming not set"
        if self._daily_stop_breached():
            return "daily loss stop breached"
        return None

    def record_loss(self, amount: Decimal | str) -> Decimal:
        """Accumulate realized+marked loss; halt + arm kill switch on breach."""
        today = datetime.now(UTC).date().isoformat()
        if self._daily_loss_day != today:
            self._daily_loss_day = today
            self._daily_loss = Decimal("0")
        self._daily_loss += Decimal(amount)
        if self._daily_stop_breached():
            self._transition(HarnessState.HALTED, halt_reason="DAILY_LOSS_BREACH")
            try:
                self.client.disable("DAILY_LOSS_BREACH", actor="oms-harness")
            except Exception:  # noqa: BLE001 — halt must not depend on kill switch
                pass
        return self._daily_loss

    # ── Cycle ────────────────────────────────────────────────────────────

    def run_cycle(self) -> HarnessState:
        if self.state == HarnessState.HALTED:
            return self.state
        if self._recovery_pending:
            self.recover()
            if self.state == HarnessState.HALTED or self._recovery_pending:
                return self.state
        fail = self._precondition_failure()
        if fail:
            return self._halt(None, fail)
        if self.signal_fn is None:
            # Scaffold: no signal wired -> no mutation, just precondition checks.
            return self.state
        order = self.signal_fn()
        if order is None:
            return self.state
        return self.submit_intent(order)

    def submit_intent(self, order: NewOrder) -> HarnessState:
        """Submit one signal order under the full ambiguity invariant.

        UNKNOWN/unresolved create state halts and never mints a fresh CID for the
        same intent; a definitive reject idles safely.
        """
        fail = self._precondition_failure()
        if fail:
            return self._halt(order.client_order_id, fail)
        if self._committed_notional + order.count > self.bankroll_cap:
            return self._halt(order.client_order_id, "bankroll cap exceeded")

        self._transition(
            HarnessState.SUBMITTING, client_order_id=order.client_order_id
        )
        order_id: str | None = None
        try:
            ack = self.client.create_order(order)
            order_id = ack.order_id
            self.audit("order_placed", {"client_order_id": order.client_order_id, "order_id": order_id})
        except (OrderRejected, ExecutionBlocked) as exc:
            self.audit("order_place_rejected", {"error": f"{type(exc).__name__}: {exc}"})
            self._transition(HarnessState.IDLE, client_order_id=order.client_order_id)
            return self.state
        except (OrderOutcomeUnknown, KalshiConflict) as exc:
            self.audit("order_place_ambiguous", {"error": f"{type(exc).__name__}: {exc}"})
            self._transition(HarnessState.RECONCILING, client_order_id=order.client_order_id)
            rec = self._reconcile_until_found(order.client_order_id)
            if rec is None:
                return self._halt(order.client_order_id, "create outcome unresolved at timeout")
            order_id = rec.order_id
            self.audit("order_place_confirmed", {"client_order_id": order.client_order_id, "order_id": order_id})

        if not order_id:
            return self._halt(order.client_order_id, "create ack missing order_id")

        self._committed_notional += order.count
        self._transition(
            HarnessState.WORKING, client_order_id=order.client_order_id, order_id=order_id
        )
        return self.state