import unittest
from datetime import timedelta
from decimal import Decimal as D

from test_foundation import NOW, book

from polyalpha.execution import FeeSchedule, Order, Simulator, walk
from polyalpha.parsing import parse_book
from polyalpha.portfolio import Portfolio
from polyalpha.risk import Limits, Risk


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.book = parse_book(book(), NOW)
        self.fees = FeeSchedule(D(".04"), NOW, "synthetic-v1")
        self.zero = FeeSchedule(D(0), NOW, "synthetic-zero")

    def test_vwap_and_fee(self):
        f = walk(self.book, Order("1", "y", "BUY", D(1000), NOW), self.zero, NOW)
        self.assertEqual(f.vwap, D(".531"))
        self.assertEqual(f.depth_slippage, D(21))
        self.assertEqual(self.fees.fee(D(100), D(".5")), D(1))

    def test_requested_example(self):
        raw = book()
        raw["asks"] = [
            {"price": p, "size": q} for p, q in [(".56", "50"), (".57", "150"), (".58", "1000")]
        ]
        f = walk(parse_book(raw, NOW), Order("1", "y", "BUY", D(300), NOW), self.zero, NOW)
        self.assertEqual(f.notional, D("171.5"))

    def test_partial_and_limit(self):
        with self.assertRaises(ValueError):
            walk(self.book, Order("1", "y", "BUY", D(3000), NOW), self.zero, NOW)
        f = walk(
            self.book,
            Order("1", "y", "BUY", D(3000), NOW, limit=D(".52"), allow_partial=True),
            self.zero,
            NOW,
        )
        self.assertEqual(f.shares, D(400))

    def test_no_reuse(self):
        sim = Simulator()
        order = Order("1", "y", "BUY", D(100), NOW, limit=D(".51"))
        f = sim.quote(self.book, order, self.zero, NOW)
        sim.commit(f)
        with self.assertRaises(ValueError):
            sim.quote(
                self.book,
                Order("2", "y", "BUY", D(100), NOW, limit=D(".51")),
                self.zero,
                NOW,
            )
        with self.assertRaises(ValueError):
            sim.commit(f)

    def test_timing(self):
        with self.assertRaises(ValueError):
            walk(
                self.book,
                Order("1", "y", "BUY", D(100), NOW + timedelta(seconds=1)),
                self.zero,
                NOW,
            )
        with self.assertRaises(ValueError):
            walk(
                self.book,
                Order("1", "y", "BUY", D(100), NOW),
                self.zero,
                NOW + timedelta(seconds=31),
            )

    def test_round_trip_accounting(self):
        p = Portfolio()
        buy = walk(self.book, Order("b", "y", "BUY", D(100), NOW), self.fees, NOW)
        p.apply(buy, "m", "e", "c")
        self.assertEqual(p.cash, D(10000) - buy.notional - buy.fees)
        sell = walk(self.book, Order("s", "y", "SELL", D(50), NOW), self.fees, NOW)
        p.apply(sell, "m", "e", "c")
        self.assertEqual(p.positions["y"].basis, (buy.notional + buy.fees) / 2)
        self.assertEqual(p.realized, sell.notional - sell.fees - (buy.notional + buy.fees) / 2)
        with self.assertRaises(ValueError):
            p.apply(sell, "m", "e", "c")
        p.settle("resolution", "y", D(1), NOW, NOW)
        self.assertEqual(p.cash - p.initial_cash, p.realized)

    def test_cluster_budget_and_breaker(self):
        p = Portfolio()
        from polyalpha.portfolio import Position

        for i, cost in enumerate([180, 120, 100]):
            p.positions[str(i)] = Position(
                str(i), str(i), "e", "shared", "politics", D(400), D(cost)
            )
        risk = Risk(D(10000), Limits(normal=D(".02")))
        self.assertEqual(risk.budget(p, D(10000), "new", "e", "shared", "politics"), D(100))
        self.assertEqual(risk.budget(p, D(10000), "new", "e", "", "politics"), 0)
        risk.observe(D(10000), NOW)
        risk.observe(D(9700), NOW)
        self.assertTrue(risk.halted)

    def test_missing_liquidation_is_not_midpoint(self):
        p = Portfolio()
        p.apply(
            walk(self.book, Order("b", "y", "BUY", D(200), NOW), self.zero, NOW),
            "m",
            "e",
            "c",
        )
        mark = p.mark({"y": self.book}, {"y": self.zero}, NOW)
        self.assertEqual(mark["unliquidated"]["y"], "100")
        self.assertEqual(mark["liquidation_equity"], p.cash + D(49))
