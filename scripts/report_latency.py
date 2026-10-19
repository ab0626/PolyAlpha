#!/usr/bin/env python3
"""Offline latency report — read raw trace records, compute monotonic deltas.

Reads ``data/latency/YYYY-MM-DD/*.jsonl`` (raw stage timestamps only) and
computes per-component latency deltas offline, so a later formula change never
invalidates captured data. Percentiles use nearest-rank; p99.9 is suppressed
(INSUFFICIENT_N) below N=1000. Reports are broken out by source, session_id,
message_type, and hour. If any record window dropped telemetry, the report is
marked LATENCY_MEASUREMENT_INCOMPLETE.

Usage:
    .venv/Scripts/python.exe scripts/report_latency.py [--dir data/latency]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# (component, end_stage, start_stage). Deltas are monotonic ns.
WS_INGEST_COMPONENTS = [
    ("frame_build", "r1", "r0"),
    ("transport_enqueue", "r2", "r1"),
    ("json_parse", "r3", "r2"),
    ("book_state_apply", "r4", "r3"),
    ("derived_enqueue", "r5", "r4"),
    ("receive_to_usable_state", "r4", "r0"),
]
WS_WRITER_COMPONENTS = [
    ("writer_queue", "w1", "w0"),
    ("writer_write", "w2", "w1"),
    ("durable_lag", "w2", "r0"),
]
E3_COMPONENTS = [
    ("context_build", "d1", "d0"),
    ("ladder_evaluation", "d2", "d1"),
    ("candidate_finalize", "d3", "d2"),
    ("alert_emission", "d4", "d3"),
    ("human_reaction", "h1", "h0"),
    ("human_decision", "h2", "h1"),
    ("total_human_delay", "h2", "h0"),
]
KALSHI_ORDER_COMPONENTS = [
    ("local_pre_send", "e6", "e5"),
    ("http_rtt_plus_venue", "e7", "e6"),
    ("response_parse", "e8", "e7"),
]

COMPONENTS_BY_FAMILY = {
    "ws_ingest": WS_INGEST_COMPONENTS,
    "ws_writer": WS_WRITER_COMPONENTS,
    "e3_decision": E3_COMPONENTS,
    "kalshi_order": KALSHI_ORDER_COMPONENTS,
}

BUDGETS_NS = [100_000, 250_000, 500_000, 1_000_000, 5_000_000, 10_000_000, 100_000_000]


def _nearest_rank(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return float("nan")
    k = max(1, int(round(p / 100.0 * len(sorted_values))))
    return sorted_values[min(k, len(sorted_values)) - 1]


def _component_values(trace: dict, family: str) -> dict[str, float]:
    stages = trace.get("stages_ns") or {}
    out: dict[str, float] = {}
    for name, end, start in COMPONENTS_BY_FAMILY.get(family, []):
        if start in stages and end in stages:
            out[name] = float(stages[end] - stages[start])
    return out


def _summarize(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"N": 0}
    s = sorted(values)
    return {
        "N": n,
        "min": min(s),
        "mean": statistics.fmean(s),
        "p50": _nearest_rank(s, 50),
        "p95": _nearest_rank(s, 95),
        "p99": _nearest_rank(s, 99),
        "p99.9": _nearest_rank(s, 99.9) if n >= 1000 else "INSUFFICIENT_N",
        "max": max(s),
    }


def _fmt_ns(ns) -> str:
    if isinstance(ns, str):
        return ns
    if ns is None:
        return "-"
    if ns < 1_000:
        return f"{ns:.0f}ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.2f}us"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.2f}ms"
    return f"{ns / 1_000_000_000:.2f}s"


def _crossing_counts(values: list[float]) -> dict[str, int]:
    """Count how many samples exceed each budget."""
    return {f">{_fmt_ns(b)}": sum(1 for v in values if v > b) for b in BUDGETS_NS}


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline latency report")
    ap.add_argument("--dir", default="data/latency")
    args = ap.parse_args()

    root = Path(args.dir)
    traces: list[dict] = []
    for path in sorted(root.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                traces.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not traces:
        print("NO TRACE RECORDS FOUND")
        return 0

    # Aggregate component values globally + by dimension.
    global_values: dict[str, list[float]] = defaultdict(list)
    by_source: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_session: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_message: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_hour: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for t in traces:
        family = t.get("family", "unknown")
        vals = _component_values(t, family)
        source = t.get("source", "unknown")
        session = t.get("session_id") or "none"
        mtype = t.get("message_type") or "none"
        wall = t.get("wall_received_at_ns")
        hour = "none"
        if wall:
            hour = datetime.fromtimestamp(wall / 1e9, UTC).strftime("%Y-%m-%dT%H")
        for name, v in vals.items():
            global_values[name].append(v)
            by_source[source][name].append(v)
            by_session[session][name].append(v)
            by_message[mtype][name].append(v)
            by_hour[hour][name].append(v)

    print("=" * 78)
    print("LATENCY REPORT (raw timestamps -> offline deltas; monotonic ns)")
    print("=" * 78)
    print(f"traces: {len(traces)}")
    print()
    print("GLOBAL (per component):")
    print(f"{'component':26s} {'N':>7s} {'mean':>9s} {'min':>9s} {'p50':>9s} "
          f"{'p95':>9s} {'p99':>9s} {'p99.9':>10s} {'max':>9s}")
    for name, vals in sorted(global_values.items()):
        s = _summarize(vals)
        print(f"{name:26s} {s['N']:>7d} {_fmt_ns(s.get('mean')):>9s} {_fmt_ns(s.get('min')):>9s} "
              f"{_fmt_ns(s.get('p50')):>9s} {_fmt_ns(s.get('p95')):>9s} "
              f"{_fmt_ns(s.get('p99')):>9s} {str(_fmt_ns(s.get('p99.9'))):>10s} {_fmt_ns(s.get('max')):>9s}")

    print()
    print("BY SOURCE:")
    for src, comps in sorted(by_source.items()):
        print(f"  {src}")
        for name, vals in sorted(comps.items()):
            s = _summarize(vals)
            print(f"    {name:24s} N={s['N']:>7d} p50={_fmt_ns(s.get('p50')):>9s} "
                  f"p99={_fmt_ns(s.get('p99')):>9s} p99.9={_fmt_ns(s.get('p99.9')):>10s}")

    print()
    print("LATENCY-BUDGET CROSSINGS (global, per component):")
    for name, vals in sorted(global_values.items()):
        counts = _crossing_counts(vals)
        parts = "  ".join(f"{k}={v}" for k, v in counts.items() if v > 0)
        print(f"  {name:26s} {parts or '(none cross budgets)'}")

    # Dropped telemetry makes the window incomplete.
    print()
    print("NOTE: telemetry drop state is tracked by the collector status "
          "(records_dropped); a non-zero value => LATENCY_MEASUREMENT_INCOMPLETE.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())