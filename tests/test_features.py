"""Tests for order-book feature extraction."""

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from polyalpha.domain import Book, Level
from polyalpha.features.orderbook import (
    BookFeatureState,
    ask_depth,
    best_ask,
    best_bid,
    bid_depth,
    depth_imbalance,
    extract_all_features,
    liquidity_concentration,
    microprice,
    midprice,
    momentum,
    orderbook_imbalance,
    price_distance_from_extremes,
    realized_volatility,
    relative_spread,
    spread,
    spread_bps,
)

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_book(
    bids=None,
    asks=None,
    token="y",
    source=NOW,
):
    if bids is None:
        bids = [("0.49", "100"), ("0.48", "200")]
    if asks is None:
        asks = [("0.51", "150"), ("0.52", "300")]
    return Book(
        token_id=token,
        condition_id="c",
        source_at=source,
        received_at=source,
        bids=tuple(Level(D(p), D(s)) for p, s in bids),
        asks=tuple(Level(D(p), D(s)) for p, s in asks),
        tick_size=D("0.01"),
        min_order_size=D(1),
        source_hash="test",
    )


class OrderBookFeatureTests(unittest.TestCase):
    def test_basic_price_features(self):
        book = make_book()
        self.assertEqual(best_bid(book), D("0.49"))
        self.assertEqual(best_ask(book), D("0.51"))
        self.assertEqual(midprice(book), D("0.50"))
        self.assertEqual(spread(book), D("0.02"))
        self.assertAlmostEqual(float(relative_spread(book)), 0.04, places=4)

    def test_depth_features(self):
        book = make_book()
        self.assertEqual(bid_depth(book, 2), D("300"))
        self.assertEqual(ask_depth(book, 2), D("450"))
        imb = orderbook_imbalance(book, 2)
        self.assertIsNotNone(imb)
        # (300 - 450) / (300 + 450) = -150/750 = -0.2
        self.assertAlmostEqual(float(imb), -0.2, places=4)

    def test_microprice(self):
        book = make_book()
        # microprice = (0.51 * 100 + 0.49 * 150) / (100 + 150)
        # = (51 + 73.5) / 250 = 124.5 / 250 = 0.498
        mp = microprice(book)
        self.assertIsNotNone(mp)
        self.assertAlmostEqual(float(mp), 0.498, places=4)

    def test_spread_bps(self):
        book = make_book()
        bps = spread_bps(book)
        self.assertIsNotNone(bps)
        # 0.02 / 0.50 * 10000 = 400 bps
        self.assertAlmostEqual(float(bps), 400, places=1)

    def test_book_slope(self):
        from polyalpha.features.orderbook import book_slope

        book = make_book()
        slopes = book_slope(book, 2)
        self.assertIn("ask_slope", slopes)
        self.assertIn("bid_slope", slopes)
        # ask_slope = (0.52 - 0.51) / 450 = 0.01 / 450
        self.assertAlmostEqual(float(slopes["ask_slope"]), 0.01 / 450, places=8)

    def test_liquidity_concentration(self):

        book = make_book()
        conc = liquidity_concentration(book, 2)
        self.assertIn("bid_concentration", conc)
        self.assertIn("ask_concentration", conc)
        # Concentration should be between 0 and 1
        self.assertGreater(conc["bid_concentration"], 0)
        self.assertLessEqual(conc["bid_concentration"], 1)

    def test_momentum(self):
        book = make_book()
        state = BookFeatureState()
        state.midpoints.append(D("0.48"))
        m = momentum(book, state)
        self.assertEqual(m, D("0.02"))  # 0.50 - 0.48

    def test_realized_volatility(self):
        state = BookFeatureState()
        for price in ["0.50", "0.51", "0.49", "0.52", "0.48"]:
            state.midpoints.append(D(price))
        rv = realized_volatility(state)
        self.assertIsNotNone(rv)
        self.assertGreater(rv, 0)

    def test_realized_volatility_insufficient_data(self):
        state = BookFeatureState()
        state.midpoints.append(D("0.50"))
        self.assertIsNone(realized_volatility(state))

    def test_price_distance_from_extremes(self):
        book = make_book()  # mid = 0.50
        state = BookFeatureState()
        state.midpoints.append(D("0.45"))
        state.midpoints.append(D("0.55"))
        dist = price_distance_from_extremes(book, state)
        # range = 0.55 - 0.45 = 0.10
        # dist_from_high = (0.55 - 0.50) / 0.10 = 0.5
        # dist_from_low = (0.50 - 0.45) / 0.10 = 0.5
        self.assertAlmostEqual(float(dist["dist_from_high"]), 0.5, places=4)
        self.assertAlmostEqual(float(dist["dist_from_low"]), 0.5, places=4)

    def test_extract_all_features(self):
        book = make_book()
        features = extract_all_features(book)
        self.assertIn("midpoint", features)
        self.assertIn("spread", features)
        self.assertIn("imbalance", features)
        self.assertIn("microprice", features)
        self.assertIn("momentum", features)

    def test_empty_book_features(self):
        book = make_book(bids=[], asks=[("0.51", "100")])
        self.assertIsNone(best_bid(book))
        self.assertIsNone(spread(book))
        self.assertIsNone(microprice(book))

    def test_depth_imbalance(self):
        book = make_book()
        di = depth_imbalance(book, 10)
        self.assertIsNotNone(di)
        # Same as orderbook_imbalance at level 2
        self.assertEqual(di, orderbook_imbalance(book, 2))


if __name__ == "__main__":
    unittest.main()
