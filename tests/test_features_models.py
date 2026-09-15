"""Tests for features/market.py, features/cross_market.py, features/temporal.py, features/external.py."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polyalpha.domain import Book, Level, Market

D = Decimal

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
DEADLINE = datetime(2026, 3, 1, tzinfo=UTC)
RECEIVED = datetime(2026, 1, 1, tzinfo=UTC)


def _market(**overrides):
    defaults = dict(
        market_id="m1",
        condition_id="c1",
        event_ids=("e1",),
        question="Test question?",
        description="Test description",
        resolution_source="official",
        deadline=DEADLINE,
        active=True,
        closed=False,
        accepting_orders=True,
        enable_order_book=True,
        liquidity=D(10000),
        volume=D(50000),
        fees_enabled=True,
        fee_parameters_json='{"rate":0.02}',
        yes_token_id="y1",
        no_token_id="n1",
        received_at=RECEIVED,
        category="politics",
    )
    defaults.update(overrides)
    return Market(**defaults)


def _book(bid=0.49, ask=0.52, bid_size=100, ask_size=100, levels=3):
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


# ── features/market.py ──────────────────────────────────────────────────────


class TestMarketFeatures:
    def test_liquidity_score_zero(self):
        from polyalpha.features.market import liquidity_score

        m = _market(liquidity=D(0))
        assert liquidity_score(m) == D(1).ln()

    def test_liquidity_score_large(self):
        from polyalpha.features.market import liquidity_score

        m = _market(liquidity=D(100000))
        assert liquidity_score(m) > D(10)

    def test_liquidity_score_none(self):
        from polyalpha.features.market import liquidity_score

        m = _market(liquidity=None)
        assert liquidity_score(m) == D(1).ln()

    def test_volume_score_zero(self):
        from polyalpha.features.market import volume_score

        m = _market(volume=D(0))
        assert volume_score(m) == D(1).ln()

    def test_volume_score_none(self):
        from polyalpha.features.market import volume_score

        m = _market(volume=None)
        assert volume_score(m) == D(1).ln()

    def test_liquidity_volume_ratio(self):
        from polyalpha.features.market import liquidity_volume_ratio

        m = _market(liquidity=D(10000), volume=D(50000))
        assert liquidity_volume_ratio(m) == D(5)

    def test_liquidity_volume_ratio_zero_liquidity(self):
        from polyalpha.features.market import liquidity_volume_ratio

        m = _market(liquidity=D(0), volume=D(100))
        assert liquidity_volume_ratio(m) is None

    def test_liquidity_volume_ratio_none_liq(self):
        from polyalpha.features.market import liquidity_volume_ratio

        m = _market(liquidity=None, volume=D(100))
        assert liquidity_volume_ratio(m) is None

    def test_has_fee_schedule_true(self):
        from polyalpha.features.market import has_fee_schedule

        m = _market(fees_enabled=True, fee_parameters_json='{"rate":0.02}')
        assert has_fee_schedule(m) == 1

    def test_has_fee_schedule_no_params(self):
        from polyalpha.features.market import has_fee_schedule

        m = _market(fees_enabled=True, fee_parameters_json=None)
        assert has_fee_schedule(m) == 0

    def test_has_fee_schedule_disabled(self):
        from polyalpha.features.market import has_fee_schedule

        m = _market(fees_enabled=False)
        assert has_fee_schedule(m) == 0

    def test_has_resolution_source_true(self):
        from polyalpha.features.market import has_resolution_source

        m = _market(resolution_source="official")
        assert has_resolution_source(m) == 1

    def test_has_resolution_source_none(self):
        from polyalpha.features.market import has_resolution_source

        m = _market(resolution_source=None)
        assert has_resolution_source(m) == 0

    def test_extract_market_features_completeness(self):
        from polyalpha.features.market import extract_market_features

        m = _market()
        feats = extract_market_features(m)
        expected_keys = {
            "liquidity_score",
            "volume_score",
            "liquidity_volume_ratio",
            "has_fee_schedule",
            "has_resolution_source",
        }
        assert expected_keys == set(feats.keys())


# ── features/cross_market.py ────────────────────────────────────────────────


class TestCrossMarketFeatures:
    def test_single_book_returns_none(self):
        from polyalpha.features.cross_market import price_correlation_features

        book = _book()
        result = price_correlation_features({"y1": book}, {"n1": book})
        assert result["cross_market_spread_dispersion"] is None
        assert result["cross_market_mid_dispersion"] is None

    def test_empty_books(self):
        from polyalpha.features.cross_market import price_correlation_features

        result = price_correlation_features({}, {})
        assert result["cross_market_spread_dispersion"] is None

    def test_two_books_same_price(self):
        from polyalpha.features.cross_market import price_correlation_features

        b1 = _book(bid=0.49, ask=0.51)
        b2 = _book(bid=0.49, ask=0.51)
        result = price_correlation_features({"y1": b1, "y2": b2}, {})
        assert result["cross_market_mid_dispersion"] == D(0)
        assert result["cross_market_count"] == 2

    def test_two_books_different_prices(self):
        from polyalpha.features.cross_market import price_correlation_features

        b1 = _book(bid=0.40, ask=0.42)
        b2 = _book(bid=0.60, ask=0.62)
        result = price_correlation_features({"y1": b1, "y2": b2}, {})
        assert result["cross_market_mid_dispersion"] > D(0)
        assert result["cross_market_spread_dispersion"] == D(0)

    def test_relative_value_signals_empty(self):
        from polyalpha.features.cross_market import relative_value_signals

        result = relative_value_signals({}, {})
        assert result["partition_deviation"] is None
        assert result["max_relative_mispricing"] is None

    def test_relative_value_signals_partition_sum(self):
        from polyalpha.features.cross_market import relative_value_signals

        b1 = _book(bid=0.39, ask=0.41)
        b2 = _book(bid=0.59, ask=0.61)
        forecasts = {"m1": D("0.40"), "m2": D("0.60")}
        result = relative_value_signals({"m1": b1, "m2": b2}, forecasts)
        assert result["partition_deviation"] == D(0)
        assert result["model_partition_sum"] == D(1)

    def test_relative_value_signals_overpriced(self):
        from polyalpha.features.cross_market import relative_value_signals

        forecasts = {"m1": D("0.60"), "m2": D("0.60")}
        result = relative_value_signals({}, forecasts)
        assert result["partition_deviation"] == D("0.20")

    def test_relative_value_mispricing(self):
        from polyalpha.features.cross_market import relative_value_signals

        b1 = _book(bid=0.49, ask=0.51)
        forecasts = {"m1": D("0.60"), "m2": D("0.40")}
        result = relative_value_signals({"m1": b1}, forecasts)
        assert result["max_relative_mispricing"] == D("0.10")


# ── features/temporal.py ────────────────────────────────────────────────────


class TestTemporalFeatures:
    def test_time_until_resolution(self):
        from polyalpha.features.temporal import time_until_resolution

        m = _market(deadline=DEADLINE)
        secs = time_until_resolution(m, NOW)
        expected = (DEADLINE - NOW).total_seconds()
        assert secs == D(str(expected))

    def test_time_until_resolution_none_deadline(self):
        from polyalpha.features.temporal import time_until_resolution

        m = _market(deadline=None)
        assert time_until_resolution(m, NOW) is None

    def test_time_until_resolution_past_deadline(self):
        from polyalpha.features.temporal import time_until_resolution

        m = _market(deadline=NOW - timedelta(days=1))
        assert time_until_resolution(m, NOW) == D(0)

    def test_market_age(self):
        from polyalpha.features.temporal import market_age

        m = _market(received_at=RECEIVED)
        age = market_age(m, NOW)
        expected = (NOW - RECEIVED).total_seconds()
        assert age == D(str(expected))

    def test_resolution_urgency_far(self):
        from polyalpha.features.temporal import resolution_urgency

        m = _market(deadline=NOW + timedelta(days=365))
        urgency = resolution_urgency(m, NOW)
        # Far deadline → low urgency
        assert urgency < D("0.1")

    def test_resolution_urgency_near(self):
        from polyalpha.features.temporal import resolution_urgency

        m = _market(deadline=NOW + timedelta(hours=1))
        urgency = resolution_urgency(m, NOW)
        # Very near deadline → high urgency
        assert urgency > D("0.9")

    def test_resolution_urgency_none(self):
        from polyalpha.features.temporal import resolution_urgency

        m = _market(deadline=None)
        assert resolution_urgency(m, NOW) == D("0.5")

    def test_is_weekend(self):
        from polyalpha.features.temporal import is_weekend

        # Saturday
        assert is_weekend(datetime(2026, 1, 17, tzinfo=UTC)) == 1
        # Monday
        assert is_weekend(datetime(2026, 1, 19, tzinfo=UTC)) == 0

    def test_hour_of_day(self):
        from polyalpha.features.temporal import hour_of_day

        assert hour_of_day(NOW) == 12

    def test_days_in_week(self):
        from polyalpha.features.temporal import days_in_week

        # 2026-01-15 is Thursday = 3
        assert days_in_week(NOW) == 3

    def test_extract_temporal_features(self):
        from polyalpha.features.temporal import extract_temporal_features

        m = _market()
        feats = extract_temporal_features(m, NOW)
        expected_keys = {
            "time_until_resolution",
            "market_age_seconds",
            "resolution_urgency",
            "is_weekend",
            "hour_of_day",
            "day_of_week",
        }
        assert expected_keys == set(feats.keys())
        assert feats["is_weekend"] == 0
        assert feats["hour_of_day"] == 12


# ── features/external.py ────────────────────────────────────────────────────


class TestExternalFeatures:
    def test_empty_facts(self):
        from polyalpha.features.external import extract_external_features

        result = extract_external_features([], NOW)
        assert result["external_fact_count"] == 0
        assert result["has_external_data"] == 0
        assert result["external_recency_hours"] is None

    def test_single_fact(self):
        from polyalpha.external import ExternalFact
        from polyalpha.features.external import extract_external_features

        fact = ExternalFact(
            event_id="e1",
            source_url="https://example.com",
            published_at=NOW - timedelta(hours=2),
            retrieved_at=NOW,
            features={"polling_avg": 0.55, "sample_size": 1000},
        )
        result = extract_external_features([fact], NOW)
        assert result["external_fact_count"] == 1
        assert result["has_external_data"] == 1
        assert D(str(result["external_recency_hours"])) == D("2.0")
        assert result["ext_polling_avg"] == 0.55

    def test_multiple_facts_averaged(self):
        from polyalpha.external import ExternalFact
        from polyalpha.features.external import extract_external_features

        f1 = ExternalFact("e1", "url", NOW - timedelta(hours=1), NOW, {"val": 0.6})
        f2 = ExternalFact("e1", "url", NOW - timedelta(hours=3), NOW, {"val": 0.4})
        result = extract_external_features([f1, f2], NOW)
        assert result["external_fact_count"] == 2
        assert result["ext_val"] == 0.5

    def test_recency_is_newest(self):
        from polyalpha.external import ExternalFact
        from polyalpha.features.external import extract_external_features

        f1 = ExternalFact("e1", "url", NOW - timedelta(hours=10), NOW, {})
        f2 = ExternalFact("e1", "url", NOW - timedelta(hours=1), NOW, {})
        result = extract_external_features([f1, f2], NOW)
        assert D(str(result["external_recency_hours"])) == D("1.0")
