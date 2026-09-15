import hashlib
import random
import unittest
from datetime import timedelta
from decimal import Decimal as D

from test_foundation import NOW, book, market
from test_integration import ChangeModel

from polyalpha.backtest import Engine
from polyalpha.execution import FeeSchedule, Order, walk
from polyalpha.monitoring import code_provenance
from polyalpha.parsing import parse_book
from polyalpha.storage import Store


class RegressionTests(unittest.TestCase):
    def test_random_depth_conserves_notional_and_respects_limit(self):
        rng = random.Random(417)
        for _ in range(100):
            raw = book()
            raw["bids"] = [{"price": ".01", "size": "100"}]
            prices = sorted(rng.sample(range(5, 96), 5))
            sizes = [rng.randint(1, 200) for _ in prices]
            raw["asks"] = [
                {"price": str(D(p) / 100), "size": str(q)} for p, q in zip(prices, sizes)
            ]
            parsed = parse_book(raw, NOW)
            requested = D(rng.randint(1, sum(sizes) + 100))
            limit = D(prices[2]) / 100
            fill = walk(
                parsed,
                Order("test", "y", "BUY", requested, NOW, limit=limit, allow_partial=True),
                FeeSchedule(D(".04"), NOW, "test"),
                NOW,
            )
            self.assertLessEqual(fill.shares, requested)
            self.assertEqual(fill.notional, sum((p * q for p, q in fill.levels), D(0)))
            self.assertTrue(all(p <= limit for p, q in fill.levels))
            self.assertGreaterEqual(fill.depth_slippage, 0)

    def test_deterministic_replay_digest_and_journal(self):
        with Store(":memory:") as store:
            store.append("market", "m", NOW, dict(market(), feesEnabled=False))
            for second in (0, 2):
                at = NOW + timedelta(seconds=second)
                store.append("book", "y", at, book(source=at), at)
            records = list(store.replay(NOW + timedelta(seconds=2)))
        assignments = {"m": {"event": "e", "cluster": "c", "category": "fixture"}}
        first = Engine(assignments, model=ChangeModel()).run(records)
        second = Engine(assignments, model=ChangeModel()).run(records)
        self.assertEqual(first, second)
        self.assertTrue(first["fill_journal"])
        self.assertEqual(first["input_records"], 3)

    def test_unverified_string_cannot_be_a_settlement(self):
        with Store(":memory:") as store:
            store.append(
                "settlement",
                "y",
                NOW,
                dict(
                    token_id="y",
                    payout="1",
                    known_at=NOW.isoformat(),
                    source_url="fixture",
                    verified="false",
                ),
            )
            with self.assertRaises(ValueError):
                Engine({}).run(store.replay(NOW))

    def test_resolution_before_metadata_blocks_later_entry(self):
        with Store(":memory:") as store:
            store.append(
                "settlement",
                "y",
                NOW,
                dict(
                    token_id="y",
                    payout="1",
                    known_at=NOW.isoformat(),
                    source_url="fixture",
                    verified=True,
                ),
            )
            store.append("market", "m", NOW, dict(market(), feesEnabled=False))
            store.append("book", "y", NOW, book(), NOW)
            report = Engine(
                {"m": {"event": "e", "cluster": "c", "category": "fixture"}}, model=ChangeModel()
            ).run(store.replay(NOW))
            self.assertIn("resolved_market", report["decisions"][0]["rejections"])

    def test_incomplete_valuation_does_not_report_definitive_pnl(self):
        with Store(":memory:") as store:
            store.append("market", "m", NOW, dict(market(), feesEnabled=False))
            for second in (0, 2):
                at = NOW + timedelta(seconds=second)
                store.append("book", "y", at, book(source=at), at)
            store.append("stream_gap", "market", NOW + timedelta(seconds=3), {})
            report = Engine(
                {"m": {"event": "e", "cluster": "c", "category": "fixture"}}, model=ChangeModel()
            ).run(store.replay(NOW + timedelta(seconds=3)))
            self.assertFalse(report["valuation_complete"])
            self.assertIsNone(report["net_pnl"])
            self.assertFalse(report["risk_state"]["halted"])

    def test_source_hash_is_nonempty(self):
        self.assertNotEqual(code_provenance()["source_sha256"], hashlib.sha256().hexdigest())

    def test_liquidation_cannot_reuse_already_sold_depth(self):
        from polyalpha.execution import Simulator
        from polyalpha.portfolio import Portfolio

        parsed = parse_book(book(), NOW)
        schedule = FeeSchedule(D(".04"), NOW, "fixture")
        simulator = Simulator()
        portfolio = Portfolio()
        buy = simulator.quote(parsed, Order("buy", "y", "BUY", D(200), NOW), schedule, NOW)
        portfolio.apply(buy, "m", "e", "c")
        simulator.commit(buy)
        sell = simulator.quote(parsed, Order("sell", "y", "SELL", D(100), NOW), schedule, NOW)
        portfolio.apply(sell, "m", "e", "c")
        simulator.commit(sell)
        marked = portfolio.mark({"y": parsed}, {"y": schedule}, NOW, simulator.consumed)
        self.assertEqual(marked["unliquidated"], {"y": "100"})
        self.assertEqual(marked["liquidation_equity"], portfolio.cash)
