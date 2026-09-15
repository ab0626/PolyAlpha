"""Tests for expected value calculation and Kelly sizing."""

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from polyalpha.domain import Book, Level
from polyalpha.execution import FeeSchedule, Order, walk
from polyalpha.expected_value import (
    ExpectedValueResult,
    calculate_fee_per_share,
    calculate_gross_edge,
    calculate_liquidity_penalty,
    calculate_stale_data_penalty,
    calculate_uncertainty_penalty,
    expected_value,
    fractional_kelly,
    kelly_fraction,
)
from polyalpha.forecasting import Forecast
from polyalpha.uncertainty import HistoricalErrorUncertainty

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_book():
    return Book(
        token_id="y",
        condition_id="c",
        source_at=NOW,
        received_at=NOW,
        bids=tuple([Level(D("0.49"), D("100"))]),
        asks=tuple([Level(D("0.51"), D("150")), Level(D("0.52"), D("300"))]),
        tick_size=D("0.01"),
        min_order_size=D(1),
        source_hash="test",
    )


class ExpectedValueTests(unittest.TestCase):
    def test_gross_edge_yes(self):
        edge = calculate_gross_edge(D("0.64"), D("0.57"), "YES")
        self.assertEqual(edge, D("0.07"))

    def test_gross_edge_no(self):
        edge = calculate_gross_edge(D("0.64"), D("0.40"), "NO")
        # (1 - 0.64) - 0.40 = 0.36 - 0.40 = -0.04
        self.assertEqual(edge, D("-0.04"))

    def test_fee_per_share(self):
        class FakeFill:
            shares = D(100)
            fees = D("1.50")

        fps = calculate_fee_per_share(FakeFill())
        self.assertEqual(fps, D("0.015"))

    def test_fee_per_share_zero_shares(self):
        class FakeFill:
            shares = D(0)
            fees = D(0)

        fps = calculate_fee_per_share(FakeFill())
        self.assertEqual(fps, D(0))

    def test_uncertainty_penalty(self):
        est = HistoricalErrorUncertainty(default_buffer=D("0.05"))
        unc = est.estimate(D("0.60"), {})
        penalty = calculate_uncertainty_penalty(unc, confidence=D("1.0"))
        self.assertGreater(penalty, D(0))

    def test_liquidity_penalty(self):
        penalty = calculate_liquidity_penalty(D(1000), D(100))
        self.assertGreater(penalty, D(0))
        # Low depth should have higher penalty
        penalty_low = calculate_liquidity_penalty(D(10), D(100))
        self.assertGreater(penalty_low, penalty)

    def test_stale_data_penalty(self):
        penalty_zero = calculate_stale_data_penalty(0)
        penalty_old = calculate_stale_data_penalty(60)
        self.assertEqual(penalty_zero, D(0))
        self.assertGreater(penalty_old, D(0))

    def test_full_expected_value(self):
        book = make_book()
        fees = FeeSchedule(D(0), NOW, "test-zero")
        fill = walk(book, Order("1", "y", "BUY", D(300), NOW), fees, NOW)
        forecast = Forecast("m", NOW, D("0.64"), D("0.59"), D("0.69"), "test")
        est = HistoricalErrorUncertainty(default_buffer=D("0.025"))
        uncertainty = est.estimate(D("0.64"), {})

        result = expected_value(
            forecast,
            fill,
            uncertainty,
            side="YES",
            resolution_penalty=D("0.01"),
        )
        self.assertIsInstance(result, ExpectedValueResult)
        self.assertEqual(result.side, "YES")
        self.assertGreater(result.gross_edge, 0)
        # Net edge should be less than gross due to penalties
        self.assertLess(result.net_edge, result.gross_edge)

    def test_expected_value_rejects_sell(self):
        with self.assertRaises(ValueError):

            class FakeFill:
                shares = D(100)
                fees = D(0)
                side = "SELL"
                vwap = D("0.50")
                depth_slippage = D(0)

            expected_value(
                Forecast("m", NOW, D("0.6"), D("0.5"), D("0.7"), "t"),
                FakeFill(),
                HistoricalErrorUncertainty().estimate(D("0.6"), {}),
            )


class KellyTests(unittest.TestCase):
    def test_kelly_fraction_positive_edge(self):
        # p=0.6, price=0.5: edge = 0.6 - 0.5 = 0.1
        k = kelly_fraction(D("0.6"), D("0.5"))
        self.assertGreater(k, D(0))
        self.assertLessEqual(k, D(1))

    def test_kelly_fraction_no_edge(self):
        k = kelly_fraction(D("0.5"), D("0.5"))
        self.assertEqual(k, D(0))

    def test_kelly_fraction_negative_edge(self):
        k = kelly_fraction(D("0.4"), D("0.5"))
        self.assertEqual(k, D(0))

    def test_kelly_boundary_prices(self):
        self.assertEqual(kelly_fraction(D("0.5"), D("0")), D(0))
        self.assertEqual(kelly_fraction(D("0.5"), D("1")), D(0))

    def test_fractional_kelly(self):
        full = kelly_fraction(D("0.7"), D("0.5"))
        frac = fractional_kelly(D("0.7"), D("0.5"), D("0.25"))
        self.assertEqual(frac, full * D("0.25"))

    def test_kelly_high_probability(self):
        # Very high probability should give large Kelly fraction
        k = kelly_fraction(D("0.95"), D("0.50"))
        self.assertGreater(k, D("0.5"))


if __name__ == "__main__":
    unittest.main()
