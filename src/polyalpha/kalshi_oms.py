"""Demo automated execution control plane — durable state machine over the venue adapter.

This is the execution harness (NULL signal), NOT an OMS with pre-trade gating,
sizing, or a strategy signal. It drives ``KalshiExecClient`` through the
place -> rest -> cancel -> reconcile lifecycle with one hard invariant:

    UNKNOWN or unresolved state  ==>  no new intents.

Any ambiguous create/cancel (outcome unknown) enters a bounded
reconcile-with-backoff loop against the SAME ``client_order_id``. If an
authoritative state cannot be reached within the timeout, the harness arms the
kill switch and halts — it never proceeds to the next cycle while a prior
intent is unresolved.

Durability (v0.5): an optional write-ahead ``ExecutionJournal`` persists every
safety transition (SUBMITTING with the minted CID before POST, CANCELING before
DELETE) so a crash after a side effect but before its response leaves a
recoverable (client_order_id, state). On construction the harness reloads any
nonterminal episode and ``recover()`` drives it to terminal before a fresh CID
may be minted. A separate OS-held process lease (singleton) is acquired by the
runner, not the harness.

The signal is NULL (a deterministic 1-contract post-only bid that is then
cancelled); it carries no edge and makes no alpha claim.

States: IDLE -> SUBMITTING -> WORKING -> CANCELING -> RECONCILING -> IDLE,
with HALTED as the terminal safety state.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable
from uuid import uuid4

from .execution_journal import ExecutionJournal, JournalRecord
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


# States that are NOT terminal: a crash here must block new intents and recover
# the persisted episode first.
NONTERMINAL_STATES = {
    HarnessState.SUBMITTING,
    HarnessState.WORKING,
    HarnessState.CANCELING,
    HarnessState.RECONCILING,
}

AuditFn = Callable[[str, Any], None]  # (event, fields_dict) -> None


class DemoExecutionHarness:
    """State machine that places/cancels one NULL-signal order per cycle.

    ``client`` is any object exposing ``create_order`` / ``cancel_order`` /
    ``reconcile`` / ``disable`` with the same signatures as ``KalshiExecClient``
    (duck-typed so tests can inject a scripted fake).

    ``journal_path`` (optional) enables the write-ahead durability journal.
    """

    def __init__(
        self,
        client,
        ticker: str,
        *,
        reconcile_timeout: float = 30.0,
        reconcile_poll: float = 1.0,
        audit: AuditFn | None = None,
        journal_path: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self.client = client
        self.ticker = ticker
        self.reconcile_timeout = reconcile_timeout
        self.reconcile_poll = reconcile_poll
        self.audit = audit or (lambda event, fields: None)
        self.journal = ExecutionJournal(journal_path) if journal_path else None
        self.run_id = run_id or uuid4().hex
        self.sequence = 0
        self.state = HarnessState.IDLE
        self.halt_reason: str | None = None
        self.cid: str | None = None
        self.order_id: str | None = None
        self._recovery_pending = False
        self._load_nonterminal_episode()

    # ── Persistence / state ──────────────────────────────────────────────

    def _load_nonterminal_episode(self) -> None:
        """Reload a persisted nonterminal episode (startup recovery trigger)."""
        if self.journal is None:
            return
        latest = self.journal.latest()
        if latest is None or latest.state not in {s.value for s in NONTERMINAL_STATES}:
            return
        try:
            self.state = HarnessState(latest.state)
        except ValueError:
            # Unknown state string in journal: fail closed, not silently idle.
            self.state = HarnessState.HALTED
            self.halt_reason = f"unrecognized journal state: {latest.state!r}"
            self._recovery_pending = True
            return
        self.cid = latest.client_order_id
        self.order_id = latest.order_id
        self.halt_reason = latest.halt_reason
        if latest.ticker:
            self.ticker = latest.ticker
        self._recovery_pending = True

    def _transition(
        self,
        state: HarnessState,
        **fields: Any,
    ) -> None:
        """Set state, update tracked identity, and persist (write-ahead) first.

        The journal write is flushed + fsync'd here, BEFORE the caller performs
        the corresponding venue side effect.
        """
        self.state = state
        if "client_order_id" in fields and fields["client_order_id"] is not None:
            self.cid = fields["client_order_id"]
        if "order_id" in fields and fields["order_id"] is not None:
            self.order_id = fields["order_id"]
        if "halt_reason" in fields and fields["halt_reason"] is not None:
            self.halt_reason = fields["halt_reason"]
        self.sequence += 1
        if self.journal is not None:
            self.journal.append(
                JournalRecord(
                    run_id=self.run_id,
                    sequence=self.sequence,
                    state=state.value,
                    ticker=self.ticker,
                    client_order_id=self.cid,
                    order_id=self.order_id,
                    timestamp=datetime.now(UTC).isoformat(),
                    last_reconcile=fields.get("last_reconcile"),
                    halt_reason=self.halt_reason,
                )
            )
        self.audit(state.value, fields)

    # ── Reconciliation helpers ───────────────────────────────────────────

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

    def _reconcile_for_recovery(self, cid: str) -> tuple[str, ReconcileResult | None]:
        """Poll reconcile for startup recovery.

        Returns:
          ("found", rec)     — venue authoritatively holds the order;
          ("absent", None)   — venue reachable and persistently has no such order;
          ("ambiguous", None) — venue unreachable for the whole window.
        """
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

    def _halt(self, cid: str | None, reason: str) -> HarnessState:
        self._transition(
            HarnessState.HALTED, client_order_id=cid, halt_reason=reason
        )
        try:
            self.client.disable("ABNORMAL_RECONCILIATION", actor="oms-harness")
        except Exception:  # noqa: BLE001 — halt must not depend on the kill switch
            pass
        return HarnessState.HALTED

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
                try:
                    self.client.cancel_order(order_id, market_ticker=self.ticker)
                    self.audit("order_cancel_retried", {"client_order_id": cid, "order_id": order_id})
                except (OrderRejected, OrderOutcomeUnknown):
                    pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.reconcile_poll)

    # ── Recovery ─────────────────────────────────────────────────────────

    def recover(self) -> HarnessState:
        """Drive a persisted nonterminal episode to terminal before new intents.

        Never mints a fresh CID while a nonterminal episode exists.
        """
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
            # Venue authoritatively holds no such order: the episode never
            # materialized (or fully resolved). Close it.
            self.audit("recovery_absent", {"client_order_id": cid})
            self._transition(HarnessState.IDLE, client_order_id=cid)
            self._recovery_pending = False
            return self.state

        # kind == "found": venue authoritatively holds the order.
        status = rec.status
        remaining = rec.remaining_count
        if status == "canceled" or status == "filled" or (
            remaining is not None and remaining == 0
        ):
            self.audit("recovery_terminal", {"client_order_id": cid, "status": status})
            self._transition(HarnessState.IDLE, client_order_id=cid, order_id=rec.order_id)
            self._recovery_pending = False
            return self.state

        # Working/resting: continue the cleanup path (cancel it).
        order_id = rec.order_id or self.order_id
        if not order_id:
            return self._halt(cid, "recovered working order missing order_id")
        self._transition(HarnessState.CANCELING, client_order_id=cid, order_id=order_id)
        try:
            self.client.cancel_order(order_id, market_ticker=self.ticker)
        except (OrderRejected, OrderOutcomeUnknown):
            pass
        if not self._resolve_cancel(cid, order_id):
            return self._halt(cid, "startup recovery cancel unresolved at timeout")
        self._transition(HarnessState.IDLE, client_order_id=cid, order_id=order_id)
        self._recovery_pending = False
        self.audit("recovery_cleanup_done", {"client_order_id": cid, "order_id": order_id})
        return self.state

    # ── Cycle ────────────────────────────────────────────────────────────

    def run_cycle(self) -> HarnessState:
        """Run one place -> rest -> cancel -> reconcile cycle."""
        if self.state == HarnessState.HALTED:
            return self.state
        if self._recovery_pending:
            self.recover()
            if self.state == HarnessState.HALTED or self._recovery_pending:
                return self.state

        # ── SUBMITTING ───────────────────────────────────────────────────
        cid = new_client_order_id()
        # Write-ahead: persist SUBMITTING + CID BEFORE the create side effect.
        self._transition(HarnessState.SUBMITTING, client_order_id=cid)
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
            self._transition(HarnessState.IDLE, client_order_id=cid)
            return self.state
        except (OrderOutcomeUnknown, KalshiConflict) as exc:
            # Ambiguous: the order may already exist (unknown outcome or 409).
            # Reconcile against the SAME client_order_id before anything else.
            self.audit("order_place_ambiguous", {"error": f"{type(exc).__name__}: {exc}"})
            self._transition(HarnessState.RECONCILING, client_order_id=cid)
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
        self._transition(HarnessState.WORKING, client_order_id=cid, order_id=order_id)
        # Write-ahead: persist CANCELING BEFORE the cancel side effect.
        self._transition(HarnessState.CANCELING, client_order_id=cid, order_id=order_id)
        try:
            self.client.cancel_order(order_id, market_ticker=self.ticker)
            self.audit("order_canceled", {"client_order_id": cid, "order_id": order_id})
            self._transition(HarnessState.IDLE, client_order_id=cid, order_id=order_id)
        except (OrderRejected, OrderOutcomeUnknown):
            # Ambiguous: the cancel may not have taken effect.
            self._transition(HarnessState.RECONCILING, client_order_id=cid, order_id=order_id)
            if not self._resolve_cancel(cid, order_id):
                return self._halt(cid, "cancel outcome unresolved at timeout")
            self._transition(HarnessState.IDLE, client_order_id=cid, order_id=order_id)

        return self.state