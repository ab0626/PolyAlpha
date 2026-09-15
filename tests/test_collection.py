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

from polyalpha.burnin_gate import (  # noqa: E402
    BurnInReport,
    evaluate_burn_in_gate,
    verify_real_data_start_marker,
    write_burn_in_report,
    write_real_data_start_marker,
)
from polyalpha.collector_health import (  # noqa: E402
    HealthReport,
    build_health_report,
    render_dashboard,
    render_terminal,
)
from polyalpha.daily_manifest import ManifestWriter, build_daily_manifest  # noqa: E402
from polyalpha.market_collector import CollectorStats, MarketCollector  # noqa: E402
from polyalpha.market_metadata import MetadataStore  # noqa: E402
from polyalpha.phases import PhaseStore, initialize_phase  # noqa: E402
from polyalpha.rawstore import SOURCE_MARKET_WS, RawStore, list_days  # noqa: E402
from polyalpha.reconciler import Reconciler, reconcile_book  # noqa: E402
from polyalpha.replay_verification import run_dual_replay  # noqa: E402

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


def _marker_kwargs(commit="abc123"):
    return dict(
        baseline_tag="v0.3.0-research-baseline-334b911",
        baseline_commit=commit,
        implementation_commit="e2b072a",
        research_logic_sha256="r" * 64,
        collector_sha256="c" * 64,
        interface_sha256="i" * 64,
        baseline_config_sha256="bcfg" + "0" * 60,
        feature_schema_sha256="f" * 64,
        burnin_report_sha256="b" * 64,
        execution_mode="SHADOW_OR_COLLECTION_ONLY",
        alpha_status="UNKNOWN",
    )


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

    def test_manifest_hash_chain(self, tmp_path):
        """Day t's manifest must chain to day t-1 via previous_manifest_sha256."""
        from polyalpha.rawstore import RawStore as RS

        raw_root = tmp_path / "raw"
        man_root = tmp_path / "manifests"
        writer = ManifestWriter(man_root)
        for i, day in enumerate([date(2026, 1, 1), date(2026, 1, 2)]):
            # Force a raw file into the day directory.
            directory = raw_root / str(day.year) / f"{day.month:02d}" / f"{day.day:02d}"
            directory.mkdir(parents=True, exist_ok=True)
            store = RS(str(raw_root), "v1")
            store.append(SOURCE_MARKET_WS, "c", {"i": i}, wire='{"i":' + str(i) + "}")
            store.close()
            manifest = build_daily_manifest(
                RS(str(raw_root)), day,
                collector_commit="c", config_hash="cfg",
                markets_observed=0, resolved_markets=0,
                dropped_connections=0, reconciliations=0, book_mismatches=0,
            )
            writer.write(manifest)
        m1 = writer.read(date(2026, 1, 1))
        m2 = writer.read(date(2026, 1, 2))
        assert m1.previous_manifest_sha256 is None
        assert m2.previous_manifest_sha256 == m1.combined_hash()
        # Tampering with day 1's manifest file breaks the chain on day 2.
        day1_path = man_root / "2026-01-01.json"
        original = day1_path.read_text(encoding="utf-8")
        day1_path.write_text(original.replace('"date": "2026-01-01"', '"date": "2026-01-09"'), encoding="utf-8")
        m2_read = writer.read(date(2026, 1, 2))
        assert m2_read.combined_hash() == m2.combined_hash()  # day2 itself unchanged
        # But recomputing day1's hash now differs from what day2 recorded.
        m1_now = writer.read(date(2026, 1, 1))
        assert m1_now.combined_hash() != m2_read.previous_manifest_sha256


# ══════════════════════════════════════════════════════════════════════════
# BURN-IN GATE + REAL_DATA_START MARKER
# ══════════════════════════════════════════════════════════════════════════


class TestBurnInGate:
    def test_all_pass(self):
        gate = evaluate_burn_in_gate(
            raw_corruption_count=0,
            replay_deterministic=True,
            delta_on_stale_count=0,
            unrecoverable_reconnect_count=0,
            timestamp_invariant_failures=0,
            reconciliation_total=100,
            reconciliation_mismatches=1,
            heartbeat_recovery=True,
            restart_recovery=True,
            partial_file_recovery=True,
            metadata_point_in_time=True,
            resolution_captured=True,
            model_hash_unchanged=True,
        )
        assert gate.all_pass is True

    def test_reconciliation_bounded_pass_at_one_percent(self):
        gate = evaluate_burn_in_gate(
            raw_corruption_count=0,
            replay_deterministic=True,
            delta_on_stale_count=0,
            unrecoverable_reconnect_count=0,
            timestamp_invariant_failures=0,
            reconciliation_total=1000,
            reconciliation_mismatches=10,  # exactly 1% — bounded threshold
            heartbeat_recovery=True,
            restart_recovery=True,
            partial_file_recovery=True,
            metadata_point_in_time=True,
            resolution_captured=True,
            model_hash_unchanged=True,
        )
        assert gate.all_pass is True

    def test_any_failure_blocks(self):
        gate = evaluate_burn_in_gate(
            raw_corruption_count=1,
            replay_deterministic=True,
            delta_on_stale_count=0,
            unrecoverable_reconnect_count=0,
            timestamp_invariant_failures=0,
            reconciliation_total=0,
            reconciliation_mismatches=0,
            heartbeat_recovery=True,
            restart_recovery=True,
            partial_file_recovery=True,
            metadata_point_in_time=True,
            resolution_captured=True,
            model_hash_unchanged=True,
        )
        assert gate.all_pass is False
        assert gate.summary()["failed_count"] == 1

    def test_reconciliation_bounded(self):
        gate = evaluate_burn_in_gate(
            raw_corruption_count=0,
            replay_deterministic=True,
            delta_on_stale_count=0,
            unrecoverable_reconnect_count=0,
            timestamp_invariant_failures=0,
            reconciliation_total=1000,
            reconciliation_mismatches=50,  # 5% > 1% threshold
            heartbeat_recovery=True,
            restart_recovery=True,
            partial_file_recovery=True,
            metadata_point_in_time=True,
            resolution_captured=True,
            model_hash_unchanged=True,
        )
        assert gate.all_pass is False


class TestRealDataStartMarker:
    def test_write_and_verify(self, tmp_path):
        path = tmp_path / "REAL_DATA_START.json"
        marker = write_real_data_start_marker(path, **_marker_kwargs())
        assert marker.phase == "REAL_DATA_START"
        assert marker.baseline_commit == "abc123"
        assert marker.implementation_commit == "e2b072a"
        assert marker.execution_mode == "SHADOW_OR_COLLECTION_ONLY"
        assert marker.alpha_status == "UNKNOWN"
        ok, msg = verify_real_data_start_marker(path)
        assert ok is True, msg

    def test_refuses_overwrite(self, tmp_path):
        path = tmp_path / "REAL_DATA_START.json"
        write_real_data_start_marker(path, **_marker_kwargs())
        import pytest as _pytest
        with _pytest.raises(FileExistsError):
            write_real_data_start_marker(path, **_marker_kwargs())

    def test_tamper_detected(self, tmp_path):
        path = tmp_path / "REAL_DATA_START.json"
        write_real_data_start_marker(path, **_marker_kwargs())
        content = path.read_text(encoding="utf-8")
        path.write_text(
            content.replace('"phase": "REAL_DATA_START"', '"phase": "FAKE"'),
            encoding="utf-8",
        )
        ok, _ = verify_real_data_start_marker(path)
        assert ok is False


# ══════════════════════════════════════════════════════════════════════════
# RAW STORE CRASH RECOVERY (fault injection)
# ══════════════════════════════════════════════════════════════════════════


class TestRawStoreCrashRecovery:
    @staticmethod
    def _record_line(wire_text, payload, seq=0):
        """Build the exact serialized line RawStore.append would write."""
        import hashlib as _hashlib


        envelope = {
            "wire": wire_text,
            "source": SOURCE_MARKET_WS,
            "collector_version": "v1",
            "connection_id": "c",
            "message_sequence_local": seq,
            "exchange_timestamp_ms": None,
            "received_at_ns": 1,
            "received_monotonic_ns": 1,
            "processed_at_ns": 1,
            "processed_monotonic_ns": 1,
            "wire_was_bytes": False,
            "payload": payload,
        }
        canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        digest = _hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        line = {"canonical_json": canonical, "sha256": digest}
        return json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n"

    def _seed_day(self, tmp_path):
        day = tmp_path / "2026" / "09" / "15"
        day.mkdir(parents=True)
        raw = RawStore(tmp_path, "v1")
        wire = json.dumps({"valid": 1})
        raw.append(SOURCE_MARKET_WS, "c", {"valid": 1}, wire=wire)
        raw.close()  # finalized .gz
        return day

    def test_partial_trailing_line_quarantined(self, tmp_path):
        day = self._seed_day(tmp_path)
        f2 = day / "pm-0001.jsonl"
        with open(f2, "w", encoding="utf-8") as fh:
            fh.write(self._record_line(json.dumps({"b": 2}), {"b": 2}))
            fh.write('{"canonical_json":"partial')  # truncated (crash mid-write)
        rs = RawStore(tmp_path, "v1")
        scan = rs.scan()
        assert scan["partial_lines"] == 1
        # Valid record b is still replayed; the partial one is not.
        payloads = {frozenset(r.payload.items()) for r in rs.replay()}
        assert frozenset({("valid", 1)}) in payloads
        assert frozenset({("b", 2)}) in payloads

    def test_orphaned_jsonl_recovered(self, tmp_path):
        day = self._seed_day(tmp_path)
        f3 = day / "pm-0002.jsonl"  # crash before finalize
        with open(f3, "w", encoding="utf-8") as fh:
            fh.write(self._record_line(json.dumps({"c": 3}), {"c": 3}))
        rs = RawStore(tmp_path, "v1")
        payloads = {frozenset(r.payload.items()) for r in rs.replay()}
        assert frozenset({("c", 3)}) in payloads
        assert rs.scan()["valid"] == 2

    def test_tmp_promoted_to_gz(self, tmp_path):
        day = self._seed_day(tmp_path)
        import gzip as _gz
        f4 = day / "pm-0003.jsonl.gz.tmp"  # crash between gzip-write and rename
        with _gz.open(f4, "wt", encoding="utf-8") as fh:
            fh.write(self._record_line(json.dumps({"d": 4}), {"d": 4}))
        rs = RawStore(tmp_path, "v1")
        payloads = {frozenset(r.payload.items()) for r in rs.replay()}
        assert frozenset({("d", 4)}) in payloads
        # tmp must have been promoted to .gz and removed
        assert not list((tmp_path).rglob("*.tmp"))
        assert list((tmp_path).rglob("pm-0003.jsonl.gz"))

    def test_wire_bytes_preserved(self, tmp_path):
        with RawStore(tmp_path / "w", "v1") as store:
            wire = b'{"binary": true}'
            store.append(SOURCE_MARKET_WS, "c", {"binary": True}, wire=wire)
        rec = list(RawStore(tmp_path / "w").replay())[0]
        assert rec.wire_was_bytes is True
        assert rec.payload == {"binary": True}

    def test_exact_wire_text_preserved(self, tmp_path):
        with RawStore(tmp_path / "w", "v1") as store:
            wire = '{"z":1,"a":2}'  # non-canonical order must be preserved verbatim
            store.append(SOURCE_MARKET_WS, "c", json.loads(wire), wire=wire)
        rec = list(RawStore(tmp_path / "w").replay())[0]
        assert rec.wire == '{"z":1,"a":2}'

    def test_three_clocks_present(self, tmp_path):
        with RawStore(tmp_path / "w", "v1") as store:
            store.append(
                SOURCE_MARKET_WS, "c", {"n": 1}, wire='{"n":1}',
                received_at_ns=1_700_000_000_000_000_000,
                received_monotonic_ns=5_000_000_000,
                exchange_timestamp_ms=1_700_000_000_000,
            )
        rec = list(RawStore(tmp_path / "w").replay())[0]
        assert rec.exchange_timestamp_ms == 1_700_000_000_000
        assert rec.received_at_ns == 1_700_000_000_000_000_000
        assert rec.received_monotonic_ns == 5_000_000_000
        assert rec.processed_at_ns > 0
        assert rec.processed_monotonic_ns > 0


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

    def test_render_dashboard_locks_model(self):
        report = HealthReport(uptime_seconds=172800.0, messages_today=1_000_000)
        text = render_dashboard(report)
        assert "POLYALPHA COLLECTION" in text
        assert "2d 0h" in text  # 172800s = 2 days
        assert "LOCKED" in text  # model performance must be locked


# ══════════════════════════════════════════════════════════════════════════
# PHASE STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════


class TestPhaseStore:
    def test_init_to_baseline_frozen(self, tmp_path):
        path = tmp_path / "phase.json"
        state = initialize_phase(path, "abc123")
        assert state.phase == "BASELINE_FROZEN"
        assert state.baseline_commit == "abc123"

    def test_valid_transition(self, tmp_path):
        path = tmp_path / "phase.json"
        initialize_phase(path, "abc")
        store = PhaseStore(path, "abc")
        store.transition("BURNIN_RUNNING")
        store.transition("BURNIN_PASSED")
        assert store.read().phase == "BURNIN_PASSED"
        assert len(store.read().transition_log) == 3

    def test_invalid_transition_rejected(self, tmp_path):
        path = tmp_path / "phase.json"
        initialize_phase(path, "abc")
        store = PhaseStore(path, "abc")
        with pytest.raises(ValueError, match="invalid transition"):
            store.transition("BURNIN_PASSED")  # DEVELOPMENT can only -> BASELINE_FROZEN

    def test_require(self, tmp_path):
        path = tmp_path / "phase.json"
        initialize_phase(path, "abc")
        store = PhaseStore(path, "abc")
        store.require("BASELINE_FROZEN")  # ok
        with pytest.raises(ValueError):
            store.require("BURNIN_PASSED")

    def test_marker_requires_burnin_passed(self, tmp_path):
        """REAL_DATA_START marker must not write unless phase == BURNIN_PASSED."""
        from polyalpha.phases import PhaseStore as PS

        phase_path = tmp_path / "phase.json"
        initialize_phase(phase_path, "abc")  # -> BASELINE_FROZEN
        store = PS(phase_path, "abc")
        marker_path = tmp_path / "REAL_DATA_START.json"
        with pytest.raises(ValueError):
            write_real_data_start_marker(
                marker_path, phase_store=store, **_marker_kwargs(commit="abc"),
            )
        assert not marker_path.exists()

        # Advance through the phases, then it must succeed.
        store.transition("BURNIN_RUNNING")
        store.transition("BURNIN_PASSED")
        write_real_data_start_marker(
            marker_path, phase_store=store, **_marker_kwargs(commit="abc"),
        )
        assert marker_path.exists()


# ══════════════════════════════════════════════════════════════════════════
# DUAL-REPLAY DETERMINISM
# ══════════════════════════════════════════════════════════════════════════


class TestDualReplay:
    def test_replay_a_equals_replay_b(self, tmp_path):
        raw_root = tmp_path / "raw"
        with RawStore(raw_root, "v1") as store:
            for i in range(5):
                store.append(
                    SOURCE_MARKET_WS, "c", {"i": i}, wire=f'{{"i":{i}}}',
                    received_at_ns=1_700_000_000_000_000_000 + i,
                )
        result = run_dual_replay(raw_root)
        assert result.deterministic is True
        assert result.hash_a == result.hash_b
        assert result.records_a == 5
        assert result.records_b == 5

    def test_replay_detects_mutation(self, tmp_path):
        raw_root = tmp_path / "raw"
        with RawStore(raw_root, "v1") as store:
            store.append(SOURCE_MARKET_WS, "c", {"i": 1}, wire='{"i":1}')
        scan_before = RawStore(raw_root).scan()
        assert scan_before["partial_lines"] == 0
        # Append a forged/truncated line: must be detected as a partial record.
        import gzip as _gz
        target = list(raw_root.rglob("*.jsonl.gz"))[0]
        with _gz.open(target, "rb") as fh:
            content = fh.read()
        with _gz.open(target, "wb") as fh:
            fh.write(content)
            fh.write(b'{"canonical_json":"partial')
        scan_after = RawStore(raw_root).scan()
        assert scan_after["partial_lines"] == 1
        assert scan_after["valid"] == scan_before["valid"]  # valid records intact
        # A truncated trailing line is quarantined, not silently replayed.
        assert RawStore(raw_root).count() == 1


# ══════════════════════════════════════════════════════════════════════════
# BURN-IN REPORT
# ══════════════════════════════════════════════════════════════════════════


def _burnin_report(**overrides):
    base = dict(
        period_start="2026-09-15", period_end="2026-09-16",
        baseline_commit="abc123", implementation_commit="e2b072a",
        research_logic_sha256="r" * 64, collector_sha256="c" * 64,
        interface_sha256="i" * 64,
        messages=1_000_000, markets=500, tokens_observed=1000,
        lifecycle_events=50, resolved_markets=20, reconnects=7,
        forced_failures=10,
        hash_mismatches=0, unexplained_partial_lines=0,
        manifest_chain_failures=0, wire_fidelity_failures=0,
        replay_hash_a="a" * 64, replay_hash_b="a" * 64,
        replay_deterministic=True,
        rest_reconciliations=1000, rest_matching=999,
        rest_corrected_mismatches=1, rest_unresolved_mismatches=0,
        stale_delta_applications=0,
        receive_lag_p50=82.0, receive_lag_p95=241.0, receive_lag_p99=618.0,
        processing_lag_p50=1.0, processing_lag_p95=5.0, processing_lag_p99=20.0,
        clock_anomalies=0,
        metadata_reconstruction_ok=True, resolution_lifecycle_ok=True,
        crash_recovery_ok=True, gate_passed=True,
        phase_machine_violations=0, intent_ledger_duplicates=0,
        kill_switch_bypasses=0, approval_boundary_bypasses=0,
        venue_transmissions_attempted=0,
        fault_injection={
            "network_disconnect": True, "hard_kill": True,
            "partial_jsonl": True, "rest_429": True,
        },
        live_ready={
            "intent_restart_recovery": True, "approval_restart_recovery": True,
            "toctou_revalidation": True, "risk_ownership": True,
        },
    )
    base.update(overrides)
    return BurnInReport(**base)


class TestBurnInReport:
    def test_write_and_hash(self, tmp_path):
        report = _burnin_report()
        directory, digest = write_burn_in_report(report, tmp_path / "burnin")
        assert (tmp_path / "burnin" / "burnin-report.json").exists()
        assert (tmp_path / "burnin" / "burnin-report.md").exists()
        assert len(digest) == 64
        # Hash is stable.
        d2, digest2 = write_burn_in_report(report, tmp_path / "burnin2")
        assert digest == digest2

    def test_report_markdown_content(self, tmp_path):
        report = _burnin_report()
        _, digest = write_burn_in_report(report, tmp_path / "burnin")
        md = (tmp_path / "burnin" / "burnin-report.md").read_text(encoding="utf-8")
        assert "BURN-IN INTEGRITY REPORT" in md
        assert "FINAL GATE" in md
        assert "Gate: PASS" in md
        assert "1,000,000" in md
        assert "Venue transmissions attempted: 0" in md
        assert "intent_restart_recovery: PASS" in md