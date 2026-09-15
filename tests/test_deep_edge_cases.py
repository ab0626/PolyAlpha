"""Deep edge-case and hidden-property tests for critical subsystems.

Focuses on numerical correctness, boundary conditions, and invariants
that a quant research audit would verify.
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


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Backtest Engine Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestBacktestEngineEdgeCases:
    def test_empty_records(self):
        """Engine with zero records should produce empty report."""
        from polyalpha.backtest import Engine

        engine = Engine(clusters={}, initial_cash=D(10000))
        report = engine.run(iter([]))
        assert report["fills"] == 0
        assert report["input_records"] == 0
        assert report["liquidation_equity"] == str(D(10000))

    def test_single_market_no_books(self):
        """Market record without book should not crash."""
        from polyalpha.backtest import Engine
        from polyalpha.storage import Record

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(clusters=clusters, initial_cash=D(10000))
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
        ]
        report = engine.run(iter(records))
        assert report["fills"] == 0

    def test_chronological_violation_raises(self):
        """Out-of-order records should raise ValueError."""
        from polyalpha.backtest import Engine
        from polyalpha.storage import Record

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(clusters=clusters, initial_cash=D(10000))
        t1 = NOW
        t2 = NOW + timedelta(hours=1)
        records = [
            Record(
                1,
                "market",
                "m1",
                t2,
                t2,
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
                    "lastUpdate": t2.isoformat(),
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
                "market",
                "m1",
                t1,
                t1,
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
                    "lastUpdate": t1.isoformat(),
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
        ]
        with pytest.raises(ValueError, match="chronological"):
            engine.run(iter(records))

    def test_report_has_all_new_metrics(self):
        """Report should include payoff_ratio and average_drawdown."""
        from polyalpha.backtest import Engine
        from polyalpha.storage import Record

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(clusters=clusters, initial_cash=D(10000))
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
        report = engine.run(iter(records))
        assert "payoff_ratio" in report
        assert "average_drawdown" in report
        assert report["payoff_ratio"] is None  # no trades
        assert report["average_drawdown"] == "0"  # no drawdown

    def test_engine_accepts_risk_limits(self):
        """Engine should accept custom risk limits."""
        from polyalpha.backtest import Engine
        from polyalpha.risk import Limits

        limits = Limits(
            normal=D("0.01"),
            market=D("0.05"),
            event=D("0.10"),
            cluster=D("0.10"),
            category=D("0.20"),
            total=D("0.40"),
            daily_loss=D("0.05"),
            drawdown=D("0.15"),
        )
        engine = Engine(clusters={}, initial_cash=D(10000), risk_limits=limits)
        assert engine.risk.limits.normal == D("0.01")
        assert engine.risk.limits.drawdown == D("0.15")

    def test_engine_accepts_custom_stale_age(self):
        """Engine should accept custom stale_metadata_age."""
        from polyalpha.backtest import Engine

        engine = Engine(clusters={}, initial_cash=D(10000), stale_metadata_age=600)
        assert engine.stale_metadata_age == 600


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Portfolio Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestPortfolioEdgeCases:
    def test_buy_then_sell_restores_cash(self):
        """Buying and selling same quantity should approximately restore cash (minus fees)."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        sell = _fill("o2", "y1", "SELL", D(100), D("0.50"), D("0"))
        p.apply(sell, "m1", "e1", "c1", "politics")
        assert p.cash == D(10000)
        assert p.realized == D(0)

    def test_partial_sell(self):
        """Selling partial position should update basis correctly."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        sell = _fill("o2", "y1", "SELL", D(50), D("0.60"), D("0"))
        p.apply(sell, "m1", "e1", "c1", "politics")
        pos = p.positions["y1"]
        assert pos.shares == D(50)
        assert p.realized == D(5)  # 50 * (0.60 - 0.50)

    def test_sell_more_than_owned_rejected(self):
        """Selling more shares than owned should raise."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        sell = _fill("o2", "y1", "SELL", D(150), D("0.60"), D("0"))
        with pytest.raises(ValueError, match="cannot sell"):
            p.apply(sell, "m1", "e1", "c1", "politics")

    def test_duplicate_fill_rejected(self):
        """Applying the same fill twice should raise."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        fill = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("1.00"))
        p.apply(fill, "m1", "e1", "c1", "politics")
        with pytest.raises(ValueError, match="duplicate"):
            p.apply(fill, "m1", "e1", "c1", "politics")

    def test_insufficient_cash_rejected(self):
        """Buying more than cash allows should raise."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(100))
        fill = _fill("o1", "y1", "BUY", D(1000), D("0.50"), D("0"))
        with pytest.raises(ValueError, match="insufficient"):
            p.apply(fill, "m1", "e1", "c1", "politics")

    def test_settle_full_position(self):
        """Full settlement should zero position and add payout to cash."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        p.settle("s1", "y1", D(1), NOW, NOW)  # YES resolves
        assert p.cash == D(10000 - 50 + 100)  # 10000 - cost + payout
        assert p.positions["y1"].shares == D(0)

    def test_settle_partial_position(self):
        """Settlement with partial shares should work."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        sell = _fill("o2", "y1", "SELL", D(50), D("0.50"), D("0"))
        p.apply(sell, "m1", "e1", "c1", "politics")
        p.settle("s1", "y1", D(1), NOW, NOW)
        assert p.positions["y1"].shares == D(0)

    def test_settle_future_rejected(self):
        """Settlement with future known_at should raise."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        future = NOW + timedelta(days=1)
        with pytest.raises(ValueError, match="future"):
            p.settle("s1", "y1", D(1), future, NOW)

    def test_duplicate_settlement_rejected(self):
        """Duplicate settlement ID should raise."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        p.apply(buy, "m1", "e1", "c1", "politics")
        p.settle("s1", "y1", D(1), NOW, NOW)
        with pytest.raises(ValueError, match="duplicate"):
            p.settle("s1", "y1", D(0), NOW, NOW)

    def test_exposure_by_dimension(self):
        """Exposures should aggregate by the requested dimension."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        buy1 = _fill("o1", "y1", "BUY", D(100), D("0.50"), D("0"))
        buy2 = _fill("o2", "y2", "BUY", D(200), D("0.50"), D("0"))
        p.apply(buy1, "m1", "e1", "c1", "politics")
        p.apply(buy2, "m2", "e1", "c1", "sports")
        cluster_exp = p.exposures("cluster")
        assert cluster_exp["c1"] == D(50) + D(100)  # basis amounts
        cat_exp = p.exposures("category")
        assert cat_exp["politics"] == D(50)
        assert cat_exp["sports"] == D(100)

    def test_mark_no_positions(self):
        """Marking with no positions should return cash as equity."""
        from polyalpha.portfolio import Portfolio

        p = Portfolio(D(10000))
        mark = p.mark({}, {}, NOW)
        assert mark["liquidation_equity"] == D(10000)
        assert mark["midpoint_equity"] == D(10000)
        assert mark["unliquidated"] == {}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Risk Engine Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestRiskEdgeCases:
    def test_zero_equity_halts(self):
        """Zero equity should trigger halt."""
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        risk.observe(D(0), NOW)
        assert risk.halted is True

    def test_drawdown_exact_threshold(self):
        """Drawdown exactly at threshold should halt."""
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        risk.observe(D(10000), NOW)
        risk.observe(D(9200), NOW + timedelta(hours=1))  # 8% drawdown
        assert risk.halted is True

    def test_daily_loss_exact_threshold(self):
        """Daily loss exactly at threshold should halt."""
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        risk.observe(D(10000), NOW)
        risk.observe(D(9800), NOW + timedelta(hours=1))  # 2% daily loss
        assert risk.halted is True
        assert "daily" in risk.reason

    def test_budget_zero_when_halted(self):
        """Budget should be zero when risk is halted."""
        from polyalpha.portfolio import Portfolio
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        risk.halted = True
        portfolio = Portfolio(D(10000))
        budget = risk.budget(portfolio, D(10000), "m1", "e1", "c1", "politics")
        assert budget == D(0)

    def test_budget_respects_all_caps(self):
        """Budget should be minimum of all cap constraints."""
        from polyalpha.portfolio import Portfolio
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        portfolio = Portfolio(D(10000))
        budget = risk.budget(portfolio, D(10000), "m1", "e1", "c1", "politics")
        # Should be min(cash=10000, normal=50, total=20000, market=200, event=500, cluster=500, category=1000)
        assert budget == D(50)  # normal fraction is smallest

    def test_limits_validation(self):
        """Limits with invalid values should raise."""
        from polyalpha.risk import Limits

        with pytest.raises(ValueError):
            Limits(normal=D(0))
        with pytest.raises(ValueError):
            Limits(normal=D(2))
        with pytest.raises(ValueError):
            Limits(drawdown=D(-1))

    def test_peak_tracking(self):
        """Peak should only increase, never decrease."""
        from polyalpha.risk import Risk

        risk = Risk(D(10000))
        risk.observe(D(10000), NOW)
        assert risk.peak == D(10000)
        risk.observe(D(9500), NOW + timedelta(hours=1))
        assert risk.peak == D(10000)
        risk.observe(D(11000), NOW + timedelta(hours=2))
        assert risk.peak == D(11000)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Compliance Module
# ══════════════════════════════════════════════════════════════════════════════


class TestCompliance:
    def test_us_research_only(self):
        from polyalpha.compliance import check_jurisdiction

        check = check_jurisdiction("US")
        assert check.can_collect is True
        assert check.can_trade is False
        assert check.can_paper_trade is True
        assert check.mode == "paper_trading"

    def test_unknown_jurisdiction_warns(self):
        from polyalpha.compliance import check_jurisdiction

        check = check_jurisdiction("ZZ")
        assert check.can_collect is True
        assert check.can_trade is True
        assert any("Unknown" in w for w in check.warnings)

    def test_sanctioned_blocked(self):
        from polyalpha.compliance import check_jurisdiction

        check = check_jurisdiction("SANCTIONED")
        assert check.mode == "blocked"
        assert check.can_collect is False

    def test_paper_only_enforcement(self):
        from polyalpha.compliance import enforce_paper_only

        assert enforce_paper_only("US") is True
        assert enforce_paper_only("SANCTIONED") is True

    def test_system_mode(self):
        from polyalpha.compliance import system_mode

        assert system_mode("US") == "paper_trading"
        assert system_mode("SANCTIONED") == "blocked"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Performance Metrics Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestPerformanceMetricsEdgeCases:
    def test_profit_factor_zero_wins(self):
        from polyalpha.performance import profit_factor

        assert profit_factor(0, 100) == 0.0

    def test_profit_factor_zero_losses(self):
        from polyalpha.performance import profit_factor

        assert profit_factor(100, 0) == float("inf")

    def test_profit_factor_both_zero(self):
        from polyalpha.performance import profit_factor

        assert profit_factor(0, 0) is None

    def test_brier_perfect(self):
        from polyalpha.performance import brier_score

        assert brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == 0.0

    def test_brier_worst(self):
        from polyalpha.performance import brier_score

        assert brier_score([0.0, 1.0], [1, 0]) == 1.0

    def test_sortino_all_positive(self):
        from polyalpha.performance import sortino_ratio

        result = sortino_ratio([0.01, 0.02, 0.03])
        assert result is None  # no downside

    def test_sortino_mixed(self):
        from polyalpha.performance import sortino_ratio

        result = sortino_ratio([0.01, -0.02, 0.03, -0.01])
        assert result is not None

    def test_drawdown_empty(self):
        from polyalpha.performance import drawdown_series

        assert drawdown_series([]) == []

    def test_drawdown_monotonic_non_increasing_from_peak(self):
        """Drawdown should never be negative."""
        from polyalpha.performance import drawdown_series

        dd = drawdown_series([100, 90, 95, 85, 100, 80])
        assert all(d >= 0 for d in dd)
        assert dd[0] == 0  # first point is always peak

    def test_holding_period_no_fills(self):
        from polyalpha.performance import holding_period_stats

        result = holding_period_stats([])
        assert result["sample_size"] == 0

    def test_trade_metrics_empty(self):
        from polyalpha.performance import trade_metrics

        result = trade_metrics([])
        assert result["total_trades"] == 0

    def test_value_at_risk_insufficient_data(self):
        from polyalpha.performance import value_at_risk

        assert value_at_risk([0.01, 0.02]) is None

    def test_information_ratio_insufficient_data(self):
        from polyalpha.performance import information_ratio

        assert information_ratio([0.01]) is None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Signals Module Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestSignalsEdgeCases:
    def test_reject_inactive_market(self):
        from polyalpha.forecasting import Forecast
        from polyalpha.signals import evaluate_entry

        market = _market()
        market = type(market)(
            market_id=market.market_id,
            condition_id=market.condition_id,
            event_ids=market.event_ids,
            question=market.question,
            description=market.description,
            resolution_source=market.resolution_source,
            deadline=market.deadline,
            active=False,
            closed=market.closed,
            accepting_orders=market.accepting_orders,
            enable_order_book=market.enable_order_book,
            liquidity=market.liquidity,
            volume=market.volume,
            fees_enabled=market.fees_enabled,
            fee_parameters_json=market.fee_parameters_json,
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
            received_at=market.received_at,
            category=market.category,
        )
        book = _book()
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D(0), NOW, "test")
        forecast = Forecast("m1", NOW, D("0.65"), D("0.62"), D("0.68"), "test")
        result = evaluate_entry(market, book, book, forecast, fees, D(10000), D(200), NOW)
        assert result.signal is None
        assert "market_inactive" in result.rejections

    def test_reject_stale_book(self):
        from polyalpha.execution import FeeSchedule
        from polyalpha.forecasting import Forecast
        from polyalpha.signals import evaluate_entry

        market = _market()
        stale_book = _book()
        # Make book old
        from polyalpha.domain import Book, Level

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
        fees = FeeSchedule(D(0), NOW, "test")
        forecast = Forecast("m1", NOW, D("0.65"), D("0.62"), D("0.68"), "test")
        result = evaluate_entry(
            market,
            stale_book,
            stale_book,
            forecast,
            fees,
            D(10000),
            D(200),
            NOW,
            max_prediction_age_seconds=30,
        )
        assert result.signal is None
        assert any("stale_book" in r for r in result.rejections)

    def test_signal_properties(self):
        """Signal should have is_executable and is_positive_edge properties."""
        from polyalpha.signals import Signal

        s = Signal(
            market_id="m1",
            token_id="y1",
            side="BUY",
            timestamp=NOW,
            fair_probability=D("0.65"),
            conservative_probability=D("0.62"),
            execution_price=D("0.55"),
            fee_per_share=D("0.01"),
            slippage_penalty=D("0.002"),
            uncertainty_penalty=D("0.01"),
            resolution_penalty=D("0.01"),
            stale_data_penalty=D(0),
            liquidity_penalty=D(0),
            gross_edge=D("0.10"),
            net_edge=D("0.05"),
            confidence=D("0.95"),
            model_version="test",
        )
        assert s.is_positive_edge is True
        assert s.is_executable is True

        s_no_edge = Signal(
            market_id="m1",
            token_id="y1",
            side="BUY",
            timestamp=NOW,
            fair_probability=D("0.50"),
            conservative_probability=D("0.48"),
            execution_price=D("0.55"),
            fee_per_share=D("0.01"),
            slippage_penalty=D("0.002"),
            uncertainty_penalty=D("0.01"),
            resolution_penalty=D("0.01"),
            stale_data_penalty=D(0),
            liquidity_penalty=D(0),
            gross_edge=D("-0.05"),
            net_edge=D("-0.07"),
            confidence=D("0.60"),
            model_version="test",
        )
        assert s_no_edge.is_positive_edge is False
        assert s_no_edge.is_executable is False


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Sizing Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestSizingEdgeCases:
    def test_fixed_fractional_zero_equity(self):
        from polyalpha.sizing import fixed_fractional_sizing

        with pytest.raises(ValueError, match="positive equity"):
            fixed_fractional_sizing(D(0), D("0.005"))

    def test_kelly_no_edge(self):
        from polyalpha.sizing import kelly_sizing

        result = kelly_sizing(D("0.50"), D("0.50"), D(10000))
        assert result.shares == D(0)

    def test_constrained_sizing_all_caps(self):
        """Constrained sizing should respect all caps simultaneously."""
        from polyalpha.sizing import constrained_sizing

        result = constrained_sizing(
            equity=D(10000),
            risk_budget=D(200),
            price=D("0.50"),
            normal_fraction=D("0.005"),
            max_market_fraction=D("0.02"),
        )
        # Should be capped by normal fraction: 10000 * 0.005 / 0.50 = 100 shares
        assert result.shares <= D(100)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Execution Numerical Invariants
# ══════════════════════════════════════════════════════════════════════════════


class TestExecutionNumericalInvariants:
    def test_fee_symmetry_at_midpoint(self):
        """Fee at price p should equal fee at (1-p) for binary contracts."""
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D("0.04"), NOW, "test")
        assert fees.fee(D(100), D("0.30")) == fees.fee(D(100), D("0.70"))

    def test_fee_quadratic_shape(self):
        """Fee should be maximized at 0.50 and symmetric."""
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D("0.04"), NOW, "test")
        f_50 = fees.fee(D(100), D("0.50"))
        f_40 = fees.fee(D(100), D("0.40"))
        f_60 = fees.fee(D(100), D("0.60"))
        assert f_50 > f_40
        assert f_50 > f_60
        assert f_40 == f_60

    def test_vwap_never_better_than_best_price(self):
        """BUY VWAP should always be >= best ask; SELL VWAP should always be <= best bid."""
        from polyalpha.execution import FeeSchedule, Order, walk

        book = _book(
            asks=[(D("0.50"), D(100)), (D("0.55"), D(100)), (D("0.60"), D(100))],
            bids=[(D("0.45"), D(100)), (D("0.40"), D(100)), (D("0.35"), D(100))],
        )
        fees = FeeSchedule(D(0), NOW, "test")
        buy_order = Order("o1", "y1", "BUY", D(150), NOW)
        buy_fill = walk(book, buy_order, fees, NOW)
        assert buy_fill.vwap >= D("0.50")

        sell_order = Order("o2", "y1", "SELL", D(150), NOW)
        sell_fill = walk(book, sell_order, fees, NOW)
        assert sell_fill.vwap <= D("0.45")

    def test_partial_fill_shares_lte_requested(self):
        """Partial fill should never exceed requested shares."""
        from polyalpha.execution import FeeSchedule, Order, walk

        book = _book(asks=[(D("0.55"), D(50))])
        fees = FeeSchedule(D(0), NOW, "test")
        order = Order("o1", "y1", "BUY", D(100), NOW, allow_partial=True)
        fill = walk(book, order, fees, NOW)
        assert fill.shares <= order.shares
        assert fill.shares == D(50)
