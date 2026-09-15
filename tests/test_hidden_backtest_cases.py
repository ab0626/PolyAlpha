"""Part 53 — Backtest hidden tests.

No signals, one signal, all winners, all losers, all unresolved,
zero/tiny/large starting capital, same timestamp multiple markets,
market resolves during position, entry and resolution same timestamp,
fees/slippage consume entire edge, partial fill, stale signal/book.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from polyalpha.execution import FeeSchedule, Fill, Order, Simulator, walk
from polyalpha.portfolio import Portfolio, Position
from polyalpha.risk import Limits, Risk

D = Decimal
TZ = timezone.utc
TS = datetime(2025, 6, 1, tzinfo=TZ)


def _ts(h=0):
    return TS + timedelta(hours=h)


def _level(price, size):
    return (D(str(price)), D(str(size)))


def _fill(order_id="o1", token="t1", shares=100, vwap=0.5, side="BUY", fees=0):
    p = D(str(vwap))
    s = D(str(shares))
    return Fill(
        order_id=order_id, token_id=token, side=side,
        requested=s, shares=s,
        notional=p * s, fees=D(str(fees)),
        depth_slippage=D(0), filled_at=TS,
        levels=((p, s),),
    )


def _portfolio(cash=10000):
    return Portfolio(D(str(cash)))


def _risk(equity=10000):
    return Risk(D(str(equity)))


def _position(token="t1", shares=100, basis=50, market_id="m1",
              event_id="e1", cluster="cl1", category="cat1"):
    return Position(token, market_id, event_id, cluster, category,
                    D(str(shares)), D(str(basis)))


# ── No signals ───────────────────────────────────────────────────────────


class TestNoSignals:
    def test_empty_fill_journal(self):
        sim = Simulator()
        assert len(sim.orders) == 0

    def test_empty_portfolio(self):
        p = _portfolio()
        assert p.cash == D(10000)
        assert len(p.positions) == 0


# ── One signal ───────────────────────────────────────────────────────────


class TestOneSignal:
    def test_single_fill_applied(self):
        p = _portfolio()
        f = _fill()
        p.apply(f, "m1", "e1", "cl1", "cat1")
        assert p.cash < D(10000)
        assert "t1" in p.positions

    def test_single_fill_portfolio_state(self):
        p = _portfolio(cash=1000)
        f = _fill(shares=100, vwap=0.5)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        assert p.cash == D(950)
        assert p.positions["t1"].shares == D(100)


# ── All winners ──────────────────────────────────────────────────────────


class TestAllWinners:
    def test_all_winning_fills(self):
        p = _portfolio(cash=10000)
        for i in range(5):
            f = _fill(order_id=f"o{i}", token=f"t{i}", shares=50, vwap=0.5)
            p.apply(f, f"m{i}", f"e{i}", "cl{i}", "cat{i}")
        # Each fill costs 50 * 0.5 = 25, total 125, cash = 9875
        assert p.fees == D(0)
        assert p.cash == D("9875.0")

    def test_all_winning_realized_pnl(self):
        p = _portfolio(cash=10000)
        f = _fill(shares=100, vwap=0.4)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        # Simulate settlement at 1.0
        p.settle("s1", "t1", D(1), _ts(1), _ts(1))
        assert p.realized > 0


# ── All losers ───────────────────────────────────────────────────────────


class TestAllLosers:
    def test_all_losing_fills(self):
        p = _portfolio(cash=10000)
        for i in range(5):
            f = _fill(order_id=f"o{i}", token=f"t{i}", shares=50, vwap=0.5)
            p.apply(f, f"m{i}", f"e{i}", "cl{i}", "cat{i}")
        # Each fill costs 50 * 0.5 = 25, total 125, cash = 9875
        assert p.cash == D("9875.0")

    def test_all_losing_settled(self):
        p = _portfolio(cash=10000)
        f = _fill(shares=100, vwap=0.6)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        p.settle("s1", "t1", D(0), _ts(1), _ts(1))
        assert p.realized < 0


# ── All unresolved ───────────────────────────────────────────────────────


class TestAllUnresolved:
    def test_unresolved_positions(self):
        p = _portfolio(cash=10000)
        for i in range(5):
            f = _fill(order_id=f"o{i}", token=f"t{i}", shares=50, vwap=0.5)
            p.apply(f, f"m{i}", f"e{i}", "cl{i}", "cat{i}")
        assert len(p.positions) == 5


# ── Starting capital edge cases ──────────────────────────────────────────


class TestStartingCapital:
    def test_zero_cash_rejected(self):
        with pytest.raises(ValueError, match="invalid initial cash"):
            Portfolio(D(0))

    def test_negative_cash_rejected(self):
        with pytest.raises(ValueError, match="invalid initial cash"):
            Portfolio(D(-100))

    def test_tiny_cash(self):
        p = Portfolio(D("0.01"))
        assert p.cash == D("0.01")

    def test_large_cash(self):
        p = Portfolio(D("999999999"))
        assert p.cash == D("999999999")

    def test_insufficient_cash_for_fill(self):
        p = Portfolio(D(10))
        f = _fill(shares=100, vwap=0.5)
        with pytest.raises(ValueError, match="insufficient cash"):
            p.apply(f, "m1", "e1", "cl1", "cat1")


# ── Same timestamp multiple markets ──────────────────────────────────────


class TestSameTimestampMultipleMarkets:
    def test_multiple_fills_same_timestamp(self):
        p = _portfolio(cash=10000)
        for i in range(5):
            f = _fill(order_id=f"o{i}", token=f"t{i}", shares=50, vwap=0.5)
            p.apply(f, f"m{i}", f"e{i}", "cl{i}", "cat{i}")
        assert len(p.positions) == 5
        # Each fill costs 50 * 0.5 = 25, total 125, cash = 9875
        assert p.cash == D("9875.0")


# ── Market resolves during position ──────────────────────────────────────


class TestResolutionDuringPosition:
    def test_settle_reduces_position(self):
        p = _portfolio()
        f = _fill(shares=100, vwap=0.5)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        p.settle("s1", "t1", D(1), _ts(1), _ts(1))
        assert p.positions["t1"].shares == D(0)

    def test_settle_full_payout(self):
        p = _portfolio()
        f = _fill(shares=100, vwap=0.5)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        initial_cash = p.cash
        p.settle("s1", "t1", D(1), _ts(1), _ts(1))
        assert p.cash == initial_cash + D(100)


# ── Entry and resolution same timestamp ──────────────────────────────────


class TestEntryResolutionSameTimestamp:
    def test_immediate_settlement(self):
        p = _portfolio()
        f = _fill(shares=100, vwap=0.5, order_id="o1")
        p.apply(f, "m1", "e1", "cl1", "cat1")
        p.settle("s1", "t1", D(1), TS, TS)
        assert p.realized > 0


# ── Fees consume entire edge ─────────────────────────────────────────────


class TestFeesConsumeEdge:
    def test_high_fee_settlement(self):
        p = _portfolio()
        f = _fill(shares=100, vwap=0.5, fees=50)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        assert p.fees == D(50)
        # Cost = notional + fees = 50 + 50 = 100, cash = 10000 - 100 = 9900
        assert p.cash == D("9900.0")


# ── Partial fill ─────────────────────────────────────────────────────────


class TestPartialFill:
    def test_partial_sell(self):
        p = _portfolio()
        f = _fill(shares=100, vwap=0.5)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        f2 = _fill(order_id="o2", shares=50, vwap=0.6, side="SELL")
        p.apply(f2, "m1", "e1", "cl1", "cat1")
        assert p.positions["t1"].shares == D(50)


# ── Duplicate fill ───────────────────────────────────────────────────────


class TestDuplicateFill:
    def test_duplicate_fill_rejected(self):
        p = _portfolio()
        f = _fill()
        p.apply(f, "m1", "e1", "cl1", "cat1")
        with pytest.raises(ValueError, match="duplicate fill"):
            p.apply(f, "m1", "e1", "cl1", "cat1")


# ── Cannot sell unowned shares ───────────────────────────────────────────


class TestSellUnowned:
    def test_sell_without_position(self):
        p = _portfolio()
        f = _fill(shares=100, vwap=0.5, side="SELL")
        with pytest.raises(ValueError, match="cannot sell unowned"):
            p.apply(f, "m1", "e1", "cl1", "cat1")

    def test_sell_more_than_owned(self):
        p = _portfolio()
        f = _fill(shares=100, vwap=0.5)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        f2 = _fill(order_id="o2", shares=200, vwap=0.6, side="SELL")
        with pytest.raises(ValueError, match="cannot sell unowned"):
            p.apply(f2, "m1", "e1", "cl1", "cat1")


# ── Duplicate settlement ─────────────────────────────────────────────────


class TestDuplicateSettlement:
    def test_duplicate_settlement_rejected(self):
        p = _portfolio()
        f = _fill()
        p.apply(f, "m1", "e1", "cl1", "cat1")
        p.settle("s1", "t1", D(1), _ts(1), _ts(1))
        with pytest.raises(ValueError, match="duplicate settlement"):
            p.settle("s1", "t1", D(1), _ts(1), _ts(1))


# ── Settlement validation ────────────────────────────────────────────────


class TestSettlementValidation:
    def test_future_settlement_rejected(self):
        p = _portfolio()
        f = _fill()
        p.apply(f, "m1", "e1", "cl1", "cat1")
        with pytest.raises(ValueError, match="future settlement"):
            p.settle("s1", "t1", D(1), _ts(100), _ts(1))

    def test_invalid_payout_rejected(self):
        p = _portfolio()
        f = _fill()
        p.apply(f, "m1", "e1", "cl1", "cat1")
        with pytest.raises(ValueError, match="invalid or future settlement"):
            p.settle("s1", "t1", D(1.5), _ts(1), _ts(1))


# ── Exposures ────────────────────────────────────────────────────────────


class TestExposures:
    def test_exposures_by_market(self):
        p = _portfolio()
        f = _fill(shares=100, vwap=0.5)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        exp = p.exposures("market_id")
        assert "m1" in exp
        assert exp["m1"] == D(50)

    def test_exposures_by_cluster(self):
        p = _portfolio()
        f = _fill(shares=100, vwap=0.5)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        exp = p.exposures("cluster")
        assert "cl1" in exp

    def test_total_exposure(self):
        p = _portfolio()
        f = _fill(shares=100, vwap=0.5)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        exp = p.exposures()
        assert "total" in exp


# ── Position average cost ────────────────────────────────────────────────


class TestAverageCost:
    def test_average_cost_single_fill(self):
        p = _portfolio()
        f = _fill(shares=100, vwap=0.5)
        p.apply(f, "m1", "e1", "cl1", "cat1")
        assert p.positions["t1"].average_cost == D("0.5")

    def test_average_cost_multiple_fills(self):
        p = _portfolio()
        f1 = _fill(order_id="o1", shares=100, vwap=0.4)
        f2 = _fill(order_id="o2", shares=100, vwap=0.6)
        p.apply(f1, "m1", "e1", "cl1", "cat1")
        p.apply(f2, "m1", "e1", "cl1", "cat1")
        # basis = 40 + 60 = 100, shares = 200
        assert p.positions["t1"].average_cost == D("0.5")


# ── Walk stale book ──────────────────────────────────────────────────────


class TestStaleBook:
    def test_stale_book_rejected(self):
        from polyalpha.domain import Book, Level
        stale_ts = TS - timedelta(seconds=60)
        b = Book(
            token_id="t1", condition_id="c1",
            source_at=stale_ts, received_at=stale_ts,
            bids=(Level(D("0.49"), D("100")),),
            asks=(Level(D("0.51"), D("100")),),
            tick_size=D("0.01"), min_order_size=D(1),
            source_hash="",
        )
        order = Order("o1", "t1", "BUY", D(50), TS)
        fees = FeeSchedule(D(0), TS, "free")
        with pytest.raises(ValueError, match="stale book"):
            walk(b, order, fees, TS)


# ── Walk future information ──────────────────────────────────────────────


class TestFutureInformation:
    def test_future_book_rejected(self):
        from polyalpha.domain import Book, Level
        future_ts = TS + timedelta(seconds=60)
        b = Book(
            token_id="t1", condition_id="c1",
            source_at=future_ts, received_at=future_ts,
            bids=(Level(D("0.49"), D("100")),),
            asks=(Level(D("0.51"), D("100")),),
            tick_size=D("0.01"), min_order_size=D(1),
            source_hash="",
        )
        order = Order("o1", "t1", "BUY", D(50), TS)
        fees = FeeSchedule(D(0), TS, "free")
        with pytest.raises(ValueError, match="future information"):
            walk(b, order, fees, TS)


# ── Risk halt during trading ─────────────────────────────────────────────


class TestRiskHaltDuringTrading:
    def test_halted_budget_zero(self):
        r = _risk()
        r.halted = True
        p = _portfolio()
        budget = r.budget(p, D(10000), "m1", "e1", "cl1", "cat1")
        assert budget == D(0)
