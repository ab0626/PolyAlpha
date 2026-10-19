"""Tests for the production execution control plane (LIVE_CALIBRATION_V0 gates)."""

from __future__ import annotations

from decimal import Decimal

from polyalpha.kalshi_exec import (
    NewOrder,
    OrderAck,
    OrderOutcomeUnknown,
    OrderSide,
    ReconcileResult,
    new_client_order_id,
)
from polyalpha.kalshi_exec_prod import CALIBRATION_PROTOCOL_VERSION
from polyalpha.kalshi_oms import HarnessState
from polyalpha.kalshi_oms_prod import ProdExecutionHarness


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
        self.create_calls: list[NewOrder] = []
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
        reconcile_timeout=0.5,
        reconcile_poll=0.01,
        journal_path=str(tmp_path / "oms_prod_journal.jsonl"),
        frozen_version=CALIBRATION_PROTOCOL_VERSION,
    )
    params.update(kwargs)
    return ProdExecutionHarness(client, "KX-1", **params)


def make_order(count="1", price="0.10") -> NewOrder:
    return NewOrder(
        ticker="KX-1",
        side=OrderSide.BID,
        count=Decimal(count),
        price=Decimal(price),
        client_order_id=new_client_order_id(),
    )


def test_version_mismatch_halts(tmp_path):
    client = FakeProdClient()
    harness = make_harness(client, tmp_path, frozen_version="wrong")
    assert harness.run_cycle() == HarnessState.HALTED
    assert harness.halt_reason == "calibration protocol version unverified/mismatched"
    assert client.create_calls == []


def test_no_signal_no_mutation(tmp_path):
    client = FakeProdClient()
    harness = make_harness(client, tmp_path)  # signal_fn defaults to None
    assert harness.run_cycle() == HarnessState.IDLE
    assert client.create_calls == []


def test_daily_loss_stop_halts_and_arms_kill_switch(tmp_path):
    client = FakeProdClient()
    harness = make_harness(client, tmp_path, daily_loss_stop=Decimal("10.00"))
    harness.record_loss(Decimal("10.00"))
    assert harness.state == HarnessState.HALTED
    assert client.disabled_reason == "DAILY_LOSS_BREACH"
    # Once halted, run_cycle refuses to do anything.
    assert harness.run_cycle() == HarnessState.HALTED


def test_bankroll_cap_blocks_oversized_notional(tmp_path):
    client = FakeProdClient()
    harness = make_harness(client, tmp_path, bankroll_cap=Decimal("1.5"))
    order = make_order(count="2")
    assert harness.submit_intent(order) == HarnessState.HALTED
    assert harness.halt_reason == "bankroll cap exceeded"
    assert client.create_calls == []


def test_ambiguous_create_reconciles_same_cid_then_works(tmp_path):
    order = make_order()
    client = FakeProdClient(
        create_exc=OrderOutcomeUnknown("network"),
        reconcile_results=[ReconcileResult(client_order_id=order.client_order_id, found=True, order_id="order-1")],
    )
    harness = make_harness(client, tmp_path)
    state = harness.submit_intent(order)
    assert state == HarnessState.WORKING
    # No new CID minted: the same client_order_id was reconciled and submitted.
    assert client.create_calls[0].client_order_id == order.client_order_id


def test_ambiguous_create_unresolved_halts(tmp_path):
    order = make_order()
    client = FakeProdClient(create_exc=OrderOutcomeUnknown("network"))  # reconcile always not-found
    harness = make_harness(client, tmp_path)
    assert harness.submit_intent(order) == HarnessState.HALTED
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