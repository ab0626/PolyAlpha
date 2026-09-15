"""Tests for position sizing, anomaly detection, quality scoring, performance metrics, and monitoring."""

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from polyalpha.anomaly import Anomaly, AnomalyDetector
from polyalpha.domain import Book, Level, Market
from polyalpha.monitoring import CalibrationDriftDetector, FeatureDriftDetector
from polyalpha.performance import (
    alpha_decay,
    brier_score,
    drawdown_series,
    edge_metrics,
    holding_period_stats,
    log_loss,
    profit_factor,
    sortino_ratio,
    trade_metrics,
)
from polyalpha.quality import Filter, market_quality_score, rank_markets
from polyalpha.sizing import (
    SizingResult,
    constrained_sizing,
    fixed_fractional_sizing,
    kelly_sizing,
)

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_book(bids=None, asks=None):
    if bids is None:
        bids = [("0.49", "100")]
    if asks is None:
        asks = [("0.51", "150"), ("0.52", "300")]
    return Book(
        token_id="y",
        condition_id="c",
        source_at=NOW,
        received_at=NOW,
        bids=tuple(Level(D(p), D(s)) for p, s in bids),
        asks=tuple(Level(D(p), D(s)) for p, s in asks),
        tick_size=D("0.01"),
        min_order_size=D(1),
        source_hash="test",
    )


def make_market(**kwargs):
    defaults = dict(
        market_id="m",
        condition_id="c",
        event_ids=("e",),
        question="Test?",
        description="",
        resolution_source="test",
        deadline=datetime(2027, 1, 1, tzinfo=UTC),
        active=True,
        closed=False,
        accepting_orders=True,
        enable_order_book=True,
        liquidity=D("6000"),
        volume=D("30000"),
        fees_enabled=True,
        fee_parameters_json='{"rate":0.04,"exponent":1,"takerOnly":true}',
        yes_token_id="y",
        no_token_id="n",
        received_at=NOW,
        category="politics",
    )
    defaults.update(kwargs)
    return Market(**defaults)


class SizingTests(unittest.TestCase):
    def test_fixed_fractional(self):
        size = fixed_fractional_sizing(D("10000"), D("0.005"))
        self.assertEqual(size, D("50"))

    def test_fixed_fractional_capped(self):
        size = fixed_fractional_sizing(D("10000"), D("0.005"), max_shares=D("30"))
        self.assertEqual(size, D("30"))

    def test_kelly_sizing(self):
        result = kelly_sizing(D("0.7"), D("0.5"), D("10000"))
        self.assertIsInstance(result, SizingResult)
        self.assertGreater(result.shares, 0)
        self.assertGreater(result.kelly_full, 0)

    def test_kelly_sizing_no_edge(self):
        result = kelly_sizing(D("0.5"), D("0.5"), D("10000"))
        self.assertEqual(result.shares, D(0))

    def test_constrained_sizing(self):
        result = constrained_sizing(
            equity=D("10000"),
            risk_budget=D("50"),
            price=D("0.50"),
        )
        self.assertIsInstance(result, SizingResult)
        self.assertGreater(result.shares, 0)

    def test_constrained_sizing_by_risk_budget(self):
        result = constrained_sizing(
            equity=D("10000"),
            risk_budget=D("10"),
            price=D("0.50"),
            normal_fraction=D("0.01"),
        )
        # normal would be 100, but budget caps at 10
        self.assertEqual(result.capped_by, "risk_budget")


class AnomalyTests(unittest.TestCase):
    def test_no_anomalies(self):
        detector = AnomalyDetector()
        book = make_book()
        features = {"spread": D("0.02"), "microprice": D("0.50"), "midpoint": D("0.50")}
        anomalies = detector.detect(book, features)
        self.assertEqual(len(anomalies), 0)

    def test_wide_spread_anomaly(self):
        detector = AnomalyDetector(spread_z_threshold=D("0.5"))
        # Seed history with varying spreads so std > 0
        for s in [
            "0.010",
            "0.012",
            "0.008",
            "0.011",
            "0.009",
            "0.010",
            "0.013",
            "0.007",
            "0.012",
            "0.010",
            "0.011",
            "0.009",
            "0.010",
            "0.012",
            "0.008",
            "0.011",
            "0.010",
            "0.009",
            "0.013",
            "0.010",
        ]:
            detector.spread_history.append(D(s))
        # Book with very wide spread (0.11 vs mean ~0.01)
        book = Book(
            token_id="y",
            condition_id="c",
            source_at=NOW,
            received_at=NOW,
            bids=tuple([Level(D("0.49"), D("100"))]),
            asks=tuple([Level(D("0.60"), D("100"))]),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        features = {"spread": D("0.11"), "microprice": D("0.50"), "midpoint": D("0.50")}
        anomalies = detector.detect(book, features)
        spread_anomalies = [a for a in anomalies if a.kind == "spread_widening"]
        self.assertGreater(len(spread_anomalies), 0)

    def test_no_bids_anomaly(self):
        detector = AnomalyDetector()
        book = make_book(bids=[], asks=[("0.51", "100")])
        anomalies = detector.detect(book, {})
        no_bid = [a for a in anomalies if a.kind == "no_bids"]
        self.assertEqual(len(no_bid), 1)
        self.assertEqual(no_bid[0].severity, D("0.9"))

    def test_liquidity_imbalance_anomaly(self):
        detector = AnomalyDetector(imbalance_threshold=D("0.5"))
        book = make_book(
            bids=[("0.49", "1000")],
            asks=[("0.51", "1")],
        )
        anomalies = detector.detect(book, {})
        imb = [a for a in anomalies if a.kind == "liquidity_imbalance"]
        self.assertGreater(len(imb), 0)

    def test_uncertainty_multiplier(self):
        detector = AnomalyDetector()
        multiplier_no = detector.uncertainty_multiplier([])
        multiplier_yes = detector.uncertainty_multiplier([Anomaly("test", D("0.5"), "test", {})])
        self.assertEqual(multiplier_no, D(1))
        self.assertGreater(multiplier_yes, D(1))


class QualityScoringTests(unittest.TestCase):
    def test_market_quality_score(self):
        market = make_market()
        book = make_book()
        score = market_quality_score(market, book)
        self.assertIsInstance(score, Decimal)
        self.assertGreater(score, 0)

    def test_market_quality_no_book(self):
        market = make_market()
        score = market_quality_score(market, None)
        self.assertGreater(score, 0)

    def test_rank_markets(self):
        m1 = make_market(market_id="m1", liquidity=D("10000"))
        m2 = make_market(market_id="m2", liquidity=D("5000"))
        ranked = rank_markets([(m1, None), (m2, None)])
        self.assertEqual(len(ranked), 2)
        # m1 should rank higher (more liquidity)
        self.assertEqual(ranked[0][0].market_id, "m1")

    def test_quality_filter_still_works(self):
        f = Filter()
        market = make_market()
        reasons = f.market_reasons(market, NOW)
        self.assertEqual(reasons, [])


class PerformanceTests(unittest.TestCase):
    def test_brier_score(self):
        score = brier_score([0.7, 0.3], [1, 0])
        # (0.7-1)^2 + (0.3-0)^2 = 0.09 + 0.09 = 0.18 / 2 = 0.09
        self.assertAlmostEqual(score, 0.09, places=4)

    def test_log_loss(self):
        ll = log_loss([0.7, 0.3], [1, 0])
        self.assertGreater(ll, 0)

    def test_profit_factor(self):
        pf = profit_factor(100, -50)
        self.assertEqual(pf, 2.0)

    def test_profit_factor_zero_loss(self):
        pf = profit_factor(100, 0)
        self.assertEqual(pf, float("inf"))

    def test_drawdown_series(self):
        dd = drawdown_series([100, 110, 105, 108, 95])
        self.assertEqual(len(dd), 5)
        self.assertEqual(dd[0], 0)
        self.assertEqual(dd[1], 0)
        # 105/110 = 0.9545..., dd = 0.0454...
        self.assertAlmostEqual(dd[2], 1 - 105 / 110, places=4)
        # 95/110 = 0.8636..., dd = 0.1363...
        self.assertAlmostEqual(dd[4], 1 - 95 / 110, places=4)

    def test_trade_metrics(self):
        fills = [
            {"side": "BUY", "notional": "50", "fees": "1", "vwap": "0.50", "shares": "100"},
            {"side": "SELL", "notional": "55", "fees": "1", "vwap": "0.55", "shares": "100"},
        ]
        result = trade_metrics(fills)
        self.assertEqual(result["total_trades"], 2)
        self.assertEqual(result["total_buys"], 1)
        self.assertEqual(result["total_sells"], 1)

    def test_trade_metrics_empty(self):
        result = trade_metrics([])
        self.assertEqual(result["total_trades"], 0)

    def test_edge_metrics(self):
        decisions = [
            {"reason": "queued"},
            {"reason": "queued"},
            {"reason": "filled", "net_edge": "0.03"},
            {"reason": "rejected"},
        ]
        result = edge_metrics(decisions)
        self.assertEqual(result["total_signals"], 2)
        self.assertEqual(result["total_fills"], 1)
        self.assertEqual(result["total_rejections"], 1)
        self.assertEqual(result["fill_rate"], 0.5)

    def test_sortino_ratio(self):
        returns = [0.01, 0.02, -0.01, 0.015, -0.005]
        sr = sortino_ratio(returns, periods_per_year=365)
        self.assertIsNotNone(sr)
        self.assertIsInstance(sr, float)

    def test_holding_period_stats(self):
        fills = [
            {"token_id": "y", "side": "BUY", "filled_at": "2026-01-01T00:00:00+00:00"},
            {"token_id": "y", "side": "SELL", "filled_at": "2026-01-01T01:00:00+00:00"},
        ]
        result = holding_period_stats(fills)
        self.assertEqual(result["sample_size"], 1)
        self.assertEqual(result["average_holding_seconds"], 3600)

    def test_alpha_decay(self):
        from datetime import timedelta

        signal_time = NOW
        observations = [
            (NOW + timedelta(seconds=30), D("0.51")),
            (NOW + timedelta(seconds=120), D("0.53")),
        ]
        result = alpha_decay(signal_time, D("0.50"), observations, [60, 300])
        self.assertIn("60", result)
        self.assertIn("300", result)
        self.assertIsNotNone(result["60"])
        self.assertIsNone(result["300"])  # no observation at 300s


class CalibrationDriftTests(unittest.TestCase):
    def test_no_drift_with_baseline(self):
        detector = CalibrationDriftDetector(window_size=50, alert_threshold=0.05, min_samples=10)
        detector.set_baseline(0.25)
        for _ in range(20):
            detector.update(0.26, 0.02)
        result = detector.check_drift()
        self.assertFalse(result["drift_detected"])

    def test_drift_detected(self):
        detector = CalibrationDriftDetector(window_size=50, alert_threshold=0.05, min_samples=10)
        detector.set_baseline(0.25)
        for _ in range(20):
            detector.update(0.35, 0.05)  # significantly worse
        result = detector.check_drift()
        self.assertTrue(result["drift_detected"])

    def test_insufficient_samples(self):
        detector = CalibrationDriftDetector(min_samples=10)
        detector.update(0.25)
        result = detector.check_drift()
        self.assertFalse(result["drift_detected"])
        self.assertEqual(result["reason"], "insufficient_samples")

    def test_variance_without_baseline(self):
        detector = CalibrationDriftDetector(window_size=50, min_samples=10)
        # High variance should trigger drift
        for i in range(20):
            detector.update(0.2 + (i % 2) * 0.3)  # alternates between 0.2 and 0.5
        result = detector.check_drift()
        self.assertTrue(result["drift_detected"])


class FeatureDriftTests(unittest.TestCase):
    def test_no_drift(self):
        detector = FeatureDriftDetector()
        detector.set_reference("spread", [0.01] * 100)
        for _ in range(100):
            detector.update("spread", 0.01)
        result = detector.check_all(threshold=0.2)
        self.assertFalse(result["any_drift"])

    def test_drift_detected(self):
        detector = FeatureDriftDetector()
        detector.set_reference("spread", [0.01] * 100)
        for _ in range(100):
            detector.update("spread", 0.05)  # very different
        result = detector.check_all(threshold=0.2)
        self.assertTrue(result["any_drift"])

    def test_psi_calculation(self):
        detector = FeatureDriftDetector()
        detector.set_reference(
            "spread", [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]
        )
        for _ in range(10):
            detector.update("spread", 0.01)
        psi = detector.psi("spread")
        self.assertIsNotNone(psi)
        self.assertIsInstance(psi, float)

    def test_insufficient_data(self):
        detector = FeatureDriftDetector()
        detector.set_reference("spread", [0.01] * 5)  # less than buckets
        for _ in range(5):
            detector.update("spread", 0.01)
        psi = detector.psi("spread")
        self.assertIsNone(psi)  # insufficient data for 10 buckets

    def test_psi_identical_distributions(self):
        detector = FeatureDriftDetector()
        detector.set_reference(
            "spread", [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]
        )
        for v in [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]:
            detector.update("spread", v)
        psi = detector.psi("spread")
        self.assertAlmostEqual(psi, 0.0, places=4)  # identical distributions -> PSI ~0

    def test_no_reference_returns_none(self):
        detector = FeatureDriftDetector()
        detector.update("spread", 0.01)
        psi = detector.psi("spread")
        self.assertIsNone(psi)  # no reference set

    def test_check_all_empty(self):
        detector = FeatureDriftDetector()
        result = detector.check_all()
        self.assertFalse(result["any_drift"])
        self.assertEqual(len(result["features"]), 0)


if __name__ == "__main__":
    unittest.main()
