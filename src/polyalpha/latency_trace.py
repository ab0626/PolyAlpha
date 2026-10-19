"""Latency trace capture — non-authoritative, off-loop, monotonic-clock deltas.

Stores RAW stage timestamps only (``stages_ns`` keyed by stage label); all
latency deltas are computed OFFLINE by ``scripts/report_latency.py`` so a later
formula change can never invalidate captured data. Internal deltas use
``time.monotonic_ns()`` exclusively; wall/venue timestamps are carried only for
cross-log correlation and are never mixed with monotonic clocks.

Telemetry is deliberately NOT authoritative: a full telemetry queue increments
a dropped counter and keeps production running (the profiler must never become a
trading-system failure). Reports mark the window ``LATENCY_MEASUREMENT_INCOMPLETE``
when ``records_dropped > 0``.
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SCHEMA = "latency-trace-v1"


@dataclass(frozen=True, slots=True)
class LatencyTrace:
    schema: str
    family: str
    source: str
    session_id: str | None
    seq: int | None
    message_type: str | None
    trace_id: str
    stages_ns: dict[str, int]
    wall_received_at_ns: int | None = None
    venue_timestamp_ns: int | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class _TraceWriter:
    """Bounded off-loop writer. Drops are counted; never raises, never fatal."""

    def __init__(self, root: str | Path, *, queue_capacity: int = 100_000,
                 batch_size: int = 256, batch_age_seconds: float = 1.0) -> None:
        self.root = Path(root)
        self.batch_size = batch_size
        self.batch_age_seconds = batch_age_seconds
        self._queue: "queue.Queue[LatencyTrace]" = queue.Queue(maxsize=queue_capacity)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.records_written = 0
        self.records_dropped = 0
        self._high_water = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="latency-trace-writer", daemon=True)
        self._thread.start()

    def try_submit(self, rec: LatencyTrace) -> bool:
        try:
            self._queue.put_nowait(rec)
        except queue.Full:
            with self._lock:
                self.records_dropped += 1
            return False
        with self._lock:
            self._high_water = max(self._high_water, self._queue.qsize())
        return True

    def _run(self) -> None:
        batch: list[LatencyTrace] = []
        while True:
            try:
                rec = self._queue.get(timeout=self.batch_age_seconds)
            except queue.Empty:
                if batch:
                    self._write(batch)
                    batch = []
                if self._stop.is_set():
                    break
                continue
            batch.append(rec)
            if len(batch) >= self.batch_size:
                self._write(batch)
                batch = []

    def _write(self, batch: list[LatencyTrace]) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self.root / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for rec in batch:
                fh.write(json.dumps(rec.to_dict(), sort_keys=True, default=str) + "\n")
            fh.flush()
        with self._lock:
            self.records_written += len(batch)

    def health(self) -> dict:
        with self._lock:
            return {
                "records_written": self.records_written,
                "records_dropped": self.records_dropped,
                "queue_high_water": self._high_water,
                "queue_depth": self._queue.qsize(),
            }

    def close(self, drain: bool = True) -> None:
        self._stop.set()
        if self._thread is not None and drain:
            self._thread.join(timeout=10.0)


class LatencyTracer:
    """Emit raw stage timestamps through a non-authoritative off-loop writer."""

    def __init__(self, root: str | Path = "data/latency", **kw) -> None:
        self.writer = _TraceWriter(root, **kw)

    def start(self) -> None:
        self.writer.start()

    def trace(
        self,
        family: str,
        source: str,
        stages_ns: dict[str, int],
        *,
        session_id: str | None = None,
        seq: int | None = None,
        message_type: str | None = None,
        wall_received_at_ns: int | None = None,
        venue_timestamp_ns: int | None = None,
        metadata: dict | None = None,
    ) -> None:
        rec = LatencyTrace(
            schema=SCHEMA,
            family=family,
            source=source,
            session_id=session_id,
            seq=seq,
            message_type=message_type,
            trace_id=uuid4().hex,
            stages_ns=dict(stages_ns),
            wall_received_at_ns=wall_received_at_ns,
            venue_timestamp_ns=venue_timestamp_ns,
            metadata=dict(metadata or {}),
        )
        self.writer.try_submit(rec)

    def health(self) -> dict:
        return self.writer.health()

    def close(self, drain: bool = True) -> None:
        self.writer.close(drain=drain)