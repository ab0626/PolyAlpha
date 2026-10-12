"""Tests for the demo execution harness state machine (ambiguity invariant)."""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from polyalpha.execution_journal import ExecutionJournal, JournalRecord
from polyalpha.kalshi_exec import (
    CancelAck,
    KalshiConflict,
    OrderAck,
    OrderOutcomeUnknown,
    ReconcileResult,
)
from polyalpha.kalshi_oms import DemoExecutionHarness, HarnessState
from polyalpha.process_lease import ProcessLease

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
        def __init__(self, reason="DAILY_LOSS_BREACH", activated_by=None):
            self.active = True
            self.reason = reason
            self.activated_by = activated_by

        def as_dict(self):
            return {"active": True, "reason": self.reason}

    def __init__(self, reason="DAILY_LOSS_BREACH", activated_by=None):
        self.kill_switch_state = self._State(reason=reason, activated_by=activated_by)
        self.enable_called = False

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


def test_clear_only_clears_initial_manual_arm(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "load_env_file", lambda *a, **k: None)
    fake = _ArmedClient(reason="MANUAL_OPERATOR", activated_by="init-fail-closed")
    monkeypatch.setattr(mod, "KalshiExecClient", lambda *a, **k: fake)
    monkeypatch.setattr(
        sys, "argv", ["kalshi_oms_demo.py", "--env-file", "/nonexistent", "--clear-kill-switch"]
    )

    rc = mod.main()

    assert rc == 0
    assert fake.enable_called is True


def test_clear_refuses_later_manual_arm(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "load_env_file", lambda *a, **k: None)
    fake = _ArmedClient(reason="MANUAL_OPERATOR", activated_by="operator")
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


class _FakeLease:
    """Stand-in for ProcessLease so runner tests never touch a real lock file."""

    def __init__(self, path):
        self.path = path

    def acquire(self):
        return True

    def release(self):
        pass


def test_interrupt_while_idle_exits_clean(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "load_env_file", lambda *a, **k: None)
    monkeypatch.setattr(mod, "make_audit", lambda path: (lambda event, fields: None))
    monkeypatch.setattr(mod, "discover_ticker", lambda *a, **k: "KX-1")
    monkeypatch.setattr(mod, "ProcessLease", _FakeLease)

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
    monkeypatch.setattr(mod, "ProcessLease", _FakeLease)

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


# ══════════════════════════════════════════════════════════════════════════
# DURABILITY — process lease + write-ahead journal + startup recovery
# ══════════════════════════════════════════════════════════════════════════


class RecoveryFake:
    """Scriptable client for durability tests (records call order)."""

    def __init__(self, *, reconcile_script=None, default_reconcile=None,
                 reconcile_exc=None, cancel_exc=None):
        self.reconcile_script = list(reconcile_script or [])
        self.default_reconcile = default_reconcile
        self.reconcile_exc = reconcile_exc
        self.cancel_exc = cancel_exc
        self.cancel_calls: list[str] = []
        self.create_cids: list[str] = []
        self.reconcile_cids: list[str] = []
        self.disabled_reason: str | None = None

    def create_order(self, order):
        self.create_cids.append(order.client_order_id)
        return OrderAck(order_id="fresh-oid", client_order_id=order.client_order_id, raw={})

    def cancel_order(self, order_id, market_ticker):
        self.cancel_calls.append(order_id)
        if self.cancel_exc:
            raise self.cancel_exc
        return CancelAck(order_id=order_id, client_order_id=None, reduced_by=Decimal("1"), raw={})

    def reconcile(self, cid):
        self.reconcile_cids.append(cid)
        if self.reconcile_exc:
            raise self.reconcile_exc
        if self.reconcile_script:
            return self.reconcile_script.pop(0)
        if self.default_reconcile is not None:
            return self.default_reconcile
        return ReconcileResult(client_order_id=cid, found=False)

    def disable(self, reason, actor=None):
        self.disabled_reason = reason


def _resting(cid, order_id="venue-oid"):
    return ReconcileResult(
        client_order_id=cid, found=True, order_id=order_id,
        status="resting", remaining_count=Decimal("1"),
    )


def _seed(tmp_path, state, cid="cid-seeded", order_id=None, ticker="KX-1"):
    journal = ExecutionJournal(tmp_path / "journal.jsonl")
    journal.append(JournalRecord(
        run_id="r0", sequence=1, state=state, ticker=ticker,
        client_order_id=cid, order_id=order_id,
    ))
    return journal.path


class TestDurability:
    def test_restart_from_submitting_absent_closes(self, tmp_path):
        jp = _seed(tmp_path, "submitting", cid="cid-seeded")
        client = RecoveryFake()  # reconcile -> found=False (authoritative absent)
        h = DemoExecutionHarness(client, "KX-1", reconcile_timeout=0.05,
                                 reconcile_poll=0.01, journal_path=jp)
        assert h._recovery_pending is True
        assert h.state == HarnessState.SUBMITTING
        assert h.recover() == HarnessState.IDLE
        assert client.reconcile_cids and client.reconcile_cids[0] == "cid-seeded"
        assert client.create_cids == []  # never minted a fresh CID during recovery

    def test_restart_from_working_resting_cancels(self, tmp_path):
        jp = _seed(tmp_path, "working", cid="cid-seeded", order_id="venue-oid")
        canceled = ReconcileResult(
            client_order_id="cid-seeded", found=True, order_id="venue-oid",
            status="canceled", remaining_count=Decimal("0"),
        )
        client = RecoveryFake(reconcile_script=[_resting("cid-seeded", "venue-oid"), canceled])
        h = DemoExecutionHarness(client, "KX-1", reconcile_timeout=0.05,
                                 reconcile_poll=0.01, journal_path=jp)
        assert h.recover() == HarnessState.IDLE
        assert client.cancel_calls == ["venue-oid"]  # cleanup path cancelled it

    def test_restart_from_canceling_canceled_closes(self, tmp_path):
        jp = _seed(tmp_path, "canceling", cid="cid-seeded", order_id="venue-oid")
        canceled = ReconcileResult(
            client_order_id="cid-seeded", found=True, order_id="venue-oid",
            status="canceled", remaining_count=Decimal("0"),
        )
        client = RecoveryFake(default_reconcile=canceled)
        h = DemoExecutionHarness(client, "KX-1", reconcile_timeout=0.05,
                                 reconcile_poll=0.01, journal_path=jp)
        assert h.recover() == HarnessState.IDLE
        assert client.cancel_calls == []  # already canceled, nothing to do

    def test_unknown_create_after_restart_halts(self, tmp_path):
        jp = _seed(tmp_path, "submitting", cid="cid-seeded")
        client = RecoveryFake(reconcile_exc=OrderOutcomeUnknown("timeout"))
        h = DemoExecutionHarness(client, "KX-1", reconcile_timeout=0.05,
                                 reconcile_poll=0.01, journal_path=jp)
        assert h.recover() == HarnessState.HALTED
        assert client.disabled_reason == "ABNORMAL_RECONCILIATION"

    def test_unknown_cancel_after_restart_halts(self, tmp_path):
        jp = _seed(tmp_path, "working", cid="cid-seeded", order_id="venue-oid")
        client = RecoveryFake(
            default_reconcile=_resting("cid-seeded", "venue-oid"),
            cancel_exc=OrderOutcomeUnknown("timeout"),
        )
        h = DemoExecutionHarness(client, "KX-1", reconcile_timeout=0.05,
                                 reconcile_poll=0.01, journal_path=jp)
        assert h.recover() == HarnessState.HALTED
        assert client.disabled_reason == "ABNORMAL_RECONCILIATION"

    def test_journal_trailing_truncation_tolerated(self, tmp_path):
        jp = tmp_path / "journal.jsonl"
        jp.write_text(
            '{"run_id":"r","sequence":1,"state":"submitting","ticker":"KX-1",'
            '"client_order_id":"cid-seeded"}\n{"partial',
            encoding="utf-8",
        )
        records, truncated = ExecutionJournal(jp).records()
        assert truncated is True
        assert records[-1].client_order_id == "cid-seeded"

    def test_journal_midfile_corruption_fails_closed(self, tmp_path):
        jp = tmp_path / "journal.jsonl"
        jp.write_text(
            '{"run_id":"r","sequence":1,"state":"submitting","ticker":"KX-1",'
            '"client_order_id":"a"}\n{garbage}\n'
            '{"run_id":"r","sequence":3,"state":"idle","ticker":"KX-1",'
            '"client_order_id":"b"}\n',
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError):
            ExecutionJournal(jp).records()

    def test_lease_contention(self, tmp_path):
        lp = tmp_path / "lease.lock"
        a = ProcessLease(lp)
        b = ProcessLease(lp)
        assert a.acquire() is True
        assert b.acquire() is False  # second instance blocked
        a.release()
        assert b.acquire() is True  # released on process exit semantics

    def test_recovered_episode_blocks_fresh_cid(self, tmp_path):
        jp = _seed(tmp_path, "submitting", cid="cid-seeded")
        client = RecoveryFake()  # reconcile -> found=False (absent)
        h = DemoExecutionHarness(client, "KX-1", reconcile_timeout=0.05,
                                 reconcile_poll=0.01, journal_path=jp)
        h.run_cycle()
        # Recovery reconciled the persisted CID FIRST, then a fresh CID was used.
        assert client.reconcile_cids and client.reconcile_cids[0] == "cid-seeded"
        assert client.create_cids and client.create_cids[0] != "cid-seeded"