"""Part 47 — Dataset audit hidden tests.

DatasetAuditor with empty dataset, single snapshot, all/none resolved,
perfect/inverted predictions, boundary probabilities, small samples,
NaN values, out-of-range probabilities, same market/event, conflicting
resolutions, YES/NO inconsistency.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyalpha.dataset_audit import DatasetAuditReport, DatasetAuditor
from polyalpha.research_dataset import (
    FeatureProvenance,
    MarketSnapshot,
    ResearchDataset,
    build_dataset_from_snapshots,
)

D = Decimal
TZ = timezone.utc
TS = datetime(2025, 1, 1, tzinfo=TZ)


def _snap(
    market_id="m1", event_id="e1", resolution=1, prob=0.5,
    bid=0.45, ask=0.55, ts_offset=0, event_cluster="cl1",
    obs_ts=None,
):
    ts = obs_ts or (TS + timedelta(hours=ts_offset))
    return MarketSnapshot(
        observation_timestamp=ts,
        market_id=market_id,
        event_id=event_id,
        condition_id=f"cond_{market_id}",
        category="politics",
        question=f"Will {market_id} resolve?",
        yes_token_id=f"yes_{market_id}",
        no_token_id=f"no_{market_id}",
        yes_best_bid=D(str(bid)) if bid is not None else None,
        yes_best_ask=D(str(ask)) if ask is not None else None,
        yes_mid=D(str((bid + ask) / 2)) if bid is not None and ask is not None else None,
        yes_spread=D(str(ask - bid)) if bid is not None and ask is not None else None,
        yes_depth_1=D(100),
        yes_depth_5=D(500),
        yes_bid_size=D(50),
        yes_ask_size=D(50),
        no_best_bid=D(str(1 - ask)) if ask is not None else None,
        no_best_ask=D(str(1 - bid)) if bid is not None else None,
        no_mid=D(str(1 - (bid + ask) / 2)) if bid is not None and ask is not None else None,
        volume=D(5000),
        liquidity=D(10000),
        hours_to_resolution=168.0,
        fees_enabled=True,
        fee_rate=D("0.02"),
        event_cluster=event_cluster,
        final_resolution=resolution,
        model_probability=D(str(prob)),
        execution_price=D(str(ask)) if ask is not None else None,
        side="BUY",
    )


def _dataset(snaps, labels=None):
    return build_dataset_from_snapshots(snaps, labels or [])


# ── Empty dataset ────────────────────────────────────────────────────────


class TestEmptyDataset:
    def test_auditor_empty_dataset_passes(self):
        ds = _dataset([])
        report = DatasetAuditor().audit(ds)
        assert report.total_rows == 0
        assert report.accepted_rows == 0
        assert report.passed

    def test_empty_dataset_summary(self):
        ds = _dataset([])
        report = DatasetAuditor().audit(ds)
        s = report.summary()
        assert s["total_rows"] == 0
        assert s["error_count"] == 0


# ── Single snapshot ──────────────────────────────────────────────────────


class TestSingleSnapshot:
    def test_single_valid_snapshot_passes(self):
        ds = _dataset([_snap()])
        report = DatasetAuditor().audit(ds)
        assert report.total_rows == 1
        assert report.passed

    def test_single_snapshot_no_checks_fail(self):
        ds = _dataset([_snap()])
        report = DatasetAuditor().audit(ds)
        assert report.error_count == 0


# ── All resolved ─────────────────────────────────────────────────────────


class TestAllResolved:
    def test_all_resolved_dataset(self):
        snaps = [_snap(market_id=f"m{i}", resolution=1) for i in range(10)]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        assert report.passed

    def test_missing_resolution_count_zero(self):
        snaps = [_snap(market_id=f"m{i}", resolution=i % 2) for i in range(5)]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        check = next(c for c in report.checks if c.name == "missing_resolution")
        assert check.passed or check.severity == "info"


# ── None resolved ────────────────────────────────────────────────────────


class TestNoneResolved:
    def test_none_resolved_warning(self):
        snaps = [_snap(market_id=f"m{i}", resolution=None) for i in range(5)]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        check = next(c for c in report.checks if c.name == "missing_resolution")
        assert check.severity in ("warning", "info")


# ── Perfect predictions ──────────────────────────────────────────────────


class TestPerfectPredictions:
    def test_perfect_predictions_no_reject(self):
        snaps = [_snap(market_id=f"m{i}", prob=1.0, resolution=1) for i in range(5)]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        prob_check = next(c for c in report.checks if c.name == "probability_bounds")
        assert not prob_check.passed
        assert prob_check.affected_rows > 0


# ── Inverted predictions ─────────────────────────────────────────────────


class TestInvertedPredictions:
    def test_inverted_predictions_pass_probability_bounds(self):
        snaps = [_snap(market_id=f"m{i}", prob=0.1, resolution=1) for i in range(5)]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        prob_check = next(c for c in report.checks if c.name == "probability_bounds")
        assert prob_check.passed


# ── Probabilities exactly 0 or 1 ─────────────────────────────────────────


class TestBoundaryProbabilities:
    def test_prob_exactly_zero_rejected(self):
        snaps = [_snap(prob=0.0)]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        prob_check = next(c for c in report.checks if c.name == "probability_bounds")
        assert not prob_check.passed

    def test_prob_exactly_one_rejected(self):
        snaps = [_snap(prob=1.0)]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        prob_check = next(c for c in report.checks if c.name == "probability_bounds")
        assert not prob_check.passed

    def test_prob_inside_bounds_accepted(self):
        snaps = [_snap(prob=0.01)]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        prob_check = next(c for c in report.checks if c.name == "probability_bounds")
        assert prob_check.passed


# ── Very small sample ────────────────────────────────────────────────────


class TestSmallSample:
    def test_single_snapshot_no_conflicts(self):
        ds = _dataset([_snap()])
        report = DatasetAuditor().audit(ds)
        assert report.error_count == 0

    def test_two_different_markets(self):
        ds = _dataset([_snap(market_id="m1"), _snap(market_id="m2")])
        report = DatasetAuditor().audit(ds)
        assert report.passed


# ── NaN values ───────────────────────────────────────────────────────────


class TestNaNValues:
    def test_nan_in_yes_mid_detected(self):
        snaps = [_snap()]
        nan_snap = MarketSnapshot(
            **{**snaps[0].__dict__, "yes_mid": D("NaN")}
        )
        ds = _dataset([nan_snap])
        with pytest.raises(Exception):
            DatasetAuditor().audit(ds)

    def test_nan_in_model_probability_detected(self):
        snaps = [_snap()]
        nan_snap = MarketSnapshot(
            **{**snaps[0].__dict__, "model_probability": D("NaN")}
        )
        ds = _dataset([nan_snap])
        with pytest.raises(Exception):
            DatasetAuditor().audit(ds)


# ── Predictions outside [0,1] ───────────────────────────────────────────


class TestPredictionsOutOfRange:
    def test_negative_probability_rejected(self):
        snaps = [_snap(prob=-0.1)]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        prob_check = next(c for c in report.checks if c.name == "probability_bounds")
        assert not prob_check.passed

    def test_probability_above_one_rejected(self):
        snaps = [_snap(prob=1.5)]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        prob_check = next(c for c in report.checks if c.name == "probability_bounds")
        assert not prob_check.passed


# ── Same market, same event ──────────────────────────────────────────────


class TestSameMarketEvent:
    def test_same_market_different_timestamps_passes(self):
        snaps = [
            _snap(market_id="m1", ts_offset=0),
            _snap(market_id="m1", ts_offset=1),
        ]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        dup_check = next(c for c in report.checks if c.name == "duplicate_market_timestamp")
        assert dup_check.passed

    def test_same_market_same_timestamp_detected(self):
        ts = TS
        snaps = [
            _snap(market_id="m1", obs_ts=ts, prob=0.5),
            _snap(market_id="m1", obs_ts=ts, prob=0.6),
        ]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        dup_check = next(c for c in report.checks if c.name == "duplicate_market_timestamp")
        assert not dup_check.passed


# ── Conflicting resolutions ──────────────────────────────────────────────


class TestConflictingResolutions:
    def test_same_market_conflicting_resolution_detected(self):
        ts = TS
        s1 = _snap(market_id="m1", resolution=1, obs_ts=ts)
        s2_dict = {
            **s1.__dict__,
            "final_resolution": 0,
            "model_probability": D("0.6"),
        }
        s2 = MarketSnapshot(**s2_dict)
        ds = _dataset([s1, s2])
        report = DatasetAuditor().audit(ds)
        conflict_check = next(
            c for c in report.checks if c.name == "conflicting_resolutions"
        )
        assert not conflict_check.passed

    def test_different_market_conflicting_events_no_conflict(self):
        snaps = [
            _snap(market_id="m1", event_id="e1", resolution=1),
            _snap(market_id="m2", event_id="e2", resolution=0),
        ]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        conflict_check = next(
            c for c in report.checks if c.name == "duplicate_event_labels"
        )
        assert conflict_check.passed


# ── YES/NO inconsistency with realistic spreads ──────────────────────────


class TestYesNoInconsistency:
    def test_consistent_yes_no_passes(self):
        snaps = [_snap(bid=0.4, ask=0.5)]
        ds = _dataset(snaps)
        report = DatasetAuditor().audit(ds)
        yn_check = next(
            c for c in report.checks if c.name == "yes_no_inconsistency"
        )
        assert yn_check.passed

    def test_inconsistent_yes_no_detected(self):
        """yes_best_bid > 1 - no_best_ask + 0.02 => inconsistency."""
        snap = MarketSnapshot(
            observation_timestamp=TS,
            market_id="m1",
            event_id="e1",
            condition_id="c1",
            category="politics",
            question="Q?",
            yes_token_id="y1",
            no_token_id="n1",
            yes_best_bid=D("0.6"),
            yes_best_ask=D("0.7"),
            yes_mid=D("0.65"),
            yes_spread=D("0.1"),
            no_best_bid=D("0.2"),
            no_best_ask=D("0.3"),
            no_mid=D("0.25"),
            no_spread=D("0.1"),
            volume=D(5000),
            liquidity=D(10000),
            hours_to_resolution=168.0,
            fees_enabled=True,
            fee_rate=D("0.02"),
            event_cluster="cl1",
            final_resolution=1,
            model_probability=D("0.5"),
        )
        ds = _dataset([snap])
        report = DatasetAuditor().audit(ds)
        yn_check = next(
            c for c in report.checks if c.name == "yes_no_inconsistency"
        )
        assert not yn_check.passed


# ── Bid > ask (impossible book) ─────────────────────────────────────────


class TestImpossibleBook:
    def test_bid_greater_than_ask_detected(self):
        snap = MarketSnapshot(
            observation_timestamp=TS,
            market_id="m1",
            event_id="e1",
            condition_id="c1",
            category="politics",
            question="Q?",
            yes_token_id="y1",
            no_token_id="n1",
            yes_best_bid=D("0.7"),
            yes_best_ask=D("0.5"),
            volume=D(5000),
            liquidity=D(10000),
            hours_to_resolution=168.0,
            fees_enabled=True,
            fee_rate=D("0.02"),
            event_cluster="cl1",
            final_resolution=1,
            model_probability=D("0.5"),
        )
        ds = _dataset([snap])
        report = DatasetAuditor().audit(ds)
        ba_check = next(
            c for c in report.checks if c.name == "impossible_bid_ask"
        )
        assert not ba_check.passed


# ── Negative sizes detected ──────────────────────────────────────────────


class TestNegativeSizesDetected:
    def test_negative_depth_detected(self):
        snap = MarketSnapshot(
            observation_timestamp=TS,
            market_id="m1",
            event_id="e1",
            condition_id="c1",
            category="politics",
            question="Q?",
            yes_token_id="y1",
            no_token_id="n1",
            yes_best_bid=D("0.45"),
            yes_best_ask=D("0.55"),
            yes_mid=D("0.50"),
            yes_spread=D("0.10"),
            yes_depth_1=D(-100),
            yes_depth_5=D(500),
            yes_bid_size=D(50),
            yes_ask_size=D(50),
            volume=D(5000),
            liquidity=D(10000),
            hours_to_resolution=168.0,
            fees_enabled=True,
            fee_rate=D("0.02"),
            event_cluster="cl1",
            final_resolution=1,
            model_probability=D("0.5"),
        )
        ds = _dataset([snap])
        report = DatasetAuditor().audit(ds)
        neg_check = next(c for c in report.checks if c.name == "negative_sizes")
        assert not neg_check.passed
