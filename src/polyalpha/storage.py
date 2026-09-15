"""Append-only receipt log. Replay is bounded by knowledge time AND source time."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .domain import utc


def timestamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Record:
    id: int
    kind: str
    entity_id: str
    received_at: datetime
    source_at: datetime | None
    payload: Any


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
                received_at TEXT NOT NULL, source_at TEXT, payload TEXT NOT NULL,
                sha256 TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS receipt_lookup
                ON receipts(kind, entity_id, received_at, id);
            CREATE TRIGGER IF NOT EXISTS receipts_no_update BEFORE UPDATE ON receipts
                BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS receipts_no_delete BEFORE DELETE ON receipts
                BEGIN SELECT RAISE(ABORT, 'append-only'); END;
        """)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def append(
        self,
        kind: str,
        entity_id: str,
        received_at: datetime,
        payload: Any,
        source_at: datetime | None = None,
    ) -> int:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO receipts(kind,entity_id,received_at,"
                "source_at,payload,sha256) VALUES(?,?,?,?,?,?)",
                (
                    kind,
                    entity_id,
                    timestamp(received_at),
                    timestamp(source_at) if source_at else None,
                    encoded,
                    hashlib.sha256(encoded.encode()).hexdigest(),
                ),
            )
        return cursor.lastrowid

    def replay(self, as_of: datetime, kind: str | None = None) -> Iterator[Record]:
        cutoff = timestamp(as_of)
        rows = self.connection.execute(
            "SELECT id,kind,entity_id,received_at,source_at,payload FROM receipts "
            "WHERE received_at<=? AND (source_at IS NULL OR source_at<=?) "
            "AND (? IS NULL OR kind=?) ORDER BY received_at,id",
            (cutoff, cutoff, kind, kind),
        )
        for row in rows:
            yield Record(
                row[0],
                row[1],
                row[2],
                datetime.fromisoformat(row[3]),
                datetime.fromisoformat(row[4]) if row[4] else None,
                json.loads(row[5]),
            )

    def latest(self, kind: str, entity_id: str, as_of: datetime) -> Record | None:
        # Source-time order avoids replacing a newer book with a late older snapshot.
        candidates = (r for r in self.replay(as_of, kind) if r.entity_id == entity_id)
        return max(
            candidates,
            key=lambda r: (r.source_at or r.received_at, r.received_at, r.id),
            default=None,
        )
