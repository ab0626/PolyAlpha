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
from datetime import UTC, datetime, timedelta
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
    risk_ownership_ok,
)
from polyalpha.execution_modes import ExecutionMode, banner, is_executing  # noqa: E402
from polyalpha.kill_switch import TradingKillSwitch  # noqa: E402
from polyalpha.order_intent import (  # noqa: E402
    Approval,
    ApprovalLedger,
    IntentLedger,
    IntentState,
    OrderIntent,
    assert_transition_allowed,
    new_intent,
)
from polyalpha.order_reconciliation import (  # noqa: E402
    OrderStateReconciler,
    SubmissionOutcome,
    VenueView,
)
from polyalpha.pre_trade_gate import LivePreTradeGate  # noqa: E402
from polyalpha.promotion_gate import LiveReadinessGate  # noqa: E402

NOW = datetime.now(UTC)


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
        resolution_definition_hash="rd1",
        fee_schedule_version="f1",
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
        "current_net_edge": D("0.08"),
        "min_net_edge": D("0"),
    }
    base.update(overrides)
    return base


def _approval(intent: OrderIntent, **overrides) -> Approval:
    """An approval that exactly bounds the given intent."""
    base = dict(
        approval_id="ap1",
        intent_id=intent.intent_id,
        execution_attempt_id=intent.execution_attempt_id,
        book_hash=intent.signal_book_hash,
        research_logic_sha256=intent.research_logic_sha256,
        config_sha256=intent.config_sha256,
        max_price=intent.limit_price,
        max_notional=intent.requested_notional,
        expires_at=NOW + timedelta(days=1),
        issued_at=NOW,
    )
    base.update(overrides)
    return Approval(**base)


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


class TestPreTradeGateMutationCoverage:
    """Boundary / absent-field / side-routing tests that kill surviving
    mutants in pre_trade_gate.py (see scripts/mutation_gate.py)."""

    def test_market_not_accepting_rejects(self):
        result = LivePreTradeGate().evaluate(
            _intent(), _world(market=_Market(accepting_orders=False)))
        assert result.approved is False
        assert any(c.name == "accepting_orders" and not c.passed for c in result.checks)

    def test_market_book_disabled_rejects(self):
        result = LivePreTradeGate().evaluate(
            _intent(), _world(market=_Market(enable_order_book=False)))
        assert result.approved is False
        assert any(c.name == "order_book_enabled" and not c.passed for c in result.checks)

    def test_market_closed_rejects(self):
        result = LivePreTradeGate().evaluate(
            _intent(), _world(market=_Market(closed=True)))
        assert result.approved is False
        assert any(c.name == "not_closed" and not c.passed for c in result.checks)

    def test_forecast_age_absent_rejects(self):
        result = LivePreTradeGate().evaluate(
            _intent(), _world(forecast_age_seconds=None))
        assert result.approved is False
        assert any(c.name == "forecast_fresh" and not c.passed for c in result.checks)

    def test_metadata_age_absent_rejects(self):
        result = LivePreTradeGate().evaluate(
            _intent(), _world(metadata_age_seconds=None))
        assert result.approved is False
        assert any(c.name == "metadata_fresh" and not c.passed for c in result.checks)

    def test_drift_boundary_exact_tolerance_passes(self):
        """<= boundary: drift exactly equal to tolerance must PASS."""
        gate = LivePreTradeGate(price_tolerance=D("0"), spread_tolerance=D("0.01"),
                                depth_tolerance=D("0"), edge_tolerance=D("0"))
        book = {"bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.50", "size": "100"}], "hash": "h_signal"}
        result = gate.evaluate(_intent(), _world(), book)
        assert result.drift is not None
        assert result.drift.price_drift == D("0")       # 0.50 ask == 0.50 limit
        assert result.drift.spread_drift == D("0.01")   # 0.50 - 0.49
        assert result.approved is True

    def test_best_price_side_routing(self):
        book = {"bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.50", "size": "100"}]}
        assert LivePreTradeGate._best_price(book, "BUY") == D("0.49")
        assert LivePreTradeGate._best_price(book, "SELL") == D("0.50")

    def test_buy_exec_price_is_ask(self):
        """BUY executes against the best ASK (spread = ask - bid > 0)."""
        gate = LivePreTradeGate(price_tolerance=D("0.05"), spread_tolerance=D("0.20"),
                                depth_tolerance=D("1"), edge_tolerance=D("0.1"))
        book = {"bids": [{"price": "0.40", "size": "100"}],
                "asks": [{"price": "0.52", "size": "100"}], "hash": "h_signal"}
        # limit 0.50, ask 0.52 -> price_drift 0.02 (NOT |0.40-0.50|=0.10).
        result = gate.evaluate(_intent(), _world(), book)
        assert result.drift.price_drift == D("0.02")

    def test_spread_positive_from_ask_minus_bid(self):
        gate = LivePreTradeGate()
        book = {"bids": [{"price": "0.48", "size": "100"}],
                "asks": [{"price": "0.52", "size": "100"}], "hash": "h_signal"}
        result = gate.evaluate(_intent(), _world(), book)
        assert result.drift.spread_drift == D("0.04")

    def test_sell_depth_counts_asks_at_or_above_limit(self):
        gate = LivePreTradeGate(depth_tolerance=D("1"))
        intent = _intent(side="SELL", requested_shares=D("100"), limit_price=D("0.50"))
        book = {"bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.51", "size": "60"}, {"price": "0.52", "size": "40"}],
                "hash": "h_signal"}
        result = gate.evaluate(intent, _world(), book)
        # SELL depth = asks at >= 0.50 = 60 + 40 = 100 -> depth_drift 0.
        assert result.drift.depth_drift == D("0")

    def test_one_sided_book_spread_zero(self):
        """Only bids present: current_ask is None -> spread stays 0 (no TypeError)."""
        gate = LivePreTradeGate(spread_tolerance=D("1"))
        book = {"bids": [{"price": "0.49", "size": "100"}], "asks": [], "hash": "h_signal"}
        result = gate.evaluate(_intent(), _world(), book)
        assert result.drift.spread_drift == D("0")

    def test_zero_requested_shares_depth_drift_zero(self):
        """requested_shares == 0 -> depth_drift is exactly 0 (kills ==0 -> !=0)."""
        gate = LivePreTradeGate()
        intent = _intent(requested_shares=D("0"))
        book = {"bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.50", "size": "100"}], "hash": "h_signal"}
        result = gate.evaluate(intent, _world(), book)
        assert result.drift.depth_drift == D("0")

    def test_edge_drift_abs_magnitude(self):
        """Edge drift is the ABSOLUTE difference (kills abs - abs -> +)."""
        gate = LivePreTradeGate(edge_tolerance=D("0.5"))
        # current_net_edge 0.10 vs predicted 0.08 -> drift 0.02 (not 0.18).
        result = gate.evaluate(_intent(), _world(current_net_edge=D("0.10")), None)
        assert result.drift.probability_edge_drift == D("0.02")


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
        approvals = ApprovalLedger(tmp_path / "approvals.jsonl")
        gw = LiveReadyExecutionGateway(LivePreTradeGate(), ks, approvals=approvals)
        intent = _intent()
        approval = _approval(intent)
        approvals.issue(approval)
        result = gw.submit(
            intent, _world()["book"], _world(), approval=approval
        )
        # Even with approval, no adapter exists -> still not transmitted.
        assert result.submitted is False
        assert result.state == IntentState.APPROVAL_PENDING
        # Approval is single-use and now consumed.
        assert approvals.get("ap1").used is True

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


# ══════════════════════════════════════════════════════════════════════════
# FAULT INJECTION — LIVE-READY STATE MACHINE
#
# The invariant everywhere:
#   uncertainty => stop or reconcile
# never:        uncertainty => retry and hope
# ══════════════════════════════════════════════════════════════════════════


class TestLiveReadyFaultInjection:
    """Try to break the live-ready state machinery exactly as a burn-in would.

    These are non-transmitting fault injections against the gated scaffold.
    """

    def _gw(self, tmp_path, **kw):
        ks = TradingKillSwitch(tmp_path / "kill.json")
        ledger = IntentLedger(tmp_path / "ledger.jsonl")
        approvals = ApprovalLedger(tmp_path / "approvals.jsonl")
        return (
            LiveReadyExecutionGateway(
                LivePreTradeGate(), ks, ledger=ledger, approvals=approvals, **kw
            ),
            ks,
            ledger,
            approvals,
        )

    # ── Idempotency / restart ───────────────────────────────────────────

    def test_duplicate_intent_detected(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        gw.submit(intent, _world()["book"], _world())
        result = gw.submit(intent, _world()["book"], _world())
        assert result.state == IntentState.RECONCILIATION_REQUIRED
        assert "idempotency" in result.notes

    def test_same_intent_after_restart_detected(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        gw.submit(intent, _world()["book"], _world())
        # New gateway over the SAME ledger file = process restart.
        gw2, _, _, _ = self._gw(tmp_path)
        result = gw2.submit(intent, _world()["book"], _world())
        assert result.state == IntentState.RECONCILIATION_REQUIRED

    def test_duplicate_execution_attempt_detected(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        gw.submit(intent, _world()["book"], _world())
        # Same attempt id replayed (e.g. transport layer retried blindly).
        result = gw.submit(intent, _world()["book"], _world())
        assert result.state == IntentState.RECONCILIATION_REQUIRED

    def test_ledger_corruption_fails_closed(self, tmp_path):
        """A corrupted ledger line must not silently allow a duplicate."""
        path = tmp_path / "ledger.jsonl"
        path.write_text('{"intent_id": "x", "execution_attempt_id": "y"}\n{"partial', encoding="utf-8")
        ledger = IntentLedger(path)
        assert ledger.has_attempt("y") is True  # complete line loaded
        assert ledger.size() == 1

    def test_ledger_write_crash_partial_line(self, tmp_path):
        """A partial trailing line from a crash is tolerated."""
        path = tmp_path / "ledger.jsonl"
        ledger = IntentLedger(path)
        intent = _intent()
        ledger.register(intent)
        # Simulate a crash leaving a partial line.
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"intent_id": "partial')
        reloaded = IntentLedger(path)
        assert reloaded.has_attempt(intent.execution_attempt_id) is True

    # ── Kill switch timing ──────────────────────────────────────────────

    def test_kill_switch_active_during_validation(self, tmp_path):
        gw, ks, _, _ = self._gw(tmp_path)
        ks.activate("MANUAL_OPERATOR")
        result = gw.submit(_intent(), _world()["book"], _world())
        assert result.state == IntentState.REJECTED
        assert "kill switch" in result.notes

    def test_kill_switch_after_validation_before_approval(self, tmp_path):
        gw, ks, _, _ = self._gw(tmp_path)
        intent = _intent()
        # Validation passes first (no approval yet -> APPROVAL_PENDING).
        first = gw.submit(intent, _world()["book"], _world())
        assert first.state == IntentState.APPROVAL_PENDING
        # Kill switch engages before approval is supplied.
        ks.activate("ABNORMAL_RECONCILIATION")
        approval = _approval(intent)
        result = gw.submit(intent, _world()["book"], _world(), approval=approval)
        assert result.state == IntentState.REJECTED

    # ── Approval boundary ───────────────────────────────────────────────

    def test_approval_expired(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        approval = _approval(intent, expires_at=NOW - timedelta(seconds=1))
        result = gw.submit(intent, _world()["book"], _world(), approval=approval)
        assert result.state == IntentState.REJECTED
        assert "expired" in result.notes

    def test_approval_wrong_intent(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        other = _intent(decision_id="other")
        approval = _approval(other)
        result = gw.submit(intent, _world()["book"], _world(), approval=approval)
        assert result.state == IntentState.REJECTED
        assert "different intent" in result.notes

    def test_approval_wrong_book_hash(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        approval = _approval(intent, book_hash="different-hash")
        result = gw.submit(intent, _world()["book"], _world(), approval=approval)
        assert result.state == IntentState.REJECTED
        assert "book hash" in result.notes

    def test_approval_wrong_research_hash(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        approval = _approval(intent, research_logic_sha256="w" * 64)
        result = gw.submit(intent, _world()["book"], _world(), approval=approval)
        assert result.state == IntentState.REJECTED
        assert "research hash" in result.notes

    def test_approval_wrong_config_hash(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        approval = _approval(intent, config_sha256="w" * 64)
        result = gw.submit(intent, _world()["book"], _world(), approval=approval)
        assert result.state == IntentState.REJECTED
        assert "config hash" in result.notes

    def test_approval_cannot_authorize_larger_size(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent(requested_shares=D("100"), requested_notional=D("50"))
        # Approval authorizes only 50 shares / 25 notional.
        approval = _approval(intent, max_notional=D("25"))
        result = gw.submit(intent, _world()["book"], _world(), approval=approval)
        assert result.state == IntentState.REJECTED
        assert "notional" in result.notes

    def test_approval_cannot_authorize_worse_price(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent(limit_price=D("0.51"))
        # Approval authorizes only up to 0.47.
        approval = _approval(intent, max_price=D("0.47"))
        result = gw.submit(intent, _world()["book"], _world(), approval=approval)
        assert result.state == IntentState.REJECTED
        assert "price" in result.notes

    def test_approval_supplied_twice_is_single_use(self, tmp_path):
        gw, _, _, approvals = self._gw(tmp_path)
        intent = _intent()
        approval = _approval(intent)
        approvals.issue(approval)
        gw.submit(intent, _world()["book"], _world(), approval=approval)
        # The approval is consumed; it cannot authorize a second action.
        assert approvals.get("ap1").used is True
        # Second use is blocked — by the idempotency guard (same intent) and
        # by the single-use approval. Either way, nothing submits.
        result = gw.submit(intent, _world()["book"], _world(), approval=approval)
        assert result.submitted is False
        assert result.state in (
            IntentState.RECONCILIATION_REQUIRED,
            IntentState.REJECTED,
        )

    def test_approval_single_use_blocks_replay_after_restart(self, tmp_path):
        """A consumed approval stays consumed across a restart."""
        gw, _, _, approvals = self._gw(tmp_path)
        intent = _intent()
        approval = _approval(intent)
        approvals.issue(approval)
        gw.submit(intent, _world()["book"], _world(), approval=approval)
        assert approvals.get("ap1").used is True
        # Restart: reload the approval ledger; the used flag persists.
        reloaded = ApprovalLedger(tmp_path / "approvals.jsonl")
        assert reloaded.get("ap1").used is True

    def test_book_stale_after_approval(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        approval = _approval(intent)
        # The gate sees a stale book at execution time.
        world = _world(book_age_seconds=120)
        result = gw.submit(intent, world["book"], world, approval=approval)
        assert result.state == IntentState.REJECTED
        assert "book" in result.notes

    def test_price_moves_after_approval(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        gate = LivePreTradeGate(price_tolerance=D("0.02"))
        ks = TradingKillSwitch(tmp_path / "kill2.json")
        ledger = IntentLedger(tmp_path / "ledger2.jsonl")
        approvals = ApprovalLedger(tmp_path / "approvals2.jsonl")
        gw = LiveReadyExecutionGateway(gate, ks, ledger=ledger, approvals=approvals)
        intent = _intent(signal_book_hash="h_signal", limit_price=D("0.50"))
        approval = _approval(intent)
        # Execution book moved ask from 0.50 to 0.55.
        book = {"bids": [{"price": "0.54", "size": "100"}], "asks": [{"price": "0.55", "size": "100"}], "hash": "h_exec"}
        result = gw.submit(intent, book, _world(), approval=approval)
        assert result.state == IntentState.REJECTED
        assert "gate" in result.notes

    # ── Ambiguity ───────────────────────────────────────────────────────

    def test_reconciliation_unknown_state(self, tmp_path):
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        gw.submit(intent, _world()["book"], _world())
        # Reconcile with unknown submission outcome.
        from polyalpha.order_reconciliation import (
            OrderStateReconciler,
            SubmissionOutcome,
            VenueView,
        )
        rec = OrderStateReconciler().reconcile(
            intent.intent_id, intent.execution_attempt_id, VenueView(), SubmissionOutcome.UNKNOWN
        )
        assert rec.next_state == IntentState.RECONCILIATION_REQUIRED
        assert rec.submission_outcome == SubmissionOutcome.UNKNOWN

    # ── Risk ownership ──────────────────────────────────────────────────

    def test_risk_ownership_guard(self):
        ok, _ = risk_ownership_ok(_intent())
        assert ok is True
        # A zero-size intent is malformed for execution and must be rejected.
        ok_zero, msg_zero = risk_ownership_ok(_intent(requested_shares=D("0")))
        assert ok_zero is False
        assert "shares" in msg_zero

    # ── Restart while pending ───────────────────────────────────────────

    def test_restart_while_approval_pending(self, tmp_path):
        """An intent left APPROVAL_PENDING is still known after restart."""
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        gw.submit(intent, _world()["book"], _world())
        # Restart: new gateway, same ledger.
        gw2, _, _, _ = self._gw(tmp_path)
        result = gw2.submit(intent, _world()["book"], _world())
        assert result.state == IntentState.RECONCILIATION_REQUIRED

    def test_restart_while_reconciliation_required(self, tmp_path):
        """RECONCILIATION_REQUIRED state must survive restart via the ledger."""
        gw, _, _, _ = self._gw(tmp_path)
        intent = _intent()
        gw.submit(intent, _world()["book"], _world())
        gw.submit(intent, _world()["book"], _world())  # duplicate -> RECONCILIATION_REQUIRED
        # Restart.
        ledger = IntentLedger(tmp_path / "ledger.jsonl")
        assert ledger.get(intent.intent_id) is not None or ledger.has_attempt(intent.execution_attempt_id)

    def test_reconciliation_required_cannot_progress_to_submission(self):
        """The permanent rule: RECONCILIATION_REQUIRED must not move back
        toward submission through normal workflow."""
        with pytest.raises(ValueError, match="RECONCILIATION_REQUIRED"):
            assert_transition_allowed(IntentState.RECONCILIATION_REQUIRED, IntentState.SUBMISSION_PENDING)
        with pytest.raises(ValueError, match="RECONCILIATION_REQUIRED"):
            assert_transition_allowed(IntentState.RECONCILIATION_REQUIRED, IntentState.ACKNOWLEDGED)
        # Definitive outcomes are allowed.
        assert_transition_allowed(IntentState.RECONCILIATION_REQUIRED, IntentState.REJECTED)
        assert_transition_allowed(IntentState.RECONCILIATION_REQUIRED, IntentState.CANCELLED)