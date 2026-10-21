#!/usr/bin/env python3
"""Phase 0 — ERCOT causal-source -> Kalshi reaction latency (READ-ONLY).

Implements the frozen contract in docs/PHASE0_INFO_LATENCY.md. Consumes existing
captures only; writes a derived report to stdout / a JSONL. Never trades, never
mutates a ledger, never touches #6C collectors.

Usage:
    .venv/Scripts/python.exe scripts/phase0_ercot_latency.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from energy_e3_runner import (  # noqa: E402
    ACTUALS,
    FORECAST_VINTAGES,
    _load_actuals,
    _load_forecast,
)
from polyalpha.energy.kalshi import ercot_operating_day  # noqa: E402
from polyalpha.energy.physical import ForecastErrorModel  # noqa: E402
from polyalpha.energy.shadow_observation import MAX_LEAD, MIN_RESIDUALS  # noqa: E402

D = Decimal

# Frozen Phase 0 constants (docs/PHASE0_INFO_LATENCY.md).
SERIES = "KXTXERCOTPEAKD"
DELTA_SURPRISE_MW = D("100")
DELTA_REACTION = D("0.01")
REPRICE_BAND = D("0.02")
TAKER_FEE = D("0.07")
LIVE_MAX_INGEST = 30 * 60  # seconds: received_at - publish <= 30 min

LADDERS = ROOT / "data" / "energy" / "kalshi" / "ladders"


def _dec(v):
    try:
        return D(str(v))
    except Exception:
        return None


def _parse_iso(v):
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _vintage_peaks(path: Path) -> dict[tuple[str, str], dict]:
    """{(delivery_day, publish_timestamp): {peak, received_at, publish_dt}}."""
    by_key: dict[tuple[str, str], list[tuple[Decimal, str]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            delivery = r.get("delivery_day")
            pub = r.get("publish_timestamp")
            v = _dec(r.get("system_total_mw"))
            if not delivery or not pub or v is None:
                continue
            by_key[(str(delivery), str(pub))].append((v, r.get("received_at") or ""))

    out: dict[tuple[str, str], dict] = {}
    for key, rows in by_key.items():
        out[key] = {
            "peak": max(r[0] for r in rows),
            "received_at": min(r[1] for r in rows),
            "publish_dt": _parse_iso(key[1]),
        }
    return out


def _events(vintages: dict) -> list[dict]:
    """Surprise events: consecutive same-day vintages with |dpeak| >= 100 MW."""
    by_day: dict[str, list[tuple[datetime, Decimal, str, datetime]]] = defaultdict(list)
    for (day, pub), v in vintages.items():
        dt = v["publish_dt"]
        if dt is None:
            continue
        by_day[day].append((dt, v["peak"], v["received_at"], _parse_iso(v["received_at"]) or dt))

    events: list[dict] = []
    for day, rows in by_day.items():
        rows.sort(key=lambda x: x[0])
        for prev, cur in zip(rows, rows[1:]):
            prev_dt, prev_peak, prev_recv, _ = prev
            cur_dt, cur_peak, cur_recv, _ = cur
            dpeak = cur_peak - prev_peak
            if abs(dpeak) < DELTA_SURPRISE_MW:
                continue
            # Live-only filter: exclude backfilled vintages.
            recv = cur[2]
            recv_dt = _parse_iso(recv) if recv else None
            if recv_dt is None or (recv_dt - cur_dt).total_seconds() > LIVE_MAX_INGEST:
                continue
            events.append({
                "delivery_day": day,
                "t_publish": cur_dt,
                "prev_peak": prev_peak,
                "new_peak": cur_peak,
                "delta_peak": dpeak,
                "received_at": recv_dt,
            })
    return events


def _fair_value(model: ForecastErrorModel, day: str, publish_dt: datetime, peak_mw: Decimal, strike: Decimal) -> Decimal | None:
    """Vintage-specific point-in-time P(actual > strike) using frozen residuals."""
    from datetime import date as _date
    try:
        target = _date.fromisoformat(day)
    except ValueError:
        return None
    lead = (target - publish_dt.date()).days
    lead = max(0, min(lead, model.max_lead))
    eps = model.residuals.get(lead, [])
    if len(eps) < model.min_residuals:
        return None
    threshold = strike - peak_mw
    r = sum(1 for e in eps if e <= threshold)
    n = len(eps)
    return D(1) - D(r) / D(n + 1)


def _load_ladder(days: set[str]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """(quotes_by_ticker, meta_by_day). Streams ladder files, substring-filtered."""
    quotes: dict[str, list[dict]] = defaultdict(list)
    meta: dict[str, list[dict]] = defaultdict(list)
    files = sorted(LADDERS.glob("*.jsonl"))
    for f in files:
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                if SERIES not in line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("series_ticker") != SERIES:
                    continue
                raw = r.get("raw") or {}
                close = raw.get("close_time")
                if not close:
                    continue
                op_day = ercot_operating_day(close)
                if op_day not in days:
                    continue
                ticker = r.get("ticker")
                cap = _parse_iso(r.get("captured_at"))
                if not ticker or cap is None:
                    continue
                yes_bid = _dec(raw.get("yes_bid_dollars"))
                yes_ask = _dec(raw.get("yes_ask_dollars"))
                yes_ask_size = _dec(raw.get("yes_ask_size_fp"))
                yes_bid_size = _dec(raw.get("yes_bid_size_fp"))
                thr = _dec(r.get("threshold"))
                if thr is None:
                    continue
                mid = None
                if yes_bid is not None and yes_ask is not None:
                    mid = (yes_bid + yes_ask) / D(2)
                elif yes_bid is not None:
                    mid = yes_bid
                elif yes_ask is not None:
                    mid = yes_ask
                quotes[ticker].append({
                    "captured_at": cap,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "yes_ask_size": yes_ask_size,
                    "yes_bid_size": yes_bid_size,
                    "mid": mid,
                })
                meta[op_day].append({"ticker": ticker, "threshold": thr})
    for t in quotes:
        quotes[t].sort(key=lambda q: q["captured_at"])
    # Dedupe meta per day.
    meta_out: dict[str, list[dict]] = {}
    for day, items in meta.items():
        seen = {}
        for it in items:
            seen[it["ticker"]] = it
        meta_out[day] = list(seen.values())
    return quotes, meta_out


def main() -> int:
    vintages = _vintage_peaks(FORECAST_VINTAGES)
    events = _events(vintages)
    if not events:
        print("no surprise events found (>= 100 MW live revisions)")
        return 0

    actuals = _load_actuals(ACTUALS)
    forecast_peaks, _first = _load_forecast(FORECAST_VINTAGES)
    model = ForecastErrorModel(actuals, forecast_peaks, max_lead=MAX_LEAD, min_residuals=MIN_RESIDUALS)

    days = {e["delivery_day"] for e in events}
    quotes, meta = _load_ladder(days)

    print(f"surprise events (live, >= {DELTA_SURPRISE_MW} MW): {len(events)}")
    print(f"operating days covered: {sorted(days)}")
    print()

    reaction_lats: list[float] = []
    repricing_lats: list[float] = []
    ingest_lats: list[float] = []
    edges: list[float] = []
    details: list[dict] = []

    for e in events:
        day = e["delivery_day"]
        day_meta = meta.get(day)
        if not day_meta:
            continue
        # ATM strike = nearest to new daily-peak forecast.
        atm = min(day_meta, key=lambda m: abs(m["threshold"] - e["new_peak"]))
        ticker = atm["ticker"]
        strike = atm["threshold"]
        qs = quotes.get(ticker)
        if not qs:
            continue
        p_new = _fair_value(model, day, e["t_publish"], e["new_peak"], strike)
        p_old = _fair_value(model, day, e["t_publish"], e["prev_peak"], strike)

        ingest = (e["received_at"] - e["t_publish"]).total_seconds()
        ingest_lats.append(ingest)

        direction = 1 if e["delta_peak"] > 0 else -1  # +1 => mid should rise
        t_first = None
        t_rep = None
        edge_at_action = None
        edge_has_size = False
        base_mid = None
        actionable_seen = False
        for q in qs:
            if q["captured_at"] < e["t_publish"]:
                base_mid = q["mid"]
                continue
            mid = q["mid"]
            if mid is None:
                continue
            # First reaction: mid moved >= 0.01 in the revision direction.
            if t_first is None and base_mid is not None and (mid - base_mid) * direction >= DELTA_REACTION:
                t_first = q["captured_at"]
            # Material repricing (relative, model-free): mid moved >= 0.02 in direction.
            if t_rep is None and base_mid is not None and (mid - base_mid) * direction >= REPRICE_BAND:
                t_rep = q["captured_at"]
            # Actionable edge: first snapshot at/after our source receipt, WITH real
            # ask size (a wide/stale ask without size is not executable).
            if (not actionable_seen and q["captured_at"] >= e["received_at"]
                    and p_new is not None and q["yes_ask"] is not None):
                actionable_seen = True
                size = q.get("yes_ask_size")
                if size is not None and size > 0:
                    edge_at_action = float(p_new - q["yes_ask"] - TAKER_FEE)
                    edge_has_size = True
            if t_first is not None and t_rep is not None and actionable_seen:
                break

        if t_first is not None:
            reaction_lats.append((t_first - e["t_publish"]).total_seconds())
        if t_rep is not None:
            repricing_lats.append((t_rep - e["t_publish"]).total_seconds())
        if edge_at_action is not None and edge_has_size:
            edges.append(edge_at_action)

        details.append({
            "delivery_day": day,
            "t_publish": e["t_publish"].isoformat(),
            "delta_peak_mw": str(e["delta_peak"]),
            "strike": str(strike),
            "p_old": str(p_old) if p_old is not None else None,
            "p_new": str(p_new) if p_new is not None else None,
            "ticker": ticker,
            "source_ingest_s": round(ingest, 1),
            "market_reaction_s": round((t_first - e["t_publish"]).total_seconds(), 1) if t_first else None,
            "material_repricing_s": round((t_rep - e["t_publish"]).total_seconds(), 1) if t_rep else None,
            "actionable_edge_yes": round(edge_at_action, 4) if (edge_at_action is not None and edge_has_size) else None,
        })

    def _pct(vals, p):
        if not vals:
            return None
        s = sorted(vals)
        idx = min(int(p * len(s)), len(s) - 1)
        return round(s[idx], 1)

    print("=== ERCOT -> Kalshi info-latency (30s ladder resolution) ===")
    print(f"events with a measurable reaction: {len(reaction_lats)} / {len(details)}")
    print()
    for name, vals in [
        ("source_ingest_latency   ", ingest_lats),
        ("market_reaction_latency ", reaction_lats),
        ("material_repricing      ", repricing_lats),
    ]:
        print(f"{name} p50={_pct(vals,0.50)}s  p95={_pct(vals,0.95)}s  p99={_pct(vals,0.99)}s  n={len(vals)}")
    print()
    print("executable edge at actionable time (P_new - yes_ask - 0.07), ask-size > 0:")
    print(f"  max={max(edges) if edges else None}  p50={_pct(edges,0.50)}  p95={_pct(edges,0.95)}  n_positive={sum(1 for x in edges if x > 0)}/{len(edges)}")
    print()

    out_path = ROOT / "data" / "energy" / "kalshi" / "phase0_ercot_latency.jsonl"
    with out_path.open("a", encoding="utf-8") as fh:
        for d in details:
            fh.write(json.dumps(d, sort_keys=True) + "\n")
    print(f"details appended -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())