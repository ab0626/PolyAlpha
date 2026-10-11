#!/usr/bin/env python3
"""Continuous ERCOT forecast-vintage collector.

Polls the anonymous ERCOT MIS for the Seven-Day Load Forecast and appends every
NEW forecast document to the immutable vintage store (full hourly intraday path):

    data/energy/ercot/forecast_vintages.jsonl   one row per (doc, day, hour)
    data/energy/ercot/receipts.jsonl            one raw-report receipt per doc
    data/energy/ercot/status.json               atomic freshness heartbeat

Restart is idempotent: captured doc_ids are reloaded from the receipts store, so
a re-run never re-appends a document it already stored. ``publish_timestamp`` is
the observation identity, so intraday revisions are never collapsed.

Gap accounting: ``status.json`` records ``last_successful_poll_at`` (collector
alive + polling) separately from ``last_new_doc_at`` (last NEW ERCOT
publication), so a quiet ERCOT feed is not mistaken for a dead collector.

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
    FORECAST_REPORT_TYPE_ID,
    append_jsonl,
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Continuous ERCOT forecast-vintage collector")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help="seconds between polls (default 300)")
    ap.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    ap.add_argument("--root", default="data/energy/ercot")
    ap.add_argument("--report-type-id", type=int, default=FORECAST_REPORT_TYPE_ID)
    ap.add_argument("--max-docs-per-poll", type=int, default=0,
                    help="cap new documents downloaded per poll (0 = unlimited)")
    ap.add_argument("--status-file", default=None,
                    help="override the freshness status path (default: <root>/status.json)")
    args = ap.parse_args()

    root = Path(args.root)
    rpath = receipts_path(root)
    vpath = vintages_path(root)
    spath = Path(args.status_file) if args.status_file else status_path(root)

    collector_version = _git_head()
    captured = load_captured_doc_ids(rpath)

    total_vintage_rows = 0
    if vpath.exists():
        with vpath.open("r", encoding="utf-8") as fh:
            total_vintage_rows = sum(1 for _ in fh)

    status = {
        "started_at": datetime.now(UTC).isoformat(),
        "collector_version": collector_version,
        "interval_seconds": args.interval,
        "last_successful_poll_at": None,
        "last_new_doc_at": None,
        "last_new_doc_received_at": None,
        "last_new_doc_count": 0,
        "total_docs_captured": len(captured),
        "total_vintage_rows": total_vintage_rows,
        "last_error": None,
        "last_error_at": None,
    }
    write_status(spath, status)

    run = 0
    while args.cycles == 0 or run < args.cycles:
        received = datetime.now(UTC)
        try:
            docs = [d for d in list_documents(args.report_type_id)
                    if d.friendly_name.lower().endswith("csv")]
            new_docs = [d for d in docs if str(d.doc_id) not in captured]
            if args.max_docs_per_poll and len(new_docs) > args.max_docs_per_poll:
                # Backfill oldest-first so recent revisions still land this cycle.
                new_docs.sort(key=lambda d: d.publish_date, reverse=True)
                new_docs = new_docs[: args.max_docs_per_poll]
            else:
                new_docs.sort(key=lambda d: d.publish_date)

            n_receipts = 0
            n_rows = 0
            newest_publish = None
            for d in new_docs:
                try:
                    blob = download_document(d.doc_id)
                    receipt, rows = build_doc_records(d, blob, received, collector_version)
                except Exception as exc:  # noqa: BLE001 — per-doc best effort
                    print(f"[{received.isoformat()}] WARN doc {d.doc_id} failed: {exc}", file=sys.stderr, flush=True)
                    continue
                append_jsonl(rpath, [receipt])
                append_jsonl(vpath, rows)
                captured.add(str(d.doc_id))
                n_receipts += 1
                n_rows += len(rows)
                newest_publish = d.publish_date

            status["last_successful_poll_at"] = received.isoformat()
            status["last_error"] = None
            status["last_error_at"] = None
            status["last_new_doc_count"] = n_receipts
            status["total_docs_captured"] = len(captured)
            status["total_vintage_rows"] += n_rows
            if newest_publish is not None:
                status["last_new_doc_at"] = newest_publish
                status["last_new_doc_received_at"] = received.isoformat()
            write_status(spath, status)

            if n_receipts:
                print(
                    f"[{received.isoformat()}] captured {n_receipts} new docs "
                    f"({n_rows} vintage rows), newest publish {newest_publish}, "
                    f"total docs {len(captured)}",
                    flush=True,
                )
            else:
                print(f"[{received.isoformat()}] no new forecast docs (poll ok)", flush=True)
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