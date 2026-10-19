"""Tests for the production resting-fill control plane (LIVE_CALIBRATION_V0)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from polyalpha.execution_journal_prod import ProdExecutionJournal, ProdJournalRecord
from polyalpha.kalshi_exec import (
    OrderAck,
    OrderOutcomeUnknown,
    OrderSide,
    ReconcileResult,
)
from polyalpha.kalshi_exec_prod import CALIBRATION_PROTOCOL_VERSION
from polyalpha.kalshi_oms import HarnessState
from polyalpha.kalshi_oms_prod import ProdExecutionHarness, ProdOrderIntent


class FakeKillState:
    def __init__(self, active=False, reason=None):
        self.active = active
        self.reason = reason


class FakeArmedState:
    def __init__(self, armed=True):
        self.armed = armed


class FakeProdClient:
    def __init__(self, *, create_exc=None, reconcile_results=None, reconcile_exc=None):
        self.create_exc = create_exc
        self.reconcile_results = list(reconcile_results or [])
        self.reconcile_exc = reconcile_exc
        self.create_calls = []
        self.cancel_calls: list[str] = []
        self.disabled_reason: str | None = None
        self.kill_switch_state = FakeKillState(active=False)
        self.armed_state = FakeArmedState(armed=True)

    def create_order(self, order):
        self.create_calls.append(order)
        if self.create_exc:
            raise self.create_exc
        return OrderAck(order_id="order-1", client_order_id=order.client_order_id, raw={})

    def cancel_order(self, order_id, market_ticker):
        self.cancel_calls.append(order_id)
        return None

    def reconcile(self, cid):
        if self.reconcile_exc:
            raise self.reconcile_exc
        if self.reconcile_results:
            return self.reconcile_results.pop(0)
        return ReconcileResult(client_order_id=cid, found=False)

    def disable(self, reason, actor=None):
        self.disabled_reason = reason


def make_harness(client, tmp_path, **kwargs):
    params = dict(
        reconcile_timeout=0.3,
        reconcile_poll=0.01,
        journal_path=str(tmp_path / "oms_prod_journal.jsonl"),
        frozen_version=CALIBRATION_PROTOCOL_VERSION,
    )
    params.update(kwargs)
    return ProdExecutionHarness(client, "KX-1", **params)


def make_intent(count="1", price="0.10", expiry=None):
    return ProdOrderIntent(
        ticker="KX-1",
        side=OrderSide.BID,
        price=Decimal(price),
        count=Decimal(count),
        client_order_id=uuid4().hex,
        expiry_deadline=expiry,
    )


def resting(cid, order_id="order-1", remaining="1", status="resting"):
    return ReconcileResult(
        client_order_id=cid,
        found=True,
        order_id=order_id,
        status=status,
        remaining_count=Decimal(remaining),
    )


def write_working_episode(tmp_path, *, cid="cid-1", order_id="order-1", expiry=None):
    journal_path = tmp_path / "oms_prod_journal.jsonl"
    ProdExecutionJournal(journal_path).append(
        ProdJournalRecord(
            run_id="run-1",
            sequence=1,
            state="working",
            ticker="KX-1",
            client_order_id=cid,
            order_id=order_id,
            calibration_version=CALIBRATION_PROTOCOL_VERSION,
            side="bid",
            price="0.10",
            count="1",
            signal_timestamp="2026-10-18T00:00:00+00:00",
            conservative_edge="0.05",
            expiry_deadline=expiry,
        )
    )
    return journal_path


def future_expiry():
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def past_expiry():
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat()


# ── Gates ─────────────────────────────────────────────────────────────────

def test_version_mismatch_halts(tmp_path):
    client = FakeProdClient()
    harness = make_harness(client, tmp_path, frozen_version="wrong")
    assert harness.run_cycle() == HarnessState.HALTED
    assert harness.halt_reason == "calibration protocol version unverified/mismatched"
    assert client.create_calls == []


def test_no_signal_no_mutation(tmp_path):
    client = FakeProdClient()
    harness = make_harness(client, tmp_path)
    assert harness.run_cycle() == HarnessState.IDLE
    assert client.create_calls == []


def test_daily_loss_stop_halts_and_arms_kill_switch(tmp_path):
    client = FakeProdClient()
    harness = make_harness(client, tmp_path, daily_loss_stop=Decimal("10.00"))
    harness.record_loss(Decimal("10.00"))
    assert harness.state == HarnessState.HALTED
    assert client.disabled_reason == "DAILY_LOSS_BREACH"


def test_bankroll_cap_blocks_oversized_notional(tmp_path):
    client = FakeProdClient()
    harness = make_harness(client, tmp_path, bankroll_cap=Decimal("1.5"))
    assert harness.submit_intent(make_intent(count="2")) == HarnessState.HALTED
    assert harness.halt_reason == "bankroll cap exceeded"
    assert client.create_calls == []


def test_ambiguous_create_reconciles_same_cid_then_rests(tmp_path):
    intent = make_intent()
    client = FakeProdClient(
        create_exc=OrderOutcomeUnknown("network"),
        reconcile_results=[resting(intent.client_order_id)],
    )
    harness = make_harness(client, tmp_path)
    assert harness.submit_intent(intent) == HarnessState.WORKING
    assert client.create_calls[0].client_order_id == intent.client_order_id


def test_ambiguous_create_unresolved_halts(tmp_path):
    client = FakeProdClient(create_exc=OrderOutcomeUnknown("network"))
    harness = make_harness(client, tmp_path)
    assert harness.submit_intent(make_intent()) == HarnessState.HALTED
    assert harness.halt_reason == "create outcome unresolved at timeout"


def test_kill_switch_active_halts(tmp_path):
    client = FakeProdClient()
    client.kill_switch_state = FakeKillState(active=True, reason="MANUAL_OPERATOR")
    harness = make_harness(client, tmp_path)
    assert harness.run_cycle() == HarnessState.HALTED
    assert client.create_calls == []


def test_operator_not_armed_halts(tmp_path):
    client = FakeProdClient()
    client.armed_state = FakeArmedState(armed=False)
    harness = make_harness(client, tmp_path)
    assert harness.run_cycle() == HarnessState.HALTED
    assert client.create_calls == []


# ── Recovery branching (the key distinction vs demo) ──────────────────────

def test_recovery_filled_is_terminal_no_cancel(tmp_path):
    client = FakeProdClient(
        reconcile_results=[
            ReconcileResult(
                client_order_id="cid-1", found=True, order_id="order-1",
                status="filled", remaining_count=Decimal("0"),
                filled_count=Decimal("1"),
            )
        ]
    )
    write_working_episode(tmp_path, expiry=future_expiry())
    harness = make_harness(client, tmp_path)
    assert harness._recovery_pending is True
    assert harness.recover() == HarnessState.IDLE
    assert client.cancel_calls == []


def test_recovery_resting_within_lifetime_keeps_resting(tmp_path):
    client = FakeProdClient(reconcile_results=[resting("cid-1", remaining="1")])
    write_working_episode(tmp_path, expiry=future_expiry())
    harness = make_harness(client, tmp_path)
    assert harness.recover() == HarnessState.WORKING
    # The intended resting order is NOT cancelled.
    assert client.cancel_calls == []


def test_recovery_resting_stale_cancels(tmp_path):
    client = FakeProdClient(
        reconcile_results=[
            resting("cid-1", remaining="1"),
            ReconcileResult(
                client_order_id="cid-1", found=True, order_id="order-1",
                status="canceled", remaining_count=Decimal("0"),
            ),
        ]
    )
    write_working_episode(tmp_path, expiry=past_expiry())
    harness = make_harness(client, tmp_path)
    assert harness.recover() == HarnessState.IDLE
    assert client.cancel_calls == ["order-1"]


def test_recovery_absent_closes_episode(tmp_path):
    client = FakeProdClient()  # reconcile default: found=False
    write_working_episode(tmp_path, expiry=future_expiry())
    harness = make_harness(client, tmp_path)
    assert harness.recover() == HarnessState.IDLE
    assert client.cancel_calls == []


def test_recovery_unreachable_halts(tmp_path):
    client = FakeProdClient(reconcile_exc=OrderOutcomeUnknown("network"))
    write_working_episode(tmp_path, expiry=future_expiry())
    harness = make_harness(client, tmp_path)
    assert harness.recover() == HarnessState.HALTED
    assert harness.halt_reason == "startup recovery unresolved (venue unreachable)"


def test_recovery_executing_then_resting_keeps_resting(tmp_path):
    client = FakeProdClient(
        reconcile_results=[
            resting("cid-1", remaining="1", status="executing"),
            resting("cid-1", remaining="1", status="resting"),
        ]
    )
    write_working_episode(tmp_path, expiry=future_expiry())
    harness = make_harness(client, tmp_path)
    assert harness.recover() == HarnessState.WORKING
    assert client.cancel_calls == []


def test_recovery_executing_unresolved_halts(tmp_path):
    client = FakeProdClient(
        reconcile_results=[resting("cid-1", remaining="1", status="executing")]
    )
    write_working_episode(tmp_path, expiry=future_expiry())
    harness = make_harness(client, tmp_path)
    assert harness.recover() == HarnessState.HALTED
    assert harness.halt_reason == "startup recovery order status unresolved at timeout"