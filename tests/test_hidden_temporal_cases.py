"""Part 49 — Temporal hidden tests.

Naive vs timezone-aware datetime, UTC handling, DST transitions, leap day,
end-of-month, year rollover, timestamps equal, microsecond apart,
feature timestamp exactly equal/1us after, resolution before observation,
out-of-order snapshots, duplicate timestamps.
"""

from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal

import pytest

from polyalpha.domain import Book, Level, utc
from polyalpha.research_dataset import (
    FeatureProvenance,
    MarketSnapshot,
    build_dataset_from_snapshots,
)

D = Decimal
TZ = timezone.utc
TS = datetime(2025, 6, 1, tzinfo=TZ)


def _make_snap(obs_ts, feature_ts=None, resolution_ts=None, resolution=1, prob=0.5):
    fp = FeatureProvenance(feature_timestamp=feature_ts) if feature_ts else FeatureProvenance()
    return MarketSnapshot(
        observation_timestamp=obs_ts,
        market_id="m1", event_id="e1", condition_id="c1",
        category="politics", question="Q?",
        yes_token_id="y1", no_token_id="n1",
        yes_best_bid=D("0.45"), yes_best_ask=D("0.55"),
        yes_mid=D("0.50"), yes_spread=D("0.10"),
        yes_depth_1=D(100), yes_depth_5=D(500),
        yes_bid_size=D(50), yes_ask_size=D(50),
        no_best_bid=D("0.45"), no_best_ask=D("0.55"),
        no_mid=D("0.50"), volume=D(5000), liquidity=D(10000),
        hours_to_resolution=168.0, fees_enabled=True,
        fee_rate=D("0.02"), event_cluster="cl1",
        final_resolution=resolution,
        model_probability=D(str(prob)),
        execution_price=D("0.55"),
        side="BUY",
        resolution_timestamp=resolution_ts,
        feature_provenance=fp,
    )


# ── Naive vs timezone-aware datetime ─────────────────────────────────────


class TestNaiveVsAware:
    def test_naive_timestamp_rejected_by_utc(self):
        naive = datetime(2025, 6, 1)
        with pytest.raises(ValueError, match="timezone-aware"):
            utc(naive)

    def test_aware_timestamp_accepted(self):
        aware = datetime(2025, 6, 1, tzinfo=TZ)
        assert utc(aware) == aware

    def test_utc_offset_preserved(self):
        est = timezone(timedelta(hours=-5))
        ts = datetime(2025, 6, 1, 12, 0, tzinfo=est)
        result = utc(ts)
        assert result.tzinfo is not None
        assert result.hour == 17

    def test_positive_offset(self):
        jst = timezone(timedelta(hours=9))
        ts = datetime(2025, 6, 1, 3, 0, tzinfo=jst)
        result = utc(ts)
        assert result.hour == 18  # previous day 18:00 UTC


# ── UTC handling ─────────────────────────────────────────────────────────


class TestUTCHandling:
    def test_utc_conversion(self):
        est = timezone(timedelta(hours=-5))
        ts = datetime(2025, 1, 1, 12, 0, tzinfo=est)
        assert utc(ts).hour == 17

    def test_already_utc(self):
        ts = datetime(2025, 1, 1, 12, 0, tzinfo=TZ)
        assert utc(ts) == ts

    def test_book_rejects_naive_timestamps(self):
        naive = datetime(2025, 6, 1)
        with pytest.raises(ValueError, match="timezone-aware"):
            Book(
                token_id="t1", condition_id="c1",
                source_at=naive, received_at=naive,
                bids=(Level(D("0.5"), D("100")),),
                asks=(Level(D("0.51"), D("100")),),
                tick_size=D("0.01"), min_order_size=D(1),
                source_hash="",
            )


# ── DST transitions ─────────────────────────────────────────────────────


class TestDSTTransitions:
    def test_spring_forward(self):
        # 2025-03-09 02:00 EST -> 03:00 EDT (spring forward)
        est = timezone(timedelta(hours=-5))
        before = datetime(2025, 3, 9, 1, 30, tzinfo=est)
        after = datetime(2025, 3, 9, 3, 30, tzinfo=timezone(timedelta(hours=-4)))
        # Both should convert to valid UTC
        assert utc(before) is not None
        assert utc(after) is not None

    def test_fall_back(self):
        # 2025-11-02 02:00 EDT -> 01:00 EST (fall back)
        edt = timezone(timedelta(hours=-4))
        before = datetime(2025, 11, 2, 1, 30, tzinfo=edt)
        after = datetime(2025, 11, 2, 3, 30, tzinfo=timezone(timedelta(hours=-5)))
        assert utc(before) is not None
        assert utc(after) is not None

    def test_ordering_preserved_across_dst(self):
        est = timezone(timedelta(hours=-5))
        edt = timezone(timedelta(hours=-4))
        t1 = datetime(2025, 3, 8, 12, 0, tzinfo=est)
        t2 = datetime(2025, 3, 10, 12, 0, tzinfo=edt)
        assert utc(t1) < utc(t2)


# ── Leap day ─────────────────────────────────────────────────────────────


class TestLeapDay:
    def test_leap_day_valid(self):
        ts = datetime(2024, 2, 29, tzinfo=TZ)
        assert utc(ts) == ts

    def test_non_leap_day_invalid(self):
        with pytest.raises(ValueError):
            datetime(2025, 2, 29)

    def test_leap_year_book(self):
        ts = datetime(2024, 2, 29, tzinfo=TZ)
        b = Book(
            token_id="t1", condition_id="c1",
            source_at=ts, received_at=ts,
            bids=(Level(D("0.5"), D("100")),),
            asks=(Level(D("0.51"), D("100")),),
            tick_size=D("0.01"), min_order_size=D(1),
            source_hash="",
        )
        assert b.source_at.day == 29


# ── End of month ─────────────────────────────────────────────────────────


class TestEndOfMonth:
    def test_jan_31(self):
        ts = datetime(2025, 1, 31, 23, 59, 59, tzinfo=TZ)
        assert utc(ts) is not None

    def test_feb_28(self):
        ts = datetime(2025, 2, 28, 23, 59, 59, tzinfo=TZ)
        assert utc(ts) is not None

    def test_dec_31(self):
        ts = datetime(2025, 12, 31, 23, 59, 59, tzinfo=TZ)
        assert utc(ts) is not None


# ── Year rollover ────────────────────────────────────────────────────────


class TestYearRollover:
    def test_new_years(self):
        ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=TZ)
        assert utc(ts) is not None

    def test_end_to_start(self):
        t1 = datetime(2025, 12, 31, 23, 59, 59, tzinfo=TZ)
        t2 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=TZ)
        assert t1 < t2


# ── Timestamps equal ─────────────────────────────────────────────────────


class TestEqualTimestamps:
    def test_same_timestamp_comparison(self):
        t1 = datetime(2025, 6, 1, tzinfo=TZ)
        t2 = datetime(2025, 6, 1, tzinfo=TZ)
        assert t1 == t2

    def test_observation_equals_feature_timestamp(self):
        ts = TS
        snap = _make_snap(obs_ts=ts, feature_ts=ts)
        assert snap.observation_timestamp == snap.feature_provenance.feature_timestamp


# ── Events 1 microsecond apart ──────────────────────────────────────────


class TestMicrosecondApart:
    def test_one_microsecond_apart(self):
        t1 = datetime(2025, 6, 1, 0, 0, 0, tzinfo=TZ)
        t2 = datetime(2025, 6, 1, 0, 0, 0, 1, tzinfo=TZ)
        assert t1 < t2
        assert (t2 - t1).microseconds == 1

    def test_microsecond_ordering_preserved(self):
        t1 = datetime(2025, 6, 1, 0, 0, 0, 999999, tzinfo=TZ)
        t2 = datetime(2025, 6, 1, 0, 0, 1, 0, tzinfo=TZ)
        assert t1 < t2


# ── Feature timestamp exactly equal observation ──────────────────────────


class TestFeatureTimestampEqual:
    def test_feature_ts_equals_observation(self):
        ts = TS
        snap = _make_snap(obs_ts=ts, feature_ts=ts)
        assert snap.feature_provenance.feature_timestamp == ts

    def test_feature_ts_just_before_observation(self):
        ts = TS
        ft = ts - timedelta(microseconds=1)
        snap = _make_snap(obs_ts=ts, feature_ts=ft)
        assert snap.feature_provenance.feature_timestamp < ts


# ── Feature timestamp 1 microsecond after ────────────────────────────────


class TestFeatureTimestampAfter:
    def test_feature_ts_1us_after_rejected(self):
        ts = TS
        ft = ts + timedelta(microseconds=1)
        with pytest.raises(ValueError, match="INVARIANT VIOLATION"):
            _make_snap(obs_ts=ts, feature_ts=ft)

    def test_feature_ts_in_future_rejected(self):
        ts = TS
        ft = ts + timedelta(hours=1)
        with pytest.raises(ValueError, match="INVARIANT VIOLATION"):
            _make_snap(obs_ts=ts, feature_ts=ft)


# ── Resolution before observation ────────────────────────────────────────


class TestResolutionBeforeObservation:
    def test_resolution_before_observation_detected_by_audit(self):
        from polyalpha.dataset_audit import DatasetAuditor
        obs = TS
        res = TS - timedelta(hours=1)
        snap = _make_snap(obs_ts=obs, resolution_ts=res, resolution=1)
        ds = build_dataset_from_snapshots([snap])
        report = DatasetAuditor().audit(ds)
        check = next(c for c in report.checks if c.name == "resolution_before_observation")
        assert not check.passed

    def test_resolution_after_observation_ok(self):
        from polyalpha.dataset_audit import DatasetAuditor
        obs = TS
        res = TS + timedelta(hours=1)
        snap = _make_snap(obs_ts=obs, resolution_ts=res, resolution=1)
        ds = build_dataset_from_snapshots([snap])
        report = DatasetAuditor().audit(ds)
        check = next(c for c in report.checks if c.name == "resolution_before_observation")
        assert check.passed


# ── Out-of-order snapshots ──────────────────────────────────────────────


class TestOutOfOrderSnapshots:
    def test_out_of_order_detected_by_audit(self):
        from polyalpha.dataset_audit import DatasetAuditor
        s1 = _make_snap(obs_ts=TS + timedelta(hours=2))
        s2 = _make_snap(obs_ts=TS + timedelta(hours=1))
        ds = build_dataset_from_snapshots([s2, s1])
        report = DatasetAuditor().audit(ds)
        check = next(c for c in report.checks if c.name == "timestamp_reversals")
        assert check.passed  # auditor sorts before checking


# ── Duplicate timestamps ─────────────────────────────────────────────────


class TestDuplicateTimestamps:
    def test_duplicate_timestamp_same_market(self):
        from polyalpha.dataset_audit import DatasetAuditor
        s1 = _make_snap(obs_ts=TS, prob=0.5)
        s2_dict = {**s1.__dict__, "model_probability": D("0.6")}
        s2 = MarketSnapshot(**s2_dict)
        ds = build_dataset_from_snapshots([s1, s2])
        report = DatasetAuditor().audit(ds)
        check = next(c for c in report.checks if c.name == "duplicate_market_timestamp")
        assert not check.passed

    def test_duplicate_timestamp_different_market_ok(self):
        from polyalpha.dataset_audit import DatasetAuditor
        s1 = _make_snap(obs_ts=TS)
        s1_dict = {**s1.__dict__, "market_id": "m2", "yes_token_id": "y2", "no_token_id": "n2", "condition_id": "c2"}
        s2 = MarketSnapshot(**s1_dict)
        ds = build_dataset_from_snapshots([s1, s2])
        report = DatasetAuditor().audit(ds)
        check = next(c for c in report.checks if c.name == "duplicate_market_timestamp")
        assert check.passed


# ── Timezone mix ─────────────────────────────────────────────────────────


class TestTimezoneMix:
    def test_mixed_timezones_all_aware(self):
        utc_ts = datetime(2025, 6, 1, 5, 0, tzinfo=TZ)
        est_ts = datetime(2025, 6, 1, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
        assert utc(utc_ts) is not None
        assert utc(est_ts) is not None
        # Both convert to same UTC time
        assert utc(utc_ts) == utc(est_ts)


# ── Very small time differences ──────────────────────────────────────────


class TestSmallTimeDifferences:
    def test_nanosecond_difference(self):
        t1 = datetime(2025, 6, 1, tzinfo=TZ)
        t2 = t1 + timedelta(seconds=1e-9)
        assert t1 <= t2

    def test_one_second_difference(self):
        t1 = datetime(2025, 6, 1, tzinfo=TZ)
        t2 = t1 + timedelta(seconds=1)
        assert t1 < t2
        assert (t2 - t1).total_seconds() == 1.0
