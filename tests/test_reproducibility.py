"""Reproducibility tests.

Part 66: Verifies that experiments and analyses are fully reproducible
when the same seed and configuration are used. This includes:
- Same deterministic seed → identical results
- Different seeds → different results
- Experiment manifest contains all required fields
- Dataset fingerprint is deterministic
- Two runs with same config produce same manifest
"""

import hashlib
import json
import random
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from polyalpha.experiments import ExperimentRecord, ExperimentTracker
from polyalpha.research_dataset import (
    MarketSnapshot,
    ResearchDataset,
    build_dataset_from_snapshots,
)
from polyalpha.performance import brier_score, drawdown_series

D = Decimal
TS = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _rng(seed):
    return random.Random(seed)


def _snapshots(n=50, seed=42):
    rng = _rng(seed)
    snaps = []
    for i in range(n):
        p = round(rng.uniform(0.3, 0.7), 4)
        mid = round(p + rng.gauss(0, 0.03), 4)
        mid = max(0.15, min(0.85, mid))
        spread = round(rng.uniform(0.01, 0.05), 4)
        outcome = 1 if rng.random() < p else 0
        ts = TS + timedelta(hours=i)
        snaps.append(MarketSnapshot(
            observation_timestamp=ts,
            market_id=f"m{i:04d}", event_id=f"evt_{i // 5}",
            condition_id=f"c{i:04d}",
            category=["politics", "sports", "crypto"][i % 3],
            question=f"Q #{i}?",
            yes_token_id=f"yes_{i}", no_token_id=f"no_{i}",
            yes_best_bid=D(str(round(mid - spread / 2, 4))),
            yes_best_ask=D(str(round(mid + spread / 2, 4))),
            yes_mid=D(str(mid)),
            yes_spread=D(str(spread)),
            yes_depth_1=D(str(rng.uniform(50, 500))),
            yes_depth_5=D(str(rng.uniform(200, 2000))),
            yes_bid_size=D(str(rng.uniform(20, 200))),
            yes_ask_size=D(str(rng.uniform(20, 200))),
            no_best_bid=D(str(round(1 - mid - spread / 2, 4))),
            no_best_ask=D(str(round(1 - mid + spread / 2, 4))),
            no_mid=D(str(round(1 - mid, 4))),
            volume=D(str(rng.uniform(100, 10000))),
            liquidity=D(str(rng.uniform(500, 50000))),
            hours_to_resolution=rng.uniform(1, 720),
            fees_enabled=True,
            fee_rate=D("0.02"),
            event_cluster=f"cluster_{i // 10}",
            final_resolution=outcome,
            model_probability=D(str(p)),
            execution_price=D(str(round(mid + spread / 2, 4))),
            side="BUY",
        ))
    return snaps


def _brier(snaps):
    resolved = [s for s in snaps if s.final_resolution is not None and s.model_probability is not None]
    if not resolved:
        return 1.0
    return sum((float(s.model_probability) - s.final_resolution) ** 2 for s in resolved) / len(resolved)


def _compute_data_hash(snaps):
    import hashlib as hl
    import json as js
    content = js.dumps(
        [{"mid": str(s.yes_mid), "prob": str(s.model_probability),
          "market_id": s.market_id, "outcome": s.final_resolution}
         for s in sorted(snaps, key=lambda x: x.observation_timestamp)],
        default=str,
    )
    return hl.sha256(content.encode()).hexdigest()[:16]


# ── Same seed → identical results ───────────────────────────────────────────


class TestSameSeedIdenticalResults:
    def test_same_rng_seed_produces_identical_snapshots(self):
        snaps1 = _snapshots(50, seed=42)
        snaps2 = _snapshots(50, seed=42)
        assert len(snaps1) == len(snaps2)
        for s1, s2 in zip(snaps1, snaps2):
            assert s1.market_id == s2.market_id
            assert s1.yes_mid == s2.yes_mid
            assert s1.model_probability == s2.model_probability
            assert s1.final_resolution == s2.final_resolution

    def test_same_seed_same_brier(self):
        snaps1 = _snapshots(50, seed=42)
        snaps2 = _snapshots(50, seed=42)
        assert _brier(snaps1) == _brier(snaps2)

    def test_same_seed_same_dataset_hash(self):
        snaps1 = _snapshots(50, seed=42)
        snaps2 = _snapshots(50, seed=42)
        ds1 = build_dataset_from_snapshots(snaps1, ["test"])
        ds2 = build_dataset_from_snapshots(snaps2, ["test"])
        assert ds1.data_hash == ds2.data_hash

    def test_same_seed_same_drawdown_series(self):
        rng1 = _rng(42)
        rng2 = _rng(42)
        equities1 = [10000.0]
        equities2 = [10000.0]
        for _ in range(20):
            equities1.append(equities1[-1] * (1 + rng1.uniform(-0.05, 0.05)))
            equities2.append(equities2[-1] * (1 + rng2.uniform(-0.05, 0.05)))
        assert drawdown_series(equities1) == drawdown_series(equities2)


# ── Different seeds → different results ──────────────────────────────────────


class TestDifferentSeedsDifferentResults:
    def test_different_rng_seeds_produce_different_snapshots(self):
        snaps1 = _snapshots(50, seed=42)
        snaps2 = _snapshots(50, seed=99)
        different = any(
            s1.yes_mid != s2.yes_mid or s1.model_probability != s2.model_probability
            for s1, s2 in zip(snaps1, snaps2)
        )
        assert different, "Different seeds produced identical snapshots"

    def test_different_seeds_different_brier(self):
        snaps1 = _snapshots(100, seed=42)
        snaps2 = _snapshots(100, seed=99)
        assert _brier(snaps1) != _brier(snaps2)


# ── Experiment manifest fields ───────────────────────────────────────────────


class TestExperimentManifest:
    def test_record_contains_all_required_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = ExperimentTracker(tmpdir)
            exp = tracker.record(
                config={"learning_rate": 0.01, "n_estimators": 100},
                metrics={"brier": 0.22, "sharpe": 1.5},
                model_version="v1.0",
                feature_version="fv1",
                training_window="2024-01-01/2024-06-30",
                validation_window="2024-07-01/2024-12-31",
            )

            required = {
                "experiment_id", "git_commit", "source_sha256",
                "model_version", "feature_version",
                "training_window", "validation_window",
                "configuration", "data_sha256", "metrics", "created_at",
            }
            record_dict = exp.to_dict()
            assert required.issubset(set(record_dict.keys())), (
                f"Missing fields: {required - set(record_dict.keys())}"
            )

    def test_experiment_id_is_unique(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = ExperimentTracker(tmpdir)
            exp1 = tracker.record(config={}, metrics={})
            exp2 = tracker.record(config={}, metrics={})
            assert exp1.experiment_id != exp2.experiment_id

    def test_experiment_recorded_to_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = ExperimentTracker(tmpdir)
            exp = tracker.record(config={"a": 1}, metrics={"b": 2})
            loaded = tracker.load(exp.experiment_id)
            assert loaded is not None
            assert loaded["experiment_id"] == exp.experiment_id
            assert loaded["configuration"] == {"a": 1}
            assert loaded["metrics"] == {"b": 2}

    def test_list_experiments_returns_all(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = ExperimentTracker(tmpdir)
            tracker.record(config={}, metrics={})
            tracker.record(config={}, metrics={})
            tracker.record(config={}, metrics={})
            exps = tracker.list_experiments()
            assert len(exps) == 3


# ── Dataset fingerprint determinism ──────────────────────────────────────────


class TestDatasetFingerprintDeterminism:
    def test_same_snapshots_same_hash(self):
        snaps1 = _snapshots(50, seed=42)
        snaps2 = _snapshots(50, seed=42)
        ds1 = build_dataset_from_snapshots(snaps1, ["test"])
        ds2 = build_dataset_from_snapshots(snaps2, ["test"])
        assert ds1.data_hash == ds2.data_hash

    def test_different_snapshots_different_hash(self):
        snaps1 = _snapshots(50, seed=42)
        snaps2 = _snapshots(50, seed=99)
        ds1 = build_dataset_from_snapshots(snaps1, ["test"])
        ds2 = build_dataset_from_snapshots(snaps2, ["test"])
        assert ds1.data_hash != ds2.data_hash

    def test_hash_independent_of_source_labels(self):
        snaps = _snapshots(50, seed=42)
        ds1 = build_dataset_from_snapshots(snaps, ["label_a"])
        ds2 = build_dataset_from_snapshots(snaps, ["label_b"])
        assert ds1.data_hash == ds2.data_hash

    def test_custom_hash_deterministic(self):
        snaps = _snapshots(50, seed=42)
        h1 = _compute_data_hash(snaps)
        h2 = _compute_data_hash(snaps)
        assert h1 == h2
        assert len(h1) == 16


# ── Two runs same config → same manifest ─────────────────────────────────────


class TestReproducibleExperimentRuns:
    def test_two_runs_same_config_same_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = ExperimentTracker(tmpdir)
            config = {"lr": 0.01, "epochs": 100, "seed": 42}
            metrics1 = {"brier": 0.22, "ece": 0.03}
            metrics2 = {"brier": 0.22, "ece": 0.03}

            exp1 = tracker.record(config=config, metrics=metrics1)
            exp2 = tracker.record(config=config, metrics=metrics2)

            loaded1 = tracker.load(exp1.experiment_id)
            loaded2 = tracker.load(exp2.experiment_id)
            assert loaded1["configuration"] == loaded2["configuration"]
            assert loaded1["metrics"] == loaded2["metrics"]

    def test_same_source_hash_across_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = ExperimentTracker(tmpdir)
            exp1 = tracker.record(config={}, metrics={})
            exp2 = tracker.record(config={}, metrics={})
            assert exp1.source_sha256 == exp2.source_sha256

    def test_experiment_json_is_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = ExperimentTracker(tmpdir)
            exp = tracker.record(config={"x": 1}, metrics={"y": 2})
            path = Path(tmpdir) / f"{exp.experiment_id}.json"
            assert path.exists()
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["experiment_id"] == exp.experiment_id
            assert data["configuration"] == {"x": 1}
