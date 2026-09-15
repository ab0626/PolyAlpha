"""Edge-case and hidden-property tests for critical numerical functions.

These tests verify invariants, boundary conditions, and numerical correctness
that a quant research audit would check. Tests are organized by subsystem.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _book(bids=None, asks=None, token_id="y1"):
    from polyalpha.domain import Book, Level

    if bids is None:
        bids = [(D("0.49"), D(500))]
    if asks is None:
        asks = [(D("0.51"), D(500))]
    return Book(
        token_id=token_id,
        condition_id="m1",
        source_at=NOW,
        received_at=NOW,
        bids=tuple(Level(p, s) for p, s in bids),
        asks=tuple(Level(p, s) for p, s in asks),
        tick_size=D("0.01"),
        min_order_size=D(1),
        source_hash="test",
    )


def _market(market_id="m1", yes_token="y1", no_token="n1", event_id="e1"):
    from polyalpha.domain import Market

    return Market(
        market_id=market_id,
        condition_id=market_id,
        event_ids=(event_id,),
        question=f"Test {market_id}",
        description="Test",
        resolution_source="test",
        deadline=NOW + timedelta(days=7),
        active=True,
        closed=False,
        accepting_orders=True,
        enable_order_book=True,
        liquidity=D(5000),
        volume=D(1000),
        fees_enabled=False,
        fee_parameters_json=None,
        yes_token_id=yes_token,
        no_token_id=no_token,
        received_at=NOW,
        category="politics",
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: VWAP / Order-Book Walking
# ══════════════════════════════════════════════════════════════════════════════


class TestVWAPEdgeCases:
    def test_single_level_fill(self):
        """Buy exactly one level's size."""
        from polyalpha.execution import FeeSchedule, Order, walk

        book = _book(asks=[(D("0.55"), D(100))], bids=[(D("0.54"), D(100))])
        fees = FeeSchedule(D(0), NOW, "test")
        order = Order("o1", "y1", "BUY", D(100), NOW)
        fill = walk(book, order, fees, NOW)
        assert fill.shares == D(100)
        assert fill.vwap == D("0.55")

    def test_partial_fill_insufficient_depth(self):
        """Request more shares than available — partial fill (allow_partial=True)."""
        from polyalpha.execution import FeeSchedule, Order, walk

        book = _book(asks=[(D("0.55"), D(50))])
        fees = FeeSchedule(D(0), NOW, "test")
        order = Order("o1", "y1", "BUY", D(100), NOW, allow_partial=True)
        fill = walk(book, order, fees, NOW)
        assert fill.shares == D(50)
        assert fill.vwap == D("0.55")

    def test_zero_depth_no_fill(self):
        """Book with zero-size levels → zero fill via allow_partial."""
        from polyalpha.domain import Book, Level
        from polyalpha.execution import FeeSchedule, Order, walk

        book = Book(
            token_id="y1",
            condition_id="m1",
            source_at=NOW,
            received_at=NOW,
            bids=(Level(D("0.40"), D(500)),),
            asks=(Level(D("0.60"), D(500)),),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        fees = FeeSchedule(D(0), NOW, "test")
        # Order for 0 shares (below min_order_size) — triggers below minimum
        # Instead: order for a lot, but consumed entirely by a prior order
        from polyalpha.execution import Simulator

        sim = Simulator()
        # Fill entire ask side
        first = Order("o0", "y1", "BUY", D(500), NOW)
        fill1 = walk(book, first, fees, NOW)
        sim.commit(fill1)
        # Second order against same book — no remaining depth
        order = Order("o1", "y1", "BUY", D(100), NOW, allow_partial=True)
        fill2 = walk(book, order, fees, NOW, consumed=sim.consumed.get("y1", {}))
        assert fill2.shares == D(0)

    def test_sell_walks_bids(self):
        """Selling should walk bids from highest to lowest."""
        from polyalpha.execution import FeeSchedule, Order, walk

        book = _book(
            bids=[(D("0.54"), D(100)), (D("0.52"), D(200)), (D("0.50"), D(500))],
            asks=[(D("0.56"), D(500))],
        )
        fees = FeeSchedule(D(0), NOW, "test")
        order = Order("o1", "y1", "SELL", D(250), NOW)
        fill = walk(book, order, fees, NOW)
        assert fill.shares == D(250)
        # VWAP = (100*0.54 + 150*0.52) / 250 = (54 + 78) / 250 = 132/250 = 0.528
        expected = D("132") / D("250")
        assert fill.vwap == expected

    def test_boundary_price_001(self):
        """Buy at the minimum price level."""
        from polyalpha.execution import FeeSchedule, Order, walk

        book = _book(asks=[(D("0.01"), D(100))], bids=[(D("0.00"), D(1))])
        fees = FeeSchedule(D(0), NOW, "test")
        order = Order("o1", "y1", "BUY", D(100), NOW)
        fill = walk(book, order, fees, NOW)
        assert fill.vwap == D("0.01")

    def test_boundary_price_099(self):
        """Sell at the maximum price level."""
        from polyalpha.execution import FeeSchedule, Order, walk

        book = _book(bids=[(D("0.99"), D(100))], asks=[(D("1.00"), D(1))])
        fees = FeeSchedule(D(0), NOW, "test")
        order = Order("o1", "y1", "SELL", D(100), NOW)
        fill = walk(book, order, fees, NOW)
        assert fill.vwap == D("0.99")

    def test_vwap_monotonic_increasing_shares(self):
        """Buying more shares should never decrease VWAP (asks sorted ascending)."""
        from polyalpha.execution import FeeSchedule, Order, walk

        book = _book(
            asks=[(D("0.50"), D(100)), (D("0.55"), D(100)), (D("0.60"), D(100))],
            bids=[(D("0.40"), D(500))],
        )
        fees = FeeSchedule(D(0), NOW, "test")
        prev_vwap = D(0)
        for n in [50, 100, 150, 200, 300]:
            order = Order("o1", "y1", "BUY", D(n), NOW)
            fill = walk(book, order, fees, NOW)
            assert fill.vwap >= prev_vwap
            prev_vwap = fill.vwap

    def test_sell_vwap_monotonic_decreasing_shares(self):
        """Selling more shares should never increase VWAP (bids sorted descending)."""
        from polyalpha.execution import FeeSchedule, Order, walk

        book = _book(
            bids=[(D("0.60"), D(100)), (D("0.55"), D(100)), (D("0.50"), D(100))],
            asks=[(D("0.65"), D(500))],
        )
        fees = FeeSchedule(D(0), NOW, "test")
        prev_vwap = D("1")
        for n in [50, 100, 150, 200, 300]:
            order = Order("o1", "y1", "SELL", D(n), NOW)
            fill = walk(book, order, fees, NOW)
            assert fill.vwap <= prev_vwap
            prev_vwap = fill.vwap


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Fees
# ══════════════════════════════════════════════════════════════════════════════


class TestFeeEdgeCases:
    def test_zero_rate(self):
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D(0), NOW, "test")
        assert fees.fee(D(100), D("0.50")) == D(0)

    def test_zero_price(self):
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D("0.04"), NOW, "test")
        assert fees.fee(D(100), D(0)) == D(0)

    def test_price_one(self):
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D("0.04"), NOW, "test")
        assert fees.fee(D(100), D(1)) == D(0)

    def test_standard_case(self):
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D("0.04"), NOW, "test")
        # 100 * 0.04 * 0.50 * 0.50 = 1.00
        assert fees.fee(D(100), D("0.50")) == D("1.00")

    def test_fee_symmetry(self):
        """Fee at price p should equal fee at price (1-p) for binary contracts."""
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D("0.04"), NOW, "test")
        fee_at_30 = fees.fee(D(100), D("0.30"))
        fee_at_70 = fees.fee(D(100), D("0.70"))
        assert fee_at_30 == fee_at_70

    def test_fee_max_at_50pct(self):
        """Fee should be maximized at probability 0.50."""
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D("0.04"), NOW, "test")
        fee_50 = fees.fee(D(100), D("0.50"))
        fee_40 = fees.fee(D(100), D("0.40"))
        fee_60 = fees.fee(D(100), D("0.60"))
        assert fee_50 >= fee_40
        assert fee_50 >= fee_60

    def test_fee_scales_linearly_with_shares(self):
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D("0.04"), NOW, "test")
        fee_100 = fees.fee(D(100), D("0.50"))
        fee_200 = fees.fee(D(200), D("0.50"))
        assert fee_200 == 2 * fee_100


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Kelly Sizing
# ══════════════════════════════════════════════════════════════════════════════


class TestKellyEdgeCases:
    def test_kelly_zero_edge(self):
        """When probability equals price, Kelly fraction should be zero."""
        from polyalpha.expected_value import kelly_fraction

        k = kelly_fraction(D("0.50"), D("0.50"))
        assert k == D(0)

    def test_kelly_negative_edge(self):
        """When probability < price, Kelly should be zero (don't bet)."""
        from polyalpha.expected_value import kelly_fraction

        k = kelly_fraction(D("0.40"), D("0.60"))
        assert k <= D(0)

    def test_kelly_positive_edge(self):
        """When probability > price, Kelly should be positive."""
        from polyalpha.expected_value import kelly_fraction

        k = kelly_fraction(D("0.65"), D("0.50"))
        assert k > D(0)

    def test_kelly_bounded_by_one(self):
        """Kelly fraction should never exceed 1.0."""
        from polyalpha.expected_value import kelly_fraction

        k = kelly_fraction(D("0.99"), D("0.01"))
        assert k <= D(1)

    def test_kelly_sizing_max_capped(self):
        from polyalpha.sizing import kelly_sizing

        result = kelly_sizing(D("0.99"), D("0.01"), D("10000"))
        # Even with huge edge, capped by max_fraction
        assert result.fraction_of_bankroll <= D("0.02")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Expected Value / Net Edge
# ══════════════════════════════════════════════════════════════════════════════


class TestExpectedValueEdgeCases:
    def test_gross_edge_yes(self):
        from polyalpha.expected_value import calculate_gross_edge

        assert calculate_gross_edge(D("0.65"), D("0.55"), "YES") == D("0.10")

    def test_gross_edge_no(self):
        from polyalpha.expected_value import calculate_gross_edge

        # fair_no = 1 - 0.65 = 0.35; no_ask = 0.40; edge = 0.35 - 0.40 = -0.05
        assert calculate_gross_edge(D("0.65"), D("0.40"), "NO") == D("-0.05")

    def test_net_edge_includes_all_costs(self):
        from polyalpha.execution import Fill
        from polyalpha.forecasting import Forecast, net_edge

        forecast = Forecast("m1", NOW, D("0.65"), D("0.62"), D("0.68"), "test")
        fill = Fill(
            order_id="o1",
            token_id="y1",
            side="BUY",
            requested=D(100),
            shares=D(100),
            notional=D(57),
            fees=D("1.00"),
            depth_slippage=D("0.002"),
            filled_at=NOW,
            levels=((D("0.57"), D(100)),),
        )
        edge = net_edge(forecast, fill, True)
        # conservative=0.62, vwap=0.57, fee/share=0.01,
        # extra_slippage=0.002, resolution_penalty=0.01
        # edge = 0.62 - 0.57 - 0.01 - 0.002 - 0.01 = 0.028
        assert edge == D("0.028")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Portfolio Accounting
# ══════════════════════════════════════════════════════════════════════════════


class TestPortfolioEdgeCases:
    def test_empty_portfolio_cash(self):
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        assert p.cash == D(10000)
        assert p.realized == D(0)

    def test_cash_decreases_on_buy(self):
        from polyalpha.execution import Fill
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        fill = Fill(
            "o1",
            "y1",
            "BUY",
            requested=D(100),
            shares=D(100),
            notional=D(55),
            fees=D("1.00"),
            depth_slippage=D(0),
            filled_at=NOW,
            levels=((D("0.55"), D(100)),),
        )
        p.apply(fill, "m1", "e1", "c1", "politics")
        assert p.cash < D(10000)

    def test_exposure_empty(self):
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        assert p.exposures("cluster") == {}
        assert p.exposures("category") == {}

    def test_mark_no_book(self):
        """Marking with no books should use cash as equity."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        mark = p.mark({}, {}, NOW, set())
        assert mark["liquidation_equity"] == D(10000)
        assert mark["unliquidated"] == {}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Risk Engine
# ══════════════════════════════════════════════════════════════════════════════


class TestRiskEdgeCases:
    def test_drawdown_detection(self):
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        risk.observe(D(10000), NOW)
        risk.observe(D(9200), NOW + timedelta(hours=1))
        assert risk.halted is True
        assert "drawdown" in risk.reason.lower() or "peak" in risk.reason.lower()

    def test_no_halt_within_limits(self):
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        risk.observe(D(10000), NOW)
        risk.observe(D(9900), NOW + timedelta(hours=1))
        risk.observe(D(9850), NOW + timedelta(hours=2))
        assert risk.halted is False

    def test_budget_respects_market_cap(self):
        from polyalpha.portfolio import Portfolio
        from polyalpha.risk import Risk

        portfolio = Portfolio(D(10000))
        risk = Risk(D(10000))
        budget = risk.budget(portfolio, D(10000), "m1", "e1", "c1", "politics")
        assert budget <= D(10000) * D("0.02")  # max_market_fraction


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Calibration Metrics
# ══════════════════════════════════════════════════════════════════════════════


class TestCalibrationEdgeCases:
    def test_brier_perfect(self):
        from polyalpha.calibration import metrics

        result = metrics([1.0, 0.0, 1.0, 0.0], [1, 0, 1, 0])
        assert result["brier"] == D(0)

    def test_brier_worst(self):
        from polyalpha.calibration import metrics

        result = metrics([0.0, 1.0, 0.0, 1.0], [1, 0, 1, 0])
        assert result["brier"] == D(1)

    def test_brier_all_50pct(self):
        from polyalpha.calibration import metrics

        result = metrics([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0])
        assert result["brier"] == D("0.25")

    def test_log_loss_clipping(self):
        """Log loss should clip extreme probabilities to avoid infinity."""
        import math

        from polyalpha.calibration import metrics

        result = metrics([0.001, 0.999, 0.5, 0.5], [1, 0, 1, 0])
        assert math.isfinite(float(result["log_loss"]))

    def test_ece_zero_for_perfect(self):
        from polyalpha.calibration import metrics

        result = metrics([1.0, 0.0, 1.0, 0.0], [1, 0, 1, 0])
        assert result["ece"] == D(0)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Uncertainty
# ══════════════════════════════════════════════════════════════════════════════


class TestUncertaintyEdgeCases:
    def test_spread_based_wider_spread_more_uncertainty(self):
        from polyalpha.uncertainty import SpreadBasedUncertainty

        estimator = SpreadBasedUncertainty()
        narrow = estimator.estimate(D("0.50"), {"spread": D("0.01")}, "m1")
        wide = estimator.estimate(D("0.50"), {"spread": D("0.10")}, "m1")
        assert wide.uncertainty_score > narrow.uncertainty_score

    def test_ensemble_averages(self):
        from polyalpha.uncertainty import EnsembleUncertainty, SpreadBasedUncertainty

        s1 = SpreadBasedUncertainty()
        ensemble = EnsembleUncertainty([s1, s1])
        result = ensemble.estimate(D("0.50"), {"spread": D("0.05")}, "m1")
        assert result.uncertainty_score > 0

    def test_bounds_contain_point_estimate(self):
        from polyalpha.uncertainty import SpreadBasedUncertainty

        estimator = SpreadBasedUncertainty()
        result = estimator.estimate(D("0.60"), {"spread": D("0.05")}, "m1")
        assert result.lower_bound <= D("0.60")
        assert result.upper_bound >= D("0.60")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Anomaly Detection
# ══════════════════════════════════════════════════════════════════════════════


class TestAnomalyEdgeCases:
    def test_empty_book_no_crash(self):
        from polyalpha.anomaly import AnomalyDetector

        book = _book(bids=[], asks=[])
        detector = AnomalyDetector()
        anomalies = detector.detect(book, {})
        # Empty book should produce no_asks and no_bids anomalies
        kinds = [a.kind for a in anomalies]
        assert "no_bids" in kinds or "no_asks" in kinds

    def test_normal_book_no_anomalies(self):
        from polyalpha.anomaly import AnomalyDetector

        book = _book()
        detector = AnomalyDetector()
        # Feed normal spread multiple times to build history
        for _ in range(15):
            detector.detect(book, {"microprice": None, "midpoint": None})
        # After history built, same book should not trigger spread anomaly
        anomalies = detector.detect(book, {"microprice": None, "midpoint": None})
        spread_anomalies = [a for a in anomalies if a.kind == "spread_widening"]
        assert len(spread_anomalies) == 0

    def test_uncertainty_multiplier_bounded(self):
        from polyalpha.anomaly import Anomaly, AnomalyDetector

        detector = AnomalyDetector()
        severe = [Anomaly("test", D("1.0"), "severe", {})]
        mult = detector.uncertainty_multiplier(severe)
        assert mult == D(2)  # 1 + max_severity

    def test_no_anomalies_multiplier_one(self):
        from polyalpha.anomaly import AnomalyDetector

        detector = AnomalyDetector()
        assert detector.uncertainty_multiplier([]) == D(1)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Market Quality
# ══════════════════════════════════════════════════════════════════════════════


class TestMarketQualityEdgeCases:
    def test_filter_rejects_inactive(self):
        from polyalpha.quality import Filter

        f = Filter(min_liquidity=D(5000), min_volume=D(25000), max_spread=D("0.05"))
        market = _market()
        reasons = f.market_reasons(market, NOW)
        # Should pass basic checks
        assert not any("inactive" in r for r in reasons)

    def test_filter_rejects_low_liquidity(self):
        from polyalpha.domain import Market
        from polyalpha.quality import Filter

        f = Filter(min_liquidity=D(5000), min_volume=D(25000), max_spread=D("0.05"))
        market = Market(
            market_id="m1",
            condition_id="m1",
            event_ids=("e1",),
            question="Test",
            description="Test",
            resolution_source="test",
            deadline=NOW + timedelta(days=7),
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            liquidity=D(100),
            volume=D(50),
            fees_enabled=False,
            fee_parameters_json=None,
            yes_token_id="y1",
            no_token_id="n1",
            received_at=NOW,
            category="politics",
        )
        reasons = f.market_reasons(market, NOW)
        assert any("liquidity" in r for r in reasons)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Backtest Report Completeness
# ══════════════════════════════════════════════════════════════════════════════


class TestBacktestReportEdgeCases:
    def _run_engine(self):
        from polyalpha.backtest import Engine
        from polyalpha.storage import Record

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(clusters=clusters, initial_cash=D(10000), min_edge=D("0.01"))
        records = [
            Record(
                1,
                "market",
                "m1",
                NOW,
                NOW,
                {
                    "id": "m1",
                    "conditionId": "m1",
                    "question": "Test",
                    "description": "Test",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": '["0.50","0.50"]',
                    "events": [{"id": "e1"}],
                    "active": True,
                    "closed": False,
                    "acceptingOrders": True,
                    "enableOrderBook": True,
                    "volume": "10000",
                    "liquidity": "5000",
                    "endDate": (NOW + timedelta(days=7)).isoformat(),
                    "lastUpdate": NOW.isoformat(),
                    "bestBid": "0.49",
                    "bestAsk": "0.51",
                    "spread": "0.02",
                    "yesTokenId": "y1",
                    "noTokenId": "n1",
                    "clobTokenIds": '["y1","n1"]',
                    "category": "politics",
                    "resolutionSource": "test",
                    "feesEnabled": False,
                },
            ),
            Record(
                2,
                "book",
                "y1",
                NOW,
                NOW,
                {
                    "market": "m1",
                    "asset_id": "y1",
                    "bids": [{"price": "0.49", "size": "500"}],
                    "asks": [{"price": "0.51", "size": "500"}],
                    "hash": "h1",
                    "timestamp": str(int(NOW.timestamp() * 1000)),
                    "condition_id": "m1",
                    "tick_size": "0.01",
                    "min_order_size": "1",
                },
            ),
        ]
        return engine.run(iter(records))

    def test_report_has_all_section28_fields(self):
        report = self._run_engine()
        assert "profit_factor" in report
        assert "total_trades" in report
        assert "winners" in report
        assert "losers" in report
        assert "win_rate" in report

    def test_report_has_section23_fields(self):
        report = self._run_engine()
        assert "mark_to_mid_equity" in report
        assert "mark_to_bid_equity" in report

    def test_report_has_risk_ratios(self):
        report = self._run_engine()
        assert "sharpe_ratio" in report
        assert "sortino_ratio" in report

    def test_profit_factor_none_when_no_trades(self):
        report = self._run_engine()
        # No fills -> profit_factor is None
        assert report["profit_factor"] is None
        assert report["total_trades"] == 0

    def test_mark_to_mid_equals_cash_when_no_positions(self):
        report = self._run_engine()
        assert D(report["mark_to_mid_equity"]) == D(10000)
        assert D(report["mark_to_bid_equity"]) == D(10000)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Market Making SELL Side
# ══════════════════════════════════════════════════════════════════════════════


class TestMakerQuoterSellSide:
    def test_ask_above_fair(self):
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter(base_half_spread=D("0.02"), risk_adjustment=D("0.005"))
        inv = MakerInventory(token_id="t1", side="NONE")
        q = quoter.quote(D("0.60"), inv, side="SELL")
        assert q.side == "SELL"
        assert q.price > D("0.60")
        assert q.price <= D("0.99")

    def test_short_inventory_skews_ask_up(self):
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter(
            base_half_spread=D("0.02"),
            inventory_skew_per_share=D("0.001"),
            risk_adjustment=D("0.005"),
        )
        inv_flat = MakerInventory(token_id="t1", side="NONE")
        inv_short = MakerInventory(token_id="t1", side="SELL", shares=D("500"))
        q_flat = quoter.quote(D("0.60"), inv_flat, side="SELL")
        q_short = quoter.quote(D("0.60"), inv_short, side="SELL")
        assert q_short.price >= q_flat.price

    def test_invalid_side_raises(self):
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter()
        inv = MakerInventory(token_id="t1", side="NONE")
        with pytest.raises(ValueError, match="side"):
            quoter.quote(D("0.60"), inv, side="INVALID")
