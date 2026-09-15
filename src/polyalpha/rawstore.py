"""Immutable raw event store — the append-only source of truth for collection.

Part of the v0.3.0 data-collection architecture. Every raw message received
from an upstream feed (WebSocket market stream, REST responses) is written
once, to a gzip-compressed JSONL file keyed by UTC day, wrapped in an
envelope that records *when and from where* it arrived. Files are never
modified after being closed; a parser bug is repaired by replaying this raw
layer with the fixed parser, never by rewriting it.

Layout:
    data/raw/<YYYY>/<MM>/<DD>/<stream>-<index>.jsonl        (open/current file)
    data/raw/<YYYY>/<MM>/<DD>/<stream>-<index>.jsonl.gz     (finalized file)

The hot path writes plain JSONL so the file remains readable (and crash-safe)
while it is open; when a file is finalized (day rollover or explicit close) it
is gzip-compressed in place and removed. Replay reads finalized .gz files and
any still-open .jsonl files, oldest first.

Envelope per record:
    received_at_ns        : local clock, nanoseconds since epoch (time.time_ns)
    source                : "polymarket_market_ws" | "polymarket_rest_book" | ...
    collector_version     : git commit or version string of the collector
    connection_id         : unique id for the upstream connection/session
    message_sequence_local: per-connection monotonic sequence number
    processed_at_ns       : local clock when this record was persisted
    payload               : the raw upstream payload, unmodified
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

SOURCE_MARKET_WS = "polymarket_market_ws"
SOURCE_REST_BOOK = "polymarket_rest_book"
SOURCE_REST_MARKETS = "polymarket_rest_markets"


@dataclass(frozen=True)
class RawRecord:
    received_at_ns: int
    source: str
    collector_version: str
    connection_id: str
    message_sequence_local: int
    processed_at_ns: int
    payload: dict
    sha256: str


class RawStore:
    """Write-once raw event store partitioned by UTC day and gzip-compressed."""

    def __init__(self, root: str | Path, collector_version: str = "unknown"):
        self.root = Path(root)
        self.collector_version = collector_version
        self._writer = None
        self._day: date | None = None
        self._index = 0
        self._count = 0
        self._last_path: Path | None = None

    # ── Path helpers ────────────────────────────────────────────────────────

    def day_dir(self, day: date) -> Path:
        return self.root / str(day.year) / f"{day.month:02d}" / f"{day.day:02d}"

    def _open_filename(self, source: str, index: int) -> str:
        return f"{source.replace('/', '_')}-{index:04d}.jsonl"

    def _open_writer(self, day: date, source: str) -> None:
        directory = self.day_dir(day)
        directory.mkdir(parents=True, exist_ok=True)
        while True:
            candidate = directory / self._open_filename(source, self._index)
            if not candidate.exists():
                break
            self._index += 1
        self._writer = open(candidate, "a", encoding="utf-8", newline="\n")
        self._day = day
        self._last_path = candidate

    def close(self) -> None:
        """Flush, close, and gzip-compress the current file. Finalized files are immutable."""
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            path = self._last_path
            if path is not None and path.exists():
                self._compress(path)
        self._day = None
        self._last_path = None

    def _compress(self, path: Path) -> None:
        gz_path = path.with_suffix(".jsonl.gz")
        with open(path, "rb") as src, gzip.open(gz_path, "wb") as dst:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                dst.write(block)
        path.unlink()

    def __enter__(self) -> "RawStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def last_path(self) -> Path | None:
        return self._last_path

    # ── Write ───────────────────────────────────────────────────────────────

    def append(
        self,
        source: str,
        connection_id: str,
        payload: dict,
        received_at_ns: int | None = None,
        message_sequence_local: int | None = None,
    ) -> RawRecord:
        now = date.today()
        # Roll the file on UTC-day change.
        if self._day != now:
            self.close()
        if self._writer is None:
            self._open_writer(now, source)

        received = received_at_ns if received_at_ns is not None else time.time_ns()
        sequence = (
            message_sequence_local
            if message_sequence_local is not None
            else self._count
        )
        record = RawRecord(
            received_at_ns=received,
            source=source,
            collector_version=self.collector_version,
            connection_id=connection_id,
            message_sequence_local=sequence,
            processed_at_ns=time.time_ns(),
            payload=payload,
            sha256="",
        )
        canonical = json.dumps(
            {
                "received_at_ns": record.received_at_ns,
                "source": record.source,
                "collector_version": record.collector_version,
                "connection_id": record.connection_id,
                "message_sequence_local": record.message_sequence_local,
                "processed_at_ns": record.processed_at_ns,
                "payload": record.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        record = RawRecord(
            received_at_ns=record.received_at_ns,
            source=record.source,
            collector_version=record.collector_version,
            connection_id=record.connection_id,
            message_sequence_local=record.message_sequence_local,
            processed_at_ns=record.processed_at_ns,
            payload=record.payload,
            sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        self._writer.write(canonical + "\n")
        # Flush so a crash cannot silently lose the most recent records and so
        # replay-while-running observes appended data.
        self._writer.flush()
        self._count += 1
        return record

    # ── Read / replay ───────────────────────────────────────────────────────

    def replay(self, day: date | None = None) -> Iterator[RawRecord]:
        """Yield every raw record, optionally for one UTC day, in order.

        Deterministic: files are iterated in lexical order and records within
        each file in write order, so replay of an unchanged directory tree is
        byte-identical.
        """
        if day is not None:
            directory = self.day_dir(day)
            files = sorted(list(directory.glob("*.jsonl.gz")) + list(directory.glob("*.jsonl")))
        else:
            files = sorted(
                p
                for p in self.root.rglob("*.jsonl*")
                if p.is_file() and p.suffix in (".gz", ".jsonl")
            )
        for f in files:
            if f.suffix == ".gz":
                with gzip.open(f, "rt", encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        yield RawRecord(
                            received_at_ns=data["received_at_ns"],
                            source=data["source"],
                            collector_version=data["collector_version"],
                            connection_id=data["connection_id"],
                            message_sequence_local=data["message_sequence_local"],
                            processed_at_ns=data["processed_at_ns"],
                            payload=data["payload"],
                            sha256=data.get("sha256", ""),
                        )
            else:
                with open(f, "r", encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        yield RawRecord(
                            received_at_ns=data["received_at_ns"],
                            source=data["source"],
                            collector_version=data["collector_version"],
                            connection_id=data["connection_id"],
                            message_sequence_local=data["message_sequence_local"],
                            processed_at_ns=data["processed_at_ns"],
                            payload=data["payload"],
                            sha256=data.get("sha256", ""),
                        )

    def count(self, day: date | None = None) -> int:
        return sum(1 for _ in self.replay(day))

    def sha256_root(self, day: date | None = None) -> str:
        """Deterministic hash over every record's canonical bytes.

        Used to detect silent mutation of the raw layer (see daily manifests).
        """
        h = hashlib.sha256()
        for record in self.replay(day):
            h.update(
                json.dumps(
                    {
                        "received_at_ns": record.received_at_ns,
                        "source": record.source,
                        "collector_version": record.collector_version,
                        "connection_id": record.connection_id,
                        "message_sequence_local": record.message_sequence_local,
                        "processed_at_ns": record.processed_at_ns,
                        "payload": record.payload,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        return h.hexdigest()


def record_day(record: RawRecord) -> date:
    """UTC day for a raw record from its received_at_ns."""
    return datetime.fromtimestamp(record.received_at_ns / 1e9, UTC).date()


def list_days(root: str | Path) -> list[date]:
    """Return the UTC days present in a raw store, oldest first."""
    base = Path(root)
    days = []
    for year_dir in sorted(p for p in base.iterdir() if p.is_dir() and p.name.isdigit()):
        for month_dir in sorted(
            p for p in year_dir.iterdir() if p.is_dir() and p.name.isdigit()
        ):
            for day_dir in sorted(
                p for p in month_dir.iterdir() if p.is_dir() and p.name.isdigit()
            ):
                if list(day_dir.glob("*.jsonl*")):
                    days.append(
                        date(int(year_dir.name), int(month_dir.name), int(day_dir.name))
                    )
    return days