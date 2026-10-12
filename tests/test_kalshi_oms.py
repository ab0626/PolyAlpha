"""Tests for the demo execution harness state machine (ambiguity invariant)."""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

from polyalpha.kalshi_exec import (
    CancelAck,
    KalshiConflict,
    OrderAck,
    OrderOutcomeUnknown,
    ReconcileResult,
)
from polyalpha.kalshi_oms import DemoExecutionHarness, HarnessState

ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    """Scripted stand-in for KalshiExecClient (duck-typed)."""

    def __init__(self, *, create_exc=None, cancel_excs=None, reconcile_results=None, reconcile_exc=None):
        self.create_exc = create_exc
        self.cancel_excs = list(cancel_excs or [])
        self.reconcile_results = list(reconcile_results or [])
        self.reconcile_exc = reconcile_exc
        self.cancel_calls: list[str] = []
        self.disabled_reason: str | None = None
        self.enable_called = False

    def create_order(self, order):
        if self.create_exc:
            raise self.create_exc
        return OrderAck(order_id="order-1", client_order_id=order.client_order_id, raw={})

    def cancel_order(self, order_id, market_ticker):
        self.cancel_calls.append(order_id)
        if self.cancel_excs:
            exc = self.cancel_excs.pop(0)
            if exc is not None:
                raise exc
        return CancelAck(order_id=order_id, client_order_id=None, reduced_by=Decimal("1"), raw={})

    def reconcile(self, cid):
        if self.reconcile_exc:
            raise self.reconcile_exc
        if self.reconcile_results:
            return self.reconcile_results.pop(0)
        return ReconcileResult(client_order_id=cid, found=False)

    def disable(self, reason, actor=None):
        self.disabled_reason = reason

    def enable(self, actor=None):
        self.enable_called = True


def make_harness(client, **kwargs):
    defaults = {"reconcile_timeout": 0.05, "reconcile_poll": 0.01}
    defaults.update(kwargs)
    return DemoExecutionHarness(client, "KX-1", **defaults)


# ── Ambiguity invariant ──────────────────────────────────────────────────


def test_timeout_after_create_halts_and_arms_kill_switch():
    client = FakeClient(create_exc=OrderOutcomeUnknown("503"))
    harness = make_harness(client)

    state = harness.run_cycle()

    assert state == HarnessState.HALTED
    assert client.disabled_reason == "ABNORMAL_RECONCILIATION"


def test_timeout_after_cancel_halts_and_arms_kill_switch():
    client = FakeClient(cancel_excs=[OrderOutcomeUnknown("503")])
    harness = make_harness(client)

    state = harness.run_cycle()

    assert state == HarnessState.HALTED
    assert client.disabled_reason == "ABNORMAL_RECONCILIATION"


def test_stale_resting_after_cancel_retries_then_resolves():
    resting = ReconcileResult(
        client_order_id="c", found=True, order_id="order-1", status="resting",
        remaining_count=Decimal("1.00"), filled_count=Decimal("0.00"),
    )
    canceled = ReconcileResult(
        client_order_id="c", found=True, order_id="order-1", status="canceled",
        remaining_count=Decimal("0.00"), filled_count=Decimal("0.00"),
    )
    client = FakeClient(
        cancel_excs=[OrderOutcomeUnknown("503"), None],
        reconcile_results=[resting, canceled],
    )
    harness = make_harness(client, reconcile_timeout=1.0)

    state = harness.run_cycle()

    assert state == HarnessState.IDLE
    assert len(client.cancel_calls) == 2  # original + retry
    assert client.disabled_reason is None  # resolved, no halt


def test_create_unknown_then_confirmed_cancels_and_idles():
    # create is ambiguous, but reconcile finds the order -> cancel and idle.
    confirmed = ReconcileResult(
        client_order_id="c", found=True, order_id="order-1", status="resting",
        remaining_count=Decimal("1.00"), filled_count=Decimal("0.00"),
    )
    client = FakeClient(create_exc=OrderOutcomeUnknown("503"), reconcile_results=[confirmed])
    harness = make_harness(client, reconcile_timeout=1.0)

    state = harness.run_cycle()

    assert state == HarnessState.IDLE
    assert client.cancel_calls == ["order-1"]  # confirmed -> cancel it


def test_harness_never_clears_kill_switch():
    client = FakeClient()
    harness = make_harness(client)

    harness.run_cycle()

    assert client.enable_called is False


# ── Runner-level kill-switch gating ──────────────────────────────────────


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "kalshi_oms_demo", ROOT / "scripts" / "kalshi_oms_demo.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ArmedClient:
    class _State:
        active = True
        reason = "DAILY_LOSS_BREACH"

        def as_dict(self):
            return {"active": True, "reason": "DAILY_LOSS_BREACH"}

    kill_switch_state = _State()
    enable_called = False

    def enable(self, actor=None):
        self.enable_called = True


def test_runner_refuses_to_run_when_armed(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "load_env_file", lambda *a, **k: None)
    fake = _ArmedClient()
    monkeypatch.setattr(mod, "KalshiExecClient", lambda *a, **k: fake)
    monkeypatch.setattr(sys, "argv", ["kalshi_oms_demo.py", "--env-file", "/nonexistent"])

    rc = mod.main()

    assert rc == 1
    assert fake.enable_called is False


def test_clear_refuses_non_manual_reason(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "load_env_file", lambda *a, **k: None)
    fake = _ArmedClient()
    monkeypatch.setattr(mod, "KalshiExecClient", lambda *a, **k: fake)
    monkeypatch.setattr(
        sys, "argv", ["kalshi_oms_demo.py", "--env-file", "/nonexistent", "--clear-kill-switch"]
    )

    rc = mod.main()

    assert rc == 1
    assert fake.enable_called is False


def test_conflict_409_reconciles_same_cid():
    confirmed = ReconcileResult(
        client_order_id="c", found=True, order_id="order-1", status="resting",
        remaining_count=Decimal("1.00"), filled_count=Decimal("0.00"),
    )
    client = FakeClient(create_exc=KalshiConflict("dup"), reconcile_results=[confirmed])
    harness = make_harness(client, reconcile_timeout=1.0)

    state = harness.run_cycle()

    assert state == HarnessState.IDLE
    assert client.cancel_calls == ["order-1"]  # reconciled, then canceled


def test_reconcile_transport_failure_halts_and_arms():
    client = FakeClient(
        create_exc=OrderOutcomeUnknown("503"),
        reconcile_exc=OrderOutcomeUnknown("timeout"),
    )
    harness = make_harness(client)

    state = harness.run_cycle()

    assert state == HarnessState.HALTED
    assert client.disabled_reason == "ABNORMAL_RECONCILIATION"


def test_cancel_reconcile_transport_failure_halts_and_arms():
    client = FakeClient(
        cancel_excs=[OrderOutcomeUnknown("503")],
        reconcile_exc=OrderOutcomeUnknown("timeout"),
    )
    harness = make_harness(client)

    state = harness.run_cycle()

    assert state == HarnessState.HALTED
    assert client.disabled_reason == "ABNORMAL_RECONCILIATION"


def test_halted_exit_code_nonzero(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "load_env_file", lambda *a, **k: None)
    monkeypatch.setattr(mod, "make_audit", lambda path: (lambda event, fields: None))
    monkeypatch.setattr(mod, "discover_ticker", lambda *a, **k: "KX-1")

    class _ClearClient:
        class _State:
            active = False
            reason = None

            def as_dict(self):
                return {"active": False}

        kill_switch_state = _State()

        def disable(self, reason, actor=None):
            pass

    class _HaltingHarness:
        halt_reason = "create outcome unresolved at timeout"

        def __init__(self, *a, **k):
            pass

        def run_cycle(self):
            return HarnessState.HALTED

    monkeypatch.setattr(mod, "KalshiExecClient", lambda *a, **k: _ClearClient())
    monkeypatch.setattr(mod, "DemoExecutionHarness", _HaltingHarness)
    monkeypatch.setattr(
        sys, "argv", ["kalshi_oms_demo.py", "--env-file", "/nonexistent", "--max-cycles", "1"]
    )

    rc = mod.main()

    assert rc == 2


def test_interrupt_while_idle_exits_clean(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "load_env_file", lambda *a, **k: None)
    monkeypatch.setattr(mod, "make_audit", lambda path: (lambda event, fields: None))
    monkeypatch.setattr(mod, "discover_ticker", lambda *a, **k: "KX-1")

    class _ClearClient:
        class _State:
            active = False
            reason = None

            def as_dict(self):
                return {"active": False}

        kill_switch_state = _State()

        def __init__(self):
            self.disabled = None

        def disable(self, reason, actor=None):
            self.disabled = reason

    class _InterruptIdleHarness:
        state = HarnessState.IDLE

        def __init__(self, *a, **k):
            pass

        def run_cycle(self):
            raise KeyboardInterrupt

    client = _ClearClient()
    monkeypatch.setattr(mod, "KalshiExecClient", lambda *a, **k: client)
    monkeypatch.setattr(mod, "DemoExecutionHarness", _InterruptIdleHarness)
    monkeypatch.setattr(sys, "argv", ["kalshi_oms_demo.py", "--env-file", "/nonexistent"])

    rc = mod.main()

    assert rc == 0
    assert client.disabled is None


def test_interrupt_during_active_state_arms_and_exits_2(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "load_env_file", lambda *a, **k: None)
    monkeypatch.setattr(mod, "make_audit", lambda path: (lambda event, fields: None))
    monkeypatch.setattr(mod, "discover_ticker", lambda *a, **k: "KX-1")

    class _ClearClient:
        class _State:
            active = False
            reason = None

            def as_dict(self):
                return {"active": False}

        kill_switch_state = _State()

        def __init__(self):
            self.disabled = None

        def disable(self, reason, actor=None):
            self.disabled = reason

    class _InterruptActiveHarness:
        state = HarnessState.SUBMITTING

        def __init__(self, *a, **k):
            pass

        def run_cycle(self):
            raise KeyboardInterrupt

    client = _ClearClient()
    monkeypatch.setattr(mod, "KalshiExecClient", lambda *a, **k: client)
    monkeypatch.setattr(mod, "DemoExecutionHarness", _InterruptActiveHarness)
    monkeypatch.setattr(sys, "argv", ["kalshi_oms_demo.py", "--env-file", "/nonexistent"])

    rc = mod.main()

    assert rc == 2
    assert client.disabled == "ABNORMAL_RECONCILIATION"