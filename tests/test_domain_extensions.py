"""Tests for new domain models, ensemble, correlation, logging, and performance extensions."""

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from polyalpha.calibration import TemperatureScaling
from polyalpha.correlation import (
    ClusterRegistry,
    CorrelationMatrix,
    EventCluster,
    compute_correlation,
)
from polyalpha.ensemble import EnsembleModel
from polyalpha.performance import (
    calmar_ratio,
    conditional_value_at_risk,
    information_ratio,
    tail_ratio,
    value_at_risk,
)
from polyalpha.signals import MarketSnapshot, Signal

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class MarketSnapshotTests(unittest.TestCase):
    def test_snapshot_creation(self):
        snap = MarketSnapshot(
            market_id="m1",
            condition_id="c1",
            timestamp=NOW,
            question="Test?",
            category="politics",
            yes_bid=D("0.49"),
            yes_ask=D("0.51"),
            no_bid=D("0.48"),
            no_ask=D("0.52"),
            yes_spread=D("0.02"),
            no_spread=D("0.04"),
            yes_mid=D("0.50"),
            no_mid=D("0.50"),
            liquidity=D("10000"),
            volume=D("50000"),
            active=True,
            accepting_orders=True,
        )
        self.assertTrue(snap.is_tradeable)
        self.assertEqual(snap.mid, D("0.50"))
        self.assertEqual(snap.executable_price, D("0.51"))

    def test_snapshot_not_tradeable(self):
        snap = MarketSnapshot(
            market_id="m1",
            condition_id="c1",
            timestamp=NOW,
            question="Test?",
            category="politics",
            yes_bid=None,
            yes_ask=None,
            no_bid=None,
            no_ask=None,
            yes_spread=None,
            no_spread=None,
            yes_mid=None,
            no_mid=None,
            liquidity=None,
            volume=None,
            active=False,
            accepting_orders=False,
        )
        self.assertFalse(snap.is_tradeable)


class SignalTests(unittest.TestCase):
    def test_signal_creation(self):
        sig = Signal(
            market_id="m1",
            token_id="y",
            side="BUY",
            timestamp=NOW,
            fair_probability=D("0.64"),
            conservative_probability=D("0.61"),
            execution_price=D("0.57"),
            fee_per_share=D("0.01"),
            slippage_penalty=D("0.005"),
            uncertainty_penalty=D("0.025"),
            resolution_penalty=D("0.01"),
            stale_data_penalty=D("0"),
            liquidity_penalty=D("0.005"),
            gross_edge=D("0.07"),
            net_edge=D("0.025"),
            confidence=D("0.95"),
            model_version="v1",
            best_bid=D("0.56"),
            best_ask=D("0.57"),
            spread=D("0.01"),
        )
        self.assertTrue(sig.is_positive_edge)
        self.assertTrue(sig.is_executable)

    def test_signal_rejected(self):
        sig = Signal(
            market_id="m1",
            token_id="y",
            side="BUY",
            timestamp=NOW,
            fair_probability=D("0.55"),
            conservative_probability=D("0.52"),
            execution_price=D("0.57"),
            fee_per_share=D("0.01"),
            slippage_penalty=D("0.005"),
            uncertainty_penalty=D("0.025"),
            resolution_penalty=D("0.01"),
            stale_data_penalty=D("0"),
            liquidity_penalty=D("0.005"),
            gross_edge=D("-0.02"),
            net_edge=D("-0.05"),
            confidence=D("0.95"),
            model_version="v1",
            best_bid=D("0.56"),
            best_ask=D("0.57"),
            spread=D("0.01"),
            rejection_reason="insufficient_net_edge",
        )
        self.assertFalse(sig.is_positive_edge)
        self.assertFalse(sig.is_executable)

    def test_signal_to_dict(self):
        sig = Signal(
            market_id="m1",
            token_id="y",
            side="BUY",
            timestamp=NOW,
            fair_probability=D("0.64"),
            conservative_probability=D("0.61"),
            execution_price=D("0.57"),
            fee_per_share=D("0.01"),
            slippage_penalty=D("0.005"),
            uncertainty_penalty=D("0.025"),
            resolution_penalty=D("0.01"),
            stale_data_penalty=D("0"),
            liquidity_penalty=D("0.005"),
            gross_edge=D("0.07"),
            net_edge=D("0.025"),
            confidence=D("0.95"),
            model_version="v1",
            best_bid=D("0.56"),
            best_ask=D("0.57"),
            spread=D("0.01"),
        )
        d = sig.to_dict()
        self.assertEqual(d["market_id"], "m1")
        self.assertEqual(d["fair_probability"], "0.64")


class EnsembleModelTests(unittest.TestCase):
    def _make_model(self, p):
        """Create a stub model that returns a fixed probability."""
        from polyalpha.forecasting import Forecast

        class StubModel:
            def __init__(self, prob):
                self.prob = prob

            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id,
                    timestamp=at,
                    probability=D(str(self.prob)),
                    lower=D(str(max(0, self.prob - 0.05))),
                    upper=D(str(min(1, self.prob + 0.05))),
                    version="stub",
                )

        return StubModel(p)

    def test_ensemble_creation(self):
        m1 = self._make_model(0.6)
        m2 = self._make_model(0.4)
        ens = EnsembleModel(components=[("a", m1, D("0.5")), ("b", m2, D("0.5"))])
        self.assertEqual(len(ens.component_names), 2)

    def test_ensemble_predict(self):
        from polyalpha.domain import Book, Level

        m1 = self._make_model(0.6)
        m2 = self._make_model(0.4)
        ens = EnsembleModel(components=[("a", m1, D("0.5")), ("b", m2, D("0.5"))])
        book = Book(
            token_id="y",
            condition_id="c",
            source_at=NOW,
            received_at=NOW,
            bids=tuple([Level(D("0.49"), D("100"))]),
            asks=tuple([Level(D("0.51"), D("100"))]),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        forecast = ens.predict("m1", book, NOW)
        self.assertEqual(forecast.probability, D("0.5"))  # average of 0.6 and 0.4

    def test_ensemble_weights_must_sum_to_1(self):
        m1 = self._make_model(0.6)
        with self.assertRaises(ValueError):
            EnsembleModel(components=[("a", m1, D("0.3"))])

    def test_ensemble_update_weights(self):
        m1 = self._make_model(0.6)
        m2 = self._make_model(0.4)
        ens = EnsembleModel(components=[("a", m1, D("0.5")), ("b", m2, D("0.5"))])
        ens.update_weights({"a": D("0.7"), "b": D("0.3")})
        self.assertEqual(ens.weight_dict["a"], D("0.7"))


class CorrelationTests(unittest.TestCase):
    def test_compute_correlation_identical(self):
        prices = [D("100"), D("101"), D("102"), D("103"), D("104")]
        corr = compute_correlation(prices, prices)
        self.assertAlmostEqual(float(corr), 1.0, places=4)

    def test_compute_correlation_opposite(self):
        # Use prices where returns are truly opposite
        prices_a = [D("100"), D("102"), D("101"), D("103"), D("102")]
        prices_b = [D("100"), D("98"), D("99"), D("97"), D("98")]
        corr = compute_correlation(prices_a, prices_b)
        self.assertAlmostEqual(float(corr), -1.0, places=2)

    def test_compute_correlation_insufficient_data(self):
        corr = compute_correlation([D("1")], [D("1")])
        self.assertEqual(corr, D(0))

    def test_correlation_matrix(self):
        mat = CorrelationMatrix(tokens=["a", "b", "c"])
        mat.set("a", "b", D("0.8"))
        mat.set("b", "c", D("0.9"))
        self.assertEqual(mat.get("a", "b"), D("0.8"))
        self.assertEqual(mat.get("a", "c"), D(0))  # not set
        self.assertEqual(mat.get("a", "a"), D(1))  # diagonal

    def test_highly_correlated(self):
        mat = CorrelationMatrix(tokens=["a", "b", "c"])
        mat.set("a", "b", D("0.8"))
        mat.set("b", "c", D("0.3"))
        pairs = mat.highly_correlated(D("0.7"))
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], "a")

    def test_event_cluster(self):
        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2"],
            token_ids=["y1", "y2"],
        )
        self.assertEqual(cluster.size, 2)

    def test_cluster_registry(self):
        reg = ClusterRegistry()
        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2"],
            token_ids=["y1", "y2"],
        )
        reg.register(cluster)
        self.assertEqual(reg.get_cluster("m1").cluster_id, "c1")
        self.assertIsNone(reg.get_cluster("unknown"))

    def test_cluster_exposure(self):
        reg = ClusterRegistry()
        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2"],
            token_ids=["y1", "y2"],
        )
        reg.register(cluster)
        positions = {"y1": D("100"), "y2": D("200"), "other": D("50")}
        exposure = reg.cluster_exposure(positions)
        self.assertEqual(exposure["c1"], D("300"))


class TailRiskTests(unittest.TestCase):
    def test_var(self):
        returns = [0.01, 0.02, -0.03, 0.01, -0.05, 0.02, -0.01, 0.03, -0.02, 0.01]
        var = value_at_risk(returns, 0.95)
        self.assertIsNotNone(var)
        self.assertGreater(var, 0)  # VaR is a positive loss number

    def test_cvar(self):
        returns = [0.01, 0.02, -0.03, 0.01, -0.05, 0.02, -0.01, 0.03, -0.02, 0.01]
        cvar = conditional_value_at_risk(returns, 0.95)
        self.assertIsNotNone(cvar)
        self.assertGreaterEqual(cvar, 0)

    def test_tail_ratio(self):
        returns = [
            0.01,
            0.02,
            -0.01,
            0.03,
            -0.02,
            0.04,
            -0.01,
            0.02,
            -0.01,
            0.05,
            0.01,
            0.02,
            -0.01,
            0.03,
            -0.02,
            0.04,
            -0.01,
            0.02,
            -0.01,
            0.05,
        ]
        tr = tail_ratio(returns)
        self.assertIsNotNone(tr)
        self.assertGreater(tr, 0)

    def test_calmar_ratio(self):
        returns = [0.01, 0.02, -0.01, 0.015, -0.005]
        cr = calmar_ratio(returns, 0.05)
        self.assertIsNotNone(cr)
        self.assertGreater(cr, 0)

    def test_information_ratio(self):
        returns = [0.01, 0.02, -0.01, 0.015, -0.005]
        ir = information_ratio(returns, 0.005)
        self.assertIsNotNone(ir)

    def test_insufficient_data(self):
        self.assertIsNone(value_at_risk([0.01], 0.95))
        self.assertIsNone(conditional_value_at_risk([0.01], 0.95))
        self.assertIsNone(tail_ratio([0.01]))
        self.assertIsNone(calmar_ratio([], 0.05))
        self.assertIsNone(information_ratio([0.01]))


class TemperatureScalingTests(unittest.TestCase):
    def test_fit_and_predict(self):
        ts = TemperatureScaling()
        # Perfect calibration: probabilities match outcomes
        probs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
        outcomes = [0, 0, 0, 0, 1, 1, 1, 1, 1]
        ts.fit(probs, outcomes)
        self.assertGreater(ts.temperature, 0)

    def test_predict_before_fit(self):
        ts = TemperatureScaling()
        with self.assertRaises(RuntimeError):
            ts.predict(0.5)

    def test_temperature_scales_toward_05(self):
        ts = TemperatureScaling()
        # Overconfident probabilities: model says 0.9 but only 50% resolve
        probs = [0.9, 0.9, 0.9, 0.1, 0.1, 0.1]
        outcomes = [1, 0, 1, 0, 1, 0]
        ts.fit(probs, outcomes)
        # After scaling, 0.9 should move toward 0.5
        scaled = ts.predict(0.9)
        self.assertLess(scaled, 0.9)
        self.assertGreater(scaled, 0.1)


if __name__ == "__main__":
    unittest.main()
