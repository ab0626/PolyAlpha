"""Tests for uncertainty estimation."""

import unittest
from decimal import Decimal

from polyalpha.uncertainty import (
    EnsembleUncertainty,
    HistoricalErrorUncertainty,
    ModelDisagreementUncertainty,
    SpreadBasedUncertainty,
    UncertaintyEstimate,
    conservative_no_probability,
    conservative_probability,
)

D = Decimal


class UncertaintyTests(unittest.TestCase):
    def test_historical_error_uncertainty(self):
        est = HistoricalErrorUncertainty(default_buffer=D("0.05"))
        result = est.estimate(D("0.60"), {}, "test")
        self.assertIsInstance(result, UncertaintyEstimate)
        self.assertEqual(result.probability, D("0.60"))
        self.assertLess(result.lower_bound, D("0.60"))
        self.assertGreater(result.upper_bound, D("0.60"))
        self.assertEqual(result.method, "historical-error")

    def test_historical_error_scaling(self):
        est = HistoricalErrorUncertainty(default_buffer=D("0.05"))
        # Near 0.5 should have larger uncertainty than near extremes
        mid = est.estimate(D("0.50"), {})
        extreme = est.estimate(D("0.95"), {})
        self.assertGreater(mid.uncertainty_score, extreme.uncertainty_score)

    def test_model_disagreement_uncertainty(self):
        est = ModelDisagreementUncertainty()
        # With no models, uses fallback
        result = est.estimate(D("0.60"), {})
        self.assertEqual(result.method, "model-disagreement-fallback")

        # With agreeing models
        result2 = est.estimate(D("0.60"), {}, model_probabilities=[0.60, 0.61, 0.59])
        self.assertEqual(result2.method, "model-disagreement")
        self.assertGreater(result2.uncertainty_score, D(0))

        # With disagreeing models should have higher uncertainty
        result3 = est.estimate(D("0.60"), {}, model_probabilities=[0.40, 0.80])
        self.assertGreater(result3.uncertainty_score, result2.uncertainty_score)

    def test_spread_based_uncertainty(self):
        est = SpreadBasedUncertainty(multiplier=D("0.5"))
        features = {"spread": D("0.04")}
        result = est.estimate(D("0.60"), features)
        # uncertainty = 0.04 * 0.5 = 0.02
        self.assertEqual(result.uncertainty_score, D("0.02"))

    def test_spread_based_no_spread(self):
        est = SpreadBasedUncertainty()
        result = est.estimate(D("0.60"), {})
        # Falls back to default 0.05
        self.assertEqual(result.uncertainty_score, D("0.05"))

    def test_ensemble_uncertainty(self):
        est1 = HistoricalErrorUncertainty(default_buffer=D("0.03"))
        est2 = SpreadBasedUncertainty(multiplier=D("0.5"))
        ensemble = EnsembleUncertainty([est1, est2])
        result = ensemble.estimate(D("0.60"), {"spread": D("0.02")})
        # Should use the maximum uncertainty from both
        self.assertGreater(result.uncertainty_score, D(0))
        self.assertIn("ensemble", result.method)

    def test_conservative_probability(self):
        est = HistoricalErrorUncertainty(default_buffer=D("0.05"))
        unc = est.estimate(D("0.60"), {})
        cp = conservative_probability(D("0.60"), unc)
        self.assertLess(cp, D("0.60"))
        self.assertGreater(cp, D(0))

    def test_conservative_no_probability(self):
        est = HistoricalErrorUncertainty(default_buffer=D("0.05"))
        unc = est.estimate(D("0.60"), {})
        cnp = conservative_no_probability(D("0.60"), unc)
        # P(NO) = 1 - 0.60 = 0.40, then subtract uncertainty
        self.assertLess(cnp, D("0.40"))

    def test_conservative_with_confidence(self):
        est = HistoricalErrorUncertainty(default_buffer=D("0.05"))
        unc = est.estimate(D("0.60"), {})
        full = conservative_probability(D("0.60"), unc, confidence=D("1.0"))
        half = conservative_probability(D("0.60"), unc, confidence=D("0.5"))
        none = conservative_probability(D("0.60"), unc, confidence=D("0"))
        # Less confidence = less buffer = higher conservative probability
        self.assertGreater(half, full)
        self.assertEqual(none, D("0.60"))  # no buffer at confidence=0

    def test_ensemble_empty_raises(self):
        with self.assertRaises(ValueError):
            EnsembleUncertainty([])

    def test_bounds_valid(self):
        est = HistoricalErrorUncertainty(default_buffer=D("0.10"))
        result = est.estimate(D("0.99"), {})
        self.assertGreaterEqual(result.lower_bound, 0)
        self.assertLessEqual(result.upper_bound, 1)
        self.assertLessEqual(result.lower_bound, result.probability)
        self.assertGreaterEqual(result.upper_bound, result.probability)


if __name__ == "__main__":
    unittest.main()
