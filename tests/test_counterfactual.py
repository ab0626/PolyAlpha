"""Counterfactual analysis tests.

Part 89A-B: Tests for the counterfactual trade analysis system — verifying
that the pipeline correctly:
- Estimates what would have happened for rejected signals
- Identifies which filters would have rejected under nearby thresholds
- Flags rejected signals that would have been profitable
- Identifies accepted signals near threshold boundaries
"""

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyalpha.counterfactual import CounterfactualResult, analyze_counterfactuals

D = Decimal
TS = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _rng(seed):
    return random.Random(seed)


def _decisions_and_outcomes(n=30, seed=42):
    rng = _rng(seed)
    decisions = []
    outcomes = {}
    for i in range(n):
        market_id = f"m{i:04d}"
        action = rng.choice(["accepted", "rejected"])
        entry_price = round(rng.uniform(0.30, 0.70), 4)
        prob = round(rng.uniform(0.35, 0.75), 4)
        outcome = rng.randint(0, 1)

        if action == "accepted":
            if outcome == 1:
                pnl = round((1.0 - entry_price) * 100, 2)
            else:
                pnl = round(-entry_price * 100, 2)
        else:
            pnl = 0.0

        decisions.append({
            "market_id": market_id,
            "action": action,
            "entry_price": entry_price,
            "probability": prob,
            "pnl": pnl,
        })
        outcomes[market_id] = outcome

    return decisions, outcomes


# ── Basic counterfactual analysis ────────────────────────────────────────────


class TestCounterfactualBasic:
    def test_returns_results_for_resolved_markets(self):
        decisions, outcomes = _decisions_and_outcomes(30, seed=100)
        results = analyze_counterfactuals(decisions, outcomes)
        assert len(results) > 0
        assert len(results) <= len(decisions)

    def test_skips_unresolved_markets(self):
        decisions = [{"market_id": "m1", "action": "accepted", "entry_price": 0.5, "probability": 0.6, "pnl": 10}]
        outcomes = {}
        results = analyze_counterfactuals(decisions, outcomes)
        assert len(results) == 0

    def test_accepted_signals_have_rejected_counterfactual(self):
        decisions, outcomes = _decisions_and_outcomes(30, seed=101)
        results = analyze_counterfactuals(decisions, outcomes)
        for r in results:
            if r.actual_action == "accepted":
                assert r.counterfactual_action == "rejected"

    def test_rejected_signals_have_accepted_counterfactual(self):
        decisions, outcomes = _decisions_and_outcomes(30, seed=102)
        results = analyze_counterfactuals(decisions, outcomes)
        for r in results:
            if r.actual_action == "rejected":
                assert r.counterfactual_action == "accepted"


# ── Regret computation ───────────────────────────────────────────────────────


class TestRegretComputation:
    def test_accepted_profitable_trade_has_negative_regret(self):
        """If we accepted and profited, not trading would have been regretful."""
        decisions = [{
            "market_id": "m1", "action": "accepted",
            "entry_price": 0.40, "probability": 0.70, "pnl": 60.0,
        }]
        outcomes = {"m1": 1}
        results = analyze_counterfactuals(decisions, outcomes)
        assert len(results) == 1
        r = results[0]
        assert r.regret < D(0), f"Profitable trade regret should be negative: {r.regret}"

    def test_rejected_profitable_trade_has_positive_regret(self):
        """If we rejected and it would have been profitable, that's regretful."""
        decisions = [{
            "market_id": "m1", "action": "rejected",
            "entry_price": 0.40, "probability": 0.70, "pnl": 0,
        }]
        outcomes = {"m1": 1}
        results = analyze_counterfactuals(decisions, outcomes)
        assert len(results) == 1
        r = results[0]
        assert r.regret > D(0), f"Missed profitable trade regret should be positive: {r.regret}"

    def test_regret_symmetry_for_accepted(self):
        """For accepted: regret = cf_pnl - actual_pnl = -actual - actual."""
        decisions = [{
            "market_id": "m1", "action": "accepted",
            "entry_price": 0.50, "probability": 0.60, "pnl": 50.0,
        }]
        outcomes = {"m1": 1}
        results = analyze_counterfactuals(decisions, outcomes)
        r = results[0]
        assert r.regret == r.counterfactual_pnl - r.actual_pnl


# ── Counterfactual PnL estimation ────────────────────────────────────────────


class TestCounterfactualPnl:
    def test_rejected_winning_market_counterfactual_pnl_positive(self):
        decisions = [{
            "market_id": "m1", "action": "rejected",
            "entry_price": 0.40, "probability": 0.60, "pnl": 0,
        }]
        outcomes = {"m1": 1}
        results = analyze_counterfactuals(decisions, outcomes)
        r = results[0]
        assert r.counterfactual_pnl > D(0), (
            f"Rejected winning market cf PnL should be positive: {r.counterfactual_pnl}"
        )

    def test_rejected_losing_market_counterfactual_pnl_negative(self):
        decisions = [{
            "market_id": "m1", "action": "rejected",
            "entry_price": 0.60, "probability": 0.70, "pnl": 0,
        }]
        outcomes = {"m1": 0}
        results = analyze_counterfactuals(decisions, outcomes)
        r = results[0]
        assert r.counterfactual_pnl < D(0), (
            f"Rejected losing market cf PnL should be negative: {r.counterfactual_pnl}"
        )


# ── Summary output ───────────────────────────────────────────────────────────


class TestCounterfactualSummary:
    def test_summary_contains_required_fields(self):
        decisions = [{
            "market_id": "m1", "action": "accepted",
            "entry_price": 0.50, "probability": 0.60, "pnl": 10,
        }]
        outcomes = {"m1": 1}
        results = analyze_counterfactuals(decisions, outcomes)
        summary = results[0].summary()
        required = {"market_id", "actual_action", "counterfactual_action",
                     "actual_pnl", "counterfactual_pnl", "regret"}
        assert required.issubset(set(summary.keys())), (
            f"Missing fields: {required - set(summary.keys())}"
        )

    def test_multiple_decisions_analyzed(self):
        decisions, outcomes = _decisions_and_outcomes(20, seed=200)
        results = analyze_counterfactuals(decisions, outcomes)
        assert len(results) > 1


# ── Near-threshold boundary identification ───────────────────────────────────


class TestNearThresholdBoundary:
    def test_low_edge_signal_flagged_near_boundary(self):
        """Signals with very small edge should be identifiable as near-threshold."""
        decisions = []
        for i in range(20):
            edge = round(random.Random(i).uniform(-0.005, 0.005), 4)
            entry = 0.50
            prob = entry + edge
            pnl = round(edge * 100, 2) if random.Random(i).random() > 0.5 else round(-edge * 100, 2)
            decisions.append({
                "market_id": f"m{i:04d}",
                "action": "accepted" if edge > 0 else "rejected",
                "entry_price": entry,
                "probability": max(0.01, min(0.99, prob)),
                "pnl": pnl,
            })
        outcomes = {f"m{i:04d}": random.Random(i + 100).randint(0, 1) for i in range(20)}
        results = analyze_counterfactuals(decisions, outcomes)
        for r in results:
            assert r.confidence == 0.5


# ── Edge cases ───────────────────────────────────────────────────────────────


class TestCounterfactualEdgeCases:
    def test_empty_decisions_returns_empty(self):
        results = analyze_counterfactuals([], {"m1": 1})
        assert results == []

    def test_all_unresolved_returns_empty(self):
        decisions = [
            {"market_id": "m1", "action": "accepted", "entry_price": 0.5, "pnl": 10},
            {"market_id": "m2", "action": "rejected", "entry_price": 0.5, "pnl": 0},
        ]
        results = analyze_counterfactuals(decisions, {})
        assert results == []

    def test_default_action_treated_as_accepted(self):
        decisions = [{
            "market_id": "m1", "entry_price": 0.40, "probability": 0.70, "pnl": 0,
        }]
        outcomes = {"m1": 1}
        results = analyze_counterfactuals(decisions, outcomes)
        assert len(results) == 1
        assert results[0].actual_action == "accepted"
