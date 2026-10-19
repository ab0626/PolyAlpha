"""WebSocket transport frame (ws-frame-v2) — immutable acquisition evidence.

Separates the transport frame from the parsed/scoped analytical record. The frame
is created IMMEDIATELY after ``recv()`` (wall + monotonic clocks captured
back-to-back, before any decode/parse/dedupe/scope/book normalization) and is
persisted even when JSON parsing fails. Dedupe and ticker-scoping are derived
operations that never mutate or drop a frame.

A ``WsSession`` provides the real session identity: a process-start UUID plus a
connection counter, with a per-connection sequence that resets to 1 on every
successful connection (a process-global counter is NOT a connection sequence).

This module deliberately has no dependency on ``polyalpha.rawstore`` (which is in
the frozen US collector domain). Transport frames go to their own append-only
day-partitioned JSONL lineage, marked with an exact v2 cutover timestamp; old v1
files are never rewritten.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = "ws-frame-v2"


@dataclass(frozen=True)
class WsTransportFrame:
    schema_version: str
    frame_id: str
    source: str
    session_id: str
    session_seq: int
    received_at_ns: int
    received_monotonic_ns: int
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
) -> WsTransportFrame:
    """Create the immutable frame. Wall/mono are captured back-to-back here.

    Pass ``wall_ns``/``mono_ns`` explicitly only when the caller captured them
    itself (e.g. for a deterministic test); in production they are captured in
    this function, immediately at frame creation.
    """
    wall = wall_ns if wall_ns is not None else time.time_ns()
    mono = mono_ns if mono_ns is not None else time.monotonic_ns()
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
        wire_encoding=encoding,
        wire=wire,
        wire_sha256=sha,
    )


def frame_bytes(frame: WsTransportFrame) -> bytes:
    """Reconstruct the original wire bytes from a frame."""
    if frame.wire_encoding == "base64":
        return base64.b64decode(frame.wire)
    return frame.wire.encode("utf-8")


def frame_text(frame: WsTransportFrame) -> str:
    """Reconstruct the original wire text (UTF-8 decode) from a frame."""
    return frame_bytes(frame).decode("utf-8")


class WsFrameStore:
    """Append-only, day-partitioned JSONL store for transport frames (v2)."""

    def __init__(self, root: str | Path, source: str) -> None:
        self.root = Path(root)
        self.source = source

    def mark_cutover(self, at: datetime | None = None) -> str:
        """Record the exact v2 cutover timestamp for this stream (idempotent-ish)."""
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

    def append(self, frame: WsTransportFrame, day: str | None = None) -> None:
        day = day or datetime.now(UTC).strftime("%Y-%m-%d")
        path = self.root / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(frame.to_dict(), sort_keys=True) + "\n")
            fh.flush()