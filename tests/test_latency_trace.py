"""Tests for the non-authoritative off-loop latency trace capture."""

from __future__ import annotations

import json

from polyalpha.latency_trace import LatencyTrace, LatencyTracer


def test_trace_record_roundtrips():
    rec = LatencyTrace(
        schema="latency-trace-v1", family="ws_ingest", source="src",
        session_id="s1", seq=1, message_type="L2", trace_id="t1",
        stages_ns={"r0": 0, "r4": 100},
    )
    d = rec.to_dict()
    assert d["stages_ns"]["r0"] == 0 and d["stages_ns"]["r4"] == 100


def test_tracer_emits_and_persists(tmp_path):
    tr = LatencyTracer(tmp_path, queue_capacity=10, batch_size=1, batch_age_seconds=0.05)
    tr.start()
    tr.trace("ws_ingest", "src", {"r0": 0, "r4": 100}, session_id="s1", seq=1, message_type="L2")
    tr.close(drain=True)
    files = list(tmp_path.rglob("*.jsonl"))
    assert files
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert rows[0]["family"] == "ws_ingest"
    assert rows[0]["stages_ns"]["r4"] == 100


def test_telemetry_drop_is_not_fatal(tmp_path):
    # Tiny queue, writer blocked: drops are counted, never raised.
    tr = LatencyTracer(tmp_path, queue_capacity=1, batch_size=100, batch_age_seconds=60)
    tr.start()
    tr.trace("ws_ingest", "src", {"r0": 0, "r4": 1})
    tr.trace("ws_ingest", "src", {"r0": 0, "r4": 2})  # queue full -> dropped
    assert tr.health()["records_dropped"] == 1
    tr.close(drain=False)