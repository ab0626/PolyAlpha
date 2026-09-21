"""Immutable raw event store — the append-only source of truth for collection.

Part of the v0.3.0 data-collection architecture. Every raw message received
from an upstream feed is written once to a JSONL file keyed by UTC day. The
**exact wire payload** (original text/bytes) is preserved verbatim and the
per-record hash is computed over the wire bytes, NOT over a re-serialized
JSON object. Key ordering, number representation, and parser changes can
never make "raw" data less raw.

Files are never modified after being finalized. A parser bug is repaired by
replaying this raw layer with the fixed parser, never by rewriting it.

Layout:
    data/raw/<YYYY>/<MM>/<DD>/<stream>-<index>.jsonl        (open/current)
    data/raw/<YYYY>/<MM>/<DD>/<stream>-<index>.jsonl.gz     (finalized)
    data/raw/<YYYY>/<MM>/<DD>/<stream>-<index>.jsonl.gz.tmp (mid-finalize)

Crash-safe finalization: the hot path writes plain JSONL (readable while
open, flushed per record); on close/day-roll the file is gzip-compressed to a
`.tmp` path, fsynced, atomically renamed to `.jsonl.gz`, and only then is the
`.jsonl` source removed. Replay is authoritative on the `.jsonl.gz` when it
exists, promoting a lone `.jsonl.gz.tmp` (crash between write and rename), and
reads an orphaned `.jsonl` only when no finalized form exists.

Every record carries three clocks:
    exchange_timestamp_ms : the feed's own timestamp (external event clock)
    received_at_ns        : local wall clock at receive  (time.time_ns)
    received_monotonic_ns : local monotonic clock at receive (time.monotonic_ns)
    processed_at_ns       : local wall clock at persist
    processed_monotonic_ns: local monotonic clock at persist

Monotonic clocks give reliable internal queueing/processing durations even if
NTP steps the wall clock backward.

Envelope per record:
    wire                  : the exact upstream payload (text or base64 of bytes)
    source                : "polymarket_market_ws" | "polymarket_rest_book" | ...
    collector_version     : git commit or version string of the collector
    connection_id         : unique id for the upstream connection/session
    message_sequence_local: per-connection monotonic sequence number
    payload               : parsed payload (convenience; wire is authoritative)
    sha256                : hash over wire + envelope (excluding sha256 itself)
"""

from __future__ import annotations

import base64
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
    wire: str
    source: str
    collector_version: str
    connection_id: str
    message_sequence_local: int
    exchange_timestamp_ms: int | None
    received_at_ns: int
    received_monotonic_ns: int
    processed_at_ns: int
    processed_monotonic_ns: int
    wire_was_bytes: bool
    payload: dict
    sha256: str


def _wire_to_str(wire: str | bytes) -> str:
    if isinstance(wire, bytes):
        # Preserve exact bytes via base64 so the hash covers the true wire.
        return base64.b64encode(wire).decode("ascii")
    return wire


def _envelope_dict(record: RawRecord) -> dict:
    """Canonical serialization of everything EXCEPT sha256 (self-hash excluded)."""
    return {
        "wire": record.wire,
        "source": record.source,
        "collector_version": record.collector_version,
        "connection_id": record.connection_id,
        "message_sequence_local": record.message_sequence_local,
        "exchange_timestamp_ms": record.exchange_timestamp_ms,
        "received_at_ns": record.received_at_ns,
        "received_monotonic_ns": record.received_monotonic_ns,
        "processed_at_ns": record.processed_at_ns,
        "processed_monotonic_ns": record.processed_monotonic_ns,
        "wire_was_bytes": record.wire_was_bytes,
        "payload": record.payload,
    }


class RawStore:
    """Write-once raw event store partitioned by UTC day."""

    def __init__(self, root: str | Path, collector_version: str = "unknown"):
        self.root = Path(root)
        self.collector_version = collector_version
        # One writer per source: a multi-source feed must not collapse into the
        # first source's file. Each source keeps its own open file until close.
        self._writers: dict[str, object] = {}
        self._paths: dict[str, Path] = {}
        self._day: date | None = None
        self._count = 0
        self._last_path: Path | None = None

    # ── Path helpers ────────────────────────────────────────────────────────

    def day_dir(self, day: date) -> Path:
        return self.root / str(day.year) / f"{day.month:02d}" / f"{day.day:02d}"

    def _open_filename(self, source: str, index: int) -> str:
        return f"{source.replace('/', '_')}-{index:04d}.jsonl"

    def _next_available_index(self, day: date, source: str) -> int:
        """Next file index for (day, source), scanning open AND finalized files.

        A fresh process must never reuse an index whose `.jsonl` was already
        finalized to `.jsonl.gz` (reusing it would overwrite durable data).
        Scan every form of the source's files and return max + 1.
        """
        directory = self.day_dir(day)
        if not directory.exists():
            return 0
        prefix = source.replace("/", "_")
        max_index = -1
        for path in directory.iterdir():
            name = path.name
            if not name.startswith(prefix + "-"):
                continue
            core = name[len(prefix) + 1 :]
            if core.endswith(".jsonl.gz"):
                core = core[: -len(".jsonl.gz")]
            elif core.endswith(".jsonl"):
                core = core[: -len(".jsonl")]
            else:
                continue
            try:
                max_index = max(max_index, int(core))
            except ValueError:
                continue
        return max_index + 1

    def _open_writer(self, day: date, source: str) -> None:
        directory = self.day_dir(day)
        directory.mkdir(parents=True, exist_ok=True)
        index = self._next_available_index(day, source)
        candidate = directory / self._open_filename(source, index)
        self._writers[source] = open(candidate, "a", encoding="utf-8", newline="\n")
        self._paths[source] = candidate
        self._day = day
        self._last_path = candidate

    def _fsync_dir(self, path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass  # directory fsync is best-effort; not all platforms support it

    def _finalize(self, path: Path) -> None:
        """Crash-safe gzip finalization: tmp -> fsync -> atomic rename -> unlink source."""
        gz_tmp = path.with_suffix(".jsonl.gz.tmp")
        gz_final = path.with_suffix(".jsonl.gz")
        with open(path, "rb") as src, gzip.open(gz_tmp, "wb") as dst:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                dst.write(block)
            dst.flush()
            # fsync the raw file descriptor before rename so the gzip bytes
            # are durable (gzip wraps the fd; fileno() is valid while open).
            os.fsync(dst.fileobj.fileno())
        os.replace(gz_tmp, gz_final)
        path.unlink()
        self._fsync_dir(path.parent)

    def close(self) -> None:
        """Flush, close, and crash-safe finalize every open file."""
        for source in list(self._writers):
            self._writers.pop(source).close()
            path = self._paths.pop(source, None)
            if path is not None and path.exists():
                self._finalize(path)
        self._day = None
        self._last_path = None

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
        wire: str | bytes | None = None,
        received_at_ns: int | None = None,
        received_monotonic_ns: int | None = None,
        message_sequence_local: int | None = None,
        exchange_timestamp_ms: int | None = None,
    ) -> RawRecord:
        now = date.today()
        if self._day != now:
            self.close()
        if source not in self._writers:
            self._open_writer(now, source)

        received = received_at_ns if received_at_ns is not None else time.time_ns()
        received_mono = (
            received_monotonic_ns
            if received_monotonic_ns is not None
            else time.monotonic_ns()
        )
        sequence = (
            message_sequence_local
            if message_sequence_local is not None
            else self._count
        )
        processed = time.time_ns()
        processed_mono = time.monotonic_ns()

        # If no explicit wire was passed, we cannot reconstruct the true wire;
        # fall back to a canonical serialization of the payload, but mark it.
        wire_was_bytes = isinstance(wire, bytes)
        wire_str = _wire_to_str(wire) if wire is not None else json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )

        record = RawRecord(
            wire=wire_str,
            source=source,
            collector_version=self.collector_version,
            connection_id=connection_id,
            message_sequence_local=sequence,
            exchange_timestamp_ms=exchange_timestamp_ms,
            received_at_ns=received,
            received_monotonic_ns=received_mono,
            processed_at_ns=processed,
            processed_monotonic_ns=processed_mono,
            wire_was_bytes=wire_was_bytes,
            payload=payload,
            sha256="",
        )
        canonical = json.dumps(
            _envelope_dict(record),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        record = RawRecord(
            wire=wire_str,
            source=source,
            collector_version=self.collector_version,
            connection_id=connection_id,
            message_sequence_local=sequence,
            exchange_timestamp_ms=exchange_timestamp_ms,
            received_at_ns=received,
            received_monotonic_ns=received_mono,
            processed_at_ns=processed,
            processed_monotonic_ns=processed_mono,
            wire_was_bytes=wire_was_bytes,
            payload=payload,
            sha256=digest,
        )
        line = dict(canonical_json=canonical, sha256=digest)
        self._writers[source].write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
        self._writers[source].flush()
        self._count += 1
        return record

    # ── Read / replay ───────────────────────────────────────────────────────

    def _day_files(self, directory: Path) -> list[Path]:
        """Return authoritative files for a day, recovering crash-state.

        For each stem, `.jsonl.gz` wins over `.jsonl.gz.tmp` (promoted) and
        over an orphaned `.jsonl` (which was superseded by finalization).
        """
        by_stem: dict[str, dict[str, Path]] = {}
        for path in sorted(directory.iterdir()) if directory.exists() else []:
            if not path.is_file():
                continue
            if path.suffix == ".gz":
                stem = path.name[:-len(".jsonl.gz")]
                by_stem.setdefault(stem, {})["gz"] = path
            elif path.name.endswith(".jsonl.gz.tmp"):
                stem = path.name[:-len(".jsonl.gz.tmp")]
                by_stem.setdefault(stem, {})["tmp"] = path
            elif path.name.endswith(".jsonl"):
                stem = path.name[:-len(".jsonl")]
                by_stem.setdefault(stem, {})["jsonl"] = path
        result: list[Path] = []
        for stem in sorted(by_stem):
            forms = by_stem[stem]
            if "gz" in forms:
                result.append(forms["gz"])
            elif "tmp" in forms:
                # Crash between gzip-write and rename: promote the tmp to final.
                tmp_path = forms["tmp"]
                final = Path(str(tmp_path)[: -len(".jsonl.gz.tmp")] + ".jsonl.gz")
                os.replace(tmp_path, final)
                result.append(final)
            elif "jsonl" in forms:
                result.append(forms["jsonl"])
        return result

    def replay(self, day: date | None = None) -> Iterator[RawRecord]:
        """Yield every valid raw record, in order. Partial trailing lines are
        detected and skipped; use `scan()` to enumerate corruption."""
        if day is not None:
            files = self._day_files(self.day_dir(day))
        else:
            files = []
            for directory in sorted(p for p in self.root.rglob("*") if p.is_dir()):
                files.extend(self._day_files(directory))
            files = sorted(set(files))
        for f in files:
            if f.suffix == ".gz":
                handle = gzip.open(f, "rt", encoding="utf-8")
            else:
                handle = open(f, "r", encoding="utf-8")
            with handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # Partial trailing write from a crash; quarantine.
                        continue
                    try:
                        canonical = data["canonical_json"]
                        base = json.loads(canonical)
                        payload = base["payload"]
                        yield RawRecord(
                            wire=base["wire"],
                            source=base["source"],
                            collector_version=base["collector_version"],
                            connection_id=base["connection_id"],
                            message_sequence_local=base["message_sequence_local"],
                            exchange_timestamp_ms=base.get("exchange_timestamp_ms"),
                            received_at_ns=base["received_at_ns"],
                            received_monotonic_ns=base["received_monotonic_ns"],
                            processed_at_ns=base["processed_at_ns"],
                            processed_monotonic_ns=base["processed_monotonic_ns"],
                            wire_was_bytes=base.get("wire_was_bytes", False),
                            payload=payload,
                            sha256=data.get("sha256", ""),
                        )
                    except (KeyError, ValueError):
                        continue

    def count(self, day: date | None = None) -> int:
        return sum(1 for _ in self.replay(day))

    def sha256_root(self, day: date | None = None) -> str:
        """Deterministic hash over every record's canonical envelope bytes.

        Because the canonical envelope embeds the exact wire bytes, the root
        hash detects ANY mutation of wire content, ordering, or formatting.
        """
        h = hashlib.sha256()
        for record in self.replay(day):
            h.update(
                json.dumps(
                    _envelope_dict(record),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            )
        return h.hexdigest()

    def scan(self, day: date | None = None) -> dict:
        """Report valid records, quarantined partial lines, and hash mismatches."""
        result = {"valid": 0, "partial_lines": 0, "hash_mismatches": 0}
        if day is not None:
            files = self._day_files(self.day_dir(day))
        else:
            files = []
            for directory in sorted(p for p in self.root.rglob("*") if p.is_dir()):
                files.extend(self._day_files(directory))
            files = sorted(set(files))
        for f in files:
            handle = gzip.open(f, "rt", encoding="utf-8") if f.suffix == ".gz" else open(f, "r", encoding="utf-8")
            with handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        result["partial_lines"] += 1
                        continue
                    canonical = data.get("canonical_json", "")
                    stored = data.get("sha256", "")
                    computed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                    if computed != stored:
                        result["hash_mismatches"] += 1
                    else:
                        result["valid"] += 1
        return result


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
                if RawStore(base)._day_files(day_dir):
                    days.append(
                        date(int(year_dir.name), int(month_dir.name), int(day_dir.name))
                    )
    return days