#!/usr/bin/env python3
"""E3 shadow-observation runner (frozen observation rule, ENERGY_E3_SPEC v2).

Implements the frozen "per lead, once" observation rule with a literal
prospective cutover:

  - historical/backfilled ERCOT data trains the residual distribution only;
  - only post-cutover Kalshi snapshots enter the shadow observation ledger;
  - every strike at every qualifying (operating_day, lead) is recorded (the full
    denominator), not only positive-edge ones;
  - deterministic observation_id makes restarts idempotent.

Writes append-only:
    data/energy/shadow_observations.jsonl   one row per observation (status=OPEN)
    data/energy/shadow_resolutions.jsonl    one row per resolved observation

Resolution preserves BOTH the physical outcome and the venue settlement. This is
measurement only: it never trades and never emits an alpha conclusion.

Usage:
    .venv/Scripts/python.exe scripts/energy_e3_runner.py [--dry-run] [--max-days N]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.kalshi import KalshiPublicClient, ercot_operating_day  # noqa: E402
from polyalpha.energy.physical import ForecastErrorModel  # noqa: E402
from polyalpha.energy.shadow_observation import (  # noqa: E402
    FEES,
    MAX_LEAD,
    MIN_RESIDUALS,
    MODEL_VERSION,
    PROSPECTIVE_CUTOVER,
    append_jsonl,
    build_observation_id,
    load_existing_ids,
    load_resolved_ids,
)

KALSHI_LADDERS = ROOT / "data" / "energy" / "kalshi" / "ladders"
FORECAST_VINTAGES = ROOT / "data" / "energy" / "ercot" / "forecast_vintages.jsonl"
ACTUALS = ROOT / "data" / "energy" / "ercot" / "actuals.jsonl"
OBSERVATIONS = ROOT / "data" / "energy" / "shadow_observations.jsonl"
RESOLUTIONS = ROOT / "data" / "energy" / "shadow_resolutions.jsonl"

SERIES = "KXTXERCOTPEAKD"
D = Decimal
ERCOT_TZ = ZoneInfo("America/Chicago")


def _lead0_bucket(day: str) -> tuple[datetime, datetime]:
    """Lead-0 bucket = [00:00 CT on `day`, 00:00 CT on `day`+1) as UTC datetimes."""
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, 0, 0, tzinfo=ERCOT_TZ)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return D(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_actuals(path: Path) -> dict[str, Decimal]:
    """{operating_day: daily_peak} using the FIRST COMPLETE NP6-346 publication.

    Frozen settlement-vintage rule (ENERGY_E3_BASELINE_V0): for each operating
    day, select the earliest document with a complete set of hourly rows (24)
    by publish_timestamp, and compute the peak within that one document. Later
    corrected revisions never silently replace the settlement-source value.
    """
    if not path.exists():
        return {}
    docs: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"publish": "", "hours": set(), "totals": []}
    )
    with path.open("r", encoding="utf-8") as fh:
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


def _load_forecast(path: Path) -> tuple[dict, dict]:
    """Return (peaks, first).

    peaks: {(publish_day, delivery_day): daily_peak} using the latest intraday
           vintage (feeds lead-matched residual training, frozen "latest wins").
    first: {(publish_day, delivery_day): {"publish_at", "peak", "received_at"}}
           using the earliest intraday vintage (defines the lead-0 observation
           time: first snapshot after the day's first same-day forecast).
    """
    by_key: dict[tuple[str, str], list[tuple[str, str, Decimal]]] = defaultdict(list)
    if not path.exists():
        return {}, {}
    with path.open("r", encoding="utf-8") as fh:
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
                by_key[(pub, str(delivery))].append(
                    (r.get("publish_timestamp") or "", r.get("received_at") or "", v)
                )
    peaks: dict[tuple[str, str], Decimal] = {}
    first: dict[tuple[str, str], dict] = {}
    for key, rows in by_key.items():
        max_pub = max(r[0] for r in rows)
        min_pub = min(r[0] for r in rows)
        latest = [r for r in rows if r[0] == max_pub]
        earliest = [r for r in rows if r[0] == min_pub]
        peaks[key] = max(r[2] for r in latest)
        first[key] = {
            "publish_at": min_pub,
            "peak": max(r[2] for r in earliest),
            "received_at": earliest[0][1],
        }
    return peaks, first


def _load_kalshi(dir_path: Path) -> dict[str, list[dict]]:
    """{operating_day: [snapshot, ...]} sorted by captured_at, series-filtered.

    Each snapshot carries the executable book for one strike at one captured_at.
    """
    by_day: dict[str, list[dict]] = defaultdict(list)
    if not dir_path.exists():
        return {}
    for path in sorted(dir_path.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("series_ticker") != SERIES or r.get("status") != "active":
                continue
            raw = r.get("raw") or {}
            close = raw.get("close_time")
            if not close:
                continue
            cap = _parse_iso(r.get("captured_at"))
            strike = _dec(r.get("threshold"))
            if cap is None or strike is None:
                continue
            by_day[ercot_operating_day(close)].append({
                "captured_at": cap,
                "captured_at_iso": cap.isoformat(),
                "ticker": r.get("ticker") or "",
                "strike": strike,
                "yes_bid": _dec(raw.get("yes_bid_dollars")),
                "yes_ask": _dec(raw.get("yes_ask_dollars")),
                "yes_bid_size": _dec(raw.get("yes_bid_size_fp")),
                "yes_ask_size": _dec(raw.get("yes_ask_size_fp")),
                "no_bid": _dec(raw.get("no_bid_dollars")),
                "no_ask": _dec(raw.get("no_ask_dollars")),
                "no_bid_size": _dec(raw.get("no_bid_size_fp")),
                "no_ask_size": _dec(raw.get("no_ask_size_fp")),
            })
    for day in by_day:
        by_day[day].sort(key=lambda s: s["captured_at"])
    return dict(by_day)


def _residuals_for_day(actuals: dict[str, Decimal], forecast_peaks: dict, day: str) -> dict[int, list[Decimal]]:
    """Lead-bucket training residuals using actual days strictly before `day`."""
    actuals_lt = {a: v for a, v in actuals.items() if a < day}
    model = ForecastErrorModel(actuals_lt, forecast_peaks, max_lead=MAX_LEAD, min_residuals=MIN_RESIDUALS)
    return {lead: model.residuals.get(lead, []) for lead in range(MAX_LEAD + 1)}


def _survival(eps: list[Decimal], threshold: Decimal) -> Decimal:
    """Rank-corrected survival P(eps > threshold) = 1 - r/(n+1) (frozen smoothing)."""
    r = sum(1 for e in eps if e <= threshold)
    return D(1) - D(r) / D(len(eps) + 1)


def build_new_observations(
    model_version: str,
    kalshi_by_day: dict[str, list[dict]],
    forecast_peaks: dict,
    forecast_first: dict,
    actuals: dict[str, Decimal],
) -> list[dict]:
    out: list[dict] = []
    today = datetime.now(UTC).date()
    for day, snaps in sorted(kalshi_by_day.items()):
        try:
            if date.fromisoformat(day) >= today:
                # Operating day not yet complete: its lead-0 forecast vintages
                # are still forming, so "first lead-0 forecast" is not final yet.
                continue
        except ValueError:
            continue
        residuals = _residuals_for_day(actuals, forecast_peaks, day)
        eps = residuals.get(0, [])
        if len(eps) < MIN_RESIDUALS:
            continue
        f0 = forecast_first.get((day, day))
        if f0 is None:
            continue
        bucket_start, bucket_end = _lead0_bucket(day)
        if bucket_start < PROSPECTIVE_CUTOVER:
            # MISSING_PRE_CUTOVER: the lead-0 bucket begins before the cutover,
            # so the day's book is only partially observed prospectively.
            continue
        # No-lookahead: the forecast is usable only once BOTH published and
        # actually received by our pipeline.
        t_first_pub = _parse_iso(f0["publish_at"])
        t_first_recv = _parse_iso(f0["received_at"])
        if t_first_pub is None or t_first_recv is None:
            continue
        t_first = max(t_first_pub, t_first_recv)
        bucket_snaps = [s for s in snaps if bucket_start <= s["captured_at"] < bucket_end]
        if not bucket_snaps:
            continue
        candidates = [s for s in bucket_snaps if s["captured_at"] >= t_first]
        if not candidates:
            continue
        t_star_dt = min(s["captured_at"] for s in candidates)
        t_star = t_star_dt.isoformat()
        strikes = sorted({s["strike"] for s in bucket_snaps})
        fpeak = f0["peak"]
        for strike in strikes:
            snap = next(
                (s for s in bucket_snaps if s["strike"] == strike and s["captured_at"] == t_star_dt),
                None,
            )
            if snap is None:
                continue
            p_model = _survival(eps, strike - fpeak)
            fee = FEES
            edge_yes = p_model - (snap["yes_ask"] or D(1)) - fee if snap["yes_ask"] is not None else None
            edge_no = (
                (D(1) - p_model) - (snap["no_ask"] or D(1)) - fee
                if snap["no_ask"] is not None
                else None
            )
            is_candidate = bool(
                (edge_yes is not None and edge_yes > 0) or (edge_no is not None and edge_no > 0)
            )
            out.append({
                "observation_id": build_observation_id(
                    model_version, 0, snap["ticker"], strike, t_star
                ),
                "model_version": model_version,
                "lead_bucket": 0,
                "operating_day": day,
                "market_ticker": snap["ticker"],
                "strike_mw": strike,
                "kalshi_captured_at": t_star,
                "ercot_forecast_publish_at": f0["publish_at"],
                "ercot_forecast_received_at": f0["received_at"],
                "p_model": p_model,
                "yes_bid": snap["yes_bid"],
                "yes_ask": snap["yes_ask"],
                "yes_bid_size": snap["yes_bid_size"],
                "yes_ask_size": snap["yes_ask_size"],
                "no_bid": snap["no_bid"],
                "no_ask": snap["no_ask"],
                "no_bid_size": snap["no_bid_size"],
                "no_ask_size": snap["no_ask_size"],
                "fees": fee,
                "edge_yes": edge_yes,
                "edge_no": edge_no,
                "forecast_peak_mw": fpeak,
                "residual_training_n": len(eps),
                "residual_training_cutoff": day,
                "is_candidate": is_candidate,
                "status": "OPEN",
            })
    return out


def build_resolutions(
    observations_path: Path,
    actuals: dict[str, Decimal],
    client: KalshiPublicClient | None,
) -> list[dict]:
    """Resolution records for OPEN observations whose operating day has settled."""
    if not observations_path.exists():
        return []
    already = load_resolved_ids(RESOLUTIONS)
    out: list[dict] = []
    for line in observations_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obs = json.loads(line)
        except json.JSONDecodeError:
            continue
        oid = obs.get("observation_id")
        if not oid or oid in already:
            continue
        day = obs.get("operating_day")
        peak = actuals.get(day) if day else None
        if peak is None:
            continue  # not yet physically settled
        strike = _dec(obs.get("strike_mw"))
        physical_yes = 1 if (strike is not None and peak > strike) else 0

        kalshi_result = None
        kalshi_expiration = None
        if client is not None:
            try:
                m = client.market(obs.get("market_ticker") or "")
                kalshi_result = m.get("result")
                kalshi_expiration = _dec(m.get("expiration_value"))
            except Exception:  # noqa: BLE001 — market may not be settled yet
                pass

        out.append({
            "observation_id": oid,
            "ercot_peak_mw": peak,
            "physical_outcome_yes": physical_yes,
            "kalshi_result": kalshi_result,
            "kalshi_expiration_value": kalshi_expiration,
            "resolved_at": datetime.now(UTC).isoformat(),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="E3 shadow-observation runner")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print but do not write the ledger")
    ap.add_argument("--max-days", type=int, default=0,
                    help="only evaluate the first N operating days (testing)")
    ap.add_argument("--no-kalshi-resolve", action="store_true",
                    help="skip Kalshi result lookups during resolution")
    args = ap.parse_args()

    model_version = MODEL_VERSION
    actuals = _load_actuals(ACTUALS)
    forecast_peaks, forecast_first = _load_forecast(FORECAST_VINTAGES)
    kalshi_by_day = _load_kalshi(KALSHI_LADDERS)

    if args.max_days and len(kalshi_by_day) > args.max_days:
        kalshi_by_day = dict(sorted(kalshi_by_day.items())[: args.max_days])

    new_obs = build_new_observations(model_version, kalshi_by_day, forecast_peaks, forecast_first, actuals)
    existing = load_existing_ids(OBSERVATIONS)
    fresh = [o for o in new_obs if o["observation_id"] not in existing]

    if args.dry_run:
        print(f"[dry-run] prospective observations: {len(new_obs)} (new: {len(fresh)})")
        for o in fresh[:20]:
            print(json.dumps(o, sort_keys=True, default=str))
        return 0

    if fresh:
        append_jsonl(OBSERVATIONS, fresh)
        print(f"appended {len(fresh)} new observations")

    client = None if args.no_kalshi_resolve else KalshiPublicClient()
    resolutions = build_resolutions(OBSERVATIONS, actuals, client)
    if resolutions:
        append_jsonl(RESOLUTIONS, resolutions)
        print(f"appended {len(resolutions)} resolutions")

    print(
        f"ledger: {_count_lines(OBSERVATIONS)} observations, "
        f"{_count_lines(RESOLUTIONS)} resolutions"
    )
    print()
    print(_status_report(OBSERVATIONS, RESOLUTIONS))
    return 0


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.open("r", encoding="utf-8"))


def _status_report(observations_path: Path, resolutions_path: Path) -> str:
    obs: list[dict] = []
    if observations_path.exists():
        for line in observations_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    resolved = load_resolved_ids(resolutions_path)

    days = sorted({r["operating_day"] for r in obs})
    strikes = sorted({r["strike_mw"] for r in obs})

    def _pos(x: object) -> bool:
        return x is not None and D(str(x)) > 0

    def _num(x: object) -> Decimal:
        v = _dec(x)
        return v if v is not None else D(0)

    cand_yes = [r for r in obs if _pos(r.get("edge_yes"))]
    cand_no = [r for r in obs if _pos(r.get("edge_no"))]
    fill1_yes = sum(1 for r in cand_yes if _num(r.get("yes_ask_size")) >= D(1))
    fill10_yes = sum(1 for r in cand_yes if _num(r.get("yes_ask_size")) >= D(10))
    fill1_no = sum(1 for r in cand_no if _num(r.get("no_ask_size")) >= D(1))
    fill10_no = sum(1 for r in cand_no if _num(r.get("no_ask_size")) >= D(10))

    lines = [
        "E3 SHADOW OBSERVATION STATUS",
        "----------------------------",
        f"  prospective cutover:     {PROSPECTIVE_CUTOVER.isoformat()}",
        f"  eligible leads:          {sorted({r['lead_bucket'] for r in obs})}",
        "",
        f"  prospective observations: {len(obs)}",
        f"  unique operating days:    {len(days)}",
        f"  unique strikes:           {len(strikes)}",
        "",
        f"  candidate YES edges:      {len(cand_yes)}",
        f"  candidate NO edges:       {len(cand_no)}",
        "",
        f"  fillable @ 1 contract:    YES {fill1_yes}, NO {fill1_no}",
        f"  fillable @ 10 contracts:  YES {fill10_yes}, NO {fill10_no}",
        "",
        f"  resolved observations:    {len(resolved)}",
        "",
        f"  E3 ALPHA = UNKNOWN (until enough prospective operating days resolve)",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())