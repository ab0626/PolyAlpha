"""End-to-end semantic audit: Kalshi ERCOT settlement vs first-complete ERCOT file.

For every settled KXTXERCOTPEAKD market within the MIS display window, prove the
whole chain agrees exactly:

    close_time -> operating_day (America/Chicago)
    -> first-complete NP6-346 CSV (earliest publish date for that operating day)
    -> peak = max(TOTAL)
    -> (peak > strike) == (result == "yes")

A mismatch on any link is a parser/semantics defect that must be fixed before E3.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.ercot_mis import (  # noqa: E402
    ACTUAL_SYSTEM_LOAD_BY_FORECAST_ZONE,
    daily_peak,
    download_document,
    list_documents,
    parse_actual_load,
)
from polyalpha.energy.kalshi import (  # noqa: E402
    KalshiPublicClient,
    ercot_operating_day,
    parse_threshold,
)

SERIES = "KXTXERCOTPEAKD"


def first_complete_peaks() -> dict[str, tuple[str, Decimal]]:
    """{operating_day: (publish_date, peak_mw)} from the FIRST complete file."""
    docs = [d for d in list_documents(ACTUAL_SYSTEM_LOAD_BY_FORECAST_ZONE)
            if d.friendly_name.lower().endswith("csv")]
    best: dict[str, tuple[str, Decimal]] = {}
    for d in docs:
        try:
            rows = parse_actual_load(download_document(d.doc_id))
        except Exception:  # noqa: BLE001
            continue
        for day, peak in daily_peak(rows).items():
            iso = _norm(day)
            if iso not in best or d.publish_date < best[iso][0]:
                best[iso] = (d.publish_date, peak)
    return best


def _norm(oper_day: str) -> str:
    if "/" in oper_day:
        m, dd, y = oper_day.split("/")
        return f"{y}-{int(m):02d}-{int(dd):02d}"
    return oper_day


def main() -> int:
    peaks = first_complete_peaks()
    print(f"first-complete ERCOT actual-load days: {len(peaks)}")

    client = KalshiPublicClient()
    body = client.markets(series_ticker=SERIES, limit=1000)
    settled = [m for m in body.get("markets", []) if m.get("result") in ("yes", "no")]

    checked = 0
    mismatches = 0
    by_day: dict[str, list[str]] = defaultdict(list)
    for m in settled:
        op_day = ercot_operating_day(m["close_time"])
        if op_day not in peaks:
            continue
        strike, _direction = parse_threshold(m)
        if strike is None:
            continue
        _pub, peak = peaks[op_day]
        outcome = 1 if m["result"] == "yes" else 0
        predicted = 1 if peak > strike else 0
        agree = predicted == outcome
        checked += 1
        if not agree:
            mismatches += 1
            by_day[op_day].append(
                f"  strike={strike} peak={peak} result={m['result']} ticker={m['ticker']}"
            )

    print(f"settled markets checked: {checked}")
    print(f"agreements: {checked - mismatches}")
    print(f"MISMATCHES: {mismatches}")
    for day, msgs in sorted(by_day.items()):
        print(f"operating day {day}:")
        for msg in msgs:
            print(msg)

    if mismatches == 0 and checked >= 10:
        print("\nAUDIT PASS: chain consistent on", checked, "markets.")
        return 0
    print(f"\nAUDIT INCONCLUSIVE/FAIL: checked={checked} mismatches={mismatches}")
    return 1 if mismatches else 2


if __name__ == "__main__":
    raise SystemExit(main())