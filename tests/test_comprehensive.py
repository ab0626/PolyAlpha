"""Comprehensive tests for correlation, ensemble, tail risk, and integration."""

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from polyalpha.correlation import (
    ClusterRegistry,
    CorrelationMatrix,
    EventCluster,
    compute_correlation,
    rolling_correlation,
)
from polyalpha.ensemble import EnsembleModel
from polyalpha.performance import (
    calmar_ratio,
    conditional_value_at_risk,
    information_ratio,
    tail_ratio,
    value_at_risk,
)
from polyalpha.portfolio import Portfolio
from polyalpha.risk import Risk

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class CorrelationComprehensiveTests(unittest.TestCase):
    def test_compute_correlation_perfect_positive(self):
        prices = [D("100"), D("101"), D("102"), D("103"), D("104"), D("105")]
        corr = compute_correlation(prices, prices)
        self.assertAlmostEqual(float(corr), 1.0, places=4)

    def test_compute_correlation_uncorrelated(self):
        prices_a = [D("100"), D("100"), D("100"), D("100"), D("100")]
        prices_b = [D("50"), D("60"), D("50"), D("60"), D("50")]
        corr = compute_correlation(prices_a, prices_b)
        # Constant series has zero std, so correlation is 0
        self.assertEqual(corr, D(0))

    def test_compute_correlation_minimum_data(self):
        corr = compute_correlation([D("1"), D("2")], [D("1"), D("2")])
        self.assertEqual(corr, D(0))  # less than 3 points

    def test_rolling_correlation(self):
        prices_a = [D(str(100 + i)) for i in range(20)]
        prices_b = [D(str(100 + i)) for i in range(20)]
        result = rolling_correlation(prices_a, prices_b, window=5)
        self.assertEqual(len(result), 16)  # 20 - 5 + 1
        for corr in result:
            self.assertAlmostEqual(float(corr), 1.0, places=4)

    def test_correlation_matrix_bounds(self):
        mat = CorrelationMatrix(tokens=["a", "b"])
        with self.assertRaises(ValueError):
            mat.set("a", "b", D("1.5"))  # out of bounds
        with self.assertRaises(ValueError):
            mat.set("a", "b", D("-1.5"))  # out of bounds

    def test_event_cluster_avg_correlation(self):
        mat = CorrelationMatrix(tokens=["y1", "y2", "y3"])
        mat.set("y1", "y2", D("0.8"))
        mat.set("y1", "y3", D("0.6"))
        mat.set("y2", "y3", D("0.7"))
        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2", "m3"],
            token_ids=["y1", "y2", "y3"],
            correlation_matrix=mat,
        )
        avg = cluster.avg_internal_correlation
        # avg of |0.8|, |0.6|, |0.7| = 0.7
        self.assertAlmostEqual(float(avg), 0.7, places=4)

    def test_cluster_registry_exposure(self):
        reg = ClusterRegistry()
        c1 = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1"],
            token_ids=["y1"],
        )
        c2 = EventCluster(
            cluster_id="c2",
            event_id="e2",
            category="sports",
            market_ids=["m2"],
            token_ids=["y2"],
        )
        reg.register(c1)
        reg.register(c2)
        positions = {"y1": D("100"), "y2": D("200"), "y3": D("50")}
        exposure = reg.cluster_exposure(positions)
        self.assertEqual(exposure["c1"], D("100"))
        self.assertEqual(exposure["c2"], D("200"))
        self.assertNotIn("unknown", exposure)

    def test_correlation_adjusted_exposure(self):
        mat = CorrelationMatrix(tokens=["y1", "y2"])
        mat.set("y1", "y2", D("0.9"))
        c1 = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2"],
            token_ids=["y1", "y2"],
            correlation_matrix=mat,
        )
        reg = ClusterRegistry()
        reg.register(c1)
        positions = {"y1": D("100"), "y2": D("100")}
        adjusted = reg.correlation_adjusted_exposure(positions)
        # Raw exposure = 200, avg correlation = 0.9, scale = 1 + 0.9/2 = 1.45
        self.assertGreater(adjusted["c1"], D("200"))


class EnsembleComprehensiveTests(unittest.TestCase):
    def _stub(self, p):
        from polyalpha.forecasting import Forecast

        class M:
            def __init__(self, prob):
                self.p = prob

            def predict(self, mid, book, at):
                return Forecast(
                    market_id=mid,
                    timestamp=at,
                    probability=D(str(self.p)),
                    lower=D(str(max(0, self.p - 0.05))),
                    upper=D(str(min(1, self.p + 0.05))),
                    version="stub",
                )

        return M(p)

    def test_ensemble_single_component(self):
        ens = EnsembleModel(components=[("a", self._stub(0.6), D(1))])
        from polyalpha.domain import Book, Level

        book = Book(
            "y",
            "c",
            NOW,
            NOW,
            (Level(D("0.49"), D(100)),),
            (Level(D("0.51"), D(100)),),
            D("0.01"),
            D(1),
            "h",
        )
        f = ens.predict("m", book, NOW)
        self.assertEqual(f.probability, D("0.6"))

    def test_ensemble_weighted_average(self):
        ens = EnsembleModel(
            components=[
                ("a", self._stub(0.6), D("0.7")),
                ("b", self._stub(0.4), D("0.3")),
            ]
        )
        from polyalpha.domain import Book, Level

        book = Book(
            "y",
            "c",
            NOW,
            NOW,
            (Level(D("0.49"), D(100)),),
            (Level(D("0.51"), D(100)),),
            D("0.01"),
            D(1),
            "h",
        )
        f = ens.predict("m", book, NOW)
        # 0.6*0.7 + 0.4*0.3 = 0.42 + 0.12 = 0.54
        self.assertEqual(f.probability, D("0.54"))

    def test_ensemble_negative_weight_rejected(self):
        with self.assertRaises(ValueError):
            EnsembleModel(components=[("a", self._stub(0.6), D("-0.1"))])


class TailRiskComprehensiveTests(unittest.TestCase):
    def test_var_all_positive(self):
        returns = [0.01, 0.02, 0.03, 0.04, 0.05, 0.01, 0.02, 0.03, 0.04, 0.05]
        var = value_at_risk(returns, 0.95)
        self.assertIsNotNone(var)
        # VaR = -percentile(returns, 5%). All positive => VaR <= 0 (no loss risk)
        self.assertLessEqual(var, 0)

    def test_var_mixed(self):
        returns = [0.01, -0.02, 0.03, -0.04, 0.05, -0.06, 0.07, -0.08, 0.09, -0.10]
        var = value_at_risk(returns, 0.95)
        self.assertIsNotNone(var)
        self.assertGreater(var, 0)

    def test_cvar_greater_than_var(self):
        returns = [0.01, -0.02, 0.03, -0.04, 0.05, -0.06, 0.07, -0.08, 0.09, -0.10]
        var = value_at_risk(returns, 0.95)
        cvar = conditional_value_at_risk(returns, 0.95)
        self.assertIsNotNone(var)
        self.assertIsNotNone(cvar)
        self.assertGreaterEqual(cvar, var)

    def test_tail_ratio_symmetric(self):
        returns = [
            0.01,
            -0.01,
            0.02,
            -0.02,
            0.03,
            -0.03,
            0.04,
            -0.04,
            0.05,
            -0.05,
            0.01,
            -0.01,
            0.02,
            -0.02,
            0.03,
            -0.03,
            0.04,
            -0.04,
            0.05,
            -0.05,
        ]
        tr = tail_ratio(returns)
        self.assertIsNotNone(tr)
        self.assertAlmostEqual(tr, 1.0, places=1)

    def test_calmar_ratio_positive_returns(self):
        returns = [0.01, 0.02, 0.015, 0.025, 0.01]
        cr = calmar_ratio(returns, 0.02)
        self.assertIsNotNone(cr)
        self.assertGreater(cr, 0)

    def test_information_ratio_zero_benchmark(self):
        returns = [0.01, 0.02, -0.01, 0.015, -0.005]
        ir = information_ratio(returns, 0.0)
        self.assertIsNotNone(ir)


class RiskCorrelationTests(unittest.TestCase):
    def test_risk_with_correlation_registry(self):
        from polyalpha.correlation import ClusterRegistry, CorrelationMatrix, EventCluster

        mat = CorrelationMatrix(tokens=["y1", "y2"])
        mat.set("y1", "y2", D("0.9"))
        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2"],
            token_ids=["y1", "y2"],
            correlation_matrix=mat,
        )
        reg = ClusterRegistry()
        reg.register(cluster)

        risk = Risk(D("10000"))
        risk.correlation_registry = reg

        portfolio = Portfolio(D("10000"))
        # Budget should be reduced when correlation is high
        budget = risk.correlation_adjusted_budget(
            portfolio, D("10000"), "m1", "e1", "c1", "politics"
        )
        self.assertGreater(budget, D(0))

    def test_risk_halt_blocks_budget(self):
        risk = Risk(D("10000"))
        risk.halted = True
        risk.reason = "drawdown"
        portfolio = Portfolio(D("10000"))
        budget = risk.budget(portfolio, D("10000"), "m1", "e1", "c1", "politics")
        self.assertEqual(budget, D(0))


if __name__ == "__main__":
    unittest.main()
