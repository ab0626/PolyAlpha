"""Production execution control plane — LIVE_CALIBRATION_V0 (resting-fill lifecycle).

A STANDALONE harness (no longer a subclass of the frozen demo harness) so that
production recovery semantics do not inherit the demo "recovered WORKING =>
cancel" behavior. Production orders are intended to REST and fill; recovery must
distinguish an intended resting order from execution ambiguity.

Durability: a write-ahead ``ProdExecutionJournal`` persists the FULL order intent
(calibration version, ticker, side, limit price, quantity, signal timestamp,
screen edge, cancel/expiry deadline) BEFORE each venue side effect, plus
observed filled/remaining counts. On restart the harness reconciles the
``client_order_id`` and branches DETERMINISTICALLY:

    filled (or remaining == 0)          -> terminal
    canceled                            -> terminal
    resting + within lifetime           -> keep resting (do NOT cancel)
    resting + stale (past expiry)       -> cancel, then reconcile
    executing/pending/unknown           -> reconcile until settled; unresolved => HALT
    venue unreachable / truly unresolved -> arm kill switch + HALT

The frozen LIVE_CALIBRATION_V0 gates (version / operator-arming / kill switch /
daily-loss stop / bankroll cap) remain enforced on every intent. There is still
no automatic signal: ``signal_fn`` is None by default, so ``run_cycle`` is a
precondition-verifying no-op until the E3 signal layer is wired in a later pass.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .execution_journal_prod import ProdExecutionJournal, ProdJournalRecord
from .kalshi_exec import (
    SELF_TRADE_TAKER_AT_CROSS,
    ExecutionBlocked,
    KalshiConflict,
    NewOrder,
    OrderOutcomeUnknown,
    OrderRejected,
    OrderSide,
    ReconcileResult,
    TimeInForce,
)
from .kalshi_exec_prod import (
    BANKROLL_CAP,
    CALIBRATION_PROTOCOL_VERSION,
    DAILY_LOSS_STOP,
)
from .kalshi_oms import NONTERMINAL_STATES, HarnessState

_fmt = lambda d: format(d, "f")  # noqa: E731

# Venue order statuses that mean "still resting, may fill".
RESTING_STATUSES = {"resting", "working", "open", "unmatched", "active"}

SignalFn = Callable[..., "ProdOrderIntent | None"]


@dataclass(frozen=True)
class ProdOrderIntent:
    """A fully-specified production order intent (signal-derived, pre-gated)."""

    ticker: str
    side: OrderSide
    price: Decimal
    count: Decimal
    client_order_id: str
    calibration_version: str = CALIBRATION_PROTOCOL_VERSION
    signal_timestamp: str | None = None
    screen_edge: Decimal | None = None
    expiry_deadline: str | None = None
    time_in_force: TimeInForce = TimeInForce.GOOD_TILL_CANCELED
    post_only: bool = False

    def to_new_order(self) -> NewOrder:
        return NewOrder(
            ticker=self.ticker,
            side=self.side,
            count=self.count,
            price=self.price,
            client_order_id=self.client_order_id,
            time_in_force=self.time_in_force,
            self_trade_prevention_type=SELF_TRADE_TAKER_AT_CROSS,
            post_only=self.post_only,
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class ExperimentRiskLedger:
    """File-backed cumulative experiment risk (daily loss + committed notional).

    Persists the two frozen risk gates so they are HARD across one-intent process
    invocations/restarts, rather than resetting to zero on every new harness. An
    unreadable ledger fails closed: it reads as an infinite loss and infinite
    committed notional, which blocks every intent until an operator repairs it.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> tuple[str | None, Decimal, Decimal]:
        """Return (daily_loss_day, daily_loss, committed_notional)."""
        if not self.path.exists():
            return None, Decimal("0"), Decimal("0")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            day = data.get("daily_loss_day")
            loss = Decimal(str(data.get("daily_loss", "0")))
            committed = Decimal(str(data.get("committed_notional", "0")))
        except (json.JSONDecodeError, OSError, TypeError, ValueError, InvalidOperation):
            # Cannot establish risk state: fail closed (infinite loss blocks).
            return None, Decimal("inf"), Decimal("inf")
        return day, loss, committed

    def save(self, daily_loss_day: str | None, daily_loss: Decimal, committed_notional: Decimal) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "daily_loss_day": daily_loss_day,
                    "daily_loss": _fmt(daily_loss),
                    "committed_notional": _fmt(committed_notional),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)


class ProdExecutionHarness:
    """Production resting-fill state machine with deterministic recovery."""

    def __init__(
        self,
        client,
        ticker: str,
        *,
        frozen_version: str | None = None,
        daily_loss_stop: Decimal | str = DAILY_LOSS_STOP,
        bankroll_cap: Decimal | str = BANKROLL_CAP,
        signal_fn: SignalFn | None = None,
        reconcile_timeout: float = 30.0,
        reconcile_poll: float = 1.0,
        audit: Any = None,
        journal_path: str | None = None,
        run_id: str | None = None,
        risk_path: str | Path | None = None,
    ) -> None:
        self.client = client
        self.ticker = ticker
        self.frozen_version = frozen_version
        self.daily_loss_stop = Decimal(daily_loss_stop)
        self.bankroll_cap = Decimal(bankroll_cap)
        self.signal_fn = signal_fn
        self.reconcile_timeout = reconcile_timeout
        self.reconcile_poll = reconcile_poll
        self.audit = audit or (lambda event, fields: None)
        self.journal = ProdExecutionJournal(journal_path) if journal_path else None
        self.run_id = run_id or uuid4().hex
        self.sequence = 0
        self.state = HarnessState.IDLE
        self.halt_reason: str | None = None
        self.cid: str | None = None
        self.order_id: str | None = None
        self.intent: ProdOrderIntent | None = None
        self._recovery_pending = False
        self.risk_ledger = ExperimentRiskLedger(risk_path) if risk_path else None
        if self.risk_ledger is not None:
            self._daily_loss_day, self._daily_loss, self._committed_notional = self.risk_ledger.load()
        else:
            self._daily_loss = Decimal("0")
            self._daily_loss_day: str | None = None
            self._committed_notional = Decimal("0")
        self._load_nonterminal_episode()

    def _persist_risk(self) -> None:
        """Persist the cumulative risk state (no-op when no ledger is configured)."""
        if self.risk_ledger is not None:
            self.risk_ledger.save(self._daily_loss_day, self._daily_loss, self._committed_notional)

    # ── Persistence ───────────────────────────────────────────────────────

    def _load_nonterminal_episode(self) -> None:
        if self.journal is None:
            return
        latest = self.journal.latest()
        if latest is None or latest.state not in {s.value for s in NONTERMINAL_STATES}:
            return
        try:
            self.state = HarnessState(latest.state)
        except ValueError:
            self.state = HarnessState.HALTED
            self.halt_reason = f"unrecognized journal state: {latest.state!r}"
            self._recovery_pending = True
            return
        self.cid = latest.client_order_id
        self.order_id = latest.order_id
        self.halt_reason = latest.halt_reason
        if latest.ticker:
            self.ticker = latest.ticker
        if latest.side and latest.price and latest.count:
            self.intent = ProdOrderIntent(
                ticker=latest.ticker,
                side=OrderSide(latest.side),
                price=Decimal(latest.price),
                count=Decimal(latest.count),
                client_order_id=latest.client_order_id or "",
                calibration_version=latest.calibration_version or CALIBRATION_PROTOCOL_VERSION,
                signal_timestamp=latest.signal_timestamp,
                screen_edge=Decimal(latest.screen_edge) if latest.screen_edge else None,
                expiry_deadline=latest.expiry_deadline,
            )
        self._recovery_pending = True

    def _transition(
        self,
        state: HarnessState,
        *,
        client_order_id: str | None = None,
        order_id: str | None = None,
        halt_reason: str | None = None,
        intent: ProdOrderIntent | None = None,
        filled_count: Decimal | None = None,
        remaining_count: Decimal | None = None,
    ) -> None:
        self.state = state
        if client_order_id is not None:
            self.cid = client_order_id
        if order_id is not None:
            self.order_id = order_id
        if halt_reason is not None:
            self.halt_reason = halt_reason
        if intent is not None:
            self.intent = intent
        self.sequence += 1
        if self.journal is not None:
            self.journal.append(
                ProdJournalRecord(
                    run_id=self.run_id,
                    sequence=self.sequence,
                    state=state.value,
                    ticker=self.intent.ticker if self.intent else self.ticker,
                    client_order_id=self.cid,
                    order_id=self.order_id,
                    timestamp=_now(),
                    halt_reason=self.halt_reason,
                    calibration_version=self.intent.calibration_version if self.intent else None,
                    side=self.intent.side.value if self.intent else None,
                    price=_fmt(self.intent.price) if self.intent else None,
                    count=_fmt(self.intent.count) if self.intent else None,
                    signal_timestamp=self.intent.signal_timestamp if self.intent else None,
                    screen_edge=_fmt(self.intent.screen_edge)
                    if self.intent and self.intent.screen_edge is not None
                    else None,
                    expiry_deadline=self.intent.expiry_deadline if self.intent else None,
                    filled_count=_fmt(filled_count) if filled_count is not None else None,
                    remaining_count=_fmt(remaining_count) if remaining_count is not None else None,
                )
            )
        self.audit(state.value, {"client_order_id": self.cid, "order_id": self.order_id})

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

    def _precondition_failure(self) -> str | None:
        if self.frozen_version != CALIBRATION_PROTOCOL_VERSION:
            return "calibration protocol version unverified/mismatched"
        kill_active, kill_reason = self._kill_active()
        if kill_active:
            return f"kill switch active: {kill_reason}"
        armed, arm_reason = self._armed()
        if not armed:
            return arm_reason or "operator arming not set"
        if self._daily_loss >= self.daily_loss_stop:
            return "daily loss stop breached"
        return None

    def record_loss(self, amount: Decimal | str) -> Decimal:
        today = datetime.now(UTC).date().isoformat()
        if self._daily_loss_day != today:
            self._daily_loss_day = today
            self._daily_loss = Decimal("0")
        self._daily_loss += Decimal(amount)
        self._persist_risk()
        if self._daily_loss >= self.daily_loss_stop:
            self._transition(HarnessState.HALTED, halt_reason="DAILY_LOSS_BREACH")
            try:
                self.client.disable("DAILY_LOSS_BREACH", actor="oms-harness")
            except Exception:  # noqa: BLE001
                pass
        return self._daily_loss

    # ── Reconciliation helpers ───────────────────────────────────────────

    def _reconcile_until_found(self, cid: str) -> ReconcileResult | None:
        deadline = time.monotonic() + self.reconcile_timeout
        while True:
            try:
                rec = self.client.reconcile(cid)
            except OrderOutcomeUnknown:
                rec = None
            if rec is not None and rec.found:
                return rec
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.reconcile_poll)

    def _reconcile_for_recovery(self, cid: str) -> tuple[str, ReconcileResult | None]:
        deadline = time.monotonic() + self.reconcile_timeout
        while True:
            try:
                rec = self.client.reconcile(cid)
            except OrderOutcomeUnknown:
                if time.monotonic() >= deadline:
                    return "ambiguous", None
                time.sleep(self.reconcile_poll)
                continue
            if rec is not None and rec.found:
                return "found", rec
            if time.monotonic() >= deadline:
                return "absent", None
            time.sleep(self.reconcile_poll)

    def _reconcile_until_settled(self, cid: str) -> tuple[str, ReconcileResult | None]:
        deadline = time.monotonic() + self.reconcile_timeout
        while True:
            try:
                rec = self.client.reconcile(cid)
            except OrderOutcomeUnknown:
                rec = None
            if rec is not None and rec.found:
                status = rec.status
                remaining = rec.remaining_count
                if remaining is not None and remaining == 0:
                    return "terminal", rec
                if status == "canceled":
                    return "terminal", rec
                if status in RESTING_STATUSES and remaining is not None and remaining > 0:
                    return "resting", rec
            if time.monotonic() >= deadline:
                return "ambiguous", None
            time.sleep(self.reconcile_poll)

    def _resolve_cancel(self, cid: str, order_id: str) -> bool:
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
                try:
                    self.client.cancel_order(order_id, market_ticker=self.ticker)
                except (OrderRejected, OrderOutcomeUnknown):
                    pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.reconcile_poll)

    def _halt(self, cid: str | None, reason: str) -> HarnessState:
        self._transition(HarnessState.HALTED, client_order_id=cid, halt_reason=reason)
        try:
            self.client.disable("ABNORMAL_RECONCILIATION", actor="oms-harness")
        except Exception:  # noqa: BLE001
            pass
        return HarnessState.HALTED

    def _expired(self) -> bool:
        if self.intent is None or not self.intent.expiry_deadline:
            return False
        dl = _parse_iso(self.intent.expiry_deadline)
        return dl is not None and datetime.now(UTC) >= dl

    # ── Recovery ─────────────────────────────────────────────────────────

    def recover(self) -> HarnessState:
        """Deterministic recovery: never cancel an intended resting order."""
        if not self._recovery_pending:
            return self.state
        cid = self.cid
        if not cid:
            return self._halt(cid, "nonterminal journal with no client_order_id")
        self.audit("recovery_start", {"client_order_id": cid, "state": self.state.value})

        kind, rec = self._reconcile_for_recovery(cid)
        if kind == "ambiguous":
            return self._halt(cid, "startup recovery unresolved (venue unreachable)")
        if kind == "absent":
            self.audit("recovery_absent", {"client_order_id": cid})
            self._transition(HarnessState.IDLE, client_order_id=cid)
            self._recovery_pending = False
            return self.state

        status = rec.status  # type: ignore[union-attr]
        remaining = rec.remaining_count  # type: ignore[union-attr]
        filled = rec.filled_count  # type: ignore[union-attr]
        order_id = rec.order_id or self.order_id  # type: ignore[union-attr]

        # Terminal: no resting quantity left (filled), or explicitly canceled.
        if (remaining is not None and remaining == 0) or status == "canceled":
            self.audit("recovery_terminal", {"client_order_id": cid, "status": status})
            self._transition(
                HarnessState.IDLE,
                client_order_id=cid,
                order_id=order_id,
                filled_count=filled,
                remaining_count=remaining,
            )
            self._recovery_pending = False
            return self.state

        # Intended resting order: keep it if within its allowed lifetime.
        if status in RESTING_STATUSES and remaining is not None and remaining > 0:
            if self._expired():
                self.audit("recovery_stale", {"client_order_id": cid, "order_id": order_id})
                if not order_id:
                    return self._halt(cid, "stale resting order missing order_id")
                return self._cancel_and_resolve(cid, order_id)
            self.audit(
                "recovery_keep_resting",
                {"client_order_id": cid, "order_id": order_id, "remaining": str(remaining)},
            )
            self._transition(
                HarnessState.WORKING,
                client_order_id=cid,
                order_id=order_id,
                filled_count=filled,
                remaining_count=remaining,
            )
            self._recovery_pending = False
            return self.state

        # Execution ambiguity (executing/pending/unknown status): poll to settled.
        self.audit("recovery_ambiguous_status", {"client_order_id": cid, "status": status})
        outcome, rec2 = self._reconcile_until_settled(cid)
        if outcome == "ambiguous":
            return self._halt(cid, "startup recovery order status unresolved at timeout")
        if outcome == "terminal":
            self._transition(
                HarnessState.IDLE,
                client_order_id=cid,
                order_id=rec2.order_id if rec2 else order_id,
                filled_count=rec2.filled_count if rec2 else filled,
                remaining_count=rec2.remaining_count if rec2 else remaining,
            )
            self._recovery_pending = False
            return self.state
        # outcome == "resting"
        final_order_id = rec2.order_id if rec2 else order_id
        if self._expired():
            if not final_order_id:
                return self._halt(cid, "stale resting order missing order_id")
            return self._cancel_and_resolve(cid, final_order_id)
        self._transition(
            HarnessState.WORKING,
            client_order_id=cid,
            order_id=final_order_id,
            filled_count=rec2.filled_count if rec2 else filled,
            remaining_count=rec2.remaining_count if rec2 else remaining,
        )
        self._recovery_pending = False
        return self.state

    def _cancel_and_resolve(self, cid: str, order_id: str) -> HarnessState:
        self._transition(HarnessState.CANCELING, client_order_id=cid, order_id=order_id)
        try:
            self.client.cancel_order(order_id, market_ticker=self.ticker)
        except (OrderRejected, OrderOutcomeUnknown):
            pass
        if not self._resolve_cancel(cid, order_id):
            return self._halt(cid, "recovery cancel unresolved at timeout")
        self._transition(HarnessState.IDLE, client_order_id=cid, order_id=order_id)
        self._recovery_pending = False
        return self.state

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
            return self.state
        intent = self.signal_fn()
        if intent is None:
            return self.state
        return self.submit_intent(intent)

    def submit_intent(self, intent: ProdOrderIntent) -> HarnessState:
        fail = self._precondition_failure()
        if fail:
            return self._halt(intent.client_order_id, fail)
        if self._committed_notional + intent.count > self.bankroll_cap:
            return self._halt(intent.client_order_id, "bankroll cap exceeded")

        self._transition(
            HarnessState.SUBMITTING, client_order_id=intent.client_order_id, intent=intent
        )
        order_id: str | None = None
        try:
            ack = self.client.create_order(intent.to_new_order())
            order_id = ack.order_id
            self.audit("order_placed", {"client_order_id": intent.client_order_id, "order_id": order_id})
        except (OrderRejected, ExecutionBlocked) as exc:
            self.audit("order_place_rejected", {"error": f"{type(exc).__name__}: {exc}"})
            self._transition(HarnessState.IDLE, client_order_id=intent.client_order_id, intent=intent)
            return self.state
        except (OrderOutcomeUnknown, KalshiConflict) as exc:
            self.audit("order_place_ambiguous", {"error": f"{type(exc).__name__}: {exc}"})
            self._transition(HarnessState.RECONCILING, client_order_id=intent.client_order_id, intent=intent)
            rec = self._reconcile_until_found(intent.client_order_id)
            if rec is None:
                return self._halt(intent.client_order_id, "create outcome unresolved at timeout")
            order_id = rec.order_id
            self.audit("order_place_confirmed", {"client_order_id": intent.client_order_id, "order_id": order_id})

        if not order_id:
            return self._halt(intent.client_order_id, "create ack missing order_id")

        self._committed_notional += intent.count
        self._persist_risk()
        self._transition(
            HarnessState.WORKING,
            client_order_id=intent.client_order_id,
            order_id=order_id,
            intent=intent,
        )
        return self.state