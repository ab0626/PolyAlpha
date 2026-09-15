"""Tests for the live-ready execution architecture.

Covers OrderIntent + idempotency, execution gateways (paper/shadow/live-ready),
the pre-trade gate (21 checks + TOCTOU drift), the kill switch, order-state
reconciliation, cancel-on-stale, capacity curves, execution calibration, and
the promotion gate.

All tests are offline and non-transmitting: the LiveReady gateway must never
submit anything without an operator-approved venue adapter, which does not
exist in this phase.
"""

import sys
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

sys.path.insert(0, "src")

from polyalpha.cancel_policy import CancelAction, CancelPolicy, RestingOrder  # noqa: E402
from polyalpha.capacity import capacity_curve  # noqa: E402
from polyalpha.execution_calibration import (  # noqa: E402
    ExecutionObservation,
    calibrate,
)
from polyalpha.execution_gateway import (  # noqa: E402
    LiveReadyExecutionGateway,
    PaperExecutionGateway,
    ShadowExecutionGateway,
)
from polyalpha.execution_modes import ExecutionMode, banner, is_executing  # noqa: E402
from polyalpha.kill_switch import TradingKillSwitch  # noqa: E402
from polyalpha.order_intent import (  # noqa: E402
    IntentLedger,
    IntentState,
    OrderIntent,
    new_intent,
)
from polyalpha.order_reconciliation import (  # noqa: E402
    OrderStateReconciler,
    SubmissionOutcome,
    VenueView,
)
from polyalpha.pre_trade_gate import LivePreTradeGate  # noqa: E402
from polyalpha.promotion_gate import LiveReadinessGate  # noqa: E402

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class _Market:
    def __init__(self, active=True, accepting_orders=True, enable_order_book=True, closed=False):
        self.active = active
        self.accepting_orders = accepting_orders
        self.enable_order_book = enable_order_book
        self.closed = closed


def _intent(**overrides) -> OrderIntent:
    base = dict(
        decision_id="d1",
        strategy_version="s1",
        market_id="m1",
        condition_id="c1",
        token_id="t1",
        outcome="YES",
        side="BUY",
        requested_shares=D("100"),
        requested_notional=D("50"),
        limit_price=D("0.50"),
        fair_probability=D("0.60"),
        conservative_probability=D("0.55"),
        expected_vwap=D("0.50"),
        expected_fee=D("0.01"),
        expected_slippage=D("0.005"),
        gross_edge=D("0.10"),
        predicted_net_edge=D("0.08"),
        model_version="v1",
        event_id="e1",
        event_cluster="cl1",
        category="politics",
        exposures_before=(D("0"), D("0"), D("0"), D("0")),
        exposures_after=(D("50"), D("50"), D("50"), D("50")),
        book_timestamp=NOW,
        model_timestamp=NOW,
        research_logic_sha256="r" * 64,
        config_sha256="c" * 64,
        signal_book_hash="h_signal",
        created_at=NOW,
    )
    base.update(overrides)
    return new_intent(**base)


def _world(**overrides) -> dict:
    base = {
        "market": _Market(),
        "book": {"bids": [{"price": "0.49", "size": "100"}], "asks": [{"price": "0.50", "size": "100"}], "hash": "h_signal"},
        "book_age_seconds": 5,
        "forecast_age_seconds": 5,
        "metadata_age_seconds": 5,
        "resolution_definition_hash": "rd1",
        "fee_schedule_version": "f1",
        "eligibility_ok": True,
        "data_health": "GREEN",
        "reconciliation_healthy": True,
        "risk_limits_ok": True,
        "daily_loss_breaker": False,
        "drawdown_breaker": False,
        "kill_switch_active": False,
        "no_account_ambiguity": True,
        "depth_ok": True,
    }
    base.update(overrides)
    return base


# ══════════════════════════════════════════════════════════════════════════
# ORDER INTENT
# ══════════════════════════════════════════════════════════════════════════


class TestOrderIntent:
    def test_construction_and_attempt(self):
        intent = _intent()
        assert intent.state == IntentState.CREATED
        assert intent.execution_attempt_id  # assigned by new_intent
        retried = intent.next_attempt()
        assert retried.intent_id == intent.intent_id
        assert retried.execution_attempt_id != intent.execution_attempt_id

    def test_validation_rejects_naive_timestamps(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            _intent(book_timestamp=datetime(2026, 1, 1))

    def test_validation_rejects_bad_side(self):
        with pytest.raises(ValueError, match="side"):
            _intent(side="HOLD")

    def test_to_dict_roundtrip_fields(self):
        d = _intent().to_dict()
        assert d["outcome"] == "YES"
        assert d["state"] == "CREATED"
        assert d["research_logic_sha256"] == "r" * 64

    def test_ledger_idempotency(self):
        ledger = IntentLedger()
        intent = _intent()
        assert ledger.register(intent) is True
        assert ledger.register(intent) is False  # duplicate intent_id
        # A retry of the SAME logical intent (new attempt id, same intent_id)
        # must NOT create a second logical order.
        assert ledger.register(intent.next_attempt()) is False


# ══════════════════════════════════════════════════════════════════════════
# PRE-TRADE GATE
# ══════════════════════════════════════════════════════════════════════════


class TestPreTradeGate:
    def test_all_clear_approves(self):
        gate = LivePreTradeGate()
        result = gate.evaluate(_intent(), _world())
        assert result.approved is True, [c.name for c in result.checks if not c.passed]

    def test_market_inactive_rejects(self):
        gate = LivePreTradeGate()
        result = gate.evaluate(_intent(), _world(market=_Market(active=False)))
        assert result.approved is False
        assert any(c.name == "market_active" and not c.passed for c in result.checks)

    def test_kill_switch_rejects(self):
        gate = LivePreTradeGate()
        result = gate.evaluate(_intent(), _world(kill_switch_active=True))
        assert result.approved is False

    def test_stale_book_rejects(self):
        gate = LivePreTradeGate()
        result = gate.evaluate(_intent(), _world(book_age_seconds=120))
        assert result.approved is False

    def test_data_health_not_green_rejects(self):
        gate = LivePreTradeGate()
        result = gate.evaluate(_intent(), _world(data_health="RED"))
        assert result.approved is False

    def test_toctou_price_drift_rejects(self):
        gate = LivePreTradeGate(price_tolerance=D("0.02"))
        # Execution book ask moved from 0.50 to 0.55 (5c drift).
        book = {"bids": [{"price": "0.54", "size": "100"}], "asks": [{"price": "0.55", "size": "100"}], "hash": "h_exec"}
        result = gate.evaluate(_intent(), _world(), book)
        assert result.approved is False
        assert any(c.name == "price_drift_within_tolerance" and not c.passed for c in result.checks)
        assert result.drift is not None

    def test_toctou_book_hash_mismatch_rejects(self):
        gate = LivePreTradeGate()
        book = {"bids": [{"price": "0.49", "size": "100"}], "asks": [{"price": "0.50", "size": "100"}], "hash": "different"}
        result = gate.evaluate(_intent(), _world(), book)
        assert result.approved is False
        assert any(c.name == "book_hash_matches" and not c.passed for c in result.checks)

    def test_twenty_one_checks_present(self):
        gate = LivePreTradeGate()
        result = gate.evaluate(_intent(), _world())
        assert len(result.checks) >= 21


# ══════════════════════════════════════════════════════════════════════════
# EXECUTION GATEWAYS
# ══════════════════════════════════════════════════════════════════════════


class TestGateways:
    def test_paper_fills_against_book(self):
        gw = PaperExecutionGateway()
        book = {"asks": [{"price": "0.50", "size": "100"}], "bids": [{"price": "0.49", "size": "100"}]}
        result = gw.submit(_intent(), book)
        assert result.submitted is False
        assert result.filled_shares == D("100")
        assert result.vwap == D("0.50")
        assert result.state == IntentState.FILLED

    def test_paper_partial_fill(self):
        gw = PaperExecutionGateway()
        book = {"asks": [{"price": "0.50", "size": "40"}], "bids": []}
        result = gw.submit(_intent(requested_shares=D("100")), book)
        assert result.filled_shares == D("40")
        assert result.state == IntentState.PARTIALLY_FILLED

    def test_paper_no_book_fails(self):
        gw = PaperExecutionGateway()
        result = gw.submit(_intent(), None)
        assert result.state == IntentState.FAILED
        assert result.submitted is False

    def test_shadow_records_never_executes(self, tmp_path):
        log = tmp_path / "shadow.jsonl"
        gw = ShadowExecutionGateway(log)
        result = gw.submit(_intent(), {"asks": [{"price": "0.5", "size": "1"}]})
        assert result.submitted is False
        assert result.state == IntentState.APPROVAL_PENDING
        assert log.exists()
        assert len(gw.records) == 1

    def test_live_ready_never_transmits(self, tmp_path):
        ks = TradingKillSwitch(tmp_path / "kill.json")
        gw = LiveReadyExecutionGateway(LivePreTradeGate(), ks)
        result = gw.submit(_intent(), _world()["book"], _world())
        assert result.submitted is False
        assert result.state == IntentState.APPROVAL_PENDING
        assert "approval" in result.notes

    def test_live_ready_kill_switch_blocks(self, tmp_path):
        ks = TradingKillSwitch(tmp_path / "kill.json")
        ks.activate("MANUAL_OPERATOR")
        gw = LiveReadyExecutionGateway(LivePreTradeGate(), ks)
        result = gw.submit(_intent(), _world()["book"], _world())
        assert result.state == IntentState.REJECTED
        assert result.submitted is False

    def test_live_ready_gate_rejects(self, tmp_path):
        ks = TradingKillSwitch(tmp_path / "kill.json")
        gw = LiveReadyExecutionGateway(LivePreTradeGate(), ks)
        result = gw.submit(_intent(), _world()["book"], _world(market=_Market(active=False)))
        assert result.state == IntentState.REJECTED

    def test_live_ready_no_adapter_after_approval(self, tmp_path):
        ks = TradingKillSwitch(tmp_path / "kill.json")
        gw = LiveReadyExecutionGateway(LivePreTradeGate(), ks)
        result = gw.submit(
            _intent(), _world()["book"], _world(), operator_approval="approved-by-operator"
        )
        # Even with approval, no adapter exists -> still not transmitted.
        assert result.submitted is False
        assert result.state == IntentState.APPROVAL_PENDING

    def test_live_ready_idempotent(self, tmp_path):
        ks = TradingKillSwitch(tmp_path / "kill.json")
        gw = LiveReadyExecutionGateway(LivePreTradeGate(), ks)
        intent = _intent()
        gw.submit(intent, _world()["book"], _world())
        result = gw.submit(intent, _world()["book"], _world())
        assert result.state == IntentState.RECONCILIATION_REQUIRED
        assert "idempotency" in result.notes


# ══════════════════════════════════════════════════════════════════════════
# KILL SWITCH
# ══════════════════════════════════════════════════════════════════════════


class TestKillSwitch:
    def test_activate_persists(self, tmp_path):
        path = tmp_path / "kill.json"
        ks = TradingKillSwitch(path)
        assert ks.active is False
        ks.activate("DRAWDOWN_BREACH", actor="risk")
        # A fresh instance (simulating restart) must still see it active.
        assert TradingKillSwitch(path).active is True
        state = TradingKillSwitch(path).state()
        assert state.reason == "DRAWDOWN_BREACH"
        assert state.activated_by == "risk"

    def test_deactivate_requires_explicit_call(self, tmp_path):
        path = tmp_path / "kill.json"
        ks = TradingKillSwitch(path)
        ks.activate("MANUAL_OPERATOR")
        assert ks.active is True
        ks.deactivate(actor="operator")
        assert ks.active is False

    def test_unknown_reason_rejected(self, tmp_path):
        ks = TradingKillSwitch(tmp_path / "kill.json")
        with pytest.raises(ValueError, match="unknown kill reason"):
            ks.activate("NOT_A_REASON")

    def test_require_clear_raises(self, tmp_path):
        ks = TradingKillSwitch(tmp_path / "kill.json")
        ks.require_clear()  # ok
        ks.activate("CRITICAL_DEPENDENCY_OUTAGE")
        with pytest.raises(RuntimeError, match="kill switch active"):
            ks.require_clear()


# ══════════════════════════════════════════════════════════════════════════
# ORDER RECONCILIATION
# ══════════════════════════════════════════════════════════════════════════


class TestOrderReconciliation:
    def test_unknown_submission_requires_reconciliation(self):
        rec = OrderStateReconciler()
        out = rec.reconcile("i1", "a1", VenueView(), SubmissionOutcome.UNKNOWN)
        assert out.next_state == IntentState.RECONCILIATION_REQUIRED
        assert out.submission_outcome == SubmissionOutcome.UNKNOWN

    def test_definitely_rejected(self):
        rec = OrderStateReconciler()
        out = rec.reconcile("i1", "a1", VenueView(), SubmissionOutcome.REJECTED)
        assert out.next_state == IntentState.REJECTED

    def test_present_at_venue_is_acknowledged(self):
        rec = OrderStateReconciler()
        venue = VenueView(open_orders={"i1": {"shares": 100}})
        out = rec.reconcile("i1", "a1", venue, SubmissionOutcome.UNKNOWN)
        assert out.next_state == IntentState.ACKNOWLEDGED
        assert out.order_exists_at_venue is True

    def test_filled(self):
        rec = OrderStateReconciler()
        venue = VenueView(fills={"i1": [{"shares": 100}]})
        out = rec.reconcile("i1", "a1", venue)
        assert out.next_state == IntentState.FILLED
        assert out.filled_quantity == 100

    def test_cancelled(self):
        rec = OrderStateReconciler()
        venue = VenueView(cancellations={"i1"})
        out = rec.reconcile("i1", "a1", venue)
        assert out.next_state == IntentState.CANCELLED


# ══════════════════════════════════════════════════════════════════════════
# CANCEL POLICY
# ══════════════════════════════════════════════════════════════════════════


class TestCancelPolicy:
    def _order(self, **kw):
        base = dict(
            order_id="o1", token_id="t1", side="BUY",
            limit_price=D("0.5"), shares=D("100"),
            model_probability_at_placement=D("0.6"),
            book_hash_at_placement="h1", placed_at_seconds_ago=10,
        )
        base.update(kw)
        return RestingOrder(**base)

    def test_hold_when_nothing_changed(self):
        decision = CancelPolicy().evaluate(self._order(), D("0.6"), "h1")
        assert decision.action == CancelAction.HOLD

    def test_cancel_on_model_move(self):
        decision = CancelPolicy().evaluate(self._order(), D("0.7"), "h1")
        assert decision.action == CancelAction.CANCEL
        assert "model_probability_moved" in decision.triggers

    def test_cancel_on_book_change(self):
        decision = CancelPolicy().evaluate(self._order(), D("0.6"), "h2")
        assert decision.action == CancelAction.CANCEL

    def test_escalate_on_uncertain_portfolio(self):
        decision = CancelPolicy().evaluate(self._order(), D("0.6"), "h1", portfolio_uncertain=True)
        assert decision.action == CancelAction.ESCALATE

    def test_escalate_on_kill_switch(self):
        decision = CancelPolicy().evaluate(self._order(), D("0.6"), "h1", kill_switch_active=True)
        assert decision.action == CancelAction.ESCALATE

    def test_cancel_on_age(self):
        decision = CancelPolicy(max_age_seconds=5).evaluate(self._order(), D("0.6"), "h1")
        assert decision.action == CancelAction.CANCEL
        assert "order_age_exceeded" in decision.triggers


# ══════════════════════════════════════════════════════════════════════════
# CAPACITY
# ══════════════════════════════════════════════════════════════════════════


class TestCapacity:
    def test_edge_decays_with_size(self):
        # Thin book: only 100 shares at 0.50.
        book = {"asks": [{"price": "0.50", "size": "100"}], "bids": []}
        curve = capacity_curve(
            book, "t1", "BUY", fair_probability=D("0.70"), limit_price=D("0.60"),
        )
        assert len(curve.points) > 0
        # Fill fraction must be <= 1 and non-increasing as size grows.
        fills = [p.fill_fraction for p in curve.points]
        assert all(f <= D("1") for f in fills)
        assert fills[0] >= fills[-1]

    def test_capacity_point_fields(self):
        book = {"asks": [{"price": "0.50", "size": "10000"}], "bids": []}
        curve = capacity_curve(book, "t1", "BUY", D("0.60"), D("0.55"))
        p = curve.points[0]
        assert p.notional_usd == D("100")
        assert p.vwap is not None


# ══════════════════════════════════════════════════════════════════════════
# EXECUTION CALIBRATION
# ══════════════════════════════════════════════════════════════════════════


class TestExecutionCalibration:
    def _obs(self, **kw):
        base = dict(
            market_id="m1",
            predicted_vwap=D("0.50"), observed_vwap=D("0.52"),
            predicted_slippage=D("0.005"), observed_slippage=D("0.02"),
            predicted_fill_fraction=D("1.0"), observed_fill_fraction=D("0.8"),
            predicted_latency_ms=100, observed_latency_ms=250,
            spread=D("0.02"), liquidity=D("10000"), depth=D("1000"),
            volatility=D("0.1"), category="politics", trade_size=D("500"),
            hours_to_resolution=48.0,
        )
        base.update(kw)
        return ExecutionObservation(**base)

    def test_errors_computed(self):
        o = self._obs()
        assert o.vwap_error == D("0.02")
        assert o.slippage_error == D("0.015")
        assert o.fill_error == D("-0.2")
        assert o.latency_error_ms == 150

    def test_systematic_optimism_detected(self):
        report = calibrate([self._obs() for _ in range(10)])
        assert report.total == 10
        assert report.overall.systematic_optimism is True
        assert report.overall.mean_slippage_error > 0

    def test_bucketing(self):
        obs = [self._obs(spread=D("0.005")), self._obs(spread=D("0.05"))]
        report = calibrate(obs)
        assert len(report.by_spread) == 2
        assert len(report.by_category) == 1


# ══════════════════════════════════════════════════════════════════════════
# PROMOTION GATE
# ══════════════════════════════════════════════════════════════════════════


class TestPromotionGate:
    def _all_pass(self, gate: LiveReadinessGate):
        return gate.evaluate(
            delta_brier_positive=True, calibration_acceptable=True, disagreement_stable=True,
            edge_realization_acceptable=True, paper_vs_backtest_discrepancy_ok=True,
            execution_model_calibrated=True, effective_n_adequate=True,
            bootstrap_evidence_positive=True, multiple_testing_aware=True,
            cost_ladder_survives=True, latency_survives=True, parameter_stable=True,
            monte_carlo_survives=True, drawdown_acceptable=True,
            concentration_acceptable=True, exposure_caps_functioning=True,
            forward_shadow_positive=True, forward_paper_positive=True,
            final_holdout_passed=True, collector_fidelity_healthy=True,
            no_integrity_failures=True,
        )

    def test_all_pass_promotes(self):
        report = self._all_pass(LiveReadinessGate())
        assert report.promoted is True
        assert report.failed_count == 0

    def test_single_failure_blocks(self):
        gate = LiveReadinessGate()
        report = gate.evaluate(
            delta_brier_positive=True, calibration_acceptable=True, disagreement_stable=True,
            edge_realization_acceptable=True, paper_vs_backtest_discrepancy_ok=True,
            execution_model_calibrated=False,  # fail
            effective_n_adequate=True, bootstrap_evidence_positive=True,
            multiple_testing_aware=True, cost_ladder_survives=True, latency_survives=True,
            parameter_stable=True, monte_carlo_survives=True, drawdown_acceptable=True,
            concentration_acceptable=True, exposure_caps_functioning=True,
            forward_shadow_positive=True, forward_paper_positive=True,
            final_holdout_passed=True, collector_fidelity_healthy=True,
            no_integrity_failures=True,
        )
        assert report.promoted is False
        assert report.failed_count == 1


# ══════════════════════════════════════════════════════════════════════════
# EXECUTION MODES
# ══════════════════════════════════════════════════════════════════════════


class TestExecutionModes:
    def test_only_live_ready_executes(self):
        assert is_executing(ExecutionMode.LIVE_READY) is True
        assert is_executing(ExecutionMode.PAPER) is False
        assert is_executing(ExecutionMode.SHADOW) is False
        assert is_executing(ExecutionMode.RESEARCH) is False

    def test_banner_is_unambiguous(self):
        assert "MODE:PAPER" in banner(ExecutionMode.PAPER)
        assert "MODE:LIVE_READY" in banner(ExecutionMode.LIVE_READY)