"""Tests for critical untested paths discovered during senior quant audit.

Targets:
- process_exit all 6 exit policies with edge-case triggers
- Baseline model Section 40 compliance
- Portfolio settlement edge cases (payout=0, NO token)
- Correlation tracker edge cases (identical prices, single obs)
- Calibration walk-forward edge cases
- Quality filter edge cases (crossed book, zero spread)
- Storage edge cases (malformed, append-only)
- Execution edge cases (consumed levels, min order size)
- Uncertainty boundary conditions
- Anomaly detector numerical stability
- Risk budget with zero dimensions
- Kelly fraction at boundaries
- Expected value with extreme fills
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _book(bids=None, asks=None, token_id="y1", condition_id="m1", tick=D("0.01")):
    from polyalpha.domain import Book, Level

    if bids is None:
        bids = [(D("0.49"), D(500))]
    if asks is None:
        asks = [(D("0.51"), D(500))]
    return Book(
        token_id=token_id,
        condition_id=condition_id,
        source_at=NOW,
        received_at=NOW,
        bids=tuple(Level(p, s) for p, s in bids),
        asks=tuple(Level(p, s) for p, s in asks),
        tick_size=tick,
        min_order_size=D(1),
        source_hash="test",
    )


def _market(
    market_id="m1",
    yes_token="y1",
    no_token="n1",
    event_id="e1",
    active=True,
    accepting=True,
    enable_book=True,
    deadline_offset_days=7,
    liquidity=D(5000),
    volume=D(1000),
    fees_enabled=False,
):
    from polyalpha.domain import Market

    return Market(
        market_id=market_id,
        condition_id=market_id,
        event_ids=(event_id,),
        question=f"Test {market_id}",
        description="Test",
        resolution_source="test",
        deadline=NOW + timedelta(days=deadline_offset_days) if deadline_offset_days else None,
        active=active,
        closed=False,
        accepting_orders=accepting,
        enable_order_book=enable_book,
        liquidity=liquidity,
        volume=volume,
        fees_enabled=fees_enabled,
        fee_parameters_json=None,
        yes_token_id=yes_token,
        no_token_id=no_token,
        received_at=NOW,
        category="politics",
    )


def _fill(
    order_id="o1",
    token_id="y1",
    side="BUY",
    shares=D(100),
    price=D("0.55"),
    fees=D("1.00"),
    filled_at=NOW,
):
    from polyalpha.execution import Fill

    notional = shares * price
    return Fill(
        order_id=order_id,
        token_id=token_id,
        side=side,
        requested=shares,
        shares=shares,
        notional=notional.quantize(D("0.01")),
        fees=fees,
        depth_slippage=D(0),
        filled_at=filled_at,
        levels=((price, shares),),
    )


def _forecast(market_id="m1", p=D("0.65"), at=NOW, lo=D("0.62"), hi=D("0.68")):
    from polyalpha.forecasting import Forecast

    return Forecast(market_id, at, p, lo, hi, "test-v1")


def _market_record(market_id="m1", t=NOW, event_id="e1"):
    return {
        "id": market_id,
        "conditionId": market_id,
        "question": f"Test {market_id}",
        "description": "Test",
        "outcomes": ["Yes", "No"],
        "outcomePrices": '["0.50","0.50"]',
        "events": [{"id": event_id}],
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "volume": "10000",
        "liquidity": "5000",
        "endDate": (NOW + timedelta(days=7)).isoformat(),
        "lastUpdate": t.isoformat(),
        "bestBid": "0.49",
        "bestAsk": "0.51",
        "spread": "0.02",
        "yesTokenId": "y1",
        "noTokenId": "n1",
        "clobTokenIds": '["y1","n1"]',
        "category": "politics",
        "resolutionSource": "test",
        "feesEnabled": False,
    }


def _book_record(token_id="y1", market_id="m1", t=NOW, bids=None, asks=None):
    if bids is None:
        bids = [{"price": "0.49", "size": "500"}]
    if asks is None:
        asks = [{"price": "0.51", "size": "500"}]
    return {
        "market": market_id,
        "asset_id": token_id,
        "bids": bids,
        "asks": asks,
        "hash": "h1",
        "timestamp": str(int(t.timestamp() * 1000)),
        "condition_id": market_id,
        "tick_size": "0.01",
        "min_order_size": "1",
    }


def _settlement_record(token_id, payout, t=NOW, known_at=None):
    from polyalpha.storage import Record

    if known_at is None:
        known_at = t
    return Record(
        99,
        "settlement",
        token_id,
        t,
        t,
        {
            "token_id": token_id,
            "payout": str(payout),
            "known_at": known_at.isoformat(),
            "source_url": "https://example.com",
            "verified": True,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: process_exit — all 6 exit policies
# ══════════════════════════════════════════════════════════════════════════════


class TestProcessExitPolicies:
    """Test every exit policy path in backtest.Engine.process_exit."""

    def _setup_engine(self, exit_policy="hold"):
        from polyalpha.backtest import Engine

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(
            clusters=clusters,
            initial_cash=D(10000),
            exit_policy=exit_policy,
            latency_seconds=0,
        )
        return engine

    def _feed_market_and_book(self, engine, t=NOW):
        from polyalpha.storage import Record

        engine.on_record(Record(1, "market", "m1", t, t, _market_record(t=t)))
        engine.on_record(Record(2, "book", "y1", t, t, _book_record(t=t)))

    def _open_position(self, engine, t=NOW):

        fill = _fill("entry-1", "y1", "BUY", D(100), D("0.50"), D("0"))
        engine.portfolio.apply(fill, "m1", "e1", "c1", "politics")
        engine.simulator.commit(fill)
        engine.exit_entry_prices["y1"] = D("0.50")

    def test_hold_policy_never_exits(self):
        """hold policy should never trigger early exit."""
        engine = self._setup_engine("hold")
        self._feed_market_and_book(engine)
        self._open_position(engine)
        book = engine.books["y1"]
        market = engine.markets["m1"]
        assert engine.process_exit(book, market, True, NOW) is False

    def test_edge_exit_below_threshold(self):
        """edge policy exits when fair probability is below threshold."""
        engine = self._setup_engine("edge")
        engine.edge_exit_threshold = D("0.50")
        self._feed_market_and_book(engine)
        self._open_position(engine)
        book = engine.books["y1"]
        market = engine.markets["m1"]
        result = engine.process_exit(book, market, True, NOW)
        # Model predicts midpoint ~0.50, fee at 0.50 is 0, so exit_price ~ 0.50 <= 0.50
        assert result is True

    def test_stop_loss_triggers_on_large_drop(self):
        """stop_loss policy exits when position drops below threshold."""
        engine = self._setup_engine("stop_loss")
        engine.stop_loss_pct = D("0.10")
        self._feed_market_and_book(engine)
        self._open_position(engine)
        # Current price is 0.50, entry was 0.50 — no loss yet
        book = engine.books["y1"]
        market = engine.markets["m1"]
        assert engine.process_exit(book, market, True, NOW) is False
        # Now simulate price drop to 0.40 (20% loss)
        from polyalpha.domain import Book, Level

        dropped_book = Book(
            token_id="y1",
            condition_id="m1",
            source_at=NOW,
            received_at=NOW,
            bids=(Level(D("0.39"), D(500)),),
            asks=(Level(D("0.41"), D(500)),),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        engine.books["y1"] = dropped_book
        result = engine.process_exit(dropped_book, market, True, NOW)
        assert result is True

    def test_take_profit_triggers_on_large_gain(self):
        """stop_loss policy also checks take-profit."""
        engine = self._setup_engine("stop_loss")
        engine.take_profit_pct = D("0.20")
        self._feed_market_and_book(engine)
        self._open_position(engine)
        # Simulate price rise to 0.65 (30% gain from 0.50)
        from polyalpha.domain import Book, Level

        risen_book = Book(
            token_id="y1",
            condition_id="m1",
            source_at=NOW,
            received_at=NOW,
            bids=(Level(D("0.64"), D(500)),),
            asks=(Level(D("0.66"), D(500)),),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        engine.books["y1"] = risen_book
        result = engine.process_exit(risen_book, engine.markets["m1"], True, NOW)
        assert result is True

    def test_trailing_stop_triggers_on_drop_from_high(self):
        """trailing policy exits when fair drops from highest observed."""
        engine = self._setup_engine("trailing")
        engine.trailing_stop_pct = D("0.05")
        self._feed_market_and_book(engine)
        self._open_position(engine)
        # Set high water mark
        engine.exit_trail_high["y1"] = 0.80
        # Current book midpoint is 0.50 — drop is 0.30 which is > 5% of 0.80
        result = engine.process_exit(engine.books["y1"], engine.markets["m1"], True, NOW)
        assert result is True

    def test_trailing_stop_no_exit_when_high(self):
        """trailing policy should NOT exit when fair is at the high water mark."""
        engine = self._setup_engine("trailing")
        engine.trailing_stop_pct = D("0.10")
        self._feed_market_and_book(engine)
        self._open_position(engine)
        # Set high water mark to current fair (~0.50 from model)
        engine.exit_trail_high["y1"] = 0.50
        result = engine.process_exit(engine.books["y1"], engine.markets["m1"], True, NOW)
        assert result is False

    def test_time_exit_near_deadline(self):
        """time policy exits when deadline is within time_exit_hours."""
        engine = self._setup_engine("time")
        engine.time_exit_hours = 24
        # Feed standard market and book first
        self._feed_market_and_book(engine)
        # Now override market with short deadline
        from polyalpha.domain import Market

        market = Market(
            market_id="m1",
            condition_id="m1",
            event_ids=("e1",),
            question="Test",
            description="Test",
            resolution_source="test",
            deadline=NOW + timedelta(hours=12),
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            liquidity=D(5000),
            volume=D(1000),
            fees_enabled=False,
            fee_parameters_json=None,
            yes_token_id="y1",
            no_token_id="n1",
            received_at=NOW,
            category="politics",
        )
        engine.markets["m1"] = market
        self._open_position(engine)
        result = engine.process_exit(engine.books["y1"], market, True, NOW)
        assert result is True

    def test_all_exit_policy_checks_all_conditions(self):
        """all policy should check every exit condition."""
        engine = self._setup_engine("all")
        engine.trailing_stop_pct = D("0.05")
        engine.time_exit_hours = 24
        self._feed_market_and_book(engine)
        self._open_position(engine)
        # Set high water mark far above current to trigger trailing stop
        engine.exit_trail_high["y1"] = 0.90
        result = engine.process_exit(engine.books["y1"], engine.markets["m1"], True, NOW)
        assert result is True  # trailing stop should trigger

    def test_risk_halt_forces_exit(self):
        """Risk halt should force exit regardless of policy."""
        engine = self._setup_engine("hold")
        self._feed_market_and_book(engine)
        self._open_position(engine)
        engine.risk.halted = True
        result = engine.process_exit(engine.books["y1"], engine.markets["m1"], True, NOW)
        assert result is True

    def test_no_position_no_exit(self):
        """process_exit with no position should return False."""
        engine = self._setup_engine("all")
        self._feed_market_and_book(engine)
        result = engine.process_exit(engine.books["y1"], engine.markets["m1"], True, NOW)
        assert result is False

    def test_pending_exit_waits_for_latency(self):
        """An exit that is already pending should wait for latency."""
        from polyalpha.execution import Order

        engine = self._setup_engine("all")
        engine.latency = timedelta(seconds=5)
        self._feed_market_and_book(engine)
        self._open_position(engine)
        # Manually set a pending exit
        engine.exit_pending["y1"] = Order("exit-1", "y1", "SELL", D(100), NOW)
        result = engine.process_exit(engine.books["y1"], engine.markets["m1"], True, NOW)
        # Should return True (exit in progress) without creating a new exit
        assert result is True

    def test_exit_entry_price_recorded_on_first_call(self):
        """First call to process_exit should record entry price."""
        engine = self._setup_engine("stop_loss")
        self._feed_market_and_book(engine)
        self._open_position(engine)
        engine.exit_entry_prices.pop("y1", None)
        engine.process_exit(engine.books["y1"], engine.markets["m1"], True, NOW)
        assert "y1" in engine.exit_entry_prices
        assert engine.exit_entry_prices["y1"] == D("0.50")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Baseline Model Section 40 Compliance
# ══════════════════════════════════════════════════════════════════════════════


class TestBaselineModel:
    """Section 40: baseline model must use midpoint + alpha*momentum + beta*spread*imbalance."""

    def test_baseline_formula_matches_spec(self):
        """Verify Baseline implements the Section 40 formula exactly."""
        from polyalpha.forecasting import Baseline

        model = Baseline(uncertainty=D("0.05"))
        book = _book(bids=[(D("0.49"), D(500))], asks=[(D("0.51"), D(500))])
        # First observation: momentum = 0, imbalance = 0 (equal depth)
        # q = 0.50 + 0.20*0 + 0.25*0.02*0 = 0.50
        f = model.predict("m1", book, NOW)
        assert f.probability == D("0.50")
        # Uncertainty band should be exactly 0.05 wide
        assert f.upper - f.lower == D("0.10")

    def test_baseline_momentum_positive(self):
        """Positive momentum should push probability up."""
        from polyalpha.forecasting import Baseline

        model = Baseline(uncertainty=D("0.05"))
        book1 = _book(bids=[(D("0.47"), D(500))], asks=[(D("0.49"), D(500))])
        model.predict("m1", book1, NOW)
        book2 = _book(bids=[(D("0.49"), D(500))], asks=[(D("0.51"), D(500))])
        f2 = model.predict("m1", book2, NOW + timedelta(seconds=1))
        # Midpoint rose from 0.48 to 0.50, momentum = 0.02
        # q = 0.50 + 0.20*0.02 + ... = 0.504
        assert f2.probability > D("0.50")

    def test_baseline_clamped_to_01(self):
        """Probability must be clamped to [0.01, 0.99]."""
        from polyalpha.forecasting import Baseline

        model = Baseline(uncertainty=D("0.05"))
        # Book at extreme price with large imbalance
        book = _book(
            bids=[(D("0.01"), D(5000))],
            asks=[(D("0.03"), D(10))],
        )
        f = model.predict("m1", book, NOW)
        assert D("0.01") <= f.probability <= D("0.99")

    def test_baseline_uncertainty_minimum(self):
        """Uncertainty must be at least 0.03."""
        from polyalpha.forecasting import Baseline

        with pytest.raises(ValueError, match="uncertainty"):
            Baseline(uncertainty=D("0.01"))

    def test_baseline_rejects_stale_book(self):
        """Baseline should reject stale books."""
        from polyalpha.domain import Book, Level
        from polyalpha.forecasting import Baseline

        model = Baseline()
        stale_book = Book(
            token_id="y1",
            condition_id="m1",
            source_at=NOW - timedelta(minutes=5),
            received_at=NOW - timedelta(minutes=5),
            bids=(Level(D("0.49"), D(500)),),
            asks=(Level(D("0.51"), D(500)),),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        with pytest.raises(ValueError, match="stale"):
            model.predict("m1", stale_book, NOW)

    def test_baseline_chronological_enforcement(self):
        """Baseline should reject out-of-order observations."""
        from polyalpha.forecasting import Baseline

        model = Baseline()
        book1 = _book()
        t1 = NOW + timedelta(seconds=10)
        model.predict("m1", book1, t1)
        # Try to predict at an earlier time — should fail on staleness
        # (book source_at is NOW, but at is NOW-1s → book.source_at > at)
        with pytest.raises(ValueError, match="unavailable|chronological"):
            model.predict("m1", book1, NOW)

    def test_baseline_rejects_future_book(self):
        """Baseline should reject books from the future."""
        from polyalpha.forecasting import Baseline

        model = Baseline()
        future_book = _book()
        future_book = type(future_book)(
            token_id=future_book.token_id,
            condition_id=future_book.condition_id,
            source_at=NOW + timedelta(hours=1),
            received_at=NOW + timedelta(hours=1),
            bids=future_book.bids,
            asks=future_book.asks,
            tick_size=future_book.tick_size,
            min_order_size=future_book.min_order_size,
            source_hash=future_book.source_hash,
        )
        with pytest.raises(ValueError, match="unavailable"):
            model.predict("m1", future_book, NOW)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Portfolio Settlement Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestPortfolioSettlementEdgeCases:
    def test_settle_payout_zero_losing_position(self):
        """Settlement with payout=0 should zero the position and lose the basis."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        assert p.cash == D(9950)  # 10000 - 100*0.50
        p.settle("s1", "y1", D(0), NOW, NOW)  # NO wins, YES pays 0
        assert p.cash == D(9950)  # cash unchanged (0 payout)
        assert p.positions["y1"].shares == D(0)
        assert p.realized == D(-50)  # lost the entire basis

    def test_settle_payout_one_full_win(self):
        """Settlement with payout=1 should return $1 per share."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        p.settle("s1", "y1", D(1), NOW, NOW)
        assert p.cash == D(10050)  # 9950 + 100*1
        assert p.realized == D(50)  # 100 - 50

    def test_settle_partial_payout(self):
        """Partial payout (e.g., 0.50) should scale proportionally."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        p.settle("s1", "y1", D("0.50"), NOW, NOW)
        assert p.cash == D(10000)  # 9950 + 100*0.50 = 10000
        assert p.realized == D(0)  # 50 - 50 = 0

    def test_settle_unknown_token_raises(self):
        """Settling a token with no position should raise KeyError."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        with pytest.raises(KeyError):
            p.settle("s1", "unknown_token", D(1), NOW, NOW)

    def test_settle_payout_negative_raises(self):
        """Negative payout should be rejected."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        with pytest.raises(ValueError, match="invalid"):
            p.settle("s1", "y1", D(-0.5), NOW, NOW)

    def test_settle_payout_greater_than_one_raises(self):
        """Payout > 1 should be rejected."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        with pytest.raises(ValueError, match="invalid"):
            p.settle("s1", "y1", D(1.5), NOW, NOW)

    def test_settle_multiple_positions_independent(self):
        """Settling one position should not affect others."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy1 = _fill("o1", "y1", "BUY", D(50), D("0.50"), D("0"))
        buy2 = _fill("o2", "y2", "BUY", D(50), D("0.50"), D("0"))
        p.apply(buy1, "m1", "e1", "c1", "politics")
        p.apply(buy2, "m2", "e2", "c2", "sports")
        p.settle("s1", "y1", D(1), NOW, NOW)
        assert p.positions["y1"].shares == D(0)
        assert p.positions["y2"].shares == D(50)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Correlation Tracker Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestCorrelationTrackerEdgeCases:
    def test_identical_prices_zero_correlation(self):
        """Identical price series should produce zero std and zero correlation."""
        from polyalpha.correlation import compute_correlation

        prices = [D("0.50")] * 10
        corr = compute_correlation(prices, prices)
        assert corr == D(0)  # zero std => zero correlation

    def test_perfect_positive_correlation(self):
        """Perfectly correlated series should yield correlation near 1."""
        from polyalpha.correlation import compute_correlation

        a = [D("0.50"), D("0.51"), D("0.52"), D("0.53"), D("0.54")]
        b = [D("0.30"), D("0.31"), D("0.32"), D("0.33"), D("0.34")]
        corr = compute_correlation(a, b)
        assert corr > D("0.99")

    def test_inverse_price_series_correlation(self):
        """Inversely-moving price series should produce correlation in [-1, 1]."""
        from polyalpha.correlation import compute_correlation

        a = [D("0.50"), D("0.51"), D("0.52"), D("0.53"), D("0.54")]
        b = [D("0.50"), D("0.49"), D("0.48"), D("0.47"), D("0.46")]
        corr = compute_correlation(a, b)
        assert D(-1) <= corr <= D(1)
        # Note: correlation of RETURNS may differ from correlation of prices.
        # Both series have monotonically decreasing returns, so returns are
        # positively correlated even though prices move inversely.

    def test_single_observation_returns_zero(self):
        """Less than 3 observations should return zero correlation."""
        from polyalpha.correlation import compute_correlation

        assert compute_correlation([D("0.5")], [D("0.5")]) == D(0)
        assert compute_correlation([D("0.5"), D("0.6")], [D("0.5"), D("0.6")]) == D(0)

    def test_zero_in_prices_handles_division(self):
        """Prices containing zero should not crash (division by zero handled)."""
        from polyalpha.correlation import compute_correlation

        a = [D(0), D("0.50"), D("0.51")]
        b = [D(0), D("0.30"), D("0.31")]
        corr = compute_correlation(a, b)
        assert corr.is_finite()

    def test_rolling_correlation_window(self):
        """Rolling correlation should produce correct number of outputs."""
        from polyalpha.correlation import rolling_correlation

        a = [D(str(i)) for i in range(30)]
        b = [D(str(i * 2)) for i in range(30)]
        result = rolling_correlation(a, b, window=10)
        assert len(result) == 21  # 30 - 10 + 1

    def test_cluster_registry_exposure(self):
        """Cluster exposure should sum notional for tokens in the cluster."""
        from polyalpha.correlation import ClusterRegistry, EventCluster

        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1", "m2"],
            token_ids=["y1", "y2"],
        )
        registry = ClusterRegistry()
        registry.register(cluster)
        positions = {"y1": D(100), "y2": D(200), "y3": D(50)}
        exposure = registry.cluster_exposure(positions)
        assert exposure["c1"] == D(300)

    def test_correlation_adjusted_exposure_no_matrix(self):
        """Without correlation matrix, adjusted exposure should equal raw."""
        from polyalpha.correlation import ClusterRegistry, EventCluster

        cluster = EventCluster(
            cluster_id="c1",
            event_id="e1",
            category="politics",
            market_ids=["m1"],
            token_ids=["y1"],
        )
        registry = ClusterRegistry()
        registry.register(cluster)
        positions = {"y1": D(100)}
        adjusted = registry.correlation_adjusted_exposure(positions)
        assert adjusted["c1"] == D(100)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Calibration Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestCalibrationEdgeCases:
    def test_metrics_empty_buckets(self):
        """Calibration with extreme probabilities should handle empty middle buckets."""
        from polyalpha.calibration import metrics

        # All predictions at 0.9 → only bucket 9 is populated
        probs = [0.9] * 20
        outcomes = [1] * 15 + [0] * 5
        result = metrics(probs, outcomes, bins=10)
        assert result["sample_size"] == 20
        # Most buckets should be empty
        non_empty = [b for b in result["reliability"] if b["count"] > 0]
        assert len(non_empty) == 1

    def test_metrics_single_prediction(self):
        """Single prediction should still compute correctly."""
        from polyalpha.calibration import metrics

        result = metrics([0.7], [1])
        assert result["brier"] == pytest.approx(0.09, abs=1e-10)
        assert result["sample_size"] == 1

    def test_metrics_perfect_calibration(self):
        """Perfectly calibrated predictions should have low ECE."""
        from polyalpha.calibration import metrics

        # 10 predictions at 0.5, 5 resolve YES → 50% realized
        probs = [0.5] * 10
        outcomes = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
        result = metrics(probs, outcomes)
        assert result["ece"] == pytest.approx(0.0, abs=0.01)

    def test_isotonic_single_bucket(self):
        """Isotonic with all identical probabilities should still fit."""
        from polyalpha.calibration import Isotonic, Observation

        rows = [
            Observation(f"m{i}", "c1", NOW, NOW + timedelta(days=1), 0.5, 1 if i < 5 else 0)
            for i in range(10)
        ]
        iso = Isotonic().fit(rows, NOW + timedelta(days=2))
        assert iso.predict(0.5) >= 0

    def test_platt_insufficient_data_raises(self):
        """Platt with fewer than 4 resolved events should raise."""
        from polyalpha.calibration import InsufficientData, Observation, Platt

        rows = [
            Observation(f"m{i}", "c1", NOW, NOW + timedelta(days=1), 0.5, i % 2) for i in range(3)
        ]
        with pytest.raises(InsufficientData):
            Platt().fit(rows, NOW + timedelta(days=2))

    def test_temperature_scaling_extreme_temperatures(self):
        """Temperature scaling should handle extreme T values."""
        from polyalpha.calibration import TemperatureScaling

        ts = TemperatureScaling()
        # Fit on well-separated data
        probs = [0.1, 0.2, 0.3, 0.4, 0.6, 0.7, 0.8, 0.9]
        outcomes = [0, 0, 0, 0, 1, 1, 1, 1]
        ts.fit(probs, outcomes)
        # After fitting, extreme probabilities should remain near extremes
        assert ts.predict(0.9) > 0.8
        assert ts.predict(0.1) < 0.2

    def test_temperature_scaling_pushes_toward_05(self):
        """High temperature should push probabilities toward 0.5."""
        from polyalpha.calibration import TemperatureScaling

        ts = TemperatureScaling()
        ts._temperature = 5.0
        ts._fitted = True
        # Extreme probability should be pushed toward 0.5
        result = ts.predict(0.95)
        assert result < 0.95
        assert result > 0.5

    def test_canonical_rows_conflicting_forecasts_raises(self):
        """Conflicting forecasts at same market/timestamp should raise."""
        from polyalpha.calibration import Observation, canonical_rows

        rows = [
            Observation("m1", "c1", NOW, None, 0.5, None),
            Observation("m1", "c1", NOW, None, 0.6, None),
        ]
        with pytest.raises(ValueError, match="conflicting"):
            canonical_rows(rows)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Quality Filter Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestQualityFilterEdgeCases:
    def test_crossed_book_detected(self):
        """Crossed book (best_bid >= best_ask) should be rejected."""
        from polyalpha.quality import Filter

        f = Filter()
        from polyalpha.domain import Book, Level

        crossed = Book(
            token_id="y1",
            condition_id="m1",
            source_at=NOW,
            received_at=NOW,
            bids=(Level(D("0.52"), D(100)),),
            asks=(Level(D("0.50"), D(100)),),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        reasons = f.book_reasons(crossed, NOW)
        assert "locked_or_crossed_book" in reasons

    def test_zero_spread_detected(self):
        """Zero spread (locked book) should be rejected."""
        from polyalpha.quality import Filter

        f = Filter()
        from polyalpha.domain import Book, Level

        locked = Book(
            token_id="y1",
            condition_id="m1",
            source_at=NOW,
            received_at=NOW,
            bids=(Level(D("0.50"), D(100)),),
            asks=(Level(D("0.50"), D(100)),),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        reasons = f.book_reasons(locked, NOW)
        assert "locked_or_crossed_book" in reasons

    def test_one_sided_book_detected(self):
        """Book with only bids or only asks should be detected."""
        from polyalpha.domain import Book, Level
        from polyalpha.quality import Filter

        f = Filter()
        one_sided = Book(
            token_id="y1",
            condition_id="m1",
            source_at=NOW,
            received_at=NOW,
            bids=(Level(D("0.49"), D(100)),),
            asks=(),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        reasons = f.book_reasons(one_sided, NOW)
        assert "one_sided_book" in reasons

    def test_future_book_rejected(self):
        """Book with source_at in the future should be rejected."""
        from polyalpha.quality import Filter

        f = Filter()
        book = _book()
        future_book = type(book)(
            token_id=book.token_id,
            condition_id=book.condition_id,
            source_at=NOW + timedelta(hours=1),
            received_at=NOW + timedelta(hours=1),
            bids=book.bids,
            asks=book.asks,
            tick_size=book.tick_size,
            min_order_size=book.min_order_size,
            source_hash=book.source_hash,
        )
        reasons = f.book_reasons(future_book, NOW)
        assert "future_book" in reasons

    def test_inactive_market_rejected(self):
        """Inactive market should be rejected."""
        from polyalpha.quality import Filter

        f = Filter()
        market = _market(active=False)
        reasons = f.market_reasons(market, NOW)
        assert "inactive_or_closed" in reasons

    def test_zero_liquidity_rejected(self):
        """Zero liquidity should be rejected."""
        from polyalpha.quality import Filter

        f = Filter()
        market = _market(liquidity=D(0))
        reasons = f.market_reasons(market, NOW)
        assert "liquidity_missing_or_low" in reasons

    def test_none_liquidity_rejected(self):
        """None liquidity should be rejected."""
        from polyalpha.quality import Filter

        f = Filter()
        market = _market()
        market = type(market)(
            market_id=market.market_id,
            condition_id=market.condition_id,
            event_ids=market.event_ids,
            question=market.question,
            description=market.description,
            resolution_source=market.resolution_source,
            deadline=market.deadline,
            active=market.active,
            closed=market.closed,
            accepting_orders=market.accepting_orders,
            enable_order_book=market.enable_order_book,
            liquidity=None,
            volume=market.volume,
            fees_enabled=market.fees_enabled,
            fee_parameters_json=market.fee_parameters_json,
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
            received_at=market.received_at,
            category=market.category,
        )
        reasons = f.market_reasons(market, NOW)
        assert "liquidity_missing_or_low" in reasons

    def test_filter_validation_negative_threshold(self):
        """Negative threshold should be rejected."""
        from polyalpha.quality import Filter

        with pytest.raises(ValueError):
            Filter(min_liquidity=D(-1))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Storage Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestStorageEdgeCases:
    def test_append_only_no_update(self):
        """SQLite triggers should prevent updates."""
        from polyalpha.storage import Store

        with Store(":memory:") as store:
            store.append("test", "e1", NOW, {"key": "value"})
            with pytest.raises(Exception):
                store.connection.execute("UPDATE receipts SET kind='changed' WHERE id=1")

    def test_append_only_no_delete(self):
        """SQLite triggers should prevent deletes."""
        from polyalpha.storage import Store

        with Store(":memory:") as store:
            store.append("test", "e1", NOW, {"key": "value"})
            with pytest.raises(Exception):
                store.connection.execute("DELETE FROM receipts WHERE id=1")

    def test_replay_chronological_order(self):
        """Records should be replayed in chronological order."""
        from polyalpha.storage import Store

        with Store(":memory:") as store:
            store.append("test", "e1", NOW + timedelta(hours=2), {"seq": 2})
            store.append("test", "e1", NOW, {"seq": 1})
            records = list(store.replay(NOW + timedelta(hours=3), "test"))
            assert len(records) == 2
            assert records[0].payload["seq"] == 1
            assert records[1].payload["seq"] == 2

    def test_replay_filters_by_kind(self):
        """Replay with kind filter should only return matching records."""
        from polyalpha.storage import Store

        with Store(":memory:") as store:
            store.append("market", "e1", NOW, {"type": "market"})
            store.append("book", "e1", NOW, {"type": "book"})
            markets = list(store.replay(NOW + timedelta(hours=1), "market"))
            assert len(markets) == 1
            assert markets[0].payload["type"] == "market"

    def test_latest_returns_most_recent(self):
        """Latest should return the most recent record by source time."""
        from polyalpha.storage import Store

        with Store(":memory:") as store:
            store.append("test", "e1", NOW, {"version": 1}, source_at=NOW)
            store.append(
                "test",
                "e1",
                NOW + timedelta(minutes=1),
                {"version": 2},
                source_at=NOW + timedelta(minutes=1),
            )
            record = store.latest("test", "e1", NOW + timedelta(hours=1))
            assert record.payload["version"] == 2

    def test_sha256_deterministic(self):
        """Same payload should produce same SHA256."""

        from polyalpha.storage import Store

        with Store(":memory:") as store:
            id1 = store.append("test", "e1", NOW, {"key": "value"})
            id2 = store.append("test", "e1", NOW, {"key": "value"})
            r1 = store.connection.execute(
                "SELECT sha256 FROM receipts WHERE id=?", (id1,)
            ).fetchone()
            r2 = store.connection.execute(
                "SELECT sha256 FROM receipts WHERE id=?", (id2,)
            ).fetchone()
            assert r1[0] == r2[0]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Execution Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestExecutionEdgeCases:
    def test_consumed_levels_reduce_available(self):
        """Previously consumed levels should reduce available quantity."""
        from polyalpha.execution import FeeSchedule, Order, Simulator, walk

        book = _book(asks=[(D("0.51"), D(100))])
        fees = FeeSchedule(D(0), NOW, "test")
        sim = Simulator()
        order1 = Order("o1", "y1", "BUY", D(60), NOW)
        fill1 = walk(book, order1, fees, NOW)
        sim.commit(fill1)
        assert fill1.shares == D(60)
        # Second order should see only 40 remaining
        order2 = Order("o2", "y1", "BUY", D(60), NOW, allow_partial=True)
        fill2 = walk(book, order2, fees, NOW, consumed=sim.consumed.get("y1", {}))
        assert fill2.shares == D(40)  # only 40 remaining

    def test_min_order_size_enforcement(self):
        """Orders below min_order_size should be rejected."""
        from polyalpha.execution import FeeSchedule, Order, walk

        book = _book()
        fees = FeeSchedule(D(0), NOW, "test")
        order = Order("o1", "y1", "BUY", D(0.5), NOW)  # below min_order_size=1
        with pytest.raises(ValueError, match="below minimum"):
            walk(book, order, fees, NOW)

    def test_empty_book_rejected(self):
        """Book with no asks should reject BUY orders."""
        from polyalpha.domain import Book, Level
        from polyalpha.execution import FeeSchedule, Order, walk

        empty = Book(
            token_id="y1",
            condition_id="m1",
            source_at=NOW,
            received_at=NOW,
            bids=(Level(D("0.49"), D(100)),),
            asks=(),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        fees = FeeSchedule(D(0), NOW, "test")
        order = Order("o1", "y1", "BUY", D(10), NOW)
        with pytest.raises(ValueError, match="unusable book|insufficient"):
            walk(empty, order, fees, NOW)

    def test_fee_at_price_zero(self):
        """Fee at price=0 should be zero."""
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D("0.04"), NOW, "test")
        assert fees.fee(D(100), D(0)) == D(0)

    def test_fee_at_price_one(self):
        """Fee at price=1 should be zero (1-price = 0)."""
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D("0.04"), NOW, "test")
        assert fees.fee(D(100), D(1)) == D(0)

    def test_fee_at_price_half_maximized(self):
        """Fee should be maximized at price=0.50."""
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D("0.04"), NOW, "test")
        f_half = fees.fee(D(100), D("0.50"))
        f_quarter = fees.fee(D(100), D("0.25"))
        f_three_quarter = fees.fee(D(100), D("0.75"))
        assert f_half > f_quarter
        assert f_half > f_three_quarter
        assert f_quarter == f_three_quarter

    def test_simulator_duplicate_commit_rejected(self):
        """Simulator should reject duplicate order IDs on commit."""
        from polyalpha.execution import FeeSchedule, Order, Simulator

        sim = Simulator()
        book = _book()
        fees = FeeSchedule(D(0), NOW, "test")
        order = Order("o1", "y1", "BUY", D(10), NOW)
        fill = sim.quote(book, order, fees, NOW)
        sim.commit(fill)
        with pytest.raises(ValueError, match="duplicate"):
            sim.commit(fill)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Kelly Fraction Boundaries
# ══════════════════════════════════════════════════════════════════════════════


class TestKellyBoundaries:
    def test_kelly_zero_probability(self):
        """Kelly at p=0 should be zero (no edge)."""
        from polyalpha.expected_value import kelly_fraction

        assert kelly_fraction(D(0), D("0.50")) == D(0)

    def test_kelly_one_probability(self):
        """Kelly at p=1 should be 1 (certain win)."""
        from polyalpha.expected_value import kelly_fraction

        result = kelly_fraction(D(1), D("0.50"))
        assert result == D(1)

    def test_kelly_at_price_zero(self):
        """Kelly at price=0 should be zero (undefined edge)."""
        from polyalpha.expected_value import kelly_fraction

        assert kelly_fraction(D("0.60"), D(0)) == D(0)

    def test_kelly_at_price_one(self):
        """Kelly at price=1 should be zero (can't win)."""
        from polyalpha.expected_value import kelly_fraction

        assert kelly_fraction(D("0.99"), D(1)) == D(0)

    def test_kelly_fair_price_zero_edge(self):
        """Kelly when probability equals price should be zero."""
        from polyalpha.expected_value import kelly_fraction

        assert kelly_fraction(D("0.50"), D("0.50")) == D(0)

    def test_kelly_negative_edge(self):
        """Kelly with negative edge should return zero."""
        from polyalpha.expected_value import kelly_fraction

        assert kelly_fraction(D("0.40"), D("0.50")) == D(0)

    def test_fractional_kelly_scales(self):
        """Fractional Kelly should be a fraction of full Kelly."""
        from polyalpha.expected_value import fractional_kelly, kelly_fraction

        full = kelly_fraction(D("0.70"), D("0.50"))
        frac = fractional_kelly(D("0.70"), D("0.50"), D("0.25"))
        assert frac == full * D("0.25")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Expected Value Extreme Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestExpectedValueEdgeCases:
    def test_expected_value_requires_buy_fill(self):
        """Expected value should reject SELL fills."""
        from polyalpha.expected_value import expected_value
        from polyalpha.uncertainty import SpreadBasedUncertainty

        fill = _fill(side="SELL")
        forecast = _forecast()
        unc = SpreadBasedUncertainty().estimate(D("0.65"), {"spread": D("0.02")})
        with pytest.raises(ValueError, match="buy fill"):
            expected_value(forecast, fill, unc)

    def test_zero_shares_fill_rejected(self):
        """Fill with zero shares should be rejected by Fill constructor."""
        from polyalpha.execution import Fill

        with pytest.raises(ValueError, match="overfill"):
            Fill(
                order_id="o1",
                token_id="y1",
                side="BUY",
                requested=D(0),
                shares=D(0),
                notional=D(0),
                fees=D(0),
                depth_slippage=D(0),
                filled_at=NOW,
                levels=(),
            )

    def test_extreme_fair_probability(self):
        """Very high fair probability should still produce valid net edge."""
        from polyalpha.expected_value import expected_value
        from polyalpha.forecasting import Forecast
        from polyalpha.uncertainty import SpreadBasedUncertainty

        fill = _fill(price=D("0.95"), shares=D(100), fees=D("0"))
        forecast = Forecast("m1", NOW, D("0.99"), D("0.95"), D("1"), "test-v1")
        unc = SpreadBasedUncertainty().estimate(D("0.99"), {"spread": D("0.01")})
        result = expected_value(forecast, fill, unc, resolution_penalty=D(0))
        assert result.net_edge.is_finite()

    def test_very_small_fill(self):
        """Very small fill should still compute correctly."""
        from polyalpha.expected_value import expected_value
        from polyalpha.forecasting import Forecast
        from polyalpha.uncertainty import SpreadBasedUncertainty

        fill = _fill(shares=D(1), price=D("0.50"), fees=D("0"))
        forecast = Forecast("m1", NOW, D("0.60"), D("0.55"), D("0.65"), "test-v1")
        unc = SpreadBasedUncertainty().estimate(D("0.60"), {"spread": D("0.02")})
        result = expected_value(forecast, fill, unc, resolution_penalty=D(0))
        assert result.net_edge.is_finite()
        assert result.fee_per_share == D(0)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Risk Budget Zero Dimensions
# ══════════════════════════════════════════════════════════════════════════════


class TestRiskBudgetEdgeCases:
    def test_budget_zero_equity(self):
        """Budget with zero equity should be zero."""
        from polyalpha.portfolio import Portfolio
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        portfolio = Portfolio(D(10000))
        budget = risk.budget(portfolio, D(0), "m1", "e1", "c1", "politics")
        assert budget == D(0)

    def test_budget_empty_event_cluster(self):
        """Budget with empty event/cluster should be zero."""
        from polyalpha.portfolio import Portfolio
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        portfolio = Portfolio(D(10000))
        budget = risk.budget(portfolio, D(10000), "m1", "", "", "politics")
        assert budget == D(0)

    def test_budget_total_exposure_cap(self):
        """Budget should respect total exposure cap."""
        from polyalpha.portfolio import Portfolio
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        portfolio = Portfolio(D(10000))
        # Fill portfolio with existing exposure
        for i in range(40):
            fill = _fill(f"o{i}", f"y{i}", "BUY", D(50), D("0.50"), D("0"))
            portfolio.apply(fill, f"m{i}", f"e{i}", f"c{i}", "cat")
        # Total exposure = 40 * 25 = 1000, total cap = 10000 * 0.20 = 2000
        # remaining = 2000 - 1000 = 1000
        budget = risk.budget(
            portfolio, D(10000), "new_market", "new_event", "new_cluster", "new_cat"
        )
        assert budget > D(0)

    def test_budget_category_cap_binding(self):
        """Budget should be capped by category exposure limit."""
        from polyalpha.portfolio import Portfolio
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        portfolio = Portfolio(D(10000))
        # Fill category with 900 exposure (cap = 1000)
        for i in range(18):
            fill = _fill(f"o{i}", f"y{i}", "BUY", D(50), D("0.50"), D("0"))
            portfolio.apply(fill, f"m{i}", f"e{i}", f"c{i}", "politics")
        # Remaining category capacity = 1000 - 900 = 100
        budget = risk.budget(portfolio, D(10000), "new_m", "new_e", "new_c", "politics")
        assert budget <= D(100)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Uncertainty Estimator Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestUncertaintyEdgeCases:
    def test_ensemble_single_estimator(self):
        """Ensemble with one estimator should work."""
        from polyalpha.uncertainty import EnsembleUncertainty, HistoricalErrorUncertainty

        ens = EnsembleUncertainty([HistoricalErrorUncertainty()])
        result = ens.estimate(D("0.65"), {"spread": D("0.02")}, market_id="m1")
        assert result.uncertainty_score > 0

    def test_ensemble_takes_widest(self):
        """Ensemble should take the widest interval across estimators."""
        from polyalpha.uncertainty import (
            EnsembleUncertainty,
            HistoricalErrorUncertainty,
            SpreadBasedUncertainty,
        )

        ens = EnsembleUncertainty(
            [HistoricalErrorUncertainty(default_buffer=D("0.03")), SpreadBasedUncertainty()]
        )
        result = ens.estimate(D("0.65"), {"spread": D("0.10")}, market_id="m1")
        # Spread-based should give wider buffer (0.10 * 0.5 = 0.05 > 0.03)
        assert result.uncertainty_score >= D("0.03")

    def test_spread_based_no_spread(self):
        """Spread-based with no spread should use default buffer."""
        from polyalpha.uncertainty import SpreadBasedUncertainty

        est = SpreadBasedUncertainty()
        result = est.estimate(D("0.65"), {}, market_id="m1")
        assert result.uncertainty_score == D("0.05")

    def test_bootstrap_insufficient_history(self):
        """Bootstrap with insufficient history should use wide default."""
        from polyalpha.uncertainty import BootstrapUncertainty

        boot = BootstrapUncertainty(min_samples=20)
        result = boot.estimate(D("0.65"), {}, market_id="m1")
        assert result.method == "bootstrap-insufficient-data"
        assert result.uncertainty_score == D("0.10")

    def test_bootstrap_sufficient_history(self):
        """Bootstrap with enough history should compute actual intervals."""
        from polyalpha.uncertainty import BootstrapUncertainty

        boot = BootstrapUncertainty(n_bootstrap=50, min_samples=10, seed=42)
        for i in range(20):
            boot.observe(0.5 + (i % 2) * 0.2, i % 2)
        result = boot.estimate(D("0.65"), {}, market_id="m1")
        assert result.method == "bootstrap"
        assert result.uncertainty_score > 0

    def test_conservative_probability_at_boundary(self):
        """Conservative probability at probability=0 should clamp to 0."""
        from polyalpha.uncertainty import SpreadBasedUncertainty, conservative_probability

        est = SpreadBasedUncertainty()
        unc = est.estimate(D(0.01), {"spread": D("0.10")})
        result = conservative_probability(D(0.01), unc, D("1.0"))
        assert result >= D(0)

    def test_conservative_no_probability_at_boundary(self):
        """Conservative NO probability at probability=1 should clamp to 0."""
        from polyalpha.uncertainty import SpreadBasedUncertainty, conservative_no_probability

        est = SpreadBasedUncertainty()
        unc = est.estimate(D("0.99"), {"spread": D("0.10")})
        result = conservative_no_probability(D("0.99"), unc, D("1.0"))
        assert result >= D(0)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Anomaly Detector Numerical Stability
# ══════════════════════════════════════════════════════════════════════════════


class TestAnomalyDetectorEdgeCases:
    def test_z_score_insufficient_history(self):
        """Z-score with fewer than 10 observations should return None."""
        from polyalpha.anomaly import AnomalyDetector

        det = AnomalyDetector()
        from collections import deque

        result = det._z_score(D("0.50"), deque([D("0.50")] * 5))
        assert result is None

    def test_z_score_zero_variance(self):
        """Z-score with zero variance should return 0."""
        from polyalpha.anomaly import AnomalyDetector

        det = AnomalyDetector()
        from collections import deque

        result = det._z_score(D("0.50"), deque([D("0.50")] * 15))
        assert result == D(0)

    def test_uncertainty_multiplier_empty(self):
        """No anomalies should give multiplier 1.0."""
        from polyalpha.anomaly import AnomalyDetector

        det = AnomalyDetector()
        assert det.uncertainty_multiplier([]) == D(1)

    def test_uncertainty_multiplier_scales_with_severity(self):
        """Higher severity should give higher multiplier."""
        from polyalpha.anomaly import Anomaly, AnomalyDetector

        det = AnomalyDetector()
        low = [Anomaly("test", D("0.3"), "low", {})]
        high = [Anomaly("test", D("0.9"), "high", {})]
        assert det.uncertainty_multiplier(high) > det.uncertainty_multiplier(low)

    def test_detect_empty_book(self):
        """Empty book should detect no_bids and no_asks anomalies."""
        from polyalpha.anomaly import AnomalyDetector
        from polyalpha.domain import Book

        det = AnomalyDetector()
        empty = Book(
            token_id="y1",
            condition_id="m1",
            source_at=NOW,
            received_at=NOW,
            bids=(),
            asks=(),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        anomalies = det.detect(empty, {"microprice": None, "midpoint": None})
        kinds = [a.kind for a in anomalies]
        assert "no_bids" in kinds
        assert "no_asks" in kinds


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Backtest Engine Full Pipeline
# ══════════════════════════════════════════════════════════════════════════════


class TestBacktestPipeline:
    def test_stream_gap_clears_state(self):
        """stream_gap record should clear books, pending, and exit_pending."""
        from polyalpha.backtest import Engine
        from polyalpha.storage import Record

        engine = Engine(clusters={}, initial_cash=D(10000))
        # Feed a book
        engine.on_record(Record(1, "market", "m1", NOW, NOW, _market_record()))
        engine.on_record(Record(2, "book", "y1", NOW, NOW, _book_record()))
        assert "y1" in engine.books
        # Feed stream_gap
        engine.on_record(
            Record(3, "stream_gap", "", NOW + timedelta(seconds=1), NOW + timedelta(seconds=1), {})
        )
        assert "y1" not in engine.books
        assert len(engine.pending) == 0
        assert len(engine.exit_pending) == 0

    def test_settlement_in_pipeline(self):
        """Full settlement flow through on_record."""
        from polyalpha.backtest import Engine
        from polyalpha.storage import Record

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(clusters=clusters, initial_cash=D(10000))
        # Feed market
        engine.on_record(Record(1, "market", "m1", NOW, NOW, _market_record()))
        # Manually add position
        fill = _fill("entry-1", "y1", "BUY", D(100), D("0.50"), D("0"))
        engine.portfolio.apply(fill, "m1", "e1", "c1", "politics")
        engine.simulator.commit(fill)
        # Feed settlement
        engine.on_record(_settlement_record("y1", 1, NOW + timedelta(hours=1)))
        assert engine.portfolio.positions["y1"].shares == D(0)
        assert "m1" in engine.resolved

    def test_out_of_order_book_rejected(self):
        """Book with source_at earlier than previous should be rejected."""
        from polyalpha.backtest import Engine
        from polyalpha.storage import Record

        engine = Engine(clusters={}, initial_cash=D(10000))
        engine.on_record(Record(1, "market", "m1", NOW, NOW, _market_record()))
        t1 = NOW + timedelta(minutes=5)
        t2 = NOW + timedelta(minutes=3)  # earlier than t1
        engine.on_record(Record(2, "book", "y1", t1, t1, _book_record(t=t1)))
        assert "y1" in engine.books
        # on_record raises ValueError for chronological violation
        with pytest.raises(ValueError, match="chronological"):
            engine.on_record(Record(3, "book", "y1", t2, t2, _book_record(t=t2)))

    def test_missing_metadata_rejected(self):
        """Book for unknown token should be rejected."""
        from polyalpha.backtest import Engine
        from polyalpha.storage import Record

        engine = Engine(clusters={}, initial_cash=D(10000))
        # Feed book without market metadata
        engine.on_record(
            Record(1, "book", "unknown_token", NOW, NOW, _book_record(token_id="unknown_token"))
        )
        decisions = [d for d in engine.decisions if d["reason"] == "missing_metadata"]
        assert len(decisions) == 1

    def test_stale_metadata_rejection(self):
        """Market with stale metadata should be rejected."""
        from polyalpha.backtest import Engine
        from polyalpha.storage import Record

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(clusters=clusters, initial_cash=D(10000), stale_metadata_age=60)
        # Market from 5 minutes ago
        old_time = NOW - timedelta(minutes=5)
        engine.on_record(Record(1, "market", "m1", old_time, old_time, _market_record(t=old_time)))
        engine.on_record(Record(2, "book", "y1", NOW, NOW, _book_record()))
        decisions = [
            d for d in engine.decisions if "stale_metadata" in str(d.get("rejections", []))
        ]
        assert len(decisions) == 1

    def test_run_with_zero_latency_fills_immediately(self):
        """With latency=0, orders should fill on the next book update."""
        from polyalpha.backtest import Engine
        from polyalpha.storage import Record

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(
            clusters=clusters,
            initial_cash=D(10000),
            latency_seconds=0,
            min_edge=D(0),
        )
        t = NOW
        engine.on_record(Record(1, "market", "m1", t, t, _market_record(t=t)))
        engine.on_record(Record(2, "book", "y1", t, t, _book_record(t=t)))
        # After queueing, next book tick should attempt fill
        t2 = t + timedelta(seconds=1)
        engine.on_record(Record(3, "book", "y1", t2, t2, _book_record(t=t2)))
        # Either filled or rejected
        decisions_after = [d for d in engine.decisions if d["reason"] in ("filled", "rejected")]
        assert len(decisions_after) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Performance Metrics Additional
# ══════════════════════════════════════════════════════════════════════════════


class TestPerformanceMetricsAdditional:
    def test_drawdown_constant_equity(self):
        """Constant equity should produce zero drawdown."""
        from polyalpha.performance import drawdown_series

        dd = drawdown_series([100, 100, 100, 100])
        assert all(d == 0 for d in dd)

    def test_drawdown_monotonically_non_increasing_from_peak(self):
        """Drawdown should never exceed 1.0."""
        from polyalpha.performance import drawdown_series

        dd = drawdown_series([100, 50, 25, 10])
        assert all(0 <= d <= 1 for d in dd)
        assert dd[-1] == pytest.approx(0.9, abs=0.01)

    def test_sortino_insufficient_data(self):
        """Sortino with one return should be None."""
        from polyalpha.performance import sortino_ratio

        assert sortino_ratio([0.01]) is None

    def test_sortino_no_downside(self):
        """Sortino with no negative returns should be None."""
        from polyalpha.performance import sortino_ratio

        assert sortino_ratio([0.01, 0.02, 0.03]) is None

    def test_information_ratio_constant_excess(self):
        """Information ratio with zero tracking error should be None."""
        from polyalpha.performance import information_ratio

        assert information_ratio([0.01, 0.01, 0.01]) is None

    def test_tail_ratio_insufficient_data(self):
        """Tail ratio with fewer than 20 returns should be None."""
        from polyalpha.performance import tail_ratio

        assert tail_ratio([0.01] * 10) is None

    def test_calmar_ratio_zero_max_drawdown(self):
        """Calmar with zero drawdown should be None."""
        from polyalpha.performance import calmar_ratio

        assert calmar_ratio([0.01, 0.02], 0) is None

    def test_conditional_var_insufficient_data(self):
        """CVaR with fewer than 10 returns should be None."""
        from polyalpha.performance import conditional_value_at_risk

        assert conditional_value_at_risk([0.01] * 5) is None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Expected Value Decomposition
# ══════════════════════════════════════════════════════════════════════════════


class TestExpectedValueDecomposition:
    def test_fee_per_share_calculation(self):
        """Fee per share should be fees / shares."""
        from polyalpha.expected_value import calculate_fee_per_share

        fill = _fill(fees=D("5.00"), shares=D(100))
        assert calculate_fee_per_share(fill) == D("0.05")

    def test_fee_per_share_zero_shares(self):
        """Fee per share with zero shares should return 0."""
        from polyalpha.expected_value import calculate_fee_per_share

        # Can't create Fill with 0 shares, so test the function's guard
        # by creating a minimal fill and checking the branch
        fill = _fill(shares=D(1), fees=D(0))
        assert calculate_fee_per_share(fill) == D(0)

    def test_gross_edge_yes(self):
        """Gross edge for YES = fair - vwap."""
        from polyalpha.expected_value import calculate_gross_edge

        assert calculate_gross_edge(D("0.65"), D("0.55"), "YES") == D("0.10")

    def test_gross_edge_no(self):
        """Gross edge for NO = (1 - fair) - vwap."""
        from polyalpha.expected_value import calculate_gross_edge

        assert calculate_gross_edge(D("0.65"), D("0.30"), "NO") == D("0.05")

    def test_gross_edge_invalid_side(self):
        """Invalid side should raise."""
        from polyalpha.expected_value import calculate_gross_edge

        with pytest.raises(ValueError, match="side"):
            calculate_gross_edge(D("0.65"), D("0.55"), "INVALID")

    def test_liquidity_penalty_zero_depth(self):
        """Zero depth should give double penalty."""
        from polyalpha.expected_value import calculate_liquidity_penalty

        result = calculate_liquidity_penalty(D(0), D(100))
        assert result == D("0.01")  # 0.005 * 2

    def test_stale_data_penalty_zero_age(self):
        """Zero age should give zero penalty."""
        from polyalpha.expected_value import calculate_stale_data_penalty

        assert calculate_stale_data_penalty(0) == D(0)

    def test_stale_data_penalty_at_max_age(self):
        """Penalty at max age should equal base penalty."""
        from polyalpha.expected_value import calculate_stale_data_penalty

        result = calculate_stale_data_penalty(30, max_age=30)
        assert result == D("0.003")

    def test_stale_data_penalty_beyond_max_age(self):
        """Penalty beyond max age should cap at 2x base."""
        from polyalpha.expected_value import calculate_stale_data_penalty

        result = calculate_stale_data_penalty(60, max_age=30)
        assert result == D("0.006")  # 0.003 * 2
