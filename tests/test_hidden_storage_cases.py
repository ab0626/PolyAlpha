"""Part 55 — Storage hidden tests.

Duplicate primary key, invalid JSON, NULL where unexpected, Unicode market
questions, very long market question, emoji, non-ASCII event names,
empty/large batch insert.
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from polyalpha.storage import Record, Store

TZ = timezone.utc
TS = datetime(2025, 6, 1, tzinfo=TZ)


# ── Duplicate primary key ────────────────────────────────────────────────


class TestDuplicatePrimaryKey:
    def test_duplicate_primary_key_rejected(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            store.append("book", "t1", TS, {"price": 0.5})
            with pytest.raises(Exception):
                # Attempting to insert with same auto-increment id would fail
                # but Store uses autoincrement, so test different approach
                store.connection.execute(
                    "INSERT INTO receipts(id,kind,entity_id,received_at,payload,sha256) VALUES(1,?,?,?,?,?)",
                    ("book", "t1", "2025-06-01T00:00:00+00:00", '{"price":0.6}', "hash"),
                )


# ── Invalid JSON ─────────────────────────────────────────────────────────


class TestInvalidJSON:
    def test_nan_in_json_rejected(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            with pytest.raises(ValueError, match="Out of range"):
                store.append("book", "t1", TS, {"value": float("nan")})

    def test_inf_in_json_rejected(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            with pytest.raises(ValueError, match="Out of range"):
                store.append("book", "t1", TS, {"value": float("inf")})


# ── NULL handling ────────────────────────────────────────────────────────


class TestNullHandling:
    def test_source_at_none_accepted(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            row_id = store.append("market", "m1", TS, {"question": "Q?"}, source_at=None)
            assert row_id > 0

    def test_source_at_present_accepted(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            row_id = store.append("market", "m1", TS, {"question": "Q?"}, source_at=TS)
            assert row_id > 0


# ── Unicode market questions ─────────────────────────────────────────────


class TestUnicodeMarketQuestions:
    def test_unicode_question_stored(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            row_id = store.append(
                "market", "m1", TS,
                {"question": "Will the election win?"}
            )
            records = list(store.replay(TS + timedelta(hours=1), "market"))
            assert len(records) == 1
            assert records[0].payload["question"] == "Will the election win?"

    def test_cjk_characters(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            row_id = store.append(
                "market", "m1", TS,
                {"question": "?"}
            )
            records = list(store.replay(TS + timedelta(hours=1), "market"))
            assert records[0].payload["question"] == "?"

    def test_arabic_characters(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            row_id = store.append(
                "market", "m1", TS,
                {"question": " "}
            )
            records = list(store.replay(TS + timedelta(hours=1), "market"))
            assert " " in records[0].payload["question"]


# ── Very long market question ────────────────────────────────────────────


class TestLongMarketQuestion:
    def test_very_long_question_stored(self, tmp_path):
        db = tmp_path / "test.db"
        long_q = "A" * 10000
        with Store(str(db)) as store:
            row_id = store.append(
                "market", "m1", TS,
                {"question": long_q}
            )
            records = list(store.replay(TS + timedelta(hours=1), "market"))
            assert len(records[0].payload["question"]) == 10000


# ── Emoji ────────────────────────────────────────────────────────────────


class TestEmoji:
    def test_emoji_in_payload(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            row_id = store.append(
                "market", "m1", TS,
                {"question": "Will it rain?"}
            )
            records = list(store.replay(TS + timedelta(hours=1), "market"))
            assert records[0].payload["question"] == "Will it rain?"


# ── Non-ASCII event names ───────────────────────────────────────────────


class TestNonASCIINames:
    def test_non_ascii_event_name(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            row_id = store.append(
                "market", "m1", TS,
                {"event_name": "Election 2025"}
            )
            records = list(store.replay(TS + timedelta(hours=1), "market"))
            assert records[0].payload["event_name"] == "Election 2025"


# ── Empty batch insert ───────────────────────────────────────────────────


class TestEmptyBatch:
    def test_no_records_replay_empty(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            records = list(store.replay(TS + timedelta(hours=1)))
            assert len(records) == 0


# ── Large batch insert ───────────────────────────────────────────────────


class TestLargeBatch:
    def test_large_batch_insert(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            for i in range(1000):
                store.append("book", f"t{i}", TS, {"price": 0.5})
            records = list(store.replay(TS + timedelta(hours=1), "book"))
            assert len(records) == 1000

    def test_large_batch_performance(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            for i in range(500):
                store.append("book", f"t{i}", TS, {"price": 0.5, "size": i})
            records = list(store.replay(TS + timedelta(hours=1), "book"))
            assert len(records) == 500


# ── Replay filtering ─────────────────────────────────────────────────────


class TestReplayFiltering:
    def test_replay_by_kind(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            store.append("book", "t1", TS, {"price": 0.5})
            store.append("market", "m1", TS, {"question": "Q?"})
            books = list(store.replay(TS + timedelta(hours=1), "book"))
            assert len(books) == 1
            assert books[0].kind == "book"

    def test_replay_as_of_cutoff(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            store.append("book", "t1", TS, {"price": 0.5})
            store.append("book", "t1", TS + timedelta(hours=2), {"price": 0.6})
            records = list(store.replay(TS + timedelta(hours=1), "book"))
            assert len(records) == 1


# ── Latest record ────────────────────────────────────────────────────────


class TestLatestRecord:
    def test_latest_returns_most_recent(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            store.append("book", "t1", TS, {"price": 0.5}, source_at=TS)
            store.append("book", "t1", TS + timedelta(hours=1), {"price": 0.6}, source_at=TS + timedelta(hours=1))
            latest = store.latest("book", "t1", TS + timedelta(hours=2))
            assert latest.payload["price"] == 0.6

    def test_latest_returns_none_for_empty(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            latest = store.latest("book", "t1", TS)
            assert latest is None


# ── Store context manager ────────────────────────────────────────────────


class TestStoreContextManager:
    def test_context_manager_closes(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            store.append("book", "t1", TS, {"price": 0.5})
        # After context manager, connection should be closed
        with Store(str(db)) as store:
            records = list(store.replay(TS + timedelta(hours=1)))
            assert len(records) == 1


# ── Memory store ─────────────────────────────────────────────────────────


class TestMemoryStore:
    def test_in_memory_store(self):
        with Store(":memory:") as store:
            store.append("book", "t1", TS, {"price": 0.5})
            records = list(store.replay(TS + timedelta(hours=1)))
            assert len(records) == 1


# ── Schema triggers ──────────────────────────────────────────────────────


class TestSchemaTriggers:
    def test_update_rejected(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            store.append("book", "t1", TS, {"price": 0.5})
            with pytest.raises(Exception):
                store.connection.execute(
                    "UPDATE receipts SET payload=? WHERE id=1",
                    ('{"price": 0.6}',)
                )

    def test_delete_rejected(self, tmp_path):
        db = tmp_path / "test.db"
        with Store(str(db)) as store:
            store.append("book", "t1", TS, {"price": 0.5})
            with pytest.raises(Exception):
                store.connection.execute("DELETE FROM receipts WHERE id=1")
