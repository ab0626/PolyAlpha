#!/usr/bin/env python3
"""Continuous ERCOT forecast-vintage + actual-load collector.

Polls the anonymous ERCOT MIS for two report types and appends every NEW
document to immutable append-only stores (full hourly path, never collapsed):

    Seven-Day Load Forecast (NP3-560-CD):
        data/energy/ercot/forecast_vintages.jsonl   one row per (doc, day, hour)
        data/energy/ercot/receipts.jsonl            one raw-report receipt per doc
    Actual System Load (NP6-346-CD, settlement source):
        data/energy/ercot/actuals.jsonl             one row per (doc, day, hour)
        data/energy/ercot/actual_receipts.jsonl     one raw-report receipt per doc
    data/energy/ercot/status.json                   atomic freshness heartbeat

Restart is idempotent: captured doc_ids are reloaded per stream from the
receipts stores, so a re-run never re-appends a document it already stored.
``publish_timestamp`` is the observation identity, so revisions never collapse.

Gap accounting: ``status.json`` records ``last_successful_poll_at`` (collector
alive + polling) separately from ``last_new_doc_at`` / ``last_new_actual_doc_at``
(last NEW publication per stream), so a quiet ERCOT feed is not mistaken for a
dead collector.

Usage:
    .venv/Scripts/python.exe scripts/energy_forecast_collector.py --interval 300
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.ercot_mis import (  # noqa: E402
    download_document,
    list_documents,
)
from polyalpha.energy.forecast_capture import (  # noqa: E402
    ACTUAL_REPORT_TYPE_ID,
    FORECAST_REPORT_TYPE_ID,
    actual_receipts_path,
    actuals_path,
    append_jsonl,
    build_actual_records,
    build_doc_records,
    load_captured_doc_ids,
    receipts_path,
    status_path,
    vintages_path,
    write_status,
)

DEFAULT_INTERVAL = 300.0  # seconds; ERCOT publishes ~hourly, poll well inside that


def _git_head() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL
        )
        return out.decode("utf-8").strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.open("r", encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Continuous ERCOT forecast + actual collector")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help="seconds between polls (default 300)")
    ap.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    ap.add_argument("--root", default="data/energy/ercot")
    ap.add_argument("--max-docs-per-poll", type=int, default=0,
                    help="cap new documents downloaded per poll per stream (0 = unlimited)")
    ap.add_argument("--status-file", default=None,
                    help="override the freshness status path (default: <root>/status.json)")
    args = ap.parse_args()

    root = Path(args.root)
    spath = Path(args.status_file) if args.status_file else status_path(root)
    collector_version = _git_head()

    streams = [
        {
            "name": "forecast",
            "report_type_id": FORECAST_REPORT_TYPE_ID,
            "receipts": receipts_path(root),
            "data": vintages_path(root),
            "build": build_doc_records,
            "docs_key": "total_docs_captured",
            "rows_key": "total_vintage_rows",
            "new_at_key": "last_new_doc_at",
            "new_recv_key": "last_new_doc_received_at",
        },
        {
            "name": "actual",
            "report_type_id": ACTUAL_REPORT_TYPE_ID,
            "receipts": actual_receipts_path(root),
            "data": actuals_path(root),
            "build": build_actual_records,
            "docs_key": "total_actual_docs_captured",
            "rows_key": "total_actual_rows",
            "new_at_key": "last_new_actual_doc_at",
            "new_recv_key": "last_new_actual_doc_received_at",
        },
    ]

    captured: dict[str, set[str]] = {}
    total_rows: dict[str, int] = {}
    for s in streams:
        captured[s["name"]] = load_captured_doc_ids(s["receipts"])
        total_rows[s["name"]] = _count_lines(s["data"])

    status = {
        "started_at": datetime.now(UTC).isoformat(),
        "collector_version": collector_version,
        "interval_seconds": args.interval,
        "last_successful_poll_at": None,
        "last_new_doc_at": None,
        "last_new_doc_received_at": None,
        "last_new_doc_count": 0,
        "last_error": None,
        "last_error_at": None,
    }
    for s in streams:
        status[s["docs_key"]] = len(captured[s["name"]])
        status[s["rows_key"]] = total_rows[s["name"]]
        status[s["new_at_key"]] = None
        status[s["new_recv_key"]] = None
    write_status(spath, status)

    run = 0
    while args.cycles == 0 or run < args.cycles:
        poll_started = datetime.now(UTC)
        try:
            total_new = 0
            for s in streams:
                docs = [d for d in list_documents(s["report_type_id"])
                        if d.friendly_name.lower().endswith("csv")]
                new_docs = [d for d in docs if str(d.doc_id) not in captured[s["name"]]]
                if args.max_docs_per_poll and len(new_docs) > args.max_docs_per_poll:
                    new_docs.sort(key=lambda d: d.publish_date, reverse=True)
                    new_docs = new_docs[: args.max_docs_per_poll]
                else:
                    new_docs.sort(key=lambda d: d.publish_date)

                n_receipts = 0
                n_rows = 0
                newest_publish = None
                newest_received = None
                for d in new_docs:
                    try:
                        blob = download_document(d.doc_id)
                        # Receipt clock: stamp AFTER the bytes actually arrive,
                        # independently per document. A single poll-start stamp
                        # would record a doc as received before it existed,
                        # leaking future information into E3's boundary.
                        received_at = datetime.now(UTC)
                        receipt, rows = s["build"](d, blob, received_at, collector_version)
                    except Exception as exc:  # noqa: BLE001 — per-doc best effort
                        print(
                            f"[{datetime.now(UTC).isoformat()}] WARN {s['name']} doc {d.doc_id} "
                            f"failed: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                    append_jsonl(s["receipts"], [receipt])
                    append_jsonl(s["data"], rows)
                    captured[s["name"]].add(str(d.doc_id))
                    n_receipts += 1
                    n_rows += len(rows)
                    newest_publish = d.publish_date
                    newest_received = received_at

                total_rows[s["name"]] += n_rows
                status[s["docs_key"]] = len(captured[s["name"]])
                status[s["rows_key"]] = total_rows[s["name"]]
                if newest_publish is not None:
                    status[s["new_at_key"]] = newest_publish
                    status[s["new_recv_key"]] = newest_received.isoformat()
                total_new += n_receipts
                if n_receipts:
                    print(
                        f"[{newest_received.isoformat()}] {s['name']}: captured {n_receipts} docs "
                        f"({n_rows} rows), newest {newest_publish}, "
                        f"total {len(captured[s['name']])}",
                        flush=True,
                    )

            status["last_successful_poll_at"] = poll_started.isoformat()
            status["last_error"] = None
            status["last_error_at"] = None
            status["last_new_doc_count"] = total_new
            write_status(spath, status)
            if total_new == 0:
                print(f"[{poll_started.isoformat()}] no new ERCOT docs (poll ok)", flush=True)
        except Exception as exc:  # noqa: BLE001 — durable: log and retry
            status["last_error"] = f"{type(exc).__name__}: {exc}"
            status["last_error_at"] = datetime.now(UTC).isoformat()
            write_status(spath, status)
            print(f"[{datetime.now(UTC).isoformat()}] ERROR {status['last_error']}", file=sys.stderr, flush=True)

        run += 1
        if args.cycles != 0 and run >= args.cycles:
            break
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())