"""Tests for the off-loop ws-frame writer (bounded queue, fail-closed gaps)."""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from polyalpha.ws_frame import (
    GAP_QUEUE_OVERFLOW,
    DataGapError,
    WsFrameStore,
    WsFrameWriter,
    WsSession,
    capture_frame,
    fail_closed,
)


def _ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1e9)


def test_day_partition_uses_received_at_ns(tmp_path):
    store = WsFrameStore(tmp_path, "src")
    s = WsSession()
    s.on_connect()
    # Received at 23:59:59.999Z on 2026-10-19; written (by the writer) "later".
    wall = _ns("2026-10-19T23:59:59.999000+00:00")
    frame = capture_frame("src", s, "x", wall_ns=wall, mono_ns=0)
    store.append(frame)
    assert (tmp_path / "2026-10-19.jsonl").exists()
    assert not (tmp_path / "2026-10-20.jsonl").exists()


def test_writer_preserves_order_and_drains(tmp_path):
    store = WsFrameStore(tmp_path, "src")
    w = WsFrameWriter(store, queue_capacity=10, batch_size=2, batch_age_seconds=0.1, fsync_seconds=60)
    w.start()
    s = WsSession()
    s.on_connect()
    wall = _ns("2026-10-19T23:59:59.999000+00:00")  # frozen: deterministic day partition
    for i in range(5):
        assert w.try_submit(capture_frame("src", s, f"m{i}", wall_ns=wall, mono_ns=0))
    assert w.close(drain=True) is True
    day = "2026-10-19"
    rows = [json.loads(line) for line in (tmp_path / f"{day}.jsonl").read_text().splitlines()]
    assert [r["session_seq"] for r in rows] == [1, 2, 3, 4, 5]
    assert w.health()["frames_persisted"] == 5


def test_full_queue_refuses_not_drops(tmp_path):
    store = WsFrameStore(tmp_path, "src")
    w = WsFrameWriter(store, queue_capacity=2, batch_size=100, batch_age_seconds=60)
    w.start()
    s = WsSession()
    s.on_connect()
    assert w.try_submit(capture_frame("src", s, "a"))
    assert w.try_submit(capture_frame("src", s, "b"))
    assert w.try_submit(capture_frame("src", s, "c")) is False
    assert w.health()["queue_overflows"] == 1
    assert w.health()["queue_depth"] == 2  # existing frames not dropped
    w.close(drain=False)


def test_overflow_produces_gap_event(tmp_path):
    store = WsFrameStore(tmp_path, "src")
    w = WsFrameWriter(store, queue_capacity=1, batch_size=100, batch_age_seconds=60)
    w.start()
    s = WsSession()
    s.on_connect()
    assert w.try_submit(capture_frame("src", s, "a"))
    f = capture_frame("src", s, "b")
    assert w.try_submit(f) is False
    with pytest.raises(DataGapError):
        fail_closed(store, w, f, GAP_QUEUE_OVERFLOW)
    gap = json.loads((tmp_path / "transport_gaps.jsonl").read_text().splitlines()[0])
    assert gap["reason"] == GAP_QUEUE_OVERFLOW
    assert gap["first_unpersisted_frame_id"] == f.frame_id
    w.close(drain=False)


class _BoomStore:
    source = "src"

    def __init__(self, root):
        self.root = root

    def write(self, frame):
        raise OSError("disk full")

    def flush(self):
        pass

    def fsync(self):
        pass

    def close(self):
        pass

    def mark_cutover(self, at=None):
        return ""


def test_writer_exception_is_fatal(tmp_path):
    w = WsFrameWriter(_BoomStore(tmp_path), queue_capacity=10, batch_size=1, batch_age_seconds=0.1)
    w.start()
    s = WsSession()
    s.on_connect()
    assert w.try_submit(capture_frame("src", s, "x"))
    time.sleep(0.4)
    h = w.health()
    assert h["fatal"] is True
    assert h["fatal_error"] is not None
    w.close(drain=False)


class _BlockingStore:
    source = "src"

    def __init__(self, root):
        self.root = root

    def write(self, frame):
        time.sleep(60)

    def flush(self):
        pass

    def fsync(self):
        pass

    def close(self):
        pass

    def mark_cutover(self, at=None):
        return ""


def test_drain_timeout_returns_false(tmp_path):
    w = WsFrameWriter(_BlockingStore(tmp_path), queue_capacity=10, batch_size=1,
                      batch_age_seconds=0.1, drain_timeout_seconds=0.1)
    w.start()
    s = WsSession()
    s.on_connect()
    w.try_submit(capture_frame("src", s, "x"))
    time.sleep(0.2)  # let the writer pick up the frame and block inside write()
    assert w.close(drain=True) is False  # writer stuck => DRAIN_TIMEOUT


def test_duplicate_frames_both_persisted(tmp_path):
    store = WsFrameStore(tmp_path, "src")
    w = WsFrameWriter(store, queue_capacity=10, batch_size=10, batch_age_seconds=0.1)
    w.start()
    s = WsSession()
    s.on_connect()
    assert w.try_submit(capture_frame("src", s, "dup"))
    assert w.try_submit(capture_frame("src", s, "dup"))
    assert w.close(drain=True)
    assert w.health()["frames_persisted"] == 2