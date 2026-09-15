"""Part 51 — Model hidden tests.

Missing/extra features, feature ordering, constant/all-zero features,
unknown categorical, model file missing/corrupted, prediction NaN/Inf/<0/>1,
ensemble edge cases, weight issues, component exceptions.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyalpha.domain import Book, Level
from polyalpha.ensemble import EnsembleModel
from polyalpha.forecasting import Baseline, Forecast, ensemble, net_edge
from polyalpha.execution import FeeSchedule, Fill, Order

D = Decimal
TZ = timezone.utc
TS = datetime(2025, 6, 1, tzinfo=TZ)
FEE_FREE = FeeSchedule(D(0), TS, "free")


def _book(mid=0.5, spread=0.02, bid_size=100, ask_size=100):
    bid_p = mid - spread / 2
    ask_p = mid + spread / 2
    return Book(
        token_id="t1", condition_id="c1",
        source_at=TS, received_at=TS,
        bids=(Level(D(str(bid_p)), D(str(bid_size))),),
        asks=(Level(D(str(ask_p)), D(str(ask_size))),),
        tick_size=D("0.01"), min_order_size=D(1),
        source_hash="",
    )


def _fill(shares=50, vwap=0.5, side="BUY"):
    return Fill(
        order_id="o1", token_id="t1", side=side,
        requested=D(str(shares)), shares=D(str(shares)),
        notional=D(str(shares * vwap)), fees=D(0),
        depth_slippage=D(0), filled_at=TS,
        levels=((D(str(vwap)), D(str(shares))),),
    )


# ── Missing features ─────────────────────────────────────────────────────


class TestMissingFeatures:
    def test_baseline_with_empty_book_features(self):
        b = _book(mid=0.5, spread=0.02)
        model = Baseline()
        f = model.predict("m1", b, TS)
        assert 0 <= f.probability <= 1

    def test_baseline_rejects_stale_book(self):
        b = Book(
            token_id="t1", condition_id="c1",
            source_at=TS - timedelta(seconds=60),
            received_at=TS - timedelta(seconds=60),
            bids=(Level(D("0.49"), D("100")),),
            asks=(Level(D("0.51"), D("100")),),
            tick_size=D("0.01"), min_order_size=D(1),
            source_hash="",
        )
        model = Baseline()
        with pytest.raises(ValueError, match="stale"):
            model.predict("m1", b, TS)


# ── Extra features ───────────────────────────────────────────────────────


class TestExtraFeatures:
    def test_baseline_ignores_extra_book_levels(self):
        b = Book(
            token_id="t1", condition_id="c1",
            source_at=TS, received_at=TS,
            bids=(
                Level(D("0.49"), D("100")),
                Level(D("0.48"), D("200")),
                Level(D("0.47"), D("300")),
            ),
            asks=(
                Level(D("0.51"), D("100")),
                Level(D("0.52"), D("200")),
                Level(D("0.53"), D("300")),
            ),
            tick_size=D("0.01"), min_order_size=D(1),
            source_hash="",
        )
        model = Baseline()
        f = model.predict("m1", b, TS)
        assert 0 <= f.probability <= 1


# ── Feature ordering ─────────────────────────────────────────────────────


class TestFeatureOrdering:
    def test_book_with_many_levels_midpoint_unchanged(self):
        bids = [(D("0.48") - D(str(i)) * D("0.01"), D("100")) for i in range(5)]
        asks = [(D("0.52") + D(str(i)) * D("0.01"), D("100")) for i in range(5)]
        b = Book(
            token_id="t1", condition_id="c1",
            source_at=TS, received_at=TS,
            bids=tuple(Level(p, s) for p, s in bids),
            asks=tuple(Level(p, s) for p, s in asks),
            tick_size=D("0.01"), min_order_size=D(1),
            source_hash="",
        )
        assert b.mid == D("0.50")


# ── Constant feature ─────────────────────────────────────────────────────


class TestConstantFeature:
    def test_baseline_with_tight_spread(self):
        b = _book(mid=0.5, spread=0.02, bid_size=100, ask_size=100)
        model = Baseline()
        f = model.predict("m1", b, TS)
        assert 0 <= f.probability <= 1


# ── All-zero feature ─────────────────────────────────────────────────────


class TestAllZeroFeature:
    def test_zero_bid_size_rejected(self):
        with pytest.raises(ValueError, match="size must be positive"):
            _book(mid=0.5, spread=0.02, bid_size=0, ask_size=100)


# ── Model not trained ────────────────────────────────────────────────────


class TestModelNotTrained:
    def test_sklearn_model_not_trained(self):
        from polyalpha.models.trained import LogisticRegressionModel
        model = LogisticRegressionModel()
        with pytest.raises(ValueError, match="not trained"):
            model.predict("m1", _book(), TS)


# ── Forecast validation ──────────────────────────────────────────────────


class TestForecastValidation:
    def test_forecast_probability_outside_bounds(self):
        with pytest.raises(ValueError, match="invalid probability bounds"):
            Forecast(
                market_id="m1", timestamp=TS,
                probability=D("1.5"),
                lower=D(0), upper=D(1),
                version="test",
            )

    def test_forecast_lower_greater_than_probability(self):
        with pytest.raises(ValueError, match="invalid probability bounds"):
            Forecast(
                market_id="m1", timestamp=TS,
                probability=D("0.5"),
                lower=D("0.6"), upper=D(1),
                version="test",
            )

    def test_forecast_upper_less_than_probability(self):
        with pytest.raises(ValueError, match="invalid probability bounds"):
            Forecast(
                market_id="m1", timestamp=TS,
                probability=D("0.5"),
                lower=D(0), upper=D("0.4"),
                version="test",
            )

    def test_forecast_negative_probability(self):
        with pytest.raises(ValueError, match="invalid probability bounds"):
            Forecast(
                market_id="m1", timestamp=TS,
                probability=D("-0.1"),
                lower=D("-0.2"), upper=D(0),
                version="test",
            )

    def test_forecast_nan_probability(self):
        with pytest.raises(ValueError, match="invalid probability bounds"):
            Forecast(
                market_id="m1", timestamp=TS,
                probability=D("NaN"),
                lower=D(0), upper=D(1),
                version="test",
            )

    def test_forecast_inf_probability(self):
        with pytest.raises(ValueError, match="invalid probability bounds"):
            Forecast(
                market_id="m1", timestamp=TS,
                probability=D("Infinity"),
                lower=D(0), upper=D("Infinity"),
                version="test",
            )


# ── Ensemble with zero models ────────────────────────────────────────────


class TestEnsembleZeroModels:
    def test_ensemble_no_components_rejected(self):
        with pytest.raises(ValueError, match="at least one component"):
            EnsembleModel(components=[])


# ── Ensemble with one model ──────────────────────────────────────────────


class TestEnsembleOneModel:
    def test_ensemble_single_component(self):
        from polyalpha.forecasting import ProbabilityModel
        class StubModel:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.6"), lower=D("0.5"), upper=D("0.7"),
                    version="stub",
                )
        ens = EnsembleModel(components=[("stub", StubModel(), D(1))])
        f = ens.predict("m1", _book(), TS)
        assert f.probability == D("0.6")


# ── Weights not summing to 1 ────────────────────────────────────────────


class TestWeightsNotSummingToOne:
    def test_weights_sum_too_high(self):
        class StubModel:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.5"), lower=D(0), upper=D(1),
                    version="stub",
                )
        with pytest.raises(ValueError, match="weights must sum"):
            EnsembleModel(components=[
                ("a", StubModel(), D("0.6")),
                ("b", StubModel(), D("0.6")),
            ])

    def test_weights_sum_too_low(self):
        class StubModel:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.5"), lower=D(0), upper=D(1),
                    version="stub",
                )
        with pytest.raises(ValueError, match="weights must sum"):
            EnsembleModel(components=[
                ("a", StubModel(), D("0.2")),
                ("b", StubModel(), D("0.2")),
            ])


# ── Negative weights ─────────────────────────────────────────────────────


class TestNegativeWeights:
    def test_negative_weight_rejected(self):
        class StubModel:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.5"), lower=D(0), upper=D(1),
                    version="stub",
                )
        with pytest.raises(ValueError, match="non-negative"):
            EnsembleModel(components=[
                ("a", StubModel(), D("1.5")),
                ("b", StubModel(), D("-0.5")),
            ])


# ── Duplicate models ─────────────────────────────────────────────────────


class TestDuplicateModels:
    def test_duplicate_model_names_ok(self):
        class StubModel:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.5"), lower=D(0), upper=D(1),
                    version="stub",
                )
        ens = EnsembleModel(components=[
            ("a", StubModel(), D("0.5")),
            ("a", StubModel(), D("0.5")),
        ])
        assert "a" in ens.component_names


# ── Component raises exception ───────────────────────────────────────────


class TestComponentException:
    def test_failing_component_skipped(self):
        class FailingModel:
            def predict(self, market_id, book, at):
                raise ValueError("model failure")
        class GoodModel:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.6"), lower=D("0.5"), upper=D("0.7"),
                    version="good",
                )
        ens = EnsembleModel(components=[
            ("fail", FailingModel(), D("0.5")),
            ("good", GoodModel(), D("0.5")),
        ])
        f = ens.predict("m1", _book(), TS)
        assert f.probability == D("0.6")

    def test_all_components_failing(self):
        class FailingModel:
            def predict(self, market_id, book, at):
                raise ValueError("failure")
        ens = EnsembleModel(components=[
            ("a", FailingModel(), D("0.5")),
            ("b", FailingModel(), D("0.5")),
        ])
        with pytest.raises(ValueError, match="all component"):
            ens.predict("m1", _book(), TS)


# ── Component produces NaN ───────────────────────────────────────────────


class TestComponentProducesNaN:
    def test_nan_prediction_component(self):
        class NaNModel:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("NaN"),
                    lower=D(0), upper=D(1),
                    version="nan",
                )
        class GoodModel:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.6"), lower=D("0.5"), upper=D("0.7"),
                    version="good",
                )
        ens = EnsembleModel(components=[
            ("nan", NaNModel(), D("0.5")),
            ("good", GoodModel(), D("0.5")),
        ])
        # NaN model will produce invalid Forecast, skipped
        f = ens.predict("m1", _book(), TS)
        assert f.probability == D("0.6")


# ── Ensemble weighting ───────────────────────────────────────────────────


class TestEnsembleWeighting:
    def test_ensemble_weighted_average(self):
        class ModelA:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.4"), lower=D("0.3"), upper=D("0.5"),
                    version="a",
                )
        class ModelB:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.6"), lower=D("0.5"), upper=D("0.7"),
                    version="b",
                )
        ens = EnsembleModel(components=[
            ("a", ModelA(), D("0.5")),
            ("b", ModelB(), D("0.5")),
        ])
        f = ens.predict("m1", _book(), TS)
        assert f.probability == D("0.5")

    def test_ensemble_asymmetric_weights(self):
        class ModelA:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.4"), lower=D("0.3"), upper=D("0.5"),
                    version="a",
                )
        class ModelB:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.6"), lower=D("0.5"), upper=D("0.7"),
                    version="b",
                )
        ens = EnsembleModel(components=[
            ("a", ModelA(), D("0.7")),
            ("b", ModelB(), D("0.3")),
        ])
        f = ens.predict("m1", _book(), TS)
        # 0.4*0.7 + 0.6*0.3 = 0.28 + 0.18 = 0.46
        assert f.probability == D("0.46")


# ── net_edge validation ─────────────────────────────────────────────────


class TestNetEdgeValidation:
    def test_net_edge_requires_buy(self):
        f = _fill(side="SELL")
        forecast = Forecast(
            market_id="m1", timestamp=TS,
            probability=D("0.6"), lower=D("0.5"), upper=D("0.7"),
            version="test",
        )
        with pytest.raises(ValueError, match="entry evaluation requires a buy"):
            net_edge(forecast, f, True)

    def test_net_edge_negative_slippage_rejected(self):
        f = _fill()
        forecast = Forecast(
            market_id="m1", timestamp=TS,
            probability=D("0.6"), lower=D("0.5"), upper=D("0.7"),
            version="test",
        )
        with pytest.raises(ValueError, match="negative cost buffer"):
            net_edge(forecast, f, True, extra_slippage=D(-0.01))


# ── Update weights ───────────────────────────────────────────────────────


class TestUpdateWeights:
    def test_update_weights_valid(self):
        class StubModel:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.5"), lower=D(0), upper=D(1),
                    version="stub",
                )
        ens = EnsembleModel(components=[
            ("a", StubModel(), D("0.5")),
            ("b", StubModel(), D("0.5")),
        ])
        ens.update_weights({"a": D("0.3"), "b": D("0.7")})
        assert ens.weight_dict["a"] == D("0.3")
        assert ens.weight_dict["b"] == D("0.7")

    def test_update_weights_unknown_component(self):
        class StubModel:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.5"), lower=D(0), upper=D(1),
                    version="stub",
                )
        ens = EnsembleModel(components=[
            ("a", StubModel(), D("1")),
        ])
        with pytest.raises(ValueError, match="unknown component"):
            ens.update_weights({"unknown": D("1")})

    def test_update_weights_not_summing_to_one(self):
        class StubModel:
            def predict(self, market_id, book, at):
                return Forecast(
                    market_id=market_id, timestamp=at,
                    probability=D("0.5"), lower=D(0), upper=D(1),
                    version="stub",
                )
        ens = EnsembleModel(components=[
            ("a", StubModel(), D("0.5")),
            ("b", StubModel(), D("0.5")),
        ])
        with pytest.raises(ValueError, match="new weights must sum"):
            ens.update_weights({"a": D("0.3"), "b": D("0.3")})
