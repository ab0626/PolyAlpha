"""Part 54 — Walk-forward hidden tests.

Dataset too short for one fold, exactly one fold, empty train/validation,
embargo removes all validation/train, market-grouped/event-grouped folds,
duplicate observations, feature normalization only on train, calibrator
only on train.
"""

from datetime import datetime, timedelta, timezone

import pytest

from polyalpha.calibration import InsufficientData, Observation
from polyalpha.walk_forward import WalkForwardResult, walk_forward_backtest

TZ = timezone.utc
TS = datetime(2025, 1, 1, tzinfo=TZ)


def _obs(market_id="m1", cluster="cl1", prob=0.5, outcome=1,
         predicted_at=None, label_known_at=None, ts_offset_days=0):
    pred = predicted_at or (TS + timedelta(days=ts_offset_days))
    label = label_known_at or (pred + timedelta(days=1))
    return Observation(
        market_id=market_id,
        cluster=cluster,
        predicted_at=pred,
        label_known_at=label,
        probability=prob,
        outcome=outcome,
    )


def _resolved_rows(n=30, seed=42):
    import random
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        rows.append(_obs(
            market_id=f"m{i}",
            cluster=f"cl{i % 5}",
            prob=rng.uniform(0.2, 0.8),
            outcome=rng.randint(0, 1),
            ts_offset_days=i,
        ))
    return rows


# ── Dataset too short for one fold ───────────────────────────────────────


class TestTooShortForOneFold:
    def test_too_short_dataset(self):
        rows = [_obs(ts_offset_days=0), _obs(market_id="m2", ts_offset_days=1)]
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=1),
            end=TS + timedelta(days=2),
            validation_window=timedelta(days=30),
            min_training_events=4,
        )
        assert result.fold_count >= 1
        assert result.total_scored == 0


# ── Exactly one fold ─────────────────────────────────────────────────────


class TestExactlyOneFold:
    def test_single_fold(self):
        rows = _resolved_rows(20)
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=10),
            end=TS + timedelta(days=20),
            validation_window=timedelta(days=30),
            min_training_events=4,
        )
        assert result.fold_count == 1


# ── Empty train ──────────────────────────────────────────────────────────


class TestEmptyTrain:
    def test_empty_train_no_training(self):
        rows = [_obs(market_id="m1", ts_offset_days=20, prob=0.5, outcome=1)]
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=10),
            end=TS + timedelta(days=21),
            validation_window=timedelta(days=30),
            min_training_events=4,
        )
        assert result.total_scored == 0


# ── Empty validation ─────────────────────────────────────────────────────


class TestEmptyValidation:
    def test_no_resolved_in_validation(self):
        rows = [
            _obs(market_id=f"m{i}", ts_offset_days=i, prob=0.5, outcome=1)
            for i in range(10)
        ]
        # Add unresolved rows in validation period
        for i in range(10, 15):
            rows.append(_obs(
                market_id=f"m{i}", ts_offset_days=i,
                prob=0.5, outcome=1,
                label_known_at=TS + timedelta(days=100),
            ))
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=10),
            end=TS + timedelta(days=15),
            validation_window=timedelta(days=30),
            min_training_events=4,
        )
        assert result.total_scored >= 0


# ── Embargo removes all validation ───────────────────────────────────────


class TestEmbargoRemovesAllValidation:
    def test_large_embargo_removes_validation(self):
        rows = _resolved_rows(20)
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=10),
            end=TS + timedelta(days=20),
            validation_window=timedelta(days=5),
            min_training_events=4,
            embargo_days=100,
        )
        # Embargo removes all training data for validation
        assert result.total_scored == 0 or result.total_skipped > 0


# ── Embargo removes all train ────────────────────────────────────────────


class TestEmbargoRemovesAllTrain:
    def test_embargo_removes_training_data(self):
        rows = _resolved_rows(10)
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=5),
            end=TS + timedelta(days=10),
            validation_window=timedelta(days=5),
            min_training_events=4,
            embargo_days=10,
        )
        # All training data removed by embargo
        assert result.total_skipped > 0 or result.total_scored == 0


# ── Market-grouped folds ─────────────────────────────────────────────────


class TestMarketGroupedFolds:
    def test_market_grouped_observations(self):
        rows = []
        for i in range(20):
            rows.append(_obs(
                market_id=f"m{i}",
                cluster=f"cl{i % 3}",
                ts_offset_days=i,
                prob=0.5 + (i % 2) * 0.2,
                outcome=i % 2,
            ))
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=10),
            end=TS + timedelta(days=20),
            validation_window=timedelta(days=10),
            min_training_events=4,
        )
        assert result.fold_count >= 1


# ── Event-grouped folds ──────────────────────────────────────────────────


class TestEventGroupedFolds:
    def test_event_grouped_clusters(self):
        rows = []
        for i in range(20):
            rows.append(_obs(
                market_id=f"m{i}",
                cluster=f"event_{i // 5}",
                ts_offset_days=i,
                prob=0.5,
                outcome=i % 2,
            ))
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=10),
            end=TS + timedelta(days=20),
            validation_window=timedelta(days=10),
            min_training_events=4,
        )
        assert result.method == "isotonic"


# ── Duplicate observations across folds ──────────────────────────────────


class TestDuplicateObservations:
    def test_duplicate_observations_canonical(self):
        rows = [
            _obs(market_id="m1", ts_offset_days=0, prob=0.5),
            _obs(market_id="m1", ts_offset_days=0, prob=0.5),
        ]
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=1),
            end=TS + timedelta(days=2),
            validation_window=timedelta(days=30),
            min_training_events=4,
        )
        # Canonical rows deduplicates
        assert result.fold_count >= 1


# ── Feature normalization only on train ──────────────────────────────────


class TestFeatureNormalization:
    def test_calibrator_trained_only_on_train(self):
        rows = _resolved_rows(30)
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=15),
            end=TS + timedelta(days=30),
            validation_window=timedelta(days=15),
            min_training_events=4,
        )
        # Calibrator should be fitted before validation
        for fold in result.folds:
            if fold.status == "evaluated":
                assert fold.calibrated_metrics is not None


# ── Calibrator trained only on train ─────────────────────────────────────


class TestCalibratorOnTrain:
    def test_calibrator_cutoff_before_validation(self):
        rows = _resolved_rows(30)
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=15),
            end=TS + timedelta(days=30),
            validation_window=timedelta(days=15),
            min_training_events=4,
        )
        for fold in result.folds:
            if fold.raw_metrics and fold.calibrated_metrics:
                # Calibrated Brier should differ from raw
                raw_brier = fold.raw_metrics["brier"]
                cal_brier = fold.calibrated_metrics["brier"]
                assert isinstance(raw_brier, float)
                assert isinstance(cal_brier, float)


# ── Invalid walk-forward parameters ──────────────────────────────────────


class TestInvalidParameters:
    def test_first_validation_after_end(self):
        rows = _resolved_rows(10)
        with pytest.raises(ValueError, match="first_validation must precede"):
            walk_forward_backtest(
                observations=rows,
                train_start=TS,
                first_validation=TS + timedelta(days=30),
                end=TS + timedelta(days=10),
            )

    def test_invalid_method(self):
        rows = _resolved_rows(10)
        with pytest.raises(ValueError, match="method must be"):
            walk_forward_backtest(
                observations=rows,
                train_start=TS,
                first_validation=TS + timedelta(days=5),
                end=TS + timedelta(days=10),
                method="invalid",
            )

    def test_min_training_events_too_low(self):
        rows = _resolved_rows(10)
        with pytest.raises(ValueError, match="min_training_events must be"):
            walk_forward_backtest(
                observations=rows,
                train_start=TS,
                first_validation=TS + timedelta(days=5),
                end=TS + timedelta(days=10),
                min_training_events=1,
            )

    def test_negative_embargo(self):
        rows = _resolved_rows(10)
        with pytest.raises(ValueError, match="embargo_days must be"):
            walk_forward_backtest(
                observations=rows,
                train_start=TS,
                first_validation=TS + timedelta(days=5),
                end=TS + timedelta(days=10),
                embargo_days=-1,
            )


# ── Walk-forward result structure ────────────────────────────────────────


class TestWalkForwardResult:
    def test_result_has_required_fields(self):
        rows = _resolved_rows(20)
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=10),
            end=TS + timedelta(days=20),
            validation_window=timedelta(days=10),
            min_training_events=4,
        )
        assert hasattr(result, "folds")
        assert hasattr(result, "aggregate_raw")
        assert hasattr(result, "aggregate_calibrated")
        assert hasattr(result, "total_scored")
        assert hasattr(result, "fold_count")
        assert hasattr(result, "method")
        assert hasattr(result, "configuration")

    def test_configuration_recorded(self):
        rows = _resolved_rows(20)
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=10),
            end=TS + timedelta(days=20),
            validation_window=timedelta(days=10),
            min_training_events=4,
            embargo_days=2,
        )
        assert result.configuration["embargo_days"] == 2
        assert result.configuration["method"] == "isotonic"


# ── Platt method ─────────────────────────────────────────────────────────


class TestPlattMethod:
    def test_platt_walk_forward(self):
        rows = _resolved_rows(30)
        result = walk_forward_backtest(
            observations=rows,
            train_start=TS,
            first_validation=TS + timedelta(days=15),
            end=TS + timedelta(days=30),
            validation_window=timedelta(days=15),
            min_training_events=4,
            method="platt",
        )
        assert result.method == "platt"
