"""Tests for models (market_prior, fundamental), feature_importance,
alpha_decay, and enhanced resolution_risk.
"""

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polyalpha.alpha_decay import (
    AlphaDecayObservation,
    analyze_alpha_decay,
    decay_summary,
)
from polyalpha.domain import Book, Level, Market
from polyalpha.external import ExternalFact, FixtureSource
from polyalpha.feature_importance import (
    detect_leakage,
    feature_summary,
    permutation_importance,
)
from polyalpha.models import FundamentalModel, MarketPriorModel
from polyalpha.models.fundamental import (
    _bayesian_update,
    _feature_to_likelihood,
    _source_weight,
)
from polyalpha.resolution import (
    ResolutionQualityScore,
    ResolutionReview,
    definition_hash,
)

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_book(bid_price="0.49", bid_size="100", ask_price="0.51", ask_size="1000"):
    return Book(
        token_id="y",
        condition_id="c",
        source_at=NOW,
        received_at=NOW,
        bids=(Level(D(bid_price), D(bid_size)),),
        asks=(Level(D(ask_price), D(ask_size)),),
        tick_size=D("0.01"),
        min_order_size=D(1),
        source_hash="test",
    )


def _make_market(**overrides):
    defaults = dict(
        market_id="m1",
        condition_id="c",
        event_ids=("e1",),
        question="Will X happen?",
        description="Test market",
        resolution_source="Polymarket",
        deadline=NOW + timedelta(days=30),
        active=True,
        closed=False,
        accepting_orders=True,
        enable_order_book=True,
        liquidity=D("50000"),
        volume=D("100000"),
        fees_enabled=True,
        fee_parameters_json='{"rate":"0.04","exponent":1,"takerOnly":true}',
        yes_token_id="y",
        no_token_id="n",
        received_at=NOW,
        category="test",
    )
    defaults.update(overrides)
    return Market(**defaults)


class MarketPriorModelTests(unittest.TestCase):
    def test_predict_returns_forecast(self):
        model = MarketPriorModel()
        book = _make_book()
        forecast = model.predict("m1", book, NOW)
        self.assertEqual(forecast.market_id, "m1")
        self.assertGreaterEqual(forecast.probability, D("0.01"))
        self.assertLessEqual(forecast.probability, D("0.99"))
        self.assertLessEqual(forecast.lower, forecast.probability)
        self.assertGreaterEqual(forecast.upper, forecast.probability)

    def test_predict_invalid_book(self):
        model = MarketPriorModel()
        book = Book(
            token_id="y",
            condition_id="c",
            source_at=NOW,
            received_at=NOW,
            bids=(),
            asks=(),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        with self.assertRaises(ValueError):
            model.predict("m1", book, NOW)

    def test_model_version(self):
        model = MarketPriorModel()
        self.assertEqual(model.version, "market-prior-v1")

    def test_probability_clamped(self):
        model = MarketPriorModel()
        # Very wide spread should still produce valid probability
        book = Book(
            token_id="y",
            condition_id="c",
            source_at=NOW,
            received_at=NOW,
            bids=(Level(D("0.01"), D(100)),),
            asks=(Level(D("0.99"), D(100)),),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        forecast = model.predict("m1", book, NOW)
        self.assertGreaterEqual(forecast.probability, D("0.01"))
        self.assertLessEqual(forecast.probability, D("0.99"))


class FundamentalModelTests(unittest.TestCase):
    def test_stub_returns_neutral(self):
        model = FundamentalModel()
        book = _make_book()
        forecast = model.predict("m1", book, NOW)
        self.assertEqual(forecast.probability, D("0.50"))
        self.assertEqual(forecast.version, "fundamental-v2")

    def test_stub_has_wide_uncertainty(self):
        model = FundamentalModel()
        book = _make_book()
        forecast = model.predict("m1", book, NOW)
        uncertainty = forecast.upper - forecast.lower
        self.assertGreaterEqual(uncertainty, D("0.20"))

    def test_stub_with_empty_book(self):
        """Fundamental model stub returns neutral even with empty book."""
        model = FundamentalModel()
        book = Book(
            token_id="y",
            condition_id="c",
            source_at=NOW,
            received_at=NOW,
            bids=(),
            asks=(),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        forecast = model.predict("m1", book, NOW)
        self.assertEqual(forecast.probability, D("0.50"))


class PermutationImportanceTests(unittest.TestCase):
    def _dummy_model(self):
        """Model that uses 'x1' as the main predictor."""

        class Model:
            def score(self, features):
                return features.get("x1", 0.5)

        return Model()

    def test_basic_importance(self):
        model = self._dummy_model()
        X = [{"x1": float(i) / 10, "x2": 0.5} for i in range(10)]
        y = [1 if x["x1"] > 0.5 else 0 for x in X]

        result = permutation_importance(model, ["x1", "x2"], X, y, n_repeats=3, random_state=42)
        self.assertEqual(len(result), 2)
        # x1 should be more important than x2
        self.assertGreater(result[0].importance, result[1].importance)

    def test_leakage_detection(self):
        importances = [
            FeatureImportanceMock("good_feature", 0.05, 0.25, 0.20, "positive"),
            FeatureImportanceMock("leaky_feature", -0.10, 0.25, 0.15, "negative"),
        ]
        suspects = detect_leakage(importances, threshold=0.05)
        self.assertEqual(len(suspects), 1)
        self.assertEqual(suspects[0].feature_name, "leaky_feature")

    def test_empty_importances(self):
        result = feature_summary([])
        self.assertEqual(result["total_features"], 0)

    def test_feature_summary_structure(self):
        importances = [
            FeatureImportanceMock("f1", 0.10, 0.25, 0.15, "positive"),
            FeatureImportanceMock("f2", -0.02, 0.25, 0.23, "negative"),
        ]
        result = feature_summary(importances)
        self.assertEqual(result["total_features"], 2)
        self.assertIn("top_positive_features", result)
        self.assertIn("leakage_suspects", result)


class FeatureImportanceMock:
    """Mock for FeatureImportance dataclass in tests."""

    def __init__(self, name, importance, baseline, permuted, direction):
        self.feature_name = name
        self.importance = importance
        self.baseline_score = baseline
        self.permuted_score = permuted
        self.direction = direction


class AlphaDecayTests(unittest.TestCase):
    def test_basic_decay_analysis(self):
        obs = [
            AlphaDecayObservation(
                market_id="m1",
                signal_time=NOW,
                signal_side="YES",
                signal_probability=D("0.65"),
                entry_price=D("0.55"),
                price_observations={
                    30: D("0.56"),
                    60: D("0.57"),
                    300: D("0.58"),
                },
            ),
            AlphaDecayObservation(
                market_id="m2",
                signal_time=NOW,
                signal_side="YES",
                signal_probability=D("0.70"),
                entry_price=D("0.60"),
                price_observations={
                    30: D("0.61"),
                    60: D("0.62"),
                    300: D("0.63"),
                },
            ),
        ]
        results = analyze_alpha_decay(obs, horizons=(30, 60, 300))
        self.assertEqual(len(results), 3)
        # All should show positive price changes
        for r in results:
            self.assertGreater(r.mean_price_change, 0)
            self.assertGreater(r.directional_accuracy, 0)

    def test_empty_observations(self):
        results = analyze_alpha_decay([], horizons=(30, 60))
        self.assertEqual(len(results), 0)

    def test_decay_summary(self):
        from polyalpha.alpha_decay import AlphaDecayResult

        results = [
            AlphaDecayResult(30, 0.01, 0.01, 0.6, 100, 2.0, 0.05),
            AlphaDecayResult(3600, 0.005, 0.004, 0.52, 100, 0.5, 0.6),
        ]
        summary = decay_summary(results)
        self.assertTrue(summary["has_immediate_signal"])
        self.assertFalse(summary["has_slow_diffusion"])

    def test_no_observations_horizon(self):
        obs = [
            AlphaDecayObservation(
                "m1",
                NOW,
                "YES",
                D("0.6"),
                D("0.5"),
                {30: D("0.51")},  # only 30s horizon
            ),
        ]
        results = analyze_alpha_decay(obs, horizons=(30, 86400))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].horizon_seconds, 30)


class ResolutionQualityTests(unittest.TestCase):
    def test_quality_score_from_market(self):
        market = _make_market()
        score = ResolutionQualityScore.from_market(market)
        self.assertGreaterEqual(score.overall_score, D(0))
        self.assertLessEqual(score.overall_score, D(1))

    def test_quality_score_penalizes_long_question(self):
        market = _make_market(question="x" * 300)
        score = ResolutionQualityScore.from_market(market)
        self.assertLess(score.clarity_score, D("1.0"))

    def test_quality_score_rewards_official_source(self):
        market = _make_market(resolution_source="Official Government Data")
        score = ResolutionQualityScore.from_market(market)
        self.assertGreaterEqual(score.source_reliability, D("0.9"))

    def test_quality_score_penalizes_no_description(self):
        market = _make_market(description="")
        score = ResolutionQualityScore.from_market(market)
        # Empty description drops clarity by 0.2 from 1.0 to 0.8
        self.assertLessEqual(score.clarity_score, D("0.8"))

    def test_resolution_penalty_bounds(self):
        market = _make_market()
        score = ResolutionQualityScore.from_market(market)
        penalty = score.resolution_penalty
        self.assertGreaterEqual(penalty, D("0.01"))
        self.assertLessEqual(penalty, D("0.06"))

    def test_resolution_review_temporal_decay(self):
        market = _make_market()
        review = ResolutionReview(
            definition_sha256=definition_hash(market),
            reviewed_at=NOW - timedelta(days=180),
            reference="test",
            clarity=D("0.9"),
            ambiguity=D("0.1"),
            dispute_risk=D("0.1"),
        )
        # base = 0.01 + 0.05*(0.1+0.1) = 0.02
        # decay for 180 days / 90 = 2 periods = min(0.03, 0.005*2) = 0.01
        # total = 0.03
        penalty_old = review.penalty(market, NOW, review_decay_days=90)
        self.assertEqual(penalty_old, D("0.03"))

        # Fresh review: no temporal decay
        review_fresh = ResolutionReview(
            definition_sha256=definition_hash(market),
            reviewed_at=NOW - timedelta(days=1),
            reference="test",
            clarity=D("0.9"),
            ambiguity=D("0.1"),
            dispute_risk=D("0.1"),
        )
        penalty_fresh = review_fresh.penalty(market, NOW, review_decay_days=90)
        self.assertEqual(penalty_fresh, D("0.02"))  # just base, no decay

    def test_resolution_review_definition_change(self):
        market = _make_market()
        review = ResolutionReview(
            definition_sha256=definition_hash(market),
            reviewed_at=NOW - timedelta(days=1),
            reference="test",
            clarity=D("0.9"),
            ambiguity=D("0.1"),
            dispute_risk=D("0.1"),
        )
        # Change market definition
        market2 = _make_market(question="Changed question")
        with self.assertRaises(ValueError):
            review.penalty(market2, NOW)


# ─── FundamentalModel: Bayesian Updating ────────────────────────────────────


class TestBayesianUpdating(unittest.TestCase):
    def test_prior_unchanged_with_lr_1(self):
        prior = D("0.50")
        result = _bayesian_update(prior, D(1))
        self.assertEqual(result, D("0.50"))

    def test_lr_above_1_increases_posterior(self):
        prior = D("0.50")
        result = _bayesian_update(prior, D(2))
        self.assertGreater(result, D("0.50"))

    def test_lr_below_1_decreases_posterior(self):
        prior = D("0.50")
        result = _bayesian_update(prior, D("0.5"))
        self.assertLess(result, D("0.50"))

    def test_extreme_lr_clamped(self):
        result = _bayesian_update(D("0.50"), D(1000))
        self.assertLessEqual(result, D("0.99"))

    def test_negative_lr_returns_prior(self):
        prior = D("0.60")
        result = _bayesian_update(prior, D(-1))
        self.assertEqual(result, prior)


class TestFeatureToLikelihood(unittest.TestCase):
    def test_positive_feature_positive_direction(self):
        lr = _feature_to_likelihood("polling_average", 0.5)
        self.assertGreater(lr, D(1))

    def test_negative_feature_positive_direction(self):
        lr = _feature_to_likelihood("polling_average", -0.5)
        self.assertLess(lr, D(1))

    def test_unknown_feature_returns_1(self):
        lr = _feature_to_likelihood("unknown_feature", 0.5)
        self.assertEqual(lr, D(1))

    def test_clamped_values(self):
        lr = _feature_to_likelihood("polling_average", 10.0)
        self.assertTrue(lr.is_finite())


class TestSourceWeight(unittest.TestCase):
    def test_polling_higher_weight(self):
        self.assertGreater(_source_weight("polling_aggregator"), D(1))

    def test_social_lower_weight(self):
        self.assertLess(_source_weight("social_signal"), D(1))

    def test_unknown_default(self):
        self.assertEqual(_source_weight("mystery_source"), D("1.0"))


class TestFundamentalModelEnhanced(unittest.TestCase):
    def test_no_sources_returns_prior(self):
        model = FundamentalModel()
        book = _make_book()
        forecast = model.predict("m1", book, NOW, category="other")
        self.assertEqual(forecast.probability, D("0.50"))

    def test_category_prior_used(self):
        model = FundamentalModel()
        book = _make_book()
        forecast = model.predict("m1", book, NOW, category="politics")
        self.assertEqual(forecast.probability, D("0.45"))

    def test_source_updates_prior(self):
        # Create a source with a positive feature
        facts = [
            ExternalFact(
                event_id="m1",
                source_url="https://example.com",
                published_at=NOW - timedelta(hours=1),
                retrieved_at=NOW,
                features={"polling_average": 0.8},
            )
        ]
        source = FixtureSource(facts)
        source.name = "polling_aggregator"

        model = FundamentalModel(sources=[source])
        book = _make_book()
        forecast = model.predict("m1", book, NOW, category="politics")
        # Should be above the politics prior of 0.45
        self.assertGreater(forecast.probability, D("0.45"))

    def test_multiple_sources_reduce_uncertainty(self):
        facts1 = [
            ExternalFact(
                event_id="m1",
                source_url="https://a.com",
                published_at=NOW - timedelta(hours=1),
                retrieved_at=NOW,
                features={"polling_average": 0.6},
            )
        ]
        facts2 = [
            ExternalFact(
                event_id="m1",
                source_url="https://b.com",
                published_at=NOW - timedelta(hours=2),
                retrieved_at=NOW,
                features={"fundamentals_score": 0.5},
            )
        ]
        source1 = FixtureSource(facts1)
        source1.name = "source_a"
        source2 = FixtureSource(facts2)
        source2.name = "source_b"

        model = FundamentalModel(sources=[source1, source2])
        book = _make_book()
        forecast = model.predict("m1", book, NOW)
        uncertainty = forecast.upper - forecast.lower
        # Multiple sources should produce tighter uncertainty
        self.assertLessEqual(uncertainty, D("0.25"))


if __name__ == "__main__":
    unittest.main()
