"""Tests for training.py, train.py, and maker.py."""

from datetime import UTC, datetime
from decimal import Decimal
from tempfile import TemporaryDirectory

import pytest

from polyalpha.domain import Book, Level

D = Decimal

NOW = datetime(2026, 1, 15, tzinfo=UTC)


def _book_from_features(features):
    """Create a Book from feature dict."""
    mid = features.get("midpoint", 0.5)
    spread = features.get("spread", 0.02)
    return Book(
        token_id="y1",
        condition_id="c1",
        source_at=NOW,
        received_at=NOW,
        bids=(Level(D(str(mid - spread / 2)), D(str(features.get("bid_size", 100)))),),
        asks=(Level(D(str(mid + spread / 2)), D(str(features.get("ask_size", 100)))),),
        tick_size=D("0.01"),
        min_order_size=D(1),
        source_hash="h",
    )


def _sample(market_id, outcome, mid=0.5, spread=0.02, bid_size=100, ask_size=100):
    from polyalpha.training import TrainingSample

    mid_d = D(str(mid))
    spread_d = D(str(spread))
    # Round to tick grid
    bid_p = ((mid_d - spread_d / 2) * D(100)).quantize(D(1)) / D(100)
    ask_p = ((mid_d + spread_d / 2) * D(100)).quantize(D(1)) / D(100)
    features = {
        "midpoint": float(mid_d),
        "spread": float(spread_d),
        "relative_spread": float(spread_d / mid_d) if mid_d > 0 else 0.0,
        "imbalance": 0.0,
        "microprice": float(mid_d),
        "bid_size": float(bid_size),
        "ask_size": float(ask_size),
        "bid_price": float(bid_p),
        "ask_price": float(ask_p),
        "depth_ratio_top": 1.0,
        "weighted_mid": float(mid_d),
        "mid_to_micro": 0.0,
    }
    return TrainingSample(market_id=market_id, timestamp=NOW, features=features, outcome=outcome)


# ── training.py ─────────────────────────────────────────────────────────────


class TestTrainingPipeline:
    def test_training_sample_dataclass(self):
        from polyalpha.training import TrainingSample

        s = TrainingSample("m1", NOW, {"f": 0.5}, 1)
        assert s.market_id == "m1"
        assert s.outcome == 1

    def test_training_result_dataclass(self):
        from polyalpha.training import TrainingResult

        r = TrainingResult("test", 100, 50, 0.2, 0.25, 0.03, 0.5)
        assert r.model_name == "test"
        assert r.feature_importances is None

    def test_extract_features_from_records_valid(self):
        from polyalpha.training import extract_features_from_records

        records = [
            {
                "market_id": "m1",
                "entity_id": "y1",
                "timestamp": NOW.isoformat(),
                "outcome": 1,
                "book": {
                    "asset_id": "y1",
                    "market": "c1",
                    "bids": [{"price": "0.49", "size": "100"}],
                    "asks": [{"price": "0.52", "size": "100"}],
                    "tick_size": "0.01",
                    "min_order_size": "1",
                    "timestamp": "1700000000000",
                },
            }
        ]
        samples = extract_features_from_records(records)
        assert len(samples) == 1
        assert samples[0].outcome == 1

    def test_extract_features_skips_bad_records(self):
        from polyalpha.training import extract_features_from_records

        records = [{"market_id": "m1"}]  # missing book
        samples = extract_features_from_records(records)
        assert len(samples) == 0

    def test_extract_features_skips_invalid_outcome(self):
        from polyalpha.training import extract_features_from_records

        records = [
            {
                "market_id": "m1",
                "entity_id": "y1",
                "timestamp": NOW.isoformat(),
                "outcome": 5,  # invalid
                "book": {
                    "token_id": "y1",
                    "condition_id": "c1",
                    "bids": [{"price": "0.49", "size": "100"}],
                    "asks": [{"price": "0.52", "size": "100"}],
                    "tick_size": "0.01",
                    "min_order_size": "1",
                    "timestamp": "1700000000000",
                },
            }
        ]
        samples = extract_features_from_records(records)
        assert len(samples) == 0

    def test_train_logistic_regression(self):
        from polyalpha.training import train_logistic_regression

        train = [_sample(f"m{i}", i % 2, mid=0.3 + (i % 5) * 0.1) for i in range(40)]
        test = [_sample(f"mt{i}", i % 2, mid=0.3 + (i % 5) * 0.1) for i in range(20)]
        result = train_logistic_regression(train, test)
        assert result.model_name == "logistic_regression"
        assert result.train_samples == 40
        assert result.test_samples == 20
        assert result.train_brier >= 0
        assert result.test_brier >= 0
        assert result.feature_importances is not None
        assert len(result.feature_importances) == 12

    def test_train_gradient_boosted(self):
        from polyalpha.training import train_gradient_boosted

        train = [_sample(f"m{i}", i % 2, mid=0.3 + (i % 5) * 0.1) for i in range(40)]
        test = [_sample(f"mt{i}", i % 2, mid=0.3 + (i % 5) * 0.1) for i in range(20)]
        result = train_gradient_boosted(train, test, n_estimators=10, max_depth=2)
        assert result.model_name == "gradient_boosted"
        assert result.test_ece >= 0
        assert result.feature_importances is not None

    def test_optimize_ensemble_weights_empty(self):
        from polyalpha.training import optimize_ensemble_weights

        assert optimize_ensemble_weights({}, []) == {}

    def test_optimize_ensemble_weights_single(self):
        from polyalpha.training import optimize_ensemble_weights

        class FakeModel:
            def predict(self, mid, book, at):
                from polyalpha.forecasting import Forecast

                return Forecast(mid, at, D("0.6"), D("0.55"), D("0.65"), "v1")

        result = optimize_ensemble_weights({"m1": FakeModel()}, [_sample("m1", 1)])
        assert result == {"m1": 1.0}

    def test_optimize_ensemble_weights_two(self):
        from polyalpha.training import optimize_ensemble_weights

        class FakeModel:
            def __init__(self, p):
                self.p = p

            def predict(self, mid, book, at):
                from polyalpha.forecasting import Forecast

                return Forecast(
                    mid, at, D(str(self.p)), D(str(self.p - 0.05)), D(str(self.p + 0.05)), "v1"
                )

        samples = [_sample(f"m{i}", i % 2) for i in range(20)]
        result = optimize_ensemble_weights({"a": FakeModel(0.6), "b": FakeModel(0.4)}, samples)
        assert abs(result["a"] + result["b"] - 1.0) < 0.01

    def test_optimize_ensemble_weights_four_plus(self):
        from polyalpha.training import optimize_ensemble_weights

        class FakeModel:
            def predict(self, mid, book, at):
                from polyalpha.forecasting import Forecast

                return Forecast(mid, at, D("0.5"), D("0.45"), D("0.55"), "v1")

        models = {f"m{i}": FakeModel() for i in range(5)}
        result = optimize_ensemble_weights(models, [_sample("x", 0)])
        assert len(result) == 5
        assert abs(sum(result.values()) - 1.0) < 0.001

    def test_train_ensemble_insufficient_data(self):
        from polyalpha.training import train_ensemble

        samples = [_sample("m1", 1)]
        result = train_ensemble(samples, samples, samples)
        assert "error" in result

    def test_fake_book(self):
        from polyalpha.training import _fake_book

        book = _fake_book(
            {
                "midpoint": 0.6,
                "spread": 0.03,
                "bid_price": 0.58,
                "ask_price": 0.61,
                "bid_size": 200,
                "ask_size": 150,
            }
        )
        assert book.best_bid == D("0.58")
        assert book.best_ask == D("0.61")

    def test_save_load_artifacts(self):
        from polyalpha.training import load_artifacts, save_artifacts

        with TemporaryDirectory() as tmpdir:
            # Create a simple model
            from polyalpha.models.trained import LogisticRegressionModel

            lr = LogisticRegressionModel()
            X = [
                [0.5, 0.02, 0.04, 0.1, 0.51, 100, 100, 0.49, 0.52, 0.5, 0.5, 0.01]
                for _ in range(30)
            ]
            y = [1] * 20 + [0] * 10
            lr.fit(X, y)

            save_artifacts(
                {"logistic_regression": lr}, {"logistic_regression": 1.0}, {"brier": 0.2}, tmpdir
            )
            loaded = load_artifacts(tmpdir)
            assert "ensemble_config" in loaded
            assert "logistic_regression" in loaded

    def test_result_to_dict(self):
        from polyalpha.training import TrainingResult, _result_to_dict

        r = TrainingResult("test", 10, 5, 0.2, 0.25, 0.03, 0.5, feature_importances={"f1": 0.1})
        d = _result_to_dict(r)
        assert d["model_name"] == "test"
        assert d["feature_importances"]["f1"] == 0.1


# ── maker.py ────────────────────────────────────────────────────────────────


class TestMakerQueue:
    def test_valid_creation(self):
        from polyalpha.maker import MakerQueue

        q = MakerQueue("BUY", D("0.50"), D(100), D(50))
        assert q.side == "BUY"
        assert q.remaining == D(100)

    def test_invalid_side(self):
        from polyalpha.maker import MakerQueue

        with pytest.raises(ValueError, match="invalid"):
            MakerQueue("INVALID", D("0.50"), D(100), D(0))

    def test_invalid_price_zero(self):
        from polyalpha.maker import MakerQueue

        with pytest.raises(ValueError, match="invalid"):
            MakerQueue("BUY", D(0), D(100), D(0))

    def test_invalid_price_one(self):
        from polyalpha.maker import MakerQueue

        with pytest.raises(ValueError, match="invalid"):
            MakerQueue("BUY", D(1), D(100), D(0))

    def test_negative_remaining(self):
        from polyalpha.maker import MakerQueue

        with pytest.raises(ValueError, match="invalid"):
            MakerQueue("BUY", D("0.50"), D(-1), D(0))

    def test_negative_queue_ahead(self):
        from polyalpha.maker import MakerQueue

        with pytest.raises(ValueError, match="invalid"):
            MakerQueue("BUY", D("0.50"), D(100), D(-1))

    def test_on_trade_wrong_aggressor(self):
        from polyalpha.maker import MakerQueue

        q = MakerQueue("BUY", D("0.50"), D(100), D(0))
        fill = q.on_trade("BUY", D("0.50"), D(50))
        assert fill == D(0)

    def test_on_trade_wrong_price(self):
        from polyalpha.maker import MakerQueue

        q = MakerQueue("BUY", D("0.50"), D(100), D(0))
        fill = q.on_trade("SELL", D("0.51"), D(50))
        assert fill == D(0)

    def test_on_trade_negative_volume(self):
        from polyalpha.maker import MakerQueue

        q = MakerQueue("BUY", D("0.50"), D(100), D(0))
        with pytest.raises(ValueError, match="negative"):
            q.on_trade("SELL", D("0.50"), D(-10))

    def test_on_trade_exact_fill(self):
        from polyalpha.maker import MakerQueue

        q = MakerQueue("BUY", D("0.50"), D(100), D(0))
        fill = q.on_trade("SELL", D("0.50"), D(100))
        assert fill == D(100)
        assert q.remaining == D(0)

    def test_on_trade_partial_fill(self):
        from polyalpha.maker import MakerQueue

        q = MakerQueue("BUY", D("0.50"), D(100), D(0))
        fill = q.on_trade("SELL", D("0.50"), D(30))
        assert fill == D(30)
        assert q.remaining == D(70)

    def test_on_trade_queue_ahead_consumed_first(self):
        from polyalpha.maker import MakerQueue

        q = MakerQueue("BUY", D("0.50"), D(100), D(50))
        fill = q.on_trade("SELL", D("0.50"), D(50))
        # 50 consumed by queue ahead, 0 fills us
        assert fill == D(0)
        assert q.queue_ahead == D(0)
        assert q.remaining == D(100)

    def test_on_trade_queue_ahead_partial_then_fill(self):
        from polyalpha.maker import MakerQueue

        q = MakerQueue("BUY", D("0.50"), D(100), D(30))
        fill = q.on_trade("SELL", D("0.50"), D(50))
        # 30 consumed by queue, 20 fills us
        assert fill == D(20)
        assert q.queue_ahead == D(0)
        assert q.remaining == D(80)

    def test_on_trade_aggressive_volume_exceeds(self):
        from polyalpha.maker import MakerQueue

        q = MakerQueue("BUY", D("0.50"), D(100), D(0))
        fill = q.on_trade("SELL", D("0.50"), D(200))
        assert fill == D(100)
        assert q.remaining == D(0)

    def test_sell_side_queue(self):
        from polyalpha.maker import MakerQueue

        q = MakerQueue("SELL", D("0.50"), D(100), D(0))
        fill = q.on_trade("BUY", D("0.50"), D(80))
        assert fill == D(80)
        assert q.remaining == D(20)

    def test_sell_side_wrong_aggressor(self):
        from polyalpha.maker import MakerQueue

        q = MakerQueue("SELL", D("0.50"), D(100), D(0))
        fill = q.on_trade("SELL", D("0.50"), D(50))
        assert fill == D(0)
