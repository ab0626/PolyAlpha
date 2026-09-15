import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from test_foundation import NOW, market

from polyalpha.activity import collect_resolution, collect_trades
from polyalpha.parsing import parse_market
from polyalpha.resolution import ResolutionReview, definition_hash
from polyalpha.storage import Store


class ActivityTests(unittest.TestCase):
    def test_trade_envelope_cursor(self):
        trade = dict(
            token_id="y",
            condition_id="c",
            side="BUY",
            size=10,
            price=0.5,
            timestamp=int(NOW.timestamp()),
            transaction_hash="tx",
        )

        class Fake:
            def get(self, url, params):
                return {
                    "data": [trade],
                    "pagination": {"has_more": False, "next_cursor": None},
                }, NOW

        with Store(":memory:") as store:
            result = collect_trades(Fake(), store, "c")
            self.assertEqual(result, {"rows": 1, "truncated": False})
            self.assertEqual(store.latest("trade", "y", NOW).source_at, NOW)

    def test_terminal_payouts_map_original_outcome_order(self):
        terminal = dict(
            condition_id="c",
            status="resolved",
            extended_review=False,
            market_type="BINARY",
            resolution_source="reported",
            payouts=[0, 1000000],
            transaction_hash="resolution-tx",
            resolved_at=NOW.isoformat(),
        )

        class Fake:
            def get(self, url, params):
                return {"data": [terminal]}, NOW

        with Store(":memory:") as store:
            self.assertEqual(collect_resolution(Fake(), store, market()), 2)
            self.assertEqual(store.latest("settlement", "y", NOW).payload["payout"], "1")
            self.assertEqual(store.latest("settlement", "n", NOW).payload["payout"], "0")
            self.assertEqual(collect_resolution(Fake(), store, market()), 0)

    def test_proposed_resolution_never_settles(self):
        class Fake:
            def get(self, url, params):
                return {"data": [dict(condition_id="c", status="proposed")]}, NOW

        with Store(":memory:") as store:
            self.assertEqual(collect_resolution(Fake(), store, market()), 0)
            self.assertFalse(list(store.replay(NOW, "settlement")))

    def test_resolution_definition_change_invalidates_review(self):
        m = parse_market(market(), NOW)
        review = ResolutionReview(
            definition_hash(m), NOW, "manual review", D(".9"), D(".1"), D(".1")
        )
        self.assertEqual(review.penalty(m, NOW), D(".02"))
        with self.assertRaises(ValueError):
            review.penalty(replace(m, description="new wording"), NOW)
        with self.assertRaises(ValueError):
            review.penalty(m, NOW - timedelta(seconds=1))
