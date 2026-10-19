"""WebSocket transport frame (ws-frame-v2) + off-loop persistence writers.

The transport frame captures the exact wire + wall/monotonic clocks + real
session identity at ``recv()``, BEFORE decode/parse/dedupe/scope. Persistence is
decoupled from the socket event loop by dedicated WRITER THREADS with bounded,
non-blocking queues:

    recv -> capture_frame -> try_submit(frame)  [never blocks]
                              | full/fatal => DATA_GAP => fail closed

Writer failure is equivalent to overflow: a dead writer thread must surface as a
fatal producer-health flag promptly (bounded ``recv()`` timeout), never a quietly
dead writer while the market loop keeps trading.

Transport frames are partitioned by ``UTCDate(frame.received_at_ns)`` — never the
writer clock — so a 23:59:59.999Z frame written after midnight still lands in the
previous UTC-day file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = "ws-frame-v2"

GAP_QUEUE_OVERFLOW = "QUEUE_OVERFLOW"
GAP_WRITER_FAILURE = "WRITER_FAILURE"
GAP_DRAIN_TIMEOUT = "DRAIN_TIMEOUT"


class DataGapError(RuntimeError):
    """Raised when a transport/derived queue overflows or a writer dies.

    This propagates out of the collector loop so the process exits non-zero and
    the supervisor restarts into a fresh session — never a quiet reconnect.
    """


def _utc_day(received_at_ns: int) -> str:
    """UTC calendar day of a nanosecond receipt timestamp."""
    return datetime.fromtimestamp(received_at_ns / 1e9, UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class WsTransportFrame:
    schema_version: str
    frame_id: str
    source: str
    session_id: str
    session_seq: int
    received_at_ns: int
    received_monotonic_ns: int
    received_perf_ns: int
    wire_encoding: str  # "utf-8" | "base64"
    wire: str           # exact wire text (base64 when the source was bytes)
    wire_sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


class WsSession:
    """Real session identity + per-connection monotonic sequence."""

    def __init__(self) -> None:
        self._process = uuid4().hex
        self._conn = 0
        self.session_id = ""
        self.session_seq = 0

    def on_connect(self) -> str:
        self._conn += 1
        self.session_id = f"{self._process}-{self._conn}"
        self.session_seq = 1
        return self.session_id

    def take_seq(self) -> int:
        seq = self.session_seq
        self.session_seq += 1
        return seq


def capture_frame(
    source: str,
    session: WsSession,
    message: str | bytes,
    wall_ns: int | None = None,
    mono_ns: int | None = None,
    perf_ns: int | None = None,
) -> WsTransportFrame:
    """Create the immutable frame. Wall/mono/perf captured back-to-back here."""
    wall = wall_ns if wall_ns is not None else time.time_ns()
    mono = mono_ns if mono_ns is not None else time.monotonic_ns()
    perf = perf_ns if perf_ns is not None else time.perf_counter_ns()
    if isinstance(message, bytes):
        wire = base64.b64encode(message).decode("ascii")
        encoding = "base64"
        raw = message
    else:
        wire = message
        encoding = "utf-8"
        raw = message.encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    seq = session.take_seq()
    frame_id = hashlib.sha256(
        f"{session.session_id}|{seq}|{sha}".encode("utf-8")
    ).hexdigest()
    return WsTransportFrame(
        schema_version=SCHEMA_VERSION,
        frame_id=frame_id,
        source=source,
        session_id=session.session_id,
        session_seq=seq,
        received_at_ns=wall,
        received_monotonic_ns=mono,
        received_perf_ns=perf,
        wire_encoding=encoding,
        wire=wire,
        wire_sha256=sha,
    )


def frame_bytes(frame: WsTransportFrame) -> bytes:
    if frame.wire_encoding == "base64":
        return base64.b64decode(frame.wire)
    return frame.wire.encode("utf-8")


def frame_text(frame: WsTransportFrame) -> str:
    return frame_bytes(frame).decode("utf-8")


class WsFrameStore:
    """Append-only, day-partitioned JSONL store for transport frames.

    Day partition is by ``frame.received_at_ns`` (NOT the writer clock), so an
    async write after midnight never lands in the wrong UTC-day file.
    """

    def __init__(self, root: str | Path, source: str) -> None:
        self.root = Path(root)
        self.source = source
        self._fh = None
        self._day: str | None = None

    def _handle(self, day: str):
        if self._fh is not None and self._day != day:
            self._fh.flush()
            self._fh.close()
            self._fh = None
            self._day = None
        if self._fh is None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._fh = (self.root / f"{day}.jsonl").open("a", encoding="utf-8")
            self._day = day
        return self._fh

    def write(self, frame: WsTransportFrame) -> None:
        day = _utc_day(frame.received_at_ns)
        self._handle(day).write(json.dumps(frame.to_dict(), sort_keys=True) + "\n")

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def fsync(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            finally:
                self._fh.close()
                self._fh = None
                self._day = None

    def mark_cutover(self, at: datetime | None = None) -> str:
        at = at or datetime.now(UTC)
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / "CUTOVER.json"
        if not marker.exists():
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "source": self.source,
                        "cutover_at": at.isoformat(),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        return at.isoformat()

    # Convenience for tests / any remaining synchronous call sites.
    def append(self, frame: WsTransportFrame) -> None:
        self.write(frame)
        self.flush()


def write_transport_gap(store: WsFrameStore, gap: dict) -> None:
    """Best-effort synchronous append of a transport gap record."""
    path = store.root / "transport_gaps.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(gap, sort_keys=True) + "\n")
        fh.flush()


def fail_closed(store: WsFrameStore, writer, frame: WsTransportFrame | None, reason: str) -> None:
    """Record a transport gap and raise DataGapError (never reconnect quietly)."""
    gap = writer.gap_for(frame, reason)
    write_transport_gap(store, gap)
    raise DataGapError(f"{reason}: {json.dumps(gap, sort_keys=True)}")


class WsFrameWriter:
    """Dedicated writer thread with a bounded, non-blocking queue.

    ``try_submit`` never waits for disk: a full queue or a fatal writer is a
    DATA_GAP (the caller fails closed). Frames are batched and flushed on a size
    or age threshold, with fsync on a longer cadence and on orderly shutdown.
    """

    def __init__(
        self,
        store: WsFrameStore,
        *,
        queue_capacity: int = 100_000,
        batch_size: int = 256,
        batch_age_seconds: float = 1.0,
        fsync_seconds: float = 5.0,
        drain_timeout_seconds: float = 10.0,
        tracer=None,
    ) -> None:
        self.store = store
        self.tracer = tracer  # optional LatencyTracer (writer telemetry)
        self.queue_capacity = queue_capacity
        self.batch_size = batch_size
        self.batch_age_seconds = batch_age_seconds
        self.fsync_seconds = fsync_seconds
        self.drain_timeout_seconds = drain_timeout_seconds
        self._queue: "queue.Queue[WsTransportFrame]" = queue.Queue(maxsize=queue_capacity)
        self._stop = threading.Event()
        self._fatal = threading.Event()
        self._thread: threading.Thread | None = None
        self._fatal_error: BaseException | None = None
        self._lock = threading.Lock()
        self.frames_enqueued = 0
        self.frames_persisted = 0
        self.queue_overflows = 0
        self.writer_errors = 0
        self.last_enqueued_frame_id: str | None = None
        self.last_persisted_frame_id: str | None = None
        self.last_persisted_session_id: str | None = None
        self.last_persisted_session_seq: int | None = None
        self._queue_high_water = 0
        self._pending: deque[tuple[float, str]] = deque()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ws-frame-writer", daemon=True)
        self._thread.start()

    def try_submit(self, frame: WsTransportFrame) -> bool:
        """Non-blocking enqueue. False => DATA_GAP (full queue or writer dead)."""
        if self._fatal.is_set():
            with self._lock:
                self.queue_overflows += 1
            return False
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            with self._lock:
                self.queue_overflows += 1
            return False
        with self._lock:
            self.frames_enqueued += 1
            self.last_enqueued_frame_id = frame.frame_id
            self._queue_high_water = max(self._queue_high_water, self._queue.qsize())
            self._pending.append((time.perf_counter_ns(), frame.frame_id))
        return True

    def _write_batch(self, batch: list[WsTransportFrame]) -> None:
        w0 = self._pending[0][0] if self._pending else time.perf_counter_ns()
        w1 = time.perf_counter_ns()
        for frame in batch:
            self.store.write(frame)
        self.store.flush()
        w2 = time.perf_counter_ns()
        with self._lock:
            for _ in batch:
                if self._pending:
                    self._pending.popleft()
            self.frames_persisted += len(batch)
            if batch:
                self.last_persisted_frame_id = batch[-1].frame_id
                self.last_persisted_session_id = batch[-1].session_id
                self.last_persisted_session_seq = batch[-1].session_seq
        if self.tracer is not None and batch:
            oldest = batch[0]
            self.tracer.trace(
                "ws_writer", self.store.source,
                {"r0": oldest.received_perf_ns, "w0": w0, "w1": w1, "w2": w2},
                session_id=oldest.session_id, seq=oldest.session_seq,
                message_type="batch",
                metadata={"batch_size": len(batch)},
            )

    def _run(self) -> None:
        try:
            batch: list[WsTransportFrame] = []
            batch_started = time.monotonic()
            last_fsync = time.monotonic()
            while True:
                try:
                    frame = self._queue.get(timeout=self.batch_age_seconds)
                except queue.Empty:
                    if batch and (time.monotonic() - batch_started) >= self.batch_age_seconds:
                        self._write_batch(batch)
                        batch = []
                        batch_started = time.monotonic()
                    if self._stop.is_set():
                        break
                    continue
                batch.append(frame)
                if len(batch) >= self.batch_size:
                    self._write_batch(batch)
                    batch = []
                    batch_started = time.monotonic()
                if time.monotonic() - last_fsync >= self.fsync_seconds:
                    self.store.fsync()
                    last_fsync = time.monotonic()
                if self._stop.is_set() and self._queue.empty():
                    break
            if batch:
                self._write_batch(batch)
            self.store.fsync()
        except BaseException as exc:  # noqa: BLE001 — writer failure must surface as fatal
            with self._lock:
                self.writer_errors += 1
                self._fatal_error = exc
            self._fatal.set()

    def gap_for(self, frame: WsTransportFrame, reason: str) -> dict:
        h = self.health()
        return {
            "source": self.store.source,
            "reason": reason,
            "detected_at": datetime.now(UTC).isoformat(),
            "session_id": frame.session_id,
            "first_unpersisted_session_seq": frame.session_seq,
            "first_unpersisted_frame_id": frame.frame_id,
            "first_unpersisted_received_at_ns": frame.received_at_ns,
            "last_persisted_session_id": self.last_persisted_session_id,
            "last_persisted_session_seq": self.last_persisted_session_seq,
            "last_persisted_frame_id": self.last_persisted_frame_id,
            "queue_capacity": self.queue_capacity,
            "queue_depth": h["queue_depth"],
        }

    def health(self) -> dict:
        with self._lock:
            oldest_age = None
            if self._pending:
                oldest_age = int((time.perf_counter_ns() - self._pending[0][0]) / 1_000_000)
            return {
                "frames_enqueued": self.frames_enqueued,
                "frames_persisted": self.frames_persisted,
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self.queue_capacity,
                "queue_high_water": self._queue_high_water,
                "writer_errors": self.writer_errors,
                "queue_overflows": self.queue_overflows,
                "last_enqueued_frame_id": self.last_enqueued_frame_id,
                "last_persisted_frame_id": self.last_persisted_frame_id,
                "oldest_pending_age_ms": oldest_age,
                "writer_alive": self._thread is not None and self._thread.is_alive(),
                "fatal": self._fatal.is_set(),
                "fatal_error": str(self._fatal_error) if self._fatal_error else None,
            }

    def close(self, drain: bool = True) -> bool:
        """Stop the producer, drain the queue, return True on a clean shutdown."""
        self._stop.set()
        if not drain or self._thread is None:
            return not self._fatal.is_set()
        self._thread.join(timeout=self.drain_timeout_seconds)
        if self._thread.is_alive():
            return False  # DRAIN_TIMEOUT => DATA_GAP
        return not self._fatal.is_set()


class DerivedRecordWriter:
    """Dedicated writer thread for parsed/scoped analytical records.

    Owns and calls the caller-provided ``persist_fn`` on its own thread so file
    I/O (RawStore / DayWriter) stays off the socket event loop. Overflow is also
    fail-closed: there is no silently-dropped parsed record.
    """

    def __init__(
        self,
        persist_fn,
        *,
        queue_capacity: int = 100_000,
        drain_timeout_seconds: float = 10.0,
        name: str = "derived-record-writer",
    ) -> None:
        self.persist_fn = persist_fn
        self.queue_capacity = queue_capacity
        self.drain_timeout_seconds = drain_timeout_seconds
        self.name = name
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=queue_capacity)
        self._stop = threading.Event()
        self._fatal = threading.Event()
        self._fatal_error: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.records_enqueued = 0
        self.records_persisted = 0
        self.queue_overflows = 0
        self.writer_errors = 0
        self.last_persisted_frame_id: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def try_submit(self, record: dict) -> bool:
        if self._fatal.is_set():
            with self._lock:
                self.queue_overflows += 1
            return False
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            with self._lock:
                self.queue_overflows += 1
            return False
        with self._lock:
            self.records_enqueued += 1
        return True

    def _run(self) -> None:
        try:
            while True:
                try:
                    record = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                self.persist_fn(record)
                with self._lock:
                    self.records_persisted += 1
                    self.last_persisted_frame_id = record.get("frame_id")
                if self._stop.is_set() and self._queue.empty():
                    break
        except BaseException as exc:  # noqa: BLE001 — writer failure must surface as fatal
            with self._lock:
                self.writer_errors += 1
                self._fatal_error = exc
            self._fatal.set()

    def gap_for(self, frame: WsTransportFrame | None, reason: str) -> dict:
        h = self.health()
        return {
            "source": self.name,
            "reason": reason,
            "detected_at": datetime.now(UTC).isoformat(),
            "session_id": frame.session_id if frame else None,
            "first_unpersisted_session_seq": frame.session_seq if frame else None,
            "first_unpersisted_frame_id": frame.frame_id if frame else None,
            "first_unpersisted_received_at_ns": frame.received_at_ns if frame else None,
            "last_persisted_session_id": None,
            "last_persisted_session_seq": None,
            "last_persisted_frame_id": self.last_persisted_frame_id,
            "queue_capacity": self.queue_capacity,
            "queue_depth": h["queue_depth"],
        }

    def health(self) -> dict:
        with self._lock:
            return {
                "records_enqueued": self.records_enqueued,
                "records_persisted": self.records_persisted,
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self.queue_capacity,
                "writer_errors": self.writer_errors,
                "queue_overflows": self.queue_overflows,
                "last_persisted_frame_id": self.last_persisted_frame_id,
                "writer_alive": self._thread is not None and self._thread.is_alive(),
                "fatal": self._fatal.is_set(),
                "fatal_error": str(self._fatal_error) if self._fatal_error else None,
            }

    def close(self, drain: bool = True) -> bool:
        self._stop.set()
        if not drain or self._thread is None:
            return not self._fatal.is_set()
        self._thread.join(timeout=self.drain_timeout_seconds)
        if self._thread.is_alive():
            return False
        return not self._fatal.is_set()