import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polyalpha.collector import Collector
from polyalpha.parsing import parse_book, parse_market
from polyalpha.quality import Filter
from polyalpha.storage import Store

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def market():
    return dict(
        id="m",
        conditionId="c",
        question="Will the test event occur?",
        outcomes='["No", "Yes"]',
        clobTokenIds='["n", "y"]',
        events=[{"id": "event"}],
        active=True,
        closed=False,
        acceptingOrders=True,
        enableOrderBook=True,
        liquidity="6000",
        volume="30000",
        endDate="2027-01-01T00:00:00Z",
    )


def book(token="y", source=NOW):
    return dict(
        asset_id=token,
        market="c",
        timestamp=str(int(source.timestamp() * 1000)),
        bids=[{"price": "0.49", "size": "100"}],
        asks=[
            {"price": "0.54", "size": "2000"},
            {"price": "0.51", "size": "100"},
            {"price": "0.52", "size": "300"},
        ],
        tick_size="0.01",
        min_order_size="1",
        hash="synthetic",
    )


class DomainTests(unittest.TestCase):
    def test_outcome_mapping_and_unknown_fee(self):
        result = parse_market(market(), NOW)
        self.assertEqual(result.yes_token_id, "y")
        self.assertIsNone(result.fees_enabled)
        self.assertIsNone(result.fee_parameters_json)

    def test_not_all_binary_labels_are_yes_no(self):
        raw = market()
        raw["outcomes"] = ["Up", "Down"]
        with self.assertRaises(ValueError):
            parse_market(raw, NOW)

    def test_string_booleans_rejected(self):
        raw = market()
        raw["active"] = "false"
        with self.assertRaises(ValueError):
            parse_market(raw, NOW)

    def test_missing_status_fails_closed(self):
        raw = market()
        del raw["closed"]
        self.assertIn("inactive_or_closed", Filter().market_reasons(parse_market(raw, NOW), NOW))

    def test_sort_and_spread(self):
        result = parse_book(book(), NOW)
        self.assertEqual(result.best_ask, Decimal("0.51"))
        self.assertEqual(result.spread, Decimal("0.02"))
        self.assertEqual(result.mid, Decimal("0.50"))
        self.assertEqual(result.source_at, NOW)

    def test_invalid_levels(self):
        for price, size in [
            ("NaN", "1"),
            ("1.1", "1"),
            ("0.5", "-1"),
            ("0.5", "0"),
            ("0.501", "1"),
        ]:
            with self.subTest(price=price, size=size):
                raw = book()
                raw["asks"] = [{"price": price, "size": size}]
                with self.assertRaises(ValueError):
                    parse_book(raw, NOW)

    def test_duplicate_levels_rejected(self):
        raw = book()
        raw["asks"].append(raw["asks"][0])
        with self.assertRaises(ValueError):
            parse_book(raw, NOW)

    def test_token_mismatch(self):
        with self.assertRaises(ValueError):
            parse_book(book(), NOW, "wrong")

    def test_naive_datetime(self):
        with self.assertRaises(ValueError):
            parse_book(book(), datetime(2026, 1, 1))

    def test_staleness_and_future(self):
        result = parse_book(book(), NOW)
        self.assertIn("stale_book", Filter().book_reasons(result, NOW + timedelta(seconds=31)))
        self.assertIn("future_book", Filter().book_reasons(result, NOW - timedelta(seconds=1)))

    def test_one_sided_and_crossed(self):
        raw = book()
        raw["asks"] = []
        self.assertIn("one_sided_book", Filter().book_reasons(parse_book(raw, NOW), NOW))
        raw["asks"] = [{"price": "0.48", "size": "100"}]
        self.assertIn("locked_or_crossed_book", Filter().book_reasons(parse_book(raw, NOW), NOW))


class StorageTests(unittest.TestCase):
    def test_point_in_time_and_out_of_order(self):
        with Store(":memory:") as store:
            store.append("book", "y", NOW, {"version": 1}, NOW)
            store.append(
                "book",
                "y",
                NOW + timedelta(seconds=10),
                {"version": 2},
                NOW + timedelta(seconds=5),
            )
            store.append(
                "book",
                "y",
                NOW + timedelta(seconds=20),
                {"version": 0},
                NOW - timedelta(seconds=1),
            )
            self.assertEqual(store.latest("book", "y", NOW).payload["version"], 1)
            self.assertEqual(len(list(store.replay(NOW + timedelta(seconds=9)))), 1)
            self.assertEqual(
                store.latest("book", "y", NOW + timedelta(seconds=30)).payload["version"],
                2,
            )

    def test_future_source_hidden(self):
        with Store(":memory:") as store:
            store.append("book", "y", NOW, {}, NOW + timedelta(seconds=1))
            self.assertIsNone(store.latest("book", "y", NOW))

    def test_append_only_and_duplicate_receipts(self):
        with Store(":memory:") as store:
            store.append("market", "m", NOW, {})
            store.append("market", "m", NOW, {})
            self.assertEqual(len(list(store.replay(NOW))), 2)
            for statement in ("DELETE FROM receipts", 'UPDATE receipts SET kind="x"'):
                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute(statement)


class FakeTransport:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def get(self, url, params):
        self.calls.append((url, dict(params)))
        if url.endswith("/book"):
            return book(params["token_id"]), NOW
        return next(self.pages), NOW


class CollectorTests(unittest.TestCase):
    def test_two_pages_both_books_and_raw_data(self):
        fake = FakeTransport([{"markets": [market()], "next_cursor": "next"}, {"markets": []}])
        with Store(":memory:") as store:
            stats = Collector(fake, store, Filter()).collect(10, 2)
            self.assertEqual(stats["books"], 2)
            self.assertFalse(stats["truncated"])
            self.assertEqual(fake.calls[-1][1]["after_cursor"], "next")
            self.assertEqual(len(list(store.replay(NOW, "raw_book"))), 2)

    def test_truncation_visible(self):
        fake = FakeTransport([{"markets": [], "next_cursor": "more"}])
        with Store(":memory:") as store:
            self.assertTrue(Collector(fake, store, Filter()).collect()["truncated"])

    def test_repeat_cursor_rejected(self):
        fake = FakeTransport([{"markets": [], "next_cursor": "same"}] * 2)
        with Store(":memory:") as store, self.assertRaises(ValueError):
            Collector(fake, store, Filter()).collect(10, 3)

    def test_bad_payload_auditable(self):
        fake = FakeTransport([{"markets": [{"id": "broken"}]}])
        with Store(":memory:") as store:
            stats = Collector(fake, store, Filter()).collect()
            self.assertEqual(stats["errors"], 1)
            self.assertEqual(len(list(store.replay(NOW, "parse_error"))), 1)


if __name__ == "__main__":
    unittest.main()
