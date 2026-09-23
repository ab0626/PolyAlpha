"""Property-based tests for critical numerical functions.

Tests mathematical properties of fee calculation, VWAP, position sizing,
and calibration metrics across the polyalpha codebase.
"""

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from polyalpha.calibration import metrics
from polyalpha.domain import Book, Level
from polyalpha.execution import FeeSchedule, Fill, Order, Simulator
from polyalpha.expected_value import (
    calculate_fee_per_share,
    calculate_gross_edge,
    calculate_stale_data_penalty,
    fractional_kelly,
    kelly_fraction,
)
from polyalpha.performance import brier_score, drawdown_series
from polyalpha.sizing import fixed_fractional_sizing, kelly_sizing

D = Decimal

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_fee_schedule(rate="0.04"):
    return FeeSchedule(rate=D(rate), known_at=NOW, version="test")


def _make_book(bid_price="0.49", bid_size="100", ask_price="0.51", ask_size="1000"):
    return Book(
        token_id="y",
        condition_id="c",
        source_at=NOW,
        received_at=NOW,
        bids=(Level(D(bid_price), D(bid_size)),),
        asks=(Level(D(ask_price), D(ask_size)),),
        tick_size=D("0.01"),
        min_order_size=D(1),
        source_hash="test",
    )


def _make_fill(shares="100", price="0.50", fees="1.00"):
    sh = D(shares)
    px = D(price)
    return Fill(
        order_id="test",
        token_id="y",
        side="BUY",
        requested=sh,
        shares=sh,
        notional=sh * px,
        fees=D(fees),
        depth_slippage=D("0.01"),
        levels=((px, sh),),
        filled_at=NOW,
    )


class FeeCalculationTests(unittest.TestCase):
    def test_fee_formula_known_values(self):
        fs = _make_fee_schedule()
        fee = fs.fee(D(100), D("0.50"))
        self.assertEqual(fee, D("1.00"))

    def test_fee_zero_at_extremes(self):
        fs = _make_fee_schedule()
        fee_0 = fs.fee(D(100), D("0.01"))
        fee_1 = fs.fee(D(100), D("0.99"))
        fee_mid = fs.fee(D(100), D("0.50"))
        self.assertLess(fee_0, fee_mid)
        self.assertLess(fee_1, fee_mid)

    def test_fee_scales_linearly_with_shares(self):
        fs = _make_fee_schedule()
        fee_100 = fs.fee(D(100), D("0.50"))
        fee_200 = fs.fee(D(200), D("0.50"))
        self.assertEqual(fee_200, fee_100 * 2)

    def test_fee_symmetric_at_midpoint(self):
        fs = _make_fee_schedule()
        fee_30 = fs.fee(D(100), D("0.30"))
        fee_70 = fs.fee(D(100), D("0.70"))
        self.assertEqual(fee_30, fee_70)

    def test_fee_per_share_calculation(self):
        fill = _make_fill("100", "0.50", "1.00")
        fps = calculate_fee_per_share(fill)
        self.assertEqual(fps, D("0.01"))

    def test_fee_zero_shares(self):
        fill = _make_fill("100", "0.50", "0")
        fps = calculate_fee_per_share(fill)
        self.assertEqual(fps, D(0))


class VWAPTests(unittest.TestCase):
    def test_vwap_single_level(self):
        from datetime import UTC, datetime

        now = datetime(2026, 1, 1, tzinfo=UTC)
        book = _make_book(ask_size="1000")
        sim = Simulator()
        fee = _make_fee_schedule()
        order = Order("test", "y", "BUY", D(100), now)
        fill = sim.quote(book, order, fee, now)
        self.assertEqual(fill.vwap, D("0.51"))

    def test_vwap_multiple_levels(self):
        from datetime import UTC, datetime

        now = datetime(2026, 1, 1, tzinfo=UTC)
        book = Book(
            token_id="y",
            condition_id="c",
            source_at=now,
            received_at=now,
            bids=(Level(D("0.49"), D(100)),),
            asks=(
                Level(D("0.51"), D(100)),
                Level(D("0.52"), D(300)),
                Level(D("0.54"), D(2000)),
            ),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        sim = Simulator()
        fee = _make_fee_schedule()
        order = Order("test", "y", "BUY", D(1000), now)
        fill = sim.quote(book, order, fee, now)
        self.assertEqual(fill.vwap, D("0.531"))

    def test_vwap_partial_fill(self):
        from datetime import UTC, datetime

        now = datetime(2026, 1, 1, tzinfo=UTC)
        book = _make_book(ask_size="100")
        sim = Simulator()
        fee = _make_fee_schedule()
        order = Order("test", "y", "BUY", D(1000), now, allow_partial=True)
        fill = sim.quote(book, order, fee, now)
        self.assertEqual(fill.shares, D(100))
        self.assertLess(fill.shares, fill.requested)


class KellyFractionTests(unittest.TestCase):
    def test_kelly_positive_edge(self):
        k = kelly_fraction(D("0.60"), D("0.50"))
        self.assertGreater(k, 0)
        self.assertLessEqual(k, 1)

    def test_kelly_no_edge(self):
        k = kelly_fraction(D("0.50"), D("0.50"))
        self.assertEqual(k, 0)

    def test_kelly_negative_edge(self):
        k = kelly_fraction(D("0.40"), D("0.50"))
        self.assertEqual(k, 0)

    def test_kelly_boundary_prices(self):
        k = kelly_fraction(D("0.99"), D("0.01"))
        self.assertGreater(k, 0)
        self.assertLessEqual(k, 1)

    def test_fractional_kelly(self):
        fk = fractional_kelly(D("0.60"), D("0.50"), D("0.25"))
        full = kelly_fraction(D("0.60"), D("0.50"))
        self.assertEqual(fk, full * D("0.25"))

    def test_kelly_sizing_respects_bankroll(self):
        result = kelly_sizing(D("0.60"), D("0.50"), D("10000"))
        self.assertGreater(result.shares, 0)
        self.assertLessEqual(result.notional, D("10000"))


class DrawdownTests(unittest.TestCase):
    def test_drawdown_never_negative(self):
        dd = drawdown_series([100, 110, 105, 120, 90, 95])
        for d in dd:
            self.assertGreaterEqual(d, 0)

    def test_drawdown_bounded_by_one(self):
        dd = drawdown_series([100, 50, 25, 10, 5])
        for d in dd:
            self.assertLessEqual(d, 1)

    def test_drawdown_starts_at_zero(self):
        dd = drawdown_series([100, 90, 80, 70])
        self.assertEqual(dd[0], 0)

    def test_drawdown_monotonically_non_decreasing_from_peak(self):
        dd = drawdown_series([100, 110, 105, 100, 95])
        self.assertGreater(dd[3], dd[2])

    def test_drawdown_zero_when_always_new_high(self):
        dd = drawdown_series([100, 110, 120, 130, 140])
        for d in dd:
            self.assertEqual(d, 0)


class CalibrationPropertyTests(unittest.TestCase):
    def test_brier_perfect_calibration(self):
        predictions = [1.0, 0.0, 1.0, 0.0, 1.0]
        outcomes = [1, 0, 1, 0, 1]
        score = brier_score(predictions, outcomes)
        self.assertAlmostEqual(score, 0.0, places=6)

    def test_brier_worst_calibration(self):
        predictions = [1.0, 1.0, 1.0]
        outcomes = [0, 0, 0]
        score = brier_score(predictions, outcomes)
        self.assertAlmostEqual(score, 1.0, places=6)

    def test_brier_bounded(self):
        import random

        random.seed(42)
        for _ in range(10):
            n = random.randint(5, 20)
            preds = [random.random() for _ in range(n)]
            outcomes = [random.randint(0, 1) for _ in range(n)]
            score = brier_score(preds, outcomes)
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 1)

    def test_ece_perfect_calibration(self):
        result = metrics(
            [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
            [0, 0, 0, 0, 1, 1, 1, 1, 1],
        )
        self.assertLess(result["ece"], 0.3)

    def test_stale_penalty_increases_with_age(self):
        p0 = calculate_stale_data_penalty(0)
        p10 = calculate_stale_data_penalty(10)
        p30 = calculate_stale_data_penalty(30)
        self.assertLess(p0, p10)
        self.assertLess(p10, p30)

    def test_stale_penalty_bounded(self):
        penalty = calculate_stale_data_penalty(1000)
        self.assertGreaterEqual(penalty, 0)
        self.assertLessEqual(penalty, D("0.01") * 2)


class GrossEdgeTests(unittest.TestCase):
    def test_gross_edge_yes(self):
        edge = calculate_gross_edge(D("0.64"), D("0.57"), "YES")
        self.assertEqual(edge, D("0.07"))

    def test_gross_edge_no(self):
        edge = calculate_gross_edge(D("0.64"), D("0.30"), "NO")
        self.assertEqual(edge, D("0.06"))

    def test_gross_edge_negative(self):
        edge = calculate_gross_edge(D("0.50"), D("0.60"), "YES")
        self.assertEqual(edge, D("-0.10"))

    def test_gross_edge_invalid_side(self):
        with self.assertRaises(ValueError):
            calculate_gross_edge(D("0.50"), D("0.50"), "INVALID")


class FixedFractionalTests(unittest.TestCase):
    def test_basic_sizing(self):
        result = fixed_fractional_sizing(D("10000"), D("0.005"))
        self.assertEqual(result, D("50"))

    def test_max_shares_cap(self):
        result = fixed_fractional_sizing(D("1000000"), D("0.005"), max_notional=D("100"))
        self.assertEqual(result, D("100"))

    def test_invalid_equity(self):
        with self.assertRaises(ValueError):
            fixed_fractional_sizing(D("0"), D("0.005"))

    def test_invalid_fraction(self):
        with self.assertRaises(ValueError):
            fixed_fractional_sizing(D("10000"), D("0"))


class SizingResultTests(unittest.TestCase):
    def test_kelly_result_structure(self):
        result = kelly_sizing(D("0.60"), D("0.50"), D("10000"))
        self.assertIsNotNone(result.kelly_full)
        self.assertIsNotNone(result.kelly_fractional)
        self.assertGreater(result.kelly_full, 0)

    def test_kelly_sizing_no_edge(self):
        result = kelly_sizing(D("0.50"), D("0.50"), D("10000"))
        self.assertEqual(result.shares, D(0))


if __name__ == "__main__":
    unittest.main()
