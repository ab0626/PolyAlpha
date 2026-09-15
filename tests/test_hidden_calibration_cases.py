"""Part 50 — Calibration hidden tests.

All labels 0/1, all predictions 0.5/identical, single/two observations,
perfect/inverted predictions, boundary probabilities, small/empty sample,
NaN values, out-of-range probabilities. Verify reject/clip/warn behavior.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from polyalpha.calibration import (
    InsufficientData,
    Isotonic,
    Observation,
    Platt,
    TemperatureScaling,
    canonical_rows,
    metrics,
    training_rows,
)

D = Decimal = None  # noqa: used only for type hints in Observation

TZ = timezone.utc
TS = datetime(2025, 6, 1, tzinfo=TZ)


def _obs(market_id="m1", cluster="cl1", prob=0.5, outcome=1,
         predicted_at=None, label_known_at=None, ts_offset=0):
    pred = predicted_at or (TS + timedelta(hours=ts_offset))
    label = label_known_at or (pred + timedelta(hours=1))
    return Observation(
        market_id=market_id,
        cluster=cluster,
        predicted_at=pred,
        label_known_at=label,
        probability=prob,
        outcome=outcome,
    )


def _resolved_rows(n=20, seed=42):
    import random
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        rows.append(_obs(
            market_id=f"m{i}",
            cluster=f"cl{i % 5}",
            prob=rng.uniform(0.2, 0.8),
            outcome=rng.randint(0, 1),
            ts_offset=i,
        ))
    return rows


# ── All labels 0 ─────────────────────────────────────────────────────────


class TestAllLabelsZero:
    def test_all_zero_outcomes_metrics(self):
        rows = [_obs(market_id=f"m{i}", outcome=0, prob=0.5, ts_offset=i) for i in range(10)]
        m = metrics([r.probability for r in rows], [r.outcome for r in rows])
        assert m["brier"] == 0.25

    def test_all_zero_outcomes_training_insufficient(self):
        rows = [_obs(market_id=f"m{i}", outcome=0, prob=0.5, ts_offset=i) for i in range(10)]
        with pytest.raises(InsufficientData):
            training_rows(rows, TS + timedelta(hours=20))


# ── All labels 1 ─────────────────────────────────────────────────────────


class TestAllLabelsOne:
    def test_all_one_outcomes_metrics(self):
        rows = [_obs(market_id=f"m{i}", outcome=1, prob=0.5, ts_offset=i) for i in range(10)]
        m = metrics([r.probability for r in rows], [r.outcome for r in rows])
        assert m["brier"] == 0.25

    def test_all_one_outcomes_training_insufficient(self):
        rows = [_obs(market_id=f"m{i}", outcome=1, prob=0.5, ts_offset=i) for i in range(10)]
        with pytest.raises(InsufficientData):
            training_rows(rows, TS + timedelta(hours=20))


# ── All predictions 0.5 ─────────────────────────────────────────────────


class TestAllPredictionsHalf:
    def test_all_half_predictions_brier(self):
        rows = [_obs(market_id=f"m{i}", prob=0.5, outcome=i % 2, ts_offset=i) for i in range(10)]
        m = metrics([0.5] * 10, [r.outcome for r in rows])
        assert 0.0 <= m["brier"] <= 1.0

    def test_all_half_predictions_ece(self):
        rows = [_obs(market_id=f"m{i}", prob=0.5, outcome=i % 2, ts_offset=i) for i in range(10)]
        m = metrics([0.5] * 10, [r.outcome for r in rows])
        assert m["ece"] >= 0


# ── All predictions identical ────────────────────────────────────────────


class TestAllPredictionsIdentical:
    def test_constant_prediction_metrics(self):
        rows = [_obs(market_id=f"m{i}", prob=0.7, outcome=i % 2, ts_offset=i) for i in range(10)]
        m = metrics([0.7] * 10, [r.outcome for r in rows])
        assert m["sample_size"] == 10
        assert m["brier"] > 0

    def test_constant_prediction_log_loss(self):
        rows = [_obs(market_id=f"m{i}", prob=0.7, outcome=1, ts_offset=i) for i in range(10)]
        m = metrics([0.7] * 10, [1] * 10)
        assert m["log_loss"] < float("inf")


# ── Single observation ──────────────────────────────────────────────────


class TestSingleObservation:
    def test_single_obs_metrics(self):
        m = metrics([0.5], [1])
        assert m["sample_size"] == 1
        assert m["brier"] == 0.25

    def test_single_obs_training_rejected(self):
        rows = [_obs(market_id="m1", outcome=1, prob=0.5)]
        with pytest.raises(InsufficientData):
            training_rows(rows, TS + timedelta(hours=2))


# ── Two observations ─────────────────────────────────────────────────────


class TestTwoObservations:
    def test_two_obs_metrics(self):
        m = metrics([0.3, 0.7], [0, 1])
        assert m["sample_size"] == 2
        assert m["brier"] > 0

    def test_two_obs_different_outcomes_metrics(self):
        m = metrics([0.5, 0.5], [0, 1])
        assert m["brier"] == 0.25


# ── Perfect predictions ──────────────────────────────────────────────────


class TestPerfectPredictions:
    def test_perfect_predictions_zero_brier(self):
        m = metrics([0.0, 1.0, 0.0, 1.0], [0, 1, 0, 1])
        assert m["brier"] == 0.0

    def test_perfect_predictions_log_loss_near_zero(self):
        m = metrics([0.001, 0.999, 0.001, 0.999], [0, 1, 0, 1])
        assert m["log_loss"] < 0.1


# ── Perfectly inverted predictions ───────────────────────────────────────


class TestInvertedPredictions:
    def test_inverted_predictions_high_brier(self):
        m = metrics([0.9, 0.1, 0.9, 0.1], [0, 1, 0, 1])
        assert m["brier"] > 0.5

    def test_inverted_worse_than_random(self):
        m_perfect = metrics([0.001, 0.999], [0, 1])
        m_inverted = metrics([0.999, 0.001], [0, 1])
        assert m_inverted["brier"] > m_perfect["brier"]


# ── Predictions exactly 0/1 ─────────────────────────────────────────────


class TestBoundaryPredictions:
    def test_prediction_exactly_zero(self):
        m = metrics([0.0, 0.5], [0, 1])
        assert m["brier"] >= 0
        assert m["log_loss"] < float("inf")

    def test_prediction_exactly_one(self):
        m = metrics([1.0, 0.5], [1, 0])
        assert m["brier"] >= 0
        assert m["log_loss"] < float("inf")


# ── Very small sample ────────────────────────────────────────────────────


class TestVerySmallSample:
    def test_three_samples_metrics(self):
        m = metrics([0.3, 0.6, 0.9], [0, 1, 1])
        assert m["sample_size"] == 3

    def test_one_sample_training_rejected(self):
        rows = [_obs(market_id="m1", outcome=1)]
        with pytest.raises(InsufficientData):
            training_rows(rows, TS + timedelta(hours=2))


# ── Empty sample ─────────────────────────────────────────────────────────


class TestEmptySample:
    def test_empty_metrics_rejected(self):
        with pytest.raises(ValueError, match="nonempty"):
            metrics([], [])

    def test_empty_canonical_rows(self):
        result = canonical_rows([])
        assert result == []


# ── NaN values ───────────────────────────────────────────────────────────


class TestNaNValues:
    def test_nan_probability_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            metrics([float("nan"), 0.5], [0, 1])

    def test_nan_in_outcome_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            metrics([0.5, 0.5], [0, float("nan")])


# ── Predictions outside [0,1] ───────────────────────────────────────────


class TestPredictionsOutOfRange:
    def test_negative_probability_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            metrics([-0.1, 0.5], [0, 1])

    def test_probability_above_one_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            metrics([1.5, 0.5], [0, 1])


# ── Isotonic calibration ─────────────────────────────────────────────────


class TestIsotonicCalibration:
    def test_isotonic_fit_predict(self):
        rows = _resolved_rows(20)
        iso = Isotonic()
        iso.fit(rows, TS + timedelta(hours=30))
        result = iso.predict(0.5)
        assert 0 <= result <= 1

    def test_isotonic_monotonic(self):
        rows = _resolved_rows(20)
        iso = Isotonic()
        iso.fit(rows, TS + timedelta(hours=30))
        p1 = iso.predict(0.3)
        p2 = iso.predict(0.7)
        assert p1 <= p2 + 0.01

    def test_isotonic_invalid_prediction_rejected(self):
        rows = _resolved_rows(20)
        iso = Isotonic()
        iso.fit(rows, TS + timedelta(hours=30))
        with pytest.raises(ValueError, match="invalid probability"):
            iso.predict(-0.1)


# ── Platt calibration ────────────────────────────────────────────────────


class TestPlattCalibration:
    def test_platt_fit_predict(self):
        rows = _resolved_rows(20)
        platt = Platt()
        platt.fit(rows, TS + timedelta(hours=30))
        result = platt.predict(0.5)
        assert 0 <= result <= 1

    def test_platt_logit_valid(self):
        assert Platt.logit(0.5) == 0.0
        assert Platt.logit(0.999) > 0
        assert Platt.logit(0.001) < 0

    def test_platt_logit_boundary(self):
        with pytest.raises(ValueError, match="invalid probability"):
            Platt.logit(-0.1)


# ── Temperature scaling ──────────────────────────────────────────────────


class TestTemperatureScaling:
    def test_temperature_fit(self):
        ts = TemperatureScaling()
        ts.fit([0.3, 0.7, 0.5, 0.8], [0, 1, 0, 1])
        assert ts.temperature > 0

    def test_temperature_predict(self):
        ts = TemperatureScaling()
        ts.fit([0.3, 0.7, 0.5, 0.8], [0, 1, 0, 1])
        result = ts.predict(0.7)
        assert 0 <= result <= 1

    def test_temperature_unfitted_rejected(self):
        ts = TemperatureScaling()
        with pytest.raises(RuntimeError, match="not fitted"):
            ts.predict(0.5)

    def test_temperature_too_few_observations(self):
        ts = TemperatureScaling()
        with pytest.raises(ValueError, match="at least two"):
            ts.fit([0.5], [1])


# ── Canonical rows deduplication ─────────────────────────────────────────


class TestCanonicalRows:
    def test_canonical_rows_first_forecast_per_market(self):
        rows = [
            _obs(market_id="m1", prob=0.5, ts_offset=0),
            _obs(market_id="m1", prob=0.6, ts_offset=1),
        ]
        result = canonical_rows(rows)
        assert len(result) == 1
        assert result[0].probability == 0.5

    def test_canonical_rows_different_markets(self):
        rows = [
            _obs(market_id="m1", prob=0.5, ts_offset=0),
            _obs(market_id="m2", prob=0.6, ts_offset=0),
        ]
        result = canonical_rows(rows)
        assert len(result) == 2

    def test_canonical_rows_conflicting_clusters(self):
        rows = [
            _obs(market_id="m1", cluster="cl1", ts_offset=0),
            _obs(market_id="m1", cluster="cl2", ts_offset=1),
        ]
        with pytest.raises(ValueError, match="inconsistent"):
            canonical_rows(rows)
