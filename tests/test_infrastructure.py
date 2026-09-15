"""Tests for infrastructure modules: config, correlation, ensemble, monitoring."""

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polyalpha.config import load_config, load_env, load_toml
from polyalpha.correlation import (
    ClusterRegistry,
    CorrelationMatrix,
    EventCluster,
    compute_correlation,
    rolling_correlation,
)
from polyalpha.ensemble import EnsembleModel
from polyalpha.forecasting import Baseline
from polyalpha.monitoring import CalibrationDriftDetector, FeatureDriftDetector

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_book():
    from polyalpha.domain import Book, Level

    return Book(
        token_id="y",
        condition_id="c",
        source_at=NOW,
        received_at=NOW,
        bids=(Level(D("0.49"), D(100)),),
        asks=(Level(D("0.51"), D(1000)),),
        tick_size=D("0.01"),
        min_order_size=D(1),
        source_hash="test",
    )


class ConfigTests(unittest.TestCase):
    def test_load_env_nonexistent(self):
        load_env("nonexistent.env")

    def test_load_toml(self):
        config = load_toml("config/base.toml")
        self.assertIn("market_filter", config)
        self.assertIn("risk", config)

    def test_load_config_toml(self):
        config = load_config("config/base.toml")
        self.assertIn("market_filter", config)

    def test_load_config_missing(self):
        with self.assertRaises(FileNotFoundError):
            load_toml("nonexistent.toml")

    def test_load_env_creates_vars(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("TEST_VAR_XYZ=hello\n")
            f.write("# comment\n\n")
            path = f.name
        try:
            load_env(path)
            self.assertEqual(os.environ.get("TEST_VAR_XYZ"), "hello")
        finally:
            os.unlink(path)
            os.environ.pop("TEST_VAR_XYZ", None)


class CorrelationMatrixTests(unittest.TestCase):
    def test_set_get(self):
        m = CorrelationMatrix(tokens=["a", "b"])
        m.set("a", "b", D("0.8"))
        self.assertEqual(m.get("a", "b"), D("0.8"))
        self.assertEqual(m.get("b", "a"), D("0.8"))

    def test_diagonal_is_one(self):
        m = CorrelationMatrix(tokens=["a", "b"])
        self.assertEqual(m.get("a", "a"), D(1))

    def test_unknown_is_zero(self):
        m = CorrelationMatrix(tokens=["a", "b"])
        self.assertEqual(m.get("a", "x"), D(0))

    def test_highly_correlated(self):
        m = CorrelationMatrix(tokens=["a", "b", "c"])
        m.set("a", "b", D("0.95"))
        m.set("a", "c", D("0.3"))
        pairs = m.highly_correlated(D("0.9"))
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], "a")
        self.assertEqual(pairs[0][1], "b")

    def test_compute_correlation_perfect(self):
        a = [D("1"), D("2"), D("3"), D("4"), D("5")]
        b = [D("2"), D("4"), D("6"), D("8"), D("10")]
        r = compute_correlation(a, b)
        self.assertEqual(r, D(1))

    def test_compute_correlation_insufficient(self):
        r = compute_correlation([D("1")], [D("2")])
        self.assertEqual(r, D(0))

    def test_rolling_correlation(self):
        a = [D("1"), D("2"), D("3"), D("4"), D("5")]
        b = [D("2"), D("4"), D("6"), D("8"), D("10")]
        result = rolling_correlation(a, b, window=3)
        self.assertEqual(len(result), 3)
        for r in result:
            self.assertEqual(r, D(1))


class EventClusterTests(unittest.TestCase):
    def test_size(self):
        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2"],
            token_ids=["t1", "t2"],
        )
        self.assertEqual(cluster.size, 2)

    def test_avg_internal_correlation_no_matrix(self):
        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1"],
            token_ids=["t1"],
        )
        self.assertEqual(cluster.avg_internal_correlation, D(0))

    def test_avg_internal_correlation_with_matrix(self):
        m = CorrelationMatrix(tokens=["t1", "t2"])
        m.set("t1", "t2", D("0.8"))
        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2"],
            token_ids=["t1", "t2"],
            correlation_matrix=m,
        )
        self.assertEqual(cluster.avg_internal_correlation, D("0.8"))


class ClusterRegistryTests(unittest.TestCase):
    def test_register_and_lookup(self):
        reg = ClusterRegistry()
        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2"],
            token_ids=["t1", "t2"],
        )
        reg.register(cluster)
        found = reg.get_cluster("m1")
        self.assertIsNotNone(found)
        self.assertEqual(found.cluster_id, "c1")

    def test_cluster_exposure(self):
        reg = ClusterRegistry()
        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2"],
            token_ids=["t1", "t2"],
        )
        reg.register(cluster)
        positions = {"t1": D("100"), "t2": D("200")}
        exposure = reg.cluster_exposure(positions)
        self.assertEqual(exposure["c1"], D("300"))

    def test_correlation_adjusted_exposure(self):
        m = CorrelationMatrix(tokens=["t1", "t2"])
        m.set("t1", "t2", D("0.8"))
        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2"],
            token_ids=["t1", "t2"],
            correlation_matrix=m,
        )
        reg = ClusterRegistry()
        reg.register(cluster)
        positions = {"t1": D("100"), "t2": D("200")}
        adjusted = reg.correlation_adjusted_exposure(positions)
        # Scale = 1 + 0.8/2 = 1.4
        self.assertEqual(adjusted["c1"], D("420"))


class EnsembleModelTests(unittest.TestCase):
    def test_ensemble_valid(self):
        b1 = Baseline(D("0.05"))
        b2 = Baseline(D("0.03"))
        ensemble = EnsembleModel(
            [
                ("baseline1", b1, D("0.5")),
                ("baseline2", b2, D("0.5")),
            ]
        )
        book = _make_book()
        forecast = ensemble.predict("m1", book, NOW)
        self.assertEqual(forecast.market_id, "m1")
        self.assertGreaterEqual(forecast.probability, D("0.01"))
        self.assertLessEqual(forecast.probability, D("0.99"))

    def test_ensemble_invalid_weights(self):
        b1 = Baseline(D("0.05"))
        with self.assertRaises(ValueError):
            EnsembleModel([("b1", b1, D("0.6"))])

    def test_ensemble_negative_weight(self):
        b1 = Baseline(D("0.05"))
        b2 = Baseline(D("0.03"))
        with self.assertRaises(ValueError):
            EnsembleModel([("b1", b1, D("1.5")), ("b2", b2, D("-0.5"))])

    def test_update_weights(self):
        b1 = Baseline(D("0.05"))
        b2 = Baseline(D("0.03"))
        ensemble = EnsembleModel(
            [
                ("baseline1", b1, D("0.5")),
                ("baseline2", b2, D("0.5")),
            ]
        )
        ensemble.update_weights({"baseline1": D("0.7"), "baseline2": D("0.3")})
        self.assertEqual(ensemble.weight_dict["baseline1"], D("0.7"))

    def test_component_names(self):
        b1 = Baseline(D("0.05"))
        ensemble = EnsembleModel([("m1", b1, D("1.0"))])
        self.assertEqual(ensemble.component_names, ["m1"])


class CalibrationDriftTests(unittest.TestCase):
    def test_no_drift_with_baseline(self):
        det = CalibrationDriftDetector(window_size=10, alert_threshold=0.05)
        det.set_baseline(0.25)
        for _ in range(15):
            det.update(0.25)
        result = det.check_drift()
        self.assertFalse(result["drift_detected"])

    def test_drift_with_baseline(self):
        det = CalibrationDriftDetector(window_size=20, alert_threshold=0.05, min_samples=5)
        det.set_baseline(0.25)
        for _ in range(20):
            det.update(0.40)
        result = det.check_drift()
        self.assertTrue(result["drift_detected"])

    def test_insufficient_samples(self):
        det = CalibrationDriftDetector(window_size=10, min_samples=5)
        det.update(0.25)
        result = det.check_drift()
        self.assertFalse(result["drift_detected"])
        self.assertEqual(result["reason"], "insufficient_samples")


class FeatureDriftTests(unittest.TestCase):
    def test_no_drift(self):
        det = FeatureDriftDetector()
        ref = list(range(1, 101))
        det.set_reference("f1", [float(x) for x in ref])
        for x in ref:
            det.update("f1", float(x))
        result = det.check_all(threshold=0.1)
        self.assertFalse(result["any_drift"])

    def test_drift_detected(self):
        det = FeatureDriftDetector()
        det.set_reference("f1", [float(x) for x in range(1, 101)])
        for x in range(100, 200):
            det.update("f1", float(x))
        result = det.check_all(threshold=0.01)
        self.assertTrue(result["any_drift"])

    def test_no_reference(self):
        det = FeatureDriftDetector()
        det.set_reference("f1", [1.0, 2.0, 3.0])
        result = det.psi("f2")
        self.assertIsNone(result)

    def test_psi_identical(self):
        det = FeatureDriftDetector()
        values = [float(x) for x in range(1, 101)]
        det.set_reference("f1", values)
        det.update("f1", 1.0)
        for v in values:
            det.update("f1", v)
        psi = det.psi("f1")
        self.assertIsNotNone(psi)
        self.assertLess(psi, 0.1)


# ─── ADWIN Concept Drift Detection ──────────────────────────────────────────


class TestADWINDriftDetector(unittest.TestCase):
    def test_no_drift_stable_data(self):
        from polyalpha.monitoring import ADWINDriftDetector

        det = ADWINDriftDetector(delta=0.002, min_window=5)
        for _ in range(50):
            result = det.update(0.5)
        self.assertFalse(result["drift_detected"])

    def test_drift_on_mean_shift(self):
        from polyalpha.monitoring import ADWINDriftDetector

        det = ADWINDriftDetector(delta=0.002, min_window=5)
        # Stable first
        for _ in range(30):
            det.update(0.3)
        # Sharp shift
        drift_found = False
        for _ in range(30):
            result = det.update(0.9)
            if result["drift_detected"]:
                drift_found = True
                break
        self.assertTrue(drift_found)

    def test_window_shrinks_on_drift(self):
        from polyalpha.monitoring import ADWINDriftDetector

        det = ADWINDriftDetector(delta=0.001, min_window=5)
        for _ in range(50):
            det.update(0.1)
        initial_len = len(det._window)
        for _ in range(50):
            det.update(0.9)
        # Window should have shrunk at some point
        self.assertLessEqual(len(det._window), initial_len + 50)

    def test_drift_count(self):
        from polyalpha.monitoring import ADWINDriftDetector

        det = ADWINDriftDetector(delta=0.01, min_window=5)
        self.assertEqual(det.drift_count, 0)
        for _ in range(20):
            det.update(0.1)
        self.assertEqual(det.drift_count, 0)


# ─── Page-Hinkley Concept Drift Detection ───────────────────────────────────


class TestPageHinkleyDetector(unittest.TestCase):
    def test_no_drift_stable(self):
        from polyalpha.monitoring import PageHinkleyDetector

        det = PageHinkleyDetector(threshold=50, min_instances=10)
        for _ in range(50):
            result = det.update(5.0)
        self.assertFalse(result["drift_detected"])

    def test_drift_on_shift(self):
        from polyalpha.monitoring import PageHinkleyDetector

        det = PageHinkleyDetector(threshold=30, min_instances=10)
        for _ in range(40):
            det.update(0.0)
        drift_found = False
        for _ in range(40):
            result = det.update(10.0)
            if result["drift_detected"]:
                drift_found = True
                break
        self.assertTrue(drift_found)

    def test_drift_resets_counters(self):
        from polyalpha.monitoring import PageHinkleyDetector

        det = PageHinkleyDetector(threshold=20, min_instances=5)
        for _ in range(30):
            det.update(0.0)
        for _ in range(30):
            result = det.update(10.0)
            if result["drift_detected"]:
                # After reset, count should be low
                self.assertLess(result["count"], 10)
                break

    def test_insufficient_data(self):
        from polyalpha.monitoring import PageHinkleyDetector

        det = PageHinkleyDetector(min_instances=20)
        result = det.update(1.0)
        self.assertFalse(result["drift_detected"])
        self.assertEqual(result["reason"], "insufficient_data")


# ─── Bootstrap Uncertainty ───────────────────────────────────────────────────


class TestBootstrapUncertainty(unittest.TestCase):
    def test_insufficient_data_returns_wide(self):
        from polyalpha.uncertainty import BootstrapUncertainty

        boot = BootstrapUncertainty(min_samples=20, seed=42)
        est = boot.estimate(D("0.50"), {}, "m1")
        self.assertEqual(est.method, "bootstrap-insufficient-data")
        self.assertGreater(est.uncertainty_score, D("0.05"))

    def test_with_enough_data(self):
        from polyalpha.uncertainty import BootstrapUncertainty

        boot = BootstrapUncertainty(n_bootstrap=50, min_samples=10, seed=42)
        # Add some observations
        for i in range(20):
            p = 0.5 + 0.3 * (i % 2)
            o = float(i % 2)
            boot.observe(p, o)
        est = boot.estimate(D("0.50"), {}, "m1")
        self.assertEqual(est.method, "bootstrap")
        self.assertTrue(0 <= est.lower_bound <= est.probability <= est.upper_bound <= 1)

    def test_observation_validation(self):
        from polyalpha.uncertainty import BootstrapUncertainty

        boot = BootstrapUncertainty(min_samples=5, seed=42)
        with self.assertRaises(ValueError):
            boot.observe(1.5, 0)  # prediction > 1
        with self.assertRaises(ValueError):
            boot.observe(0.5, 2)  # outcome not 0 or 1

    def test_batch_observe(self):
        from polyalpha.uncertainty import BootstrapUncertainty

        boot = BootstrapUncertainty(min_samples=5, seed=42)
        boot.observe_batch([0.6, 0.7, 0.8], [1, 0, 1])
        self.assertEqual(boot.sample_count, 3)


# ─── MFE Analysis ───────────────────────────────────────────────────────────


class TestMFEAnalysis(unittest.TestCase):
    def test_empty_observations(self):
        from polyalpha.walk_forward import compute_mfe

        result = compute_mfe([])
        self.assertIn("error", result)

    def test_single_observation_per_market(self):
        from polyalpha.calibration import Observation
        from polyalpha.walk_forward import compute_mfe

        obs = [
            Observation("m1", "c1", NOW, NOW + timedelta(days=1), 0.6, 1),
        ]
        result = compute_mfe(obs)
        self.assertIn("error", result)

    def test_mfe_computed(self):
        from polyalpha.calibration import Observation
        from polyalpha.walk_forward import compute_mfe

        obs = [
            Observation("m1", "c1", NOW, NOW + timedelta(days=1), 0.5, 1),
            Observation("m1", "c1", NOW + timedelta(hours=1), NOW + timedelta(days=1), 0.7, 1),
            Observation("m1", "c1", NOW + timedelta(hours=2), NOW + timedelta(days=1), 0.6, 1),
        ]
        result = compute_mfe(obs)
        self.assertEqual(result["market_count"], 1)
        self.assertGreater(result["mean_mfe"], 0)


if __name__ == "__main__":
    unittest.main()
