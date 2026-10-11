#!/usr/bin/env python3
"""Read-only E3 readiness monitor (no scoring, no tuning, no extra thresholds).

Shows when the frozen E3 machinery is actually allowed to fire, per the operating
rule "No E3 score until a lead bucket reaches min_residuals naturally":

  - per-lead residual support vs the frozen min_residuals=5 (leads 1..6 are
    training-only; only lead 0 produces observations)
  - Kalshi ladder freshness
  - ERCOT forecast-vintage freshness
  - latest actual (settlement-source) operating day
  - eligible E3 leads
  - shadow observations recorded / resolved

Read-only: it never writes and never runs E3. "Run the frozen baseline once when
lead 0 becomes eligible" is deliberately left to a separate step.

Usage:
    .venv/Scripts/python.exe scripts/energy_e3_readiness.py [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.physical import ForecastErrorModel  # noqa: E402

ACTUALS = ROOT / "data" / "energy" / "ercot" / "actuals.jsonl"
VINTAGES = ROOT / "data" / "energy" / "ercot" / "forecast_vintages.jsonl"
KALSHI_STATUS = ROOT / "data" / "energy" / "kalshi" / "status.json"
ERCOT_STATUS = ROOT / "data" / "energy" / "ercot" / "status.json"
SHADOW_OBSERVATIONS = ROOT / "data" / "energy" / "shadow_observations.jsonl"
SHADOW_RESOLUTIONS = ROOT / "data" / "energy" / "shadow_resolutions.jsonl"

MIN_RESIDUALS = 5
MAX_LEAD = 6


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _load_actuals() -> dict[str, Decimal]:
    """{operating_day: daily_peak} from the FIRST COMPLETE NP6-346 publication.

    Mirrors the runner's settlement-vintage rule: per operating day, select the
    earliest complete (24 hourly rows) document and take its peak. Later
    corrections never silently replace the settlement-source value.
    """
    if not ACTUALS.exists():
        return {}
    docs: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"publish": "", "hours": set(), "totals": []}
    )
    with ACTUALS.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            day = r.get("operating_day")
            doc = r.get("doc_id")
            v = _dec(r.get("total_mw"))
            if not day or not doc or v is None:
                continue
            key = (str(day), str(doc))
            d = docs[key]
            pub = r.get("publish_timestamp") or ""
            if not d["publish"] or pub < d["publish"]:
                d["publish"] = pub
            h = r.get("hour_ending")
            if h:
                d["hours"].add(str(h))
            d["totals"].append(v)

    by_day: dict[str, list[dict]] = defaultdict(list)
    for (day, _doc), d in docs.items():
        by_day[day].append(d)

    out: dict[str, Decimal] = {}
    for day, ds in by_day.items():
        complete = [d for d in ds if len(d["hours"]) >= 24]
        if not complete:
            continue
        earliest = min(complete, key=lambda d: d["publish"])
        out[day] = max(earliest["totals"])
    return out


def _forecast_daily_peaks(vpath: Path) -> dict[tuple[str, str], Decimal]:
    """{(publish_day, delivery_day): daily_peak_forecast} from hourly vintages.

    Daily peak is ``max_h SystemTotal`` across the day's hourly rows; the exact
    intraday-vintage selection does not affect residual-support counting.
    """
    by_key: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    if not vpath.exists():
        return {}
    with vpath.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            pub = (r.get("publish_timestamp") or "")[:10]
            delivery = r.get("delivery_day")
            v = _dec(r.get("system_total_mw"))
            if pub and delivery and v is not None:
                by_key[(pub, str(delivery))].append(v)
    return {k: max(vals) for k, vals in by_key.items()}


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.open("r", encoding="utf-8"))


def _seconds_ago(now: datetime, value: object) -> float | None:
    ts = _parse_iso(value)
    if ts is None:
        return None
    return (now - ts).total_seconds()


def build_summary() -> dict:
    now = datetime.now(UTC)
    actuals = _load_actuals()
    forecasts = _forecast_daily_peaks(VINTAGES)
    model = ForecastErrorModel(actuals, forecasts, max_lead=MAX_LEAD, min_residuals=MIN_RESIDUALS)

    residual_counts = {lead: len(model.residuals.get(lead, [])) for lead in range(MAX_LEAD + 1)}
    eligible = [lead for lead, n in residual_counts.items() if n >= MIN_RESIDUALS]

    kalshi = _read_json(KALSHI_STATUS) or {}
    ercot = _read_json(ERCOT_STATUS) or {}

    n_observations = _count_lines(SHADOW_OBSERVATIONS)
    n_resolved = _count_lines(SHADOW_RESOLUTIONS)

    return {
        "now": now.isoformat(),
        "min_residuals": MIN_RESIDUALS,
        "max_lead": MAX_LEAD,
        "residual_counts": residual_counts,
        "eligible_leads": eligible,
        "latest_actual_day": max(actuals) if actuals else None,
        "n_actual_days": len(actuals),
        "n_forecast_vintage_pairs": len(forecasts),
        "kalshi": {
            "last_successful_capture_at": kalshi.get("last_successful_capture_at"),
            "last_row_count": kalshi.get("last_row_count"),
            "stale_seconds": _seconds_ago(now, kalshi.get("last_successful_capture_at")),
        },
        "ercot": {
            "last_successful_poll_at": ercot.get("last_successful_poll_at"),
            "last_new_doc_at": ercot.get("last_new_doc_at"),
            "total_docs_captured": ercot.get("total_docs_captured"),
            "stale_seconds": _seconds_ago(now, ercot.get("last_successful_poll_at")),
        },
        "shadow_observations_recorded": n_observations,
        "shadow_observations_resolved": n_resolved,
    }


def _render(s: dict) -> str:
    lines = [
        "E3 READINESS  (frozen: min_residuals per lead = 5)",
        "---------------------------------------------------",
        "",
        "  lead residual support:",
    ]
    for lead in range(s["max_lead"] + 1):
        n = s["residual_counts"].get(lead, 0)
        tag = "  <-- ELIGIBLE" if lead in s["eligible_leads"] else ""
        lines.append(f"    lead {lead}: {n:>3} / {s['min_residuals']}{tag}")
    if not s["eligible_leads"]:
        lines.append("    (no lead eligible yet)")
    lines += [
        "",
        "  data freshness:",
        f"    Kalshi ladder:    last capture "
        f"{s['kalshi']['last_successful_capture_at'] or 'n/a'}"
        + (f" ({s['kalshi']['stale_seconds']:.0f}s ago)" if s['kalshi']['stale_seconds'] is not None else "")
        + f", {s['kalshi']['last_row_count']} active rows",
        f"    ERCOT vintages:   last poll "
        f"{s['ercot']['last_successful_poll_at'] or 'n/a'}"
        + (f" ({s['ercot']['stale_seconds']:.0f}s ago)" if s['ercot']['stale_seconds'] is not None else "")
        + f", last new doc {s['ercot']['last_new_doc_at'] or 'n/a'}"
        + f", {s['ercot']['total_docs_captured']} docs",
        f"    latest actual day: {s['latest_actual_day'] or 'n/a'} "
        f"({s['n_actual_days']} actual days; {s['n_forecast_vintage_pairs']} forecast pub/delivery pairs)",
        "",
        f"  eligible E3 leads: {s['eligible_leads'] or 'none'}",
        "",
        f"  shadow ledger: {s['shadow_observations_recorded']} observations, "
        f"{s['shadow_observations_resolved']} resolved",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="E3 readiness monitor")
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    args = ap.parse_args()

    summary = build_summary()
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        print(_render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())