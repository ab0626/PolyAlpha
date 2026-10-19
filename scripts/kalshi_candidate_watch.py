#!/usr/bin/env python3
"""Kalshi LIVE_CALIBRATION_V0 candidate watcher (READ-ONLY, never trades).

Polls for a fresh lead-0 forecast vintage and evaluates the frozen V0 screen.
When >=1 candidate survives (fresh realized path, verified fee, 5c screen, 2c
buffer, size, freshness), it ALERTS with the full decision context. It NEVER
arms, clears, or submits anything. Zero candidates => silent.

Readiness is derived from the ACTUAL lead-0 forecast vintage (forecast_first),
never a hard-coded clock time (the lead-0 vintage for a day is ~00:34 CT, not
the ~05:50 CT settlement report).

Usage:
    once:   .venv/Scripts/python.exe scripts/kalshi_candidate_watch.py --once
    poll:   .venv/Scripts/python.exe scripts/kalshi_candidate_watch.py --interval 900
    notify: .venv/Scripts/python.exe scripts/kalshi_candidate_watch.py --interval 900 --notify
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from kalshi_prod_submit import _build_context, _verified_ercot_rule  # noqa: E402

from polyalpha.kalshi_signal_e3 import MAX_SIGNAL_AGE_SECONDS, evaluate_ladder  # noqa: E402


def _alert(candidates, day, signal_ts, realized, as_of, toaster) -> None:
    lines = ["=== KALSHI V0 CANDIDATE ALERT ==="]
    lines.append(f"operating_day: {day}")
    remaining_h = None
    if signal_ts is not None:
        age_h = (as_of - signal_ts).total_seconds() / 3600.0
        remaining_h = MAX_SIGNAL_AGE_SECONDS / 3600.0 - age_h
        lines.append(f"signal_usable_ts: {signal_ts.isoformat()}")
        lines.append(f"signal_age_hours: {age_h:.2f}  (6h window, {remaining_h:.2f}h remaining)")
    else:
        lines.append("signal_usable_ts: UNKNOWN")
        lines.append("signal_age_hours: UNKNOWN")
    lines.append(f"realized_running_max: {realized}")
    for e in candidates:
        lines.append(
            f"  {e.ticker} {e.side} p_hat={e.p_hat_e3} exec_px={e.executable_price} "
            f"size={e.executable_size} taker_fee={e.taker_fee} buffer={e.buffer} "
            f"screen_edge={e.screen_edge}"
        )
    lines.append("operator action required: arm + clear kill switch, then:")
    for e in candidates:
        lines.append(
            f"  .venv/Scripts/python.exe scripts/kalshi_prod_submit.py "
            f"--ticker {e.ticker} --side {e.side} --execute"
        )
    text = "\n".join(lines)
    print(text, flush=True)
    if toaster is not None:
        first = candidates[0]
        tail = f" ({remaining_h:.1f}h left)" if remaining_h is not None else ""
        toaster.maybe(
            "Kalshi V0 candidate",
            f"{first.ticker} {first.side} edge={first.screen_edge}{tail}",
            force=True,
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Kalshi V0 candidate watcher (read-only)")
    ap.add_argument("--interval", type=float, default=900.0, help="seconds between polls")
    ap.add_argument("--once", action="store_true", help="single check then exit")
    ap.add_argument("--notify", action="store_true", help="pop desktop toasts on alert")
    args = ap.parse_args()

    toaster = None
    if args.notify:
        from polyalpha.notify import ThrottledToaster  # noqa: E402

        toaster = ThrottledToaster(interval_seconds=60.0)

    last_key = None
    while True:
        as_of = datetime.now(UTC)
        try:
            model, ladder, day, realized, signal_ts = _build_context(as_of)
        except Exception:
            # Fail-closed: no valid context => no candidate, stay silent.
            if args.once:
                return 0
            time.sleep(args.interval)
            continue

        evals = evaluate_ladder(
            model, ladder, _verified_ercot_rule(), as_of,
            realized_running_max=realized, signal_timestamp=signal_ts,
        )
        candidates = [e for e in evals if e.intent is not None]
        if candidates:
            key = (day, signal_ts.isoformat() if signal_ts else None)
            if key != last_key:
                last_key = key
                _alert(candidates, day, signal_ts, realized, as_of, toaster)

        if args.once:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())