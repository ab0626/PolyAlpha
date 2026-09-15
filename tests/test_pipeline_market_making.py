"""Tests for pipeline and market_making modules."""

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from polyalpha.market_making import (
    MakerFillAnalysis,
    MakerInventory,
    MakerPnLEstimator,
    MakerQueue,
    MakerQuoter,
)
from polyalpha.pipeline import (
    ClaimDeduplicator,
    ClaimExtraction,
    ExtractedEntity,
    InformationPipeline,
    SimpleImpactModel,
    compute_content_hash,
)

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _claim(text="test claim", url="http://example.com", offset_hours=0):
    return ClaimExtraction(
        claim_text=text,
        source_url=url,
        published_at=NOW,
        retrieved_at=NOW,
        entities=(ExtractedEntity(entity_type="person", name="Alice", confidence=D("0.9")),),
        probability_impact=D("0.02"),
        impact_confidence=D("0.7"),
        category="politics",
        content_hash=compute_content_hash(text, url),
    )


class ExtractedEntityTests(unittest.TestCase):
    def test_valid(self):
        e = ExtractedEntity(entity_type="person", name="Alice", confidence=D("0.9"))
        self.assertEqual(e.name, "Alice")

    def test_empty_name(self):
        with self.assertRaises(ValueError):
            ExtractedEntity(entity_type="person", name="", confidence=D("0.9"))

    def test_bad_confidence(self):
        with self.assertRaises(ValueError):
            ExtractedEntity(entity_type="person", name="Alice", confidence=D("1.5"))


class ClaimExtractionTests(unittest.TestCase):
    def test_valid(self):
        c = _claim()
        self.assertEqual(c.claim_text, "test claim")

    def test_empty_text(self):
        with self.assertRaises(ValueError):
            _claim(text="")


class ClaimDeduplicatorTests(unittest.TestCase):
    def test_not_duplicate(self):
        dedup = ClaimDeduplicator(window_seconds=3600)
        c = _claim()
        self.assertFalse(dedup.is_duplicate(c))

    def test_duplicate(self):
        dedup = ClaimDeduplicator(window_seconds=3600)
        c1 = _claim("same claim", "http://same.com")
        c2 = _claim("same claim", "http://same.com")
        self.assertFalse(dedup.is_duplicate(c1))
        self.assertTrue(dedup.is_duplicate(c2))

    def test_different_text(self):
        dedup = ClaimDeduplicator(window_seconds=3600)
        c1 = _claim("claim one", "http://a.com")
        c2 = _claim("claim two", "http://a.com")
        self.assertFalse(dedup.is_duplicate(c1))
        self.assertFalse(dedup.is_duplicate(c2))

    def test_cleanup(self):
        dedup = ClaimDeduplicator(window_seconds=3600)
        c = _claim()
        dedup.is_duplicate(c)
        self.assertEqual(len(dedup.seen), 1)
        dedup.cleanup(NOW)
        self.assertEqual(len(dedup.seen), 1)  # not old enough


class SimpleImpactModelTests(unittest.TestCase):
    def test_returns_default_impact(self):
        model = SimpleImpactModel(default_impact=D("0.05"), max_impact=D("0.10"))
        impact = model.estimate_impact(_claim(), D("0.50"))
        self.assertEqual(impact, D("0.05"))

    def test_clamped_to_max(self):
        model = SimpleImpactModel(default_impact=D("0.20"), max_impact=D("0.10"))
        impact = model.estimate_impact(_claim(), D("0.50"))
        self.assertEqual(impact, D("0.10"))


class InformationPipelineTests(unittest.TestCase):
    def test_process_claim(self):
        pipeline = InformationPipeline()
        result = pipeline.process_claim(_claim(), D("0.50"))
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "politics")

    def test_dedup(self):
        pipeline = InformationPipeline()
        c1 = _claim("claim", "http://a.com")
        c2 = _claim("claim", "http://a.com")
        r1 = pipeline.process_claim(c1, D("0.50"))
        r2 = pipeline.process_claim(c2, D("0.50"))
        self.assertIsNotNone(r1)
        self.assertIsNone(r2)

    def test_probability_clamped(self):
        pipeline = InformationPipeline()
        c = _claim()
        result = pipeline.process_claim(c, D("0.95"))
        # impact is 0.02, so new prob = 0.97, still < 1
        self.assertIsNotNone(result)
        self.assertEqual(D(result["new_probability"]), D("0.97"))

    def test_probability_clamped_at_zero(self):
        pipeline = InformationPipeline()
        c = _claim()
        result = pipeline.process_claim(c, D("0.01"))
        # impact is 0.02, new prob = 0.03, delta = 0.02
        self.assertIsNotNone(result)
        self.assertEqual(D(result["delta"]), D("0.02"))

    def test_process_batch(self):
        pipeline = InformationPipeline()
        c1 = _claim("claim1", "http://a.com")
        c2 = _claim("claim2", "http://b.com")
        results = pipeline.process_batch([c1, c2], D("0.50"))
        self.assertEqual(len(results), 2)

    def test_summary(self):
        pipeline = InformationPipeline()
        pipeline.process_claim(_claim(), D("0.50"))
        summary = pipeline.summary()
        self.assertEqual(summary["claims_processed"], 1)
        self.assertEqual(summary["impacts_computed"], 1)


class ComputeContentHashTests(unittest.TestCase):
    def test_deterministic(self):
        h1 = compute_content_hash("hello", "http://a.com")
        h2 = compute_content_hash("hello", "http://a.com")
        self.assertEqual(h1, h2)

    def test_different_inputs(self):
        h1 = compute_content_hash("hello", "http://a.com")
        h2 = compute_content_hash("world", "http://a.com")
        self.assertNotEqual(h1, h2)


class MakerInventoryTests(unittest.TestCase):
    def test_net_position_long(self):
        inv = MakerInventory(token_id="t1", side="BUY", shares=D(100))
        self.assertEqual(inv.net_position, D(100))

    def test_net_position_short(self):
        inv = MakerInventory(token_id="t1", side="SELL", shares=D(100))
        self.assertEqual(inv.net_position, D(-100))

    def test_net_position_flat(self):
        inv = MakerInventory(token_id="t1", side="NONE")
        self.assertEqual(inv.net_position, D(0))


class MakerQuoterTests(unittest.TestCase):
    def test_quote_valid(self):
        quoter = MakerQuoter()
        inv = MakerInventory(token_id="t1", side="NONE")
        q = quoter.quote(D("0.50"), inv)
        self.assertEqual(q.side, "BUY")
        self.assertGreater(q.price, D(0))
        self.assertLess(q.price, D(1))

    def test_quote_inventory_skew(self):
        quoter = MakerQuoter(inventory_skew_per_share=D("0.01"))
        inv_flat = MakerInventory(token_id="t1", side="NONE")
        inv_long = MakerInventory(token_id="t1", side="BUY", shares=D(10))
        q_flat = quoter.quote(D("0.50"), inv_flat)
        q_long = quoter.quote(D("0.50"), inv_long)
        # Long inventory widens spread via abs(skew), lowering bid price
        self.assertLess(q_long.price, q_flat.price)

    def test_quote_fair_value_out_of_range(self):
        quoter = MakerQuoter()
        inv = MakerInventory(token_id="t1", side="NONE")
        with self.assertRaises(ValueError):
            quoter.quote(D("1.50"), inv)


class MakerFillAnalysisTests(unittest.TestCase):
    def test_adverse_selection_buy(self):
        fill = MakerFillAnalysis(
            token_id="t1",
            side="BUY",
            fill_price=D("0.50"),
            fill_size=D(100),
            pre_fill_mid=D("0.50"),
            post_fill_mid_1s=D("0.49"),
            post_fill_mid_5s=D("0.48"),
            post_fill_mid_60s=D("0.47"),
        )
        self.assertEqual(fill.adverse_selection_1s, D("0.01"))
        self.assertEqual(fill.adverse_selection_5s, D("0.02"))
        self.assertEqual(fill.adverse_selection_60s, D("0.03"))

    def test_adverse_selection_sell(self):
        fill = MakerFillAnalysis(
            token_id="t1",
            side="SELL",
            fill_price=D("0.50"),
            fill_size=D(100),
            pre_fill_mid=D("0.50"),
            post_fill_mid_1s=D("0.51"),
            post_fill_mid_5s=D("0.52"),
            post_fill_mid_60s=D("0.53"),
        )
        self.assertEqual(fill.adverse_selection_1s, D("0.01"))
        self.assertEqual(fill.adverse_selection_5s, D("0.02"))
        self.assertEqual(fill.adverse_selection_60s, D("0.03"))

    def test_adverse_selection_none(self):
        fill = MakerFillAnalysis(
            token_id="t1",
            side="BUY",
            fill_price=D("0.50"),
            fill_size=D(100),
            pre_fill_mid=D("0.50"),
            post_fill_mid_1s=None,
            post_fill_mid_5s=None,
            post_fill_mid_60s=None,
        )
        self.assertIsNone(fill.adverse_selection_1s)
        self.assertIsNone(fill.adverse_selection_5s)
        self.assertIsNone(fill.adverse_selection_60s)


class MakerPnLEstimatorTests(unittest.TestCase):
    def test_estimate_fill_pnl(self):
        estimator = MakerPnLEstimator(rebate_rate=D("0.001"))
        fill = MakerFillAnalysis(
            token_id="t1",
            side="BUY",
            fill_price=D("0.50"),
            fill_size=D(100),
            pre_fill_mid=D("0.50"),
            post_fill_mid_1s=D("0.49"),
            post_fill_mid_5s=D("0.48"),
            post_fill_mid_60s=D("0.47"),
        )
        pnl = estimator.estimate_fill_pnl(fill, spread_at_fill=D("0.02"))
        self.assertIn("net_pnl", pnl)
        self.assertIn("spread_capture", pnl)
        self.assertIn("adverse_selection", pnl)

    def test_aggregate_pnl(self):
        estimator = MakerPnLEstimator()
        fills = [
            {
                "spread_capture": "0.02",
                "adverse_selection": "0.01",
                "rebate": "0.001",
                "net_pnl": "0.011",
            },
            {
                "spread_capture": "0.03",
                "adverse_selection": "0.015",
                "rebate": "0.002",
                "net_pnl": "0.017",
            },
        ]
        agg = estimator.aggregate_pnl(fills)
        self.assertEqual(agg["fill_count"], 2)
        self.assertEqual(D(agg["total_spread_capture"]), D("0.05"))


class MakerQueueTests(unittest.TestCase):
    def test_no_fill_ahead(self):
        q = MakerQueue(side="BUY", price=D("0.50"), remaining=D(100), queue_ahead=D(50))
        fill = q.on_trade("SELL", D("0.50"), D(30))
        self.assertEqual(fill, D(0))
        self.assertEqual(q.queue_ahead, D(20))

    def test_partial_fill(self):
        q = MakerQueue(side="BUY", price=D("0.50"), remaining=D(100), queue_ahead=D(10))
        fill = q.on_trade("SELL", D("0.50"), D(50))
        self.assertEqual(fill, D(40))
        self.assertEqual(q.remaining, D(60))

    def test_full_fill(self):
        q = MakerQueue(side="BUY", price=D("0.50"), remaining=D(100), queue_ahead=D(0))
        fill = q.on_trade("SELL", D("0.50"), D(200))
        self.assertEqual(fill, D(100))
        self.assertEqual(q.remaining, D(0))

    def test_wrong_side_no_fill(self):
        q = MakerQueue(side="BUY", price=D("0.50"), remaining=D(100), queue_ahead=D(0))
        fill = q.on_trade("BUY", D("0.50"), D(100))
        self.assertEqual(fill, D(0))

    def test_wrong_price_no_fill(self):
        q = MakerQueue(side="BUY", price=D("0.50"), remaining=D(100), queue_ahead=D(0))
        fill = q.on_trade("SELL", D("0.51"), D(100))
        self.assertEqual(fill, D(0))

    def test_invalid_queue(self):
        with self.assertRaises(ValueError):
            MakerQueue(side="BUY", price=D("0.50"), remaining=D(0), queue_ahead=D(0))
        with self.assertRaises(ValueError):
            MakerQueue(side="BUY", price=D("0.50"), remaining=D(100), queue_ahead=D(-1))


if __name__ == "__main__":
    unittest.main()
