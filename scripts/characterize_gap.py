#!/usr/bin/env python3
"""Characterize a collection outage gap in the US retail raw store.

Single pass over the raw store: locates the largest inter-observation gap in
the book feed, counts affected markets, and verifies settlement coverage.
Emits throttled desktop toasts so a long replay is visible.

Usage:
    python scripts/characterize_gap.py [--raw-dir data/us/retail/raw] [--notify]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.rawstore import RawStore  # noqa: E402
from polyalpha.notify import ThrottledToaster  # noqa: E402

SOURCE_BOOK = "polymarket_us_retail_book"
SOURCE_SETTLEMENT = "polymarket_us_retail_settlement"


def _slug_of_book(payload: dict) -> str | None:
    md = payload.get("marketData", payload) or {}
    return md.get("marketSlug")


def main() -> int:
    parser = argparse.ArgumentParser(description="Characterize a raw-store gap")
    parser.add_argument("--raw-dir", default="data/us/retail/raw")
    parser.add_argument("--notify", action="store_true",
                        help="Pop throttled desktop toasts with replay progress")
    args = parser.parse_args()

    toaster = ThrottledToaster(interval_seconds=300) if args.notify else None

    def progress(done: int, total: int) -> None:
        if toaster is not None:
            toaster.maybe(
                "Gap scan",
                f"{100 * done // total}%  ({done:,}/{total:,} files)",
            )

    raw = RawStore(args.raw_dir)
    book: list[tuple[int, str]] = []  # (received_at_ns, slug)
    settled_slugs: set[str] = set()
    binary_settled: set[str] = set()

    for rec in raw.replay(progress=progress):
        if rec.source == SOURCE_BOOK:
            slug = _slug_of_book(rec.payload)
            if slug:
                book.append((rec.received_at_ns, slug))
        elif rec.source == SOURCE_SETTLEMENT:
            slug = rec.payload.get("slug")
            if slug:
                settled_slugs.add(slug)
                from polyalpha.us.research import _binary_label
                if _binary_label(rec.payload.get("settlement")) is not None:
                    binary_settled.add(slug)

    book.sort()

    # Largest gap between consecutive book observations.
    gap_start = gap_end = None
    max_gap_s = 0.0
    for (a, _), (b, _) in zip(book, book[1:]):
        d = (b - a) / 1e9
        if d > max_gap_s:
            max_gap_s = d
            gap_start, gap_end = a, b

    def iso(ns: int | None) -> str | None:
        return datetime.fromtimestamp(ns / 1e9, UTC).isoformat() if ns else None

    # Markets that had a book observation in the 24h before the gap started.
    active_before: set[str] = set()
    if gap_start:
        pre = gap_start - 24 * 3600 * int(1e9)
        active_before = {s for t, s in book if pre <= t < gap_start}

    summary = {
        "books_total": len(book),
        "unique_book_markets": len({s for _, s in book}),
        "gap_start": iso(gap_start),
        "gap_end": iso(gap_end),
        "gap_hours": round(max_gap_s / 3600.0, 2),
        "markets_active_before_gap": len(active_before),
        "settlement_records": len(settled_slugs),
        "binary_settled": len(binary_settled),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))

    if toaster is not None:
        toaster.maybe(
            "Gap scan done",
            f"gap {summary['gap_hours']}h, {summary['markets_active_before_gap']} active markets, "
            f"{summary['binary_settled']} settled",
            force=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
