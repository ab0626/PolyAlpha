#!/usr/bin/env python3
"""Offline latency report — validate raw traces, then compute monotonic deltas.

Reads ``data/latency/YYYY-MM-DD/*.jsonl`` and:
  - validates stage monotonicity (r0<=r1<=...<=r5, w0<=w1<=w2); violations are
    INVALID_TRACE and excluded from all latency statistics;
  - distinguishes a MISSING stage from a zero delta (missing is never 0);
  - reports zero-delta rate per component (the first trust signal at us scale);
  - prints global + source/session/message-type/hour breakdowns;
  - supports ``--slowest N`` to dump the N worst receive->usable traces.

Percentiles are nearest-rank; p99.9 is suppressed (INSUFFICIENT_N) below N=1000.
Monotonic deltas only; wall/venue timestamps are never mixed into latency.

Usage:
    .venv/Scripts/python.exe scripts/report_latency.py [--dir data/latency] [--slowest 50]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

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

# Monotonic stage order per family (for validation).
STAGE_ORDER = {
    "ws_ingest": ["r0", "r1", "r2", "r3", "r4", "r5"],
    "ws_writer": ["w0", "w1", "w2"],
    "e3_decision": ["d0", "d1", "d2", "d3", "d4", "h0", "h1", "h2"],
    "kalshi_order": ["e5", "e6", "e7", "e8"],
}

BUDGETS_NS = [100_000, 250_000, 500_000, 1_000_000, 5_000_000, 10_000_000, 100_000_000]


def _nearest_rank(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return float("nan")
    k = max(1, int(round(p / 100.0 * len(sorted_values))))
    return sorted_values[min(k, len(sorted_values)) - 1]


def _validate_trace(trace: dict) -> bool:
    """True if the trace's present stages are monotonic nondecreasing."""
    order = STAGE_ORDER.get(trace.get("family", ""), [])
    stages = trace.get("stages_ns") or {}
    prev = None
    for key in order:
        if key not in stages:
            continue
        v = stages[key]
        if not isinstance(v, int):
            return False
        if prev is not None and v < prev:
            return False
        prev = v
    return True


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
    return {f">{_fmt_ns(b)}": sum(1 for v in values if v > b) for b in BUDGETS_NS}


def _print_breakdown(title: str, groups: dict[str, dict[str, list[float]]]) -> None:
    print(f"\n{title}:")
    for key, comps in sorted(groups.items()):
        print(f"  {key}")
        for name, vals in sorted(comps.items()):
            s = _summarize(vals)
            z = sum(1 for v in vals if v == 0)
            nz = [v for v in vals if v > 0]
            print(f"    {name:24s} N={s['N']:>7d} zero={z:>7d} ({100.0 * z / max(s['N'], 1):5.1f}%) "
                  f"min_nz={_fmt_ns(min(nz)) if nz else '-':>9s} p50={_fmt_ns(s.get('p50')):>9s} "
                  f"p99={_fmt_ns(s.get('p99')):>9s} p99.9={_fmt_ns(s.get('p99.9')):>10s}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline latency report")
    ap.add_argument("--dir", default="data/latency")
    ap.add_argument("--slowest", type=int, default=0, help="dump the N slowest receive->usable traces")
    args = ap.parse_args()

    print("clock: monotonic resolution=", time.get_clock_info("monotonic").resolution,
          "monotonic=", time.get_clock_info("monotonic").monotonic)

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

    # Validate; invalid traces are excluded loudly.
    valid, invalid = [], 0
    for t in traces:
        if _validate_trace(t):
            valid.append(t)
        else:
            invalid += 1
    print(f"traces={len(traces)}  valid={len(valid)}  INVALID_TRACE={invalid}")

    global_values: dict[str, list[float]] = defaultdict(list)
    by_source: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_session: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_message: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_hour: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    slow_rows: list[tuple[float, dict]] = []

    for t in valid:
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
        if "receive_to_usable_state" in vals:
            slow_rows.append((vals["receive_to_usable_state"], t))

    print("=" * 78)
    print("GLOBAL (per component):")
    print(f"{'component':26s} {'N':>7s} {'mean':>9s} {'min':>9s} {'p50':>9s} "
          f"{'p95':>9s} {'p99':>9s} {'p99.9':>10s} {'max':>9s} {'zero%':>7s}")
    for name, vals in sorted(global_values.items()):
        s = _summarize(vals)
        z = sum(1 for v in vals if v == 0)
        print(f"{name:26s} {s['N']:>7d} {_fmt_ns(s.get('mean')):>9s} {_fmt_ns(s.get('min')):>9s} "
              f"{_fmt_ns(s.get('p50')):>9s} {_fmt_ns(s.get('p95')):>9s} "
              f"{_fmt_ns(s.get('p99')):>9s} {str(_fmt_ns(s.get('p99.9'))):>10s} {_fmt_ns(s.get('max')):>9s} "
              f"{100.0 * z / max(s['N'], 1):6.1f}%")

    _print_breakdown("BY SOURCE", by_source)
    _print_breakdown("BY SESSION", by_session)
    _print_breakdown("BY MESSAGE TYPE", by_message)
    _print_breakdown("BY HOUR", by_hour)

    print("\nLATENCY-BUDGET CROSSINGS (global, per component):")
    for name, vals in sorted(global_values.items()):
        counts = _crossing_counts(vals)
        parts = "  ".join(f"{k}={v}" for k, v in counts.items() if v > 0)
        print(f"  {name:26s} {parts or '(none cross budgets)'}")

    if args.slowest:
        slow_rows.sort(reverse=True)
        print(f"\nSLOWEST {min(args.slowest, len(slow_rows))} receive->usable traces:")
        print(f"{'trace_id':16s} {'source':30s} {'mtype':8s} {'r4-r0':>9s} "
              f"{'build':>9s} {'enq':>8s} {'parse':>9s} {'book':>8s} {'derived':>8s}")
        for _, t in slow_rows[: args.slowest]:
            st = t.get("stages_ns") or {}
            def d(a, b):
                return st[a] - st[b] if a in st and b in st else None
            print(f"{t.get('trace_id', '?')[:16]:16s} {t.get('source', '?')[:30]:30s} "
                  f"{str(t.get('message_type', ''))[:8]:8s} {_fmt_ns(d('r4', 'r0')):>9s} "
                  f"{_fmt_ns(d('r1', 'r0')):>9s} {_fmt_ns(d('r2', 'r1')):>8s} "
                  f"{_fmt_ns(d('r3', 'r2')):>9s} {_fmt_ns(d('r4', 'r3')):>8s} {_fmt_ns(d('r5', 'r4')):>8s}")

    print()
    print("NOTE: a non-zero collector records_dropped => LATENCY_MEASUREMENT_INCOMPLETE.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())