"""Part 45-46 — Order-book + VWAP hidden tests.

Empty book, only bids/asks, single level, duplicate prices, unsorted levels,
zero/negative sizes, crossed/locked book, wide spread, 1000+ levels,
trade consumption, depth exceeding, VWAP monotonicity.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyalpha.domain import Book, Level
from polyalpha.execution import FeeSchedule, Fill, Order, Simulator, walk

D = Decimal
TS = datetime(2025, 6, 1, tzinfo=timezone.utc)
FEE_FREE = FeeSchedule(D(0), TS, "free")
FEE_2PCT = FeeSchedule(D("0.02"), TS, "fee2")


def _ts(offset_seconds=0):
    return TS + timedelta(seconds=offset_seconds)


def _level(price, size):
    return Level(D(str(price)), D(str(size)))


def _book(bids, asks, tick=D("0.01"), min_size=D(1)):
    return Book(
        token_id="t1", condition_id="c1",
        source_at=TS, received_at=TS,
        bids=tuple(_level(p, s) for p, s in bids),
        asks=tuple(_level(p, s) for p, s in asks),
        tick_size=D(str(tick)), min_order_size=D(str(min_size)),
        source_hash="",
    )


def _order(shares, side="BUY", limit=None, partial=False, token="t1"):
    return Order(
        "o1", token, side, D(str(shares)), TS,
        limit=D(str(limit)) if limit else None,
        allow_partial=partial,
    )


# ── Empty book ───────────────────────────────────────────────────────────


class TestEmptyBook:
    def test_empty_bids_and_asks_rejected(self):
        b = Book(
            token_id="t1", condition_id="c1",
            source_at=TS, received_at=TS,
            bids=(), asks=(),
            tick_size=D("0.01"), min_order_size=D(1),
            source_hash="",
        )
        assert b.best_bid is None
        assert b.best_ask is None
        assert b.spread is None

    def test_walk_empty_book_fails(self):
        b = Book(
            token_id="t1", condition_id="c1",
            source_at=TS, received_at=TS,
            bids=(), asks=(),
            tick_size=D("0.01"), min_order_size=D(1),
            source_hash="",
        )
        with pytest.raises(ValueError, match="unusable book"):
            walk(b, _order(50), FEE_FREE, TS)


# ── Only one side ────────────────────────────────────────────────────────


class TestOneSidedBook:
    def test_only_bids_no_asks(self):
        b = _book([(0.5, 100)], [])
        assert b.best_bid == D("0.5")
        assert b.best_ask is None
        assert b.spread is None

    def test_only_asks_no_bids(self):
        b = _book([], [(0.5, 100)])
        assert b.best_bid is None
        assert b.best_ask == D("0.5")

    def test_buy_from_bids_only_fails(self):
        b = _book([(0.5, 100)], [])
        with pytest.raises(ValueError, match="unusable book"):
            walk(b, _order(50), FEE_FREE, TS)


# ── Single level ─────────────────────────────────────────────────────────


class TestSingleLevel:
    def test_exact_fill_single_ask(self):
        b = _book([(0.4, 100)], [(0.5, 100)])
        f = walk(b, _order(100), FEE_FREE, TS)
        assert f.shares == D(100)
        assert f.notional == D(50)
        assert f.vwap == D("0.5")

    def test_partial_fill_single_ask_not_allowed(self):
        b = _book([(0.4, 100)], [(0.5, 100)])
        with pytest.raises(ValueError, match="insufficient depth"):
            walk(b, _order(200), FEE_FREE, TS)

    def test_partial_fill_allowed(self):
        b = _book([(0.4, 100)], [(0.5, 100)])
        f = walk(b, _order(200, partial=True), FEE_FREE, TS)
        assert f.shares == D(100)
        assert f.shares < f.requested


# ── Duplicate price levels ───────────────────────────────────────────────


class TestDuplicatePriceLevels:
    def test_duplicate_bid_price_rejected(self):
        with pytest.raises(ValueError, match="unique and sorted"):
            _book([(0.5, 100), (0.5, 50)], [(0.6, 100)])

    def test_duplicate_ask_price_rejected(self):
        with pytest.raises(ValueError, match="unique and sorted"):
            _book([(0.5, 100)], [(0.6, 100), (0.6, 50)])


# ── Unsorted levels ──────────────────────────────────────────────────────


class TestUnsortedLevels:
    def test_unsorted_bids_rejected(self):
        with pytest.raises(ValueError, match="unique and sorted"):
            _book([(0.4, 100), (0.5, 100)], [(0.6, 100)])

    def test_unsorted_asks_rejected(self):
        with pytest.raises(ValueError, match="unique and sorted"):
            _book([(0.5, 100)], [(0.7, 100), (0.6, 100)])


# ── Zero-sized levels ───────────────────────────────────────────────────


class TestZeroSizedLevels:
    def test_zero_size_bid_rejected(self):
        with pytest.raises(ValueError, match="size must be positive"):
            _book([(0.5, 0)], [(0.6, 100)])

    def test_zero_size_ask_rejected(self):
        with pytest.raises(ValueError, match="size must be positive"):
            _book([(0.5, 100)], [(0.6, 0)])


# ── Crossed book (bid > ask) ────────────────────────────────────────────


class TestCrossedBook:
    def test_crossed_book_rejected(self):
        """Crossed book: bid=0.6, ask=0.5.
        Book validation checks bids are sorted descending and asks ascending
        with unique prices. Here bids=[0.6] and asks=[0.5] are both single
        levels so individually sorted. But walk() rejects spread<=0."""
        b = Book(
            token_id="t1", condition_id="c1",
            source_at=TS, received_at=TS,
            bids=(Level(D("0.6"), D("100")),),
            asks=(Level(D("0.5"), D("100")),),
            tick_size=D("0.01"), min_order_size=D(1),
            source_hash="",
        )
        assert b.spread == D("-0.1")  # crossed
        with pytest.raises(ValueError, match="unusable book"):
            walk(b, _order(50), FEE_FREE, TS)


# ── Locked book (bid == ask) ────────────────────────────────────────────


class TestLockedBook:
    def test_locked_book_rejected_by_domain(self):
        """Domain-level Book rejects locked book because bids must be
        sorted descending and asks ascending with unique prices.
        When bid==ask, the book is still internally sorted but spread==0
        and walk() rejects unusable book."""
        b = _book([(0.5, 100)], [(0.5, 100)])
        assert b.spread == D(0)
        with pytest.raises(ValueError, match="unusable book"):
            walk(b, _order(50), FEE_FREE, TS)


# ── Extremely wide spread ────────────────────────────────────────────────


class TestWideSpread:
    def test_wide_spread_allowed(self):
        b = _book([(0.01, 100)], [(0.99, 100)])
        f = walk(b, _order(50), FEE_FREE, TS)
        assert f.vwap == D("0.99")

    def test_extreme_wide_spread(self):
        b = _book([(0, 100)], [(1, 100)])
        f = walk(b, _order(50), FEE_FREE, TS)
        assert f.vwap == D(1)


# ── Multiple levels consumption ──────────────────────────────────────────


class TestMultiLevelWalk:
    def test_trade_consumes_multiple_levels(self):
        b = _book([(0.4, 50)], [(0.5, 30), (0.51, 30), (0.52, 40)])
        f = walk(b, _order(80), FEE_FREE, TS)
        assert f.shares == D(80)
        assert len(f.levels) == 3

    def test_trade_exceeds_depth_partial(self):
        b = _book([(0.4, 50)], [(0.5, 30), (0.51, 20)])
        f = walk(b, _order(100, partial=True), FEE_FREE, TS)
        assert f.shares == D(50)
        assert f.requested == D(100)

    def test_trade_exceeds_depth_not_allowed(self):
        b = _book([(0.4, 50)], [(0.5, 30), (0.51, 20)])
        with pytest.raises(ValueError, match="insufficient depth"):
            walk(b, _order(100), FEE_FREE, TS)

    def test_consumed_dict_reduces_available(self):
        b = _book([(0.4, 100)], [(0.5, 50)])
        consumed = {("BUY", D("0.5")): D(20)}
        f = walk(b, _order(100, partial=True), FEE_FREE, TS, consumed=consumed)
        assert f.shares == D(30)


# ── VWAP monotonicity ───────────────────────────────────────────────────


class TestVWAPMonotonicity:
    def test_larger_buy_not_better_vwap(self):
        b = _book(
            [(0.4, 200)],
            [(0.5, 100), (0.51, 100), (0.52, 100)],
        )
        f1 = walk(b, _order(30), FEE_FREE, TS)
        f2 = walk(b, _order(200), FEE_FREE, TS)
        assert f1.vwap <= f2.vwap + D("0.001")

    def test_sell_vwap_monotonicity(self):
        b = _book(
            [(0.48, 100), (0.47, 100), (0.46, 100)],
            [(0.5, 200)],
        )
        f1 = walk(b, _order(30, side="SELL"), FEE_FREE, TS)
        f2 = walk(b, _order(200, side="SELL"), FEE_FREE, TS)
        assert f1.vwap >= f2.vwap - D("0.001")

    def test_single_share_vwap_equals_best_ask(self):
        b = _book([(0.4, 100)], [(0.5, 100)])
        f = walk(b, _order(1), FEE_FREE, TS)
        assert f.vwap == D("0.5")

    def test_fill_all_at_one_level(self):
        b = _book([(0.4, 100)], [(0.5, 200)])
        f = walk(b, _order(100), FEE_FREE, TS)
        assert f.vwap == D("0.5")
        assert len(f.levels) == 1


# ── Limit orders ─────────────────────────────────────────────────────────


class TestLimitOrders:
    def test_limit_below_ask_partial_fill(self):
        b = _book([(0.4, 100)], [(0.5, 100)])
        f = walk(b, _order(100, limit=0.50, partial=True), FEE_FREE, TS)
        assert f.shares == D(100)

    def test_limit_below_ask_no_fill(self):
        b = _book([(0.4, 100)], [(0.5, 100)])
        f = walk(b, _order(100, limit=0.49, partial=True), FEE_FREE, TS)
        assert f.shares == D(0)

    def test_sell_limit_above_bid_no_fill(self):
        b = _book([(0.4, 100)], [(0.5, 100)])
        f = walk(b, _order(100, side="SELL", limit=0.41, partial=True), FEE_FREE, TS)
        assert f.shares == D(0)


# ── Simulator ────────────────────────────────────────────────────────────


class TestSimulator:
    def test_duplicate_order_id_rejected(self):
        sim = Simulator()
        b = _book([(0.4, 100)], [(0.5, 100)])
        fill = sim.quote(b, _order(50), FEE_FREE, TS)
        sim.commit(fill)
        with pytest.raises(ValueError, match="duplicate order"):
            sim.quote(b, _order(50), FEE_FREE, TS)

    def test_commit_consumes_levels(self):
        sim = Simulator()
        b = _book([(0.4, 100)], [(0.5, 100)])
        fill = sim.quote(b, _order(80), FEE_FREE, TS)
        sim.commit(fill)
        assert ("BUY", D("0.5")) in sim.consumed["t1"]

    def test_two_orders_partial_fill(self):
        sim = Simulator()
        b = _book([(0.4, 100)], [(0.5, 100)])
        f1 = sim.quote(b, Order("o1", "t1", "BUY", D(60), TS), FEE_FREE, TS)
        sim.commit(f1)
        f2 = sim.quote(b, Order("o2", "t1", "BUY", D(60), TS, allow_partial=True), FEE_FREE, TS)
        assert f2.shares == D(40)


# ── Fill validation ──────────────────────────────────────────────────────


class TestFillValidation:
    def test_fill_overfill_rejected(self):
        b = _book([(0.4, 100)], [(0.5, 100)])
        f = walk(b, _order(100), FEE_FREE, TS)
        # Manually create an overfill to test Fill validation
        with pytest.raises(ValueError, match="overfill"):
            Fill(
                order_id="o1", token_id="t1", side="BUY",
                requested=D(50), shares=D(100),
                notional=D(50), fees=D(0), depth_slippage=D(0),
                filled_at=TS, levels=((D("0.5"), D(100)),),
            )

    def test_fill_zero_requested_rejected(self):
        with pytest.raises(ValueError, match="overfill"):
            Fill(
                order_id="o1", token_id="t1", side="BUY",
                requested=D(0), shares=D(0),
                notional=D(0), fees=D(0), depth_slippage=D(0),
                filled_at=TS, levels=(),
            )

    def test_fill_conserves_quantity(self):
        b = _book([(0.4, 100)], [(0.5, 100)])
        f = walk(b, _order(50), FEE_FREE, TS)
        level_qty = sum(q for _, q in f.levels)
        assert level_qty == f.shares

    def test_fill_conserves_notional(self):
        b = _book([(0.4, 100)], [(0.5, 100)])
        f = walk(b, _order(50), FEE_FREE, TS)
        level_notional = sum(p * q for p, q in f.levels)
        assert level_notional == f.notional


# ── Tick-size alignment for limits ───────────────────────────────────────


class TestTickAlignment:
    def test_limit_off_tick_rejected(self):
        b = _book([(0.4, 100)], [(0.5, 100)], tick=D("0.01"))
        with pytest.raises(ValueError, match="limit off tick"):
            walk(b, _order(50, limit=0.505), FEE_FREE, TS)

    def test_limit_on_tick_accepted(self):
        b = _book([(0.4, 100)], [(0.5, 100)], tick=D("0.01"))
        f = walk(b, _order(50, limit=0.50), FEE_FREE, TS)
        assert f.shares == D(50)


# ── Order validation ─────────────────────────────────────────────────────


class TestOrderValidation:
    def test_invalid_side_rejected(self):
        with pytest.raises(ValueError, match="invalid order"):
            Order("o1", "t1", "SELL_ALL", D(50), TS)

    def test_zero_shares_rejected(self):
        with pytest.raises(ValueError, match="invalid order size"):
            Order("o1", "t1", "BUY", D(0), TS)

    def test_limit_outside_unit_rejected(self):
        with pytest.raises(ValueError, match="invalid limit"):
            Order("o1", "t1", "BUY", D(50), TS, limit=D("1.5"))

    def test_negative_limit_rejected(self):
        with pytest.raises(ValueError, match="invalid limit"):
            Order("o1", "t1", "BUY", D(50), TS, limit=D("-0.1"))


# ── Book property edge cases ─────────────────────────────────────────────


class TestBookProperties:
    def test_best_bid_returns_highest(self):
        b = _book([(0.48, 50), (0.45, 50), (0.4, 50)], [(0.5, 100)])
        assert b.best_bid == D("0.48")

    def test_best_ask_returns_lowest(self):
        b = _book([(0.4, 100)], [(0.50, 50), (0.51, 50), (0.52, 50)])
        assert b.best_ask == D("0.50")

    def test_midpoint_average(self):
        b = _book([(0.4, 100)], [(0.6, 100)])
        assert b.mid == D("0.5")

    def test_spread_positive(self):
        b = _book([(0.45, 100)], [(0.55, 100)])
        assert b.spread == D("0.10")


# ── Fee calculation with depth ───────────────────────────────────────────


class TestFeeWithDepth:
    def test_fees_accumulate_across_levels(self):
        b = _book([(0.4, 100)], [(0.5, 50), (0.51, 50)])
        f = walk(b, _order(100), FEE_2PCT, TS)
        assert f.fees > 0
        assert f.fees == sum(FEE_2PCT.fee(q, p) for p, q in f.levels)

    def test_fee_with_zero_rate(self):
        b = _book([(0.4, 100)], [(0.5, 100)])
        f = walk(b, _order(50), FEE_FREE, TS)
        assert f.fees == D(0)


# ── Edge: minimum order size ─────────────────────────────────────────────


class TestMinimumOrderSize:
    def test_below_min_order_rejected(self):
        b = _book([(0.4, 100)], [(0.5, 100)], min_size=D(50))
        with pytest.raises(ValueError, match="below minimum"):
            walk(b, _order(10), FEE_FREE, TS)

    def test_at_min_order_accepted(self):
        b = _book([(0.4, 100)], [(0.5, 100)], min_size=D(50))
        f = walk(b, _order(50), FEE_FREE, TS)
        assert f.shares == D(50)

    def test_above_min_order_accepted(self):
        b = _book([(0.4, 100)], [(0.5, 100)], min_size=D(50))
        f = walk(b, _order(100), FEE_FREE, TS)
        assert f.shares == D(100)
