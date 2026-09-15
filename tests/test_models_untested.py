"""Tests for models/market_prior.py, models/microstructure.py, models/trained.py."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polyalpha.domain import Book, Level

D = Decimal

NOW = datetime(2026, 1, 15, tzinfo=UTC)


def _book(bid=0.49, ask=0.52, bid_size=200, ask_size=100, levels=5):
    bids = []
    asks = []
    for i in range(levels):
        bp = D(str(round(bid - i * 0.01, 2)))
        ap = D(str(round(ask + i * 0.01, 2)))
        bids.append(Level(bp, D(str(bid_size * (levels - i)))))
        asks.append(Level(ap, D(str(ask_size * (levels - i)))))
    return Book(
        token_id="y1",
        condition_id="c1",
        source_at=NOW,
        received_at=NOW,
        bids=tuple(bids),
        asks=tuple(asks),
        tick_size=D("0.01"),
        min_order_size=D(1),
        source_hash="h",
    )


# ── models/market_prior.py ──────────────────────────────────────────────────


class TestMarketPriorModel:
    def test_basic_prediction(self):
        from polyalpha.models.market_prior import MarketPriorModel

        model = MarketPriorModel()
        forecast = model.predict("m1", _book(), NOW)
        assert D("0.01") <= forecast.probability <= D("0.99")
        assert forecast.version == "market-prior-v1"
        assert forecast.market_id == "m1"

    def test_imbalanced_book_shifts(self):
        from polyalpha.models.market_prior import MarketPriorModel

        model = MarketPriorModel(alpha=D("0.5"), beta=D("0.5"))
        # Heavy bid imbalance → upward bias
        fair_bid = model.predict("m1", _book(bid_size=500, ask_size=50), NOW)
        fair_ask = model.predict("m1", _book(bid_size=50, ask_size=500), NOW)
        # With more bids, microprice shifts up
        assert fair_bid.probability >= fair_ask.probability

    def test_unusable_book_raises(self):
        from polyalpha.models.market_prior import MarketPriorModel

        model = MarketPriorModel()
        book = Book(
            token_id="y1",
            condition_id="c1",
            source_at=NOW,
            received_at=NOW,
            bids=(),
            asks=(Level(D("0.52"), D(100)),),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="h",
        )
        with pytest.raises(ValueError, match="unusable"):
            model.predict("m1", book, NOW)

    def test_uncertainty_widens_with_spread(self):
        from polyalpha.models.market_prior import MarketPriorModel

        model = MarketPriorModel()
        narrow = model.predict("m1", _book(bid=0.49, ask=0.51), NOW)
        wide = model.predict("m1", _book(bid=0.40, ask=0.60), NOW)
        narrow_width = narrow.upper - narrow.lower
        wide_width = wide.upper - wide.lower
        assert wide_width >= narrow_width


# ── models/microstructure.py ────────────────────────────────────────────────


class TestMicrostructureModel:
    def test_basic_prediction(self):
        from polyalpha.models.microstructure import MicrostructureModel

        model = MicrostructureModel()
        forecast = model.predict("m1", _book(), NOW)
        assert D("0.01") <= forecast.probability <= D("0.99")
        assert forecast.version == "microstructure-v1"

    def test_unusable_book_raises(self):
        from polyalpha.models.microstructure import MicrostructureModel

        model = MicrostructureModel()
        book = Book(
            token_id="y1",
            condition_id="c1",
            source_at=NOW,
            received_at=NOW,
            bids=(),
            asks=(Level(D("0.52"), D(100)),),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="h",
        )
        with pytest.raises(ValueError, match="unusable"):
            model.predict("m1", book, NOW)

    def test_imbalance_shifts_fair(self):
        from polyalpha.models.microstructure import MicrostructureModel

        model = MicrostructureModel(imbalance_weight=D("0.20"))
        bid_heavy = model.predict("m1", _book(bid_size=500, ask_size=50), NOW)
        ask_heavy = model.predict("m1", _book(bid_size=50, ask_size=500), NOW)
        assert bid_heavy.probability >= ask_heavy.probability

    def test_uncertainty_floor(self):
        from polyalpha.models.microstructure import MicrostructureModel

        model = MicrostructureModel(uncertainty_floor=D("0.05"))
        forecast = model.predict("m1", _book(bid=0.49, ask=0.51), NOW)
        width = forecast.upper - forecast.lower
        assert width >= D("0.10")  # 2 * uncertainty_floor


# ── models/trained.py ──────────────────────────────────────────────────────


class TestTrainedModels:
    def test_book_features_extraction(self):
        from polyalpha.models.trained import _book_features

        feats = _book_features(_book())
        assert "midpoint" in feats
        assert "spread" in feats
        assert "imbalance" in feats
        assert "microprice" in feats
        assert feats["bid_size"] > 0
        assert feats["ask_size"] > 0

    def test_book_features_unusable(self):
        from polyalpha.models.trained import _book_features

        book = Book(
            token_id="y1",
            condition_id="c1",
            source_at=NOW,
            received_at=NOW,
            bids=(),
            asks=(Level(D("0.52"), D(100)),),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="h",
        )
        with pytest.raises(ValueError, match="unusable"):
            _book_features(book)

    def test_sklearn_model_not_trained(self):
        from polyalpha.models.trained import SklearnModel

        model = SklearnModel()
        with pytest.raises(ValueError, match="not trained"):
            model.predict("m1", _book(), NOW)

    def test_logistic_regression_fit_predict(self):
        from polyalpha.models.trained import LogisticRegressionModel

        model = LogisticRegressionModel()
        X = [[0.5, 0.02, 0.04, 0.1, 0.51, 100, 100, 0.49, 0.52, 0.5, 0.5, 0.01] for _ in range(30)]
        y = [1] * 20 + [0] * 10
        model.fit(X, y)
        forecast = model.predict("m1", _book(), NOW)
        assert D("0.01") <= forecast.probability <= D("0.99")
        assert forecast.version == "logistic-regression-v1"

    def test_gradient_boosted_fit_predict(self):
        from polyalpha.models.trained import GradientBoostedModel

        model = GradientBoostedModel(n_estimators=10, max_depth=2)
        X = [[0.5, 0.02, 0.04, 0.1, 0.51, 100, 100, 0.49, 0.52, 0.5, 0.5, 0.01] for _ in range(30)]
        y = [1] * 20 + [0] * 10
        model.fit(X, y)
        forecast = model.predict("m1", _book(), NOW)
        assert D("0.01") <= forecast.probability <= D("0.99")

    def test_logistic_regression_to_dict(self):
        from polyalpha.models.trained import LogisticRegressionModel

        model = LogisticRegressionModel(C=0.5, uncertainty=0.03)
        d = model.to_dict()
        assert d["C"] == 0.5
        assert d["uncertainty"] == 0.03
        assert d["model_type"] == "logistic_regression"

    def test_gradient_boosted_to_dict(self):
        from polyalpha.models.trained import GradientBoostedModel

        model = GradientBoostedModel(n_estimators=50, max_depth=4, learning_rate=0.05)
        d = model.to_dict()
        assert d["n_estimators"] == 50
        assert d["max_depth"] == 4
        assert d["model_type"] == "gradient_boosted"

    def test_sklearn_model_from_dict(self):
        from polyalpha.models.trained import SklearnModel

        params = {"uncertainty": 0.03, "version": "test-v1"}
        model = SklearnModel.from_dict(params)
        assert model._uncertainty == 0.03
        assert model.version == "test-v1"

    def test_trained_ensemble_no_components(self):
        from polyalpha.models.trained import TrainedEnsembleModel

        with pytest.raises(ValueError, match="at least one"):
            TrainedEnsembleModel(components=[])

    def test_trained_ensemble_predict(self):
        from polyalpha.models.trained import (
            GradientBoostedModel,
            LogisticRegressionModel,
            TrainedEnsembleModel,
        )

        lr = LogisticRegressionModel()
        gbm = GradientBoostedModel(n_estimators=10, max_depth=2)
        X = [[0.5, 0.02, 0.04, 0.1, 0.51, 100, 100, 0.49, 0.52, 0.5, 0.5, 0.01] for _ in range(30)]
        y = [1] * 20 + [0] * 10
        lr.fit(X, y)
        gbm.fit(X, y)

        ensemble = TrainedEnsembleModel(components=[("lr", lr), ("gbm", gbm)])
        forecast = ensemble.predict("m1", _book(), NOW)
        assert D("0.01") <= forecast.probability <= D("0.99")
        assert forecast.version == "trained-ensemble-v1"

    def test_trained_ensemble_to_dict(self):
        from polyalpha.models.trained import LogisticRegressionModel, TrainedEnsembleModel

        lr = LogisticRegressionModel()
        ensemble = TrainedEnsembleModel(components=[("lr", lr)], weights={"lr": 1.0})
        d = ensemble.to_dict()
        assert "weights" in d
        assert "lr" in d["components"]

    def test_trained_ensemble_from_dict_with_models(self):
        from polyalpha.models.trained import LogisticRegressionModel, TrainedEnsembleModel

        lr = LogisticRegressionModel()
        params = {
            "weights": {"lr": 0.6},
            "uncertainty": 0.04,
            "version": "test-ens-v1",
        }
        ensemble = TrainedEnsembleModel.from_dict(params, models={"lr": lr})
        assert ensemble.uncertainty == 0.04
        assert ensemble.version == "test-ens-v1"

    def test_trained_ensemble_from_dict_no_models(self):
        from polyalpha.models.trained import TrainedEnsembleModel

        params = {
            "weights": {"m1": 0.6},
            "uncertainty": 0.04,
            "version": "test-ens-v1",
        }
        # Empty models dict → no components → raises
        with pytest.raises(ValueError, match="at least one"):
            TrainedEnsembleModel.from_dict(params, models={})

    def test_ensemble_all_components_fail(self):
        # Models that always fail (untrained)
        from polyalpha.models.trained import SklearnModel, TrainedEnsembleModel

        bad = SklearnModel()
        ensemble = TrainedEnsembleModel(components=[("bad", bad)])
        with pytest.raises(ValueError, match="all ensemble"):
            ensemble.predict("m1", _book(), NOW)

    def test_feature_names_count(self):
        from polyalpha.models.trained import _FEATURE_NAMES

        assert len(_FEATURE_NAMES) == 12
