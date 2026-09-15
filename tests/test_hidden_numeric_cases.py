"""Part 44 — Numeric edge-case hidden tests.

Decimal precision, float/Decimal mixing, boundary prices, quantities,
fee rates, NaN/Inf, negative sizes, malformed strings, rounding boundaries.
"""

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext

import pytest

from polyalpha.domain import Book, Level, number, utc
from polyalpha.execution import FeeSchedule, Fill, Order, walk

D = Decimal
TS = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _book(bid_p, bid_s, ask_p, ask_s, tick=D("0.01"), min_size=D(1)):
    return Book(
        token_id="t1", condition_id="c1",
        source_at=TS, received_at=TS,
        bids=(Level(D(str(bid_p)), D(str(bid_s))),),
        asks=(Level(D(str(ask_p)), D(str(ask_s))),),
        tick_size=D(str(tick)), min_order_size=D(str(min_size)),
        source_hash="",
    )


def _order(shares, limit=None, allow_partial=False):
    return Order("o1", "t1", "BUY", D(str(shares)), TS, limit=D(str(limit)) if limit else None, allow_partial=allow_partial)


def _fees(rate=0):
    return FeeSchedule(D(str(rate)), TS, "test")


# ── Decimal precision ────────────────────────────────────────────────────


class TestDecimalPrecision:
    def test_number_from_string_preserves_precision(self):
        assert number("0.1") == D("0.1")
        assert number("0.10") == D("0.10")
        assert str(number("0.10")) == "0.10"

    def test_number_from_float_introduces_float_artifact(self):
        result = number(0.1)
        assert result == D("0.1")

    def test_number_from_int(self):
        assert number(0) == D(0)
        assert number(1) == D(1)

    def test_number_negative(self):
        assert number(-0.5) == D("-0.5")

    def test_number_large(self):
        assert number("999999999.99999999") == D("999999999.99999999")

    def test_number_small_positive(self):
        assert number("0.00000001") == D("0.00000001")

    def test_number_exactly_zero(self):
        assert number(0) == D(0)

    def test_number_exactly_one(self):
        assert number(1) == D(1)


# ── Float/Decimal mixing ─────────────────────────────────────────────────


class TestFloatDecimalMixing:
    def test_level_with_float_price_coerced(self):
        lvl = Level(D("0.5"), D("100"))
        assert lvl.price == D("0.5")

    def test_book_mid_calculation_decimal(self):
        b = _book(0.4, 100, 0.6, 100)
        assert b.mid == D("0.5")

    def test_book_spread_decimal(self):
        b = _book(0.4, 100, 0.6, 100)
        assert b.spread == D("0.2")

    def test_vwap_is_decimal(self):
        b = _book(0.4, 100, 0.6, 100)
        f = walk(b, _order(50), _fees(), TS)
        assert isinstance(f.vwap, Decimal)

    def test_fee_calculation_decimal_arithmetic(self):
        fee = _fees(0.02)
        result = fee.fee(D("100"), D("0.5"))
        assert isinstance(result, Decimal)
        assert result > 0


# ── Prices exactly 0 and 1 ───────────────────────────────────────────────


class TestBoundaryPrices:
    def test_price_exactly_zero_accepted(self):
        lvl = Level(D(0), D("100"))
        assert lvl.price == D(0)

    def test_price_exactly_one_accepted(self):
        lvl = Level(D(1), D("100"))
        assert lvl.price == D(1)

    def test_price_below_zero_rejected(self):
        with pytest.raises(ValueError, match="price outside"):
            Level(D("-0.01"), D("100"))

    def test_price_above_one_rejected(self):
        with pytest.raises(ValueError, match="price outside"):
            Level(D("1.01"), D("100"))

    def test_bid_at_zero(self):
        b = _book(0, 100, 0.01, 100)
        assert b.best_bid == D(0)
        assert b.best_ask == D("0.01")

    def test_ask_at_one(self):
        b = _book(0.99, 100, 1, 100)
        assert b.best_ask == D(1)


# ── Quantities: zero, tiny, huge ─────────────────────────────────────────


class TestQuantities:
    def test_zero_quantity_rejected(self):
        with pytest.raises(ValueError, match="size must be positive"):
            Level(D("0.5"), D(0))

    def test_negative_quantity_rejected(self):
        with pytest.raises(ValueError, match="size must be positive"):
            Level(D("0.5"), D(-1))

    def test_tiny_quantity_accepted(self):
        lvl = Level(D("0.5"), D("0.000001"))
        assert lvl.size == D("0.000001")

    def test_huge_quantity_accepted(self):
        lvl = Level(D("0.5"), D("99999999999"))
        assert lvl.size == D("99999999999")

    def test_tiny_order_shares(self):
        b = _book(0.4, 100, 0.6, 100, min_size=D("0.001"))
        f = walk(b, _order("0.001"), _fees(), TS)
        assert f.shares == D("0.001")

    def test_zero_order_rejected(self):
        with pytest.raises(ValueError, match="invalid order size"):
            Order("o1", "t1", "BUY", D(0), TS)


# ── Fee rate edge cases ──────────────────────────────────────────────────


class TestFeeRateEdgeCases:
    def test_zero_fee_rate(self):
        fee = _fees(0)
        assert fee.fee(D("100"), D("0.5")) == D(0)

    def test_fee_rate_exactly_one(self):
        fee = _fees(1)
        result = fee.fee(D("1"), D("0.5"))
        assert result == D("0.25")

    def test_fee_rate_negative_rejected(self):
        with pytest.raises(ValueError, match="invalid fee schedule"):
            _fees(-0.01)

    def test_fee_rate_huge(self):
        fee = _fees(100)
        result = fee.fee(D("1"), D("0.5"))
        assert result > 0

    def test_fee_inputs_nan_rejected(self):
        fee = _fees(0.02)
        with pytest.raises(ValueError, match="invalid fee inputs"):
            fee.fee(D("nan"), D("0.5"))

    def test_fee_inputs_inf_rejected(self):
        fee = _fees(0.02)
        with pytest.raises(ValueError, match="invalid fee inputs"):
            fee.fee(D("inf"), D("0.5"))


# ── NaN/Inf handling ─────────────────────────────────────────────────────


class TestNaNInfHandling:
    def test_number_nan_rejected(self):
        with pytest.raises(ValueError, match="non-finite"):
            number("NaN")

    def test_number_inf_rejected(self):
        with pytest.raises(ValueError, match="non-finite"):
            number("Infinity")

    def test_number_neg_inf_rejected(self):
        with pytest.raises(ValueError, match="non-finite"):
            number("-Infinity")

    def test_level_price_nan_rejected(self):
        with pytest.raises(ValueError, match="price outside"):
            Level(D("NaN"), D("100"))

    def test_level_size_nan_rejected(self):
        with pytest.raises(ValueError, match="size must be positive"):
            Level(D("0.5"), D("NaN"))

    def test_order_shares_nan_rejected(self):
        with pytest.raises(ValueError, match="invalid order size"):
            Order("o1", "t1", "BUY", D("NaN"), TS)


# ── Negative sizes and prices ────────────────────────────────────────────


class TestNegativeInputs:
    def test_negative_price_rejected(self):
        with pytest.raises(ValueError):
            Level(D("-0.5"), D("100"))

    def test_negative_tick_rejected(self):
        with pytest.raises(ValueError, match="invalid tick size"):
            Book(
                token_id="t1", condition_id="c1",
                source_at=TS, received_at=TS,
                bids=(Level(D("0.5"), D("100")),),
                asks=(Level(D("0.6"), D("100")),),
                tick_size=D("-0.01"), min_order_size=D(1),
                source_hash="",
            )

    def test_negative_min_order_size_rejected(self):
        with pytest.raises(ValueError, match="invalid minimum"):
            Book(
                token_id="t1", condition_id="c1",
                source_at=TS, received_at=TS,
                bids=(Level(D("0.5"), D("100")),),
                asks=(Level(D("0.6"), D("100")),),
                tick_size=D("0.01"), min_order_size=D(-1),
                source_hash="",
            )


# ── Rounding boundaries ─────────────────────────────────────────────────


class TestRoundingBoundaries:
    def test_fee_rounding_half_up(self):
        fee = _fees(0.01)
        # shares=1, price=0.5 => fee = 1 * 0.01 * 0.5 * 0.5 = 0.0025
        result = fee.fee(D(1), D("0.5"))
        assert result == D("0.0025")

    def test_fee_rounding_at_boundary(self):
        fee = _fees(0.01)
        result = fee.fee(D(100), D("0.5"))
        # 100 * 0.01 * 0.5 * 0.5 = 0.25
        assert result == D("0.25000")

    def test_vwap_no_rounding(self):
        b = _book(0.3, 100, 0.31, 100)
        f = walk(b, _order(50), _fees(), TS)
        assert f.vwap == D("0.31")

    def test_walk_exact_fill(self):
        b = _book(0.4, 100, 0.5, 100)
        f = walk(b, _order(100), _fees(), TS)
        assert f.shares == D(100)
        assert f.notional == D(50)


# ── Malformed Decimal strings ────────────────────────────────────────────


class TestMalformedDecimal:
    def test_empty_string(self):
        with pytest.raises(Exception):
            number("")

    def test_whitespace_only(self):
        with pytest.raises(Exception):
            number("  ")

    def test_garbage_string(self):
        with pytest.raises(Exception):
            number("abc")

    def test_partial_number(self):
        with pytest.raises(Exception):
            number("1.2.3")

    def test_hex_string(self):
        with pytest.raises(Exception):
            number("0x1A")


# ── Tick-size grid alignment ─────────────────────────────────────────────


class TestTickGrid:
    def test_price_off_tick_rejected(self):
        with pytest.raises(ValueError, match="price off tick grid"):
            Book(
                token_id="t1", condition_id="c1",
                source_at=TS, received_at=TS,
                bids=(Level(D("0.505"), D("100")),),
                asks=(Level(D("0.51"), D("100")),),
                tick_size=D("0.01"), min_order_size=D(1),
                source_hash="",
            )

    def test_price_on_tick_accepted(self):
        b = _book(0.50, 100, 0.51, 100, tick=D("0.01"))
        assert b.bids[0].price == D("0.50")

    def test_limit_off_tick_rejected(self):
        b = _book(0.50, 100, 0.51, 100, tick=D("0.01"))
        with pytest.raises(ValueError, match="limit off tick"):
            walk(b, _order(50, limit=0.505), _fees(), TS)


# ── number() edge cases ─────────────────────────────────────────────────


class TestNumberFunction:
    def test_number_from_decimal_passthrough(self):
        d = D("3.14159265358979")
        assert number(d) == d

    def test_number_trailing_zeros(self):
        n = number("1.10000")
        assert n == D("1.1")

    def test_number_leading_zeros(self):
        n = number("00.50")
        assert n == D("0.50")

    def test_number_minus_zero(self):
        n = number("-0")
        assert n == D(0)

    def test_number_very_small_positive(self):
        n = number("0.000000001")
        assert n > 0

    def test_number_very_large(self):
        n = number("1000000000000")
        assert n == D("1000000000000")
