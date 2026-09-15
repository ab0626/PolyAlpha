"""Tests for the v0.3.0 data-collection layer.

Covers the immutable raw event store, the market WS collector, REST
reconciliation, metadata versioning, health reporting, daily manifests, and
the collection orchestrator. All tests are offline: they exercise parsers,
state machines, and stores with synthetic frames matching the documented
Polymarket WebSocket / REST schemas.
"""

import gzip
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

sys.path.insert(0, "src")

from polyalpha.collector_health import (  # noqa: E402
    HealthReport,
    build_health_report,
    render_terminal,
)
from polyalpha.daily_manifest import ManifestWriter, build_daily_manifest  # noqa: E402
from polyalpha.market_collector import CollectorStats, MarketCollector  # noqa: E402
from polyalpha.market_metadata import MetadataStore  # noqa: E402
from polyalpha.rawstore import SOURCE_MARKET_WS, RawStore, list_days  # noqa: E402
from polyalpha.reconciler import Reconciler, reconcile_book  # noqa: E402

TOKEN = "107505882767731489358349912513945399560393482969656700824895970500493757150417"
MARKET = "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75"


def _book_frame(ts=1782753357257, hash_="h1"):
    return {
        "event_type": "book",
        "market": MARKET,
        "asset_id": TOKEN,
        "timestamp": str(ts),
        "bids": [{"price": "0.08", "size": "100"}],
        "asks": [{"price": "0.09", "size": "100"}],
        "tick_size": "0.01",
        "min_order_size": "5",
        "hash": hash_,
    }


# ══════════════════════════════════════════════════════════════════════════
# RAW EVENT STORE
# ══════════════════════════════════════════════════════════════════════════


class TestRawStore:
    def test_append_and_replay(self, tmp_path):
        with RawStore(tmp_path, "test-v1") as store:
            store.append(SOURCE_MARKET_WS, "c1", {"a": 1}, received_at_ns=1_700_000_000_000_000_000)
        records = list(RawStore(tmp_path).replay())
        assert len(records) == 1
        assert records[0].payload == {"a": 1}
        assert records[0].source == SOURCE_MARKET_WS
        assert records[0].connection_id == "c1"
        assert records[0].received_at_ns == 1_700_000_000_000_000_000
        assert records[0].collector_version == "test-v1"
        assert len(records[0].sha256) == 64

    def test_readable_while_open(self, tmp_path):
        store = RawStore(tmp_path, "v1")
        store.append(SOURCE_MARKET_WS, "c", {"n": 1})
        # Un-flushed data must be visible without closing (crash-safety).
        assert store.count() == 1
        store.close()

    def test_compressed_after_close(self, tmp_path):
        store = RawStore(tmp_path, "v1")
        store.append(SOURCE_MARKET_WS, "c", {"n": 1})
        store.close()
        gz_files = list(tmp_path.rglob("*.jsonl.gz"))
        assert len(gz_files) == 1
        assert not list(tmp_path.rglob("*.jsonl"))
        assert RawStore(tmp_path).count() == 1

    def test_replay_deterministic_hash(self, tmp_path):
        with RawStore(tmp_path, "v1") as store:
            for i in range(5):
                store.append(SOURCE_MARKET_WS, "c", {"i": i}, received_at_ns=1_700_000_000_000_000_000 + i)
        h1 = RawStore(tmp_path).sha256_root()
        h2 = RawStore(tmp_path).sha256_root()
        assert h1 == h2
        assert len(h1) == 64

    def test_day_rollover(self, tmp_path):
        store = RawStore(tmp_path, "v1")
        store.append(SOURCE_MARKET_WS, "c", {"d": 1}, received_at_ns=1_700_000_000_000_000_000)
        store.close()
        days = list_days(tmp_path)
        assert len(days) == 1

    def test_write_once_preserves_envelope(self, tmp_path):
        with RawStore(tmp_path, "v1") as store:
            store.append("polymarket_rest_book", "c2", {"book": True}, received_at_ns=1)
        rec = list(RawStore(tmp_path).replay())[0]
        assert rec.source == "polymarket_rest_book"
        assert rec.payload == {"book": True}


# ══════════════════════════════════════════════════════════════════════════
# MARKET COLLECTOR
# ══════════════════════════════════════════════════════════════════════════


class TestMarketCollector:
    def _collector(self, tmp_path):
        raw = RawStore(tmp_path / "raw", "v1")
        return MarketCollector(raw, [TOKEN], collector_version="v1"), raw

    def test_book_snapshot(self, tmp_path):
        col, raw = self._collector(tmp_path)
        derived = col.process(json.dumps(_book_frame()))
        assert len(derived) == 1
        assert derived[0]["bids"] == [{"price": "0.08", "size": "100"}]
        assert raw.count() == 1
        raw.close()

    def test_price_change_delta(self, tmp_path):
        col, raw = self._collector(tmp_path)
        col.process(json.dumps(_book_frame()))
        delta = {
            "event_type": "price_change",
            "market": MARKET,
            "timestamp": "1782753357260",
            "price_changes": [
                {
                    "asset_id": TOKEN,
                    "price": "0.09",
                    "size": "50",
                    "side": "SELL",
                    "hash": "h2",
                    "best_bid": "0.08",
                    "best_ask": "0.09",
                }
            ],
        }
        col.process(json.dumps(delta))
        assert col.books[TOKEN]["asks"] == [{"price": "0.09", "size": "50"}]
        raw.close()

    def test_delta_before_snapshot_invalidates(self, tmp_path):
        col, raw = self._collector(tmp_path)
        delta = {
            "event_type": "price_change",
            "market": MARKET,
            "timestamp": "1782753357260",
            "price_changes": [{"asset_id": TOKEN, "price": "0.09", "size": "50", "side": "SELL"}],
        }
        with pytest.raises(ValueError, match="resynchronize"):
            col.process(json.dumps(delta))
        assert col.books == {}
        raw.close()

    def test_tick_size_clears_book(self, tmp_path):
        col, raw = self._collector(tmp_path)
        col.process(json.dumps(_book_frame()))
        col.process(
            json.dumps(
                {
                    "event_type": "tick_size_change",
                    "market": MARKET,
                    "asset_id": TOKEN,
                    "old_tick_size": "0.01",
                    "new_tick_size": "0.001",
                }
            )
        )
        assert TOKEN not in col.books
        raw.close()

    def test_all_event_types_are_captured_raw(self, tmp_path):
        col, raw = self._collector(tmp_path)
        col.process(json.dumps(_book_frame()))
        delta = {
            "event_type": "price_change",
            "market": MARKET,
            "timestamp": "1",
            "price_changes": [{"asset_id": TOKEN, "price": "0.08", "size": "1", "side": "BUY"}],
        }
        col.process(json.dumps(delta))
        col.process(
            json.dumps(
                {
                    "event_type": "last_trade_price",
                    "market": MARKET,
                    "asset_id": TOKEN,
                    "price": "0.08",
                    "side": "SELL",
                }
            )
        )
        col.process(
            json.dumps(
                {
                    "event_type": "best_bid_ask",
                    "market": MARKET,
                    "asset_id": TOKEN,
                    "best_bid": "0.08",
                    "best_ask": "0.09",
                    "spread": "0.01",
                }
            )
        )
        col.process(json.dumps({"event_type": "new_market", "id": "1", "market": MARKET, "question": "Q"}))
        col.process(
            json.dumps(
                {
                    "event_type": "market_resolved",
                    "id": "1",
                    "market": MARKET,
                    "winning_asset_id": TOKEN,
                }
            )
        )
        assert raw.count() == 6
        stats = col.stats.as_dict()
        assert stats["book_events"] == 1
        assert stats["price_change_events"] == 1
        assert stats["last_trade_events"] == 1
        assert stats["best_bid_ask_events"] == 1
        assert stats["new_market_events"] == 1
        assert stats["market_resolved_events"] == 1
        raw.close()

    def test_stats_record(self):
        stats = CollectorStats()
        stats.record("book_events")
        assert stats.book_events == 1
        assert stats.as_dict()["book_events"] == 1

    def test_collector_version_embedded_in_raw(self, tmp_path):
        col, raw = self._collector(tmp_path)
        col.process(json.dumps(_book_frame()))
        rec = list(raw.replay())[0]
        assert rec.collector_version == "v1"
        # run_collector() assigns a connection id; direct process() uses default.
        assert rec.connection_id == "none"
        raw.close()


# ══════════════════════════════════════════════════════════════════════════
# REST RECONCILIATION
# ══════════════════════════════════════════════════════════════════════════


class TestReconciler:
    def _rest_book(self):
        return {
            "market": MARKET,
            "asset_id": TOKEN,
            "timestamp": "1782753357257",
            "bids": [{"price": "0.08", "size": "100"}],
            "asks": [{"price": "0.09", "size": "100"}],
            "min_order_size": "5",
            "tick_size": "0.01",
            "neg_risk": False,
            "last_trade_price": "0.090",
            "hash": "a1b2c3d4",
        }

    def test_matching_books(self):
        local = {
            "market": MARKET,
            "bids": [{"price": "0.08", "size": "100"}],
            "asks": [{"price": "0.09", "size": "100"}],
        }
        result = reconcile_book(local, self._rest_book(), TOKEN, received_at_ns=1_700_000_000_000_000_000)
        assert result.matches is True
        assert result.difference_count == 0

    def test_mismatch_detected(self):
        local = {
            "market": MARKET,
            "bids": [{"price": "0.08", "size": "200"}],
            "asks": [{"price": "0.09", "size": "100"}],
        }
        result = reconcile_book(local, self._rest_book(), TOKEN, received_at_ns=1_700_000_000_000_000_000)
        assert result.matches is False
        assert result.difference_count == 1
        assert result.max_size_difference == D("100")

    def test_reconciler_collects_results(self):
        class FakeTransport:
            def get(self, url, params):
                book = {
                    "market": MARKET,
                    "asset_id": TOKEN,
                    "timestamp": "1782753357257",
                    "bids": [{"price": "0.08", "size": "100"}],
                    "asks": [{"price": "0.09", "size": "100"}],
                    "min_order_size": "5",
                    "tick_size": "0.01",
                    "hash": "h",
                }
                from datetime import UTC, datetime
                return book, datetime.now(UTC)

        rec = Reconciler(FakeTransport(), min_spacing_seconds=0.001)
        local = {
            "market": MARKET,
            "bids": [{"price": "0.08", "size": "100"}],
            "asks": [{"price": "0.09", "size": "100"}],
        }
        results = rec.reconcile({TOKEN: local}, max_tokens=1)
        assert len(results) == 1
        assert results[0].matches is True
        assert rec.summary()["matches"] == 1

    def test_summary_empty(self):
        rec = Reconciler(None, min_spacing_seconds=0.1)
        summary = rec.summary()
        assert summary["total"] == 0
        assert summary["match_rate"] is None


# ══════════════════════════════════════════════════════════════════════════
# METADATA VERSIONING
# ══════════════════════════════════════════════════════════════════════════


class TestMetadataStore:
    def test_observe_creates_version(self):
        store = MetadataStore()
        v = store.observe("m1", {"question": "Q"})
        assert v.metadata_hash
        assert store.latest("m1") is v

    def test_no_change_does_not_close_version(self):
        store = MetadataStore()
        store.observe("m1", {"question": "Q"}, at=datetime(2026, 1, 1, tzinfo=UTC))
        store.observe("m1", {"question": "Q"}, at=datetime(2026, 1, 2, tzinfo=UTC))
        assert len(store.versions["m1"]) == 1

    def test_change_closes_prior_version(self):
        store = MetadataStore()
        store.observe("m1", {"question": "Q"}, at=datetime(2026, 1, 1, tzinfo=UTC))
        store.observe("m1", {"question": "R"}, at=datetime(2026, 1, 2, tzinfo=UTC))
        versions = store.versions["m1"]
        assert len(versions) == 2
        assert versions[0].valid_until == datetime(2026, 1, 2, tzinfo=UTC)

    def test_as_of_point_in_time(self):
        store = MetadataStore()
        store.observe("m1", {"q": "Q"}, at=datetime(2026, 1, 1, tzinfo=UTC))
        store.observe("m1", {"q": "R"}, at=datetime(2026, 1, 5, tzinfo=UTC))
        mid = store.as_of("m1", datetime(2026, 1, 3, tzinfo=UTC))
        assert mid.metadata == {"q": "Q"}
        late = store.as_of("m1", datetime(2026, 1, 6, tzinfo=UTC))
        assert late.metadata == {"q": "R"}

    def test_lifecycle_events(self):
        store = MetadataStore()
        store.record_lifecycle("new_market", {"market": MARKET})
        store.record_lifecycle("market_resolved", {"market": MARKET})
        assert len(store.lifecycle) == 2
        assert len(store.lifecycle_for(MARKET)) == 2
        assert store.summary()["lifecycle_events"] == 2


# ══════════════════════════════════════════════════════════════════════════
# DAILY MANIFEST
# ══════════════════════════════════════════════════════════════════════════


class TestDailyManifest:
    def test_manifest_write_and_read(self, tmp_path):
        with RawStore(tmp_path / "raw", "v1") as store:
            store.append(SOURCE_MARKET_WS, "c", {"n": 1})
        writer = ManifestWriter(tmp_path / "manifests")
        manifest = build_daily_manifest(
            RawStore(tmp_path / "raw"), date.today(),
            collector_commit="abc", config_hash="cfg",
            markets_observed=1, resolved_markets=0,
            dropped_connections=0, reconciliations=2, book_mismatches=0,
        )
        path = writer.write(manifest)
        assert path.exists()
        read = writer.read(date.today())
        assert read.raw_messages == 1
        assert read.combined_hash() == manifest.combined_hash()

    def test_manifest_refuses_overwrite(self, tmp_path):
        with RawStore(tmp_path / "raw", "v1") as store:
            store.append(SOURCE_MARKET_WS, "c", {"n": 1})
        writer = ManifestWriter(tmp_path / "manifests")
        manifest = build_daily_manifest(
            RawStore(tmp_path / "raw"), date.today(),
            collector_commit="abc", config_hash="cfg",
            markets_observed=0, resolved_markets=0,
            dropped_connections=0, reconciliations=0, book_mismatches=0,
        )
        writer.write(manifest)
        with pytest.raises(FileExistsError):
            writer.write(manifest)

    def test_verify_catches_mutation(self, tmp_path):
        with RawStore(tmp_path / "raw", "v1") as store:
            store.append(SOURCE_MARKET_WS, "c", {"n": 1})
        writer = ManifestWriter(tmp_path / "manifests")
        manifest = build_daily_manifest(
            RawStore(tmp_path / "raw"), date.today(),
            collector_commit="abc", config_hash="cfg",
            markets_observed=0, resolved_markets=0,
            dropped_connections=0, reconciliations=0, book_mismatches=0,
        )
        writer.write(manifest)
        ok, msg = writer.verify(date.today(), RawStore(tmp_path / "raw"))
        assert ok is True

        # Tamper: append a forged record to the finalized gz file.
        target = list((tmp_path / "raw").rglob("*.jsonl.gz"))[0]
        with gzip.open(target, "rb") as fh:
            content = fh.read()
        with gzip.open(target, "wb") as fh:
            fh.write(content)
            fh.write(b'{"received_at_ns":1,"source":"x","payload":{"forged":true}}\n')
        ok, msg = writer.verify(date.today(), RawStore(tmp_path / "raw"))
        assert ok is False
        assert "mutated" in msg


# ══════════════════════════════════════════════════════════════════════════
# HEALTH REPORT
# ══════════════════════════════════════════════════════════════════════════


class TestHealthReport:
    def test_build_health_report(self, tmp_path):
        stats = CollectorStats()
        stats.started_at = 0.0
        stats.reconnect_count = 3
        stats.messages_received = 10
        stats.book_events = 5
        stats.last_trade_events = 2
        stats.market_resolved_events = 1
        stats.receive_lag_ns = [10_000_000, 20_000_000, 30_000_000, 40_000_000, 50_000_000]
        with RawStore(tmp_path / "raw", "v1") as store:
            store.append(SOURCE_MARKET_WS, "c", {"n": 1})
            report = build_health_report(
                stats, store,
                reconcile_total=10, reconcile_matches=9,
                markets_tracked=5, tokens_tracked=10,
            )
        d = report.as_dict()
        assert d["reconnects"] == 3
        assert d["reconciliation_matches"] == 9
        assert d["reconciliation_mismatches"] == 1
        assert d["markets_tracked"] == 5
        assert d["tokens_tracked"] == 10
        assert d["median_receive_lag_ms"] == 30.0

    def test_render_terminal(self, tmp_path):
        report = HealthReport(uptime_seconds=1.5, messages_today=5)
        text = render_terminal(report)
        assert "COLLECTOR HEALTH" in text
        assert "5" in text