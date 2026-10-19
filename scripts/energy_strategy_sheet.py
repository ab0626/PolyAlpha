#!/usr/bin/env python3
"""Energy strategy sheet — read-only realized-edge measurement report.

Joins the E3 shadow-observation + resolution ledgers with the live-calibration
fill ledger and (optionally) the Kalshi WS quote stream, then prints the honest
"What do we know vs. what are we incapable of estimating yet" sheet.

Read-only: it never trades, never mutates a ledger row, and never scores the
frozen evaluation. It writes one derived report JSON.

Usage:
    .venv/Scripts/python.exe scripts/energy_strategy_sheet.py [--skip-markouts]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.strategy_sheet import (  # noqa: E402
    STATUS_CONTINUE,
    STATUS_INSUFFICIENT,
    STATUS_KILL,
    STATUS_PROMOTE,
    build_strategy_sheet,
    load_live_calibration,
    load_observations,
    load_resolutions,
)

OBSERVATIONS = ROOT / "data" / "energy" / "shadow_observations.jsonl"
RESOLUTIONS = ROOT / "data" / "energy" / "shadow_resolutions.jsonl"
LIVE_CAL = ROOT / "data" / "energy" / "kalshi" / "live_calibration_v0.jsonl"
WS_DIR = ROOT / "data" / "energy" / "kalshi" / "ws"
LADDERS = ROOT / "data" / "energy" / "kalshi" / "ladders"
MARKOUT_MEASUREMENTS = ROOT / "data" / "energy" / "kalshi" / "markout_measurements.jsonl"
REPORT = ROOT / "data" / "energy" / "reports" / "strategy_sheet_e3.json"


def _human_summary(sheet: dict) -> str:
    d = sheet["denominator"]
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("ENERGY STRATEGY SHEET — E3 (read-only realized-edge measurement)")
    lines.append("=" * 72)
    idn = sheet["identity"]
    lines.append(f"family={idn['family']}  model={idn['model_version']}  venue={idn['venue']}  series={idn['series']}")
    lines.append(f"window={idn['observation_window']['min_operating_day']}..{idn['observation_window']['max_operating_day']}")
    lines.append("")
    lines.append("WHAT WE KNOW (counts):")
    lines.append(f"  observations            {d['observations']}")
    lines.append(f"  eligible candidates     {d['eligible_candidates']}")
    lines.append(f"  resolved observations   {d['resolved_observations']}")
    lines.append(f"  real trades             {d['real_trades']}")
    lines.append(f"  real settled trades     {d['real_settled_trades']}")
    lines.append(f"  unique operating days   {d['unique_operating_days']}")
    lines.append(f"  unique strikes          {d['unique_strikes']}")
    lines.append("")
    lines.append("WHAT WE ARE INCAPABLE OF ESTIMATING YET (0 resolutions):")
    for section in ("calibration", "realized_edge", "capture"):
        lines.append(f"  {section:22s} {sheet[section]['status']}")
    lines.append("")
    # Predicted edge distribution (estimable even at 0 resolutions).
    e = sheet["edge"]
    lines.append("PREDICTED EDGE DISTRIBUTION (estimable now):")
    if e["predicted_edge"]["min"] is not None:
        pe = e["predicted_edge"]
        lines.append(f"  n={e['n']}  min={pe['min']:.4f}  median={pe['median']:.4f}  max={pe['max']:.4f}  positive={pe['positive_count']}")
    else:
        lines.append("  (no edge observations)")
    lines.append("")
    # Real trades.
    lines.append("REAL (LIVE-CALIBRATION) TRADES:")
    for rt in sheet["execution"]["real_trades"]:
        lines.append(f"  trade {rt['trade_id'][:8]}  {rt['ticker']}  side={rt['economic_side']} entry={rt['entry_price_no']}")
        lines.append(f"    predicted_edge={rt['predicted_edge']}  terminal_pnl={rt['terminal_pnl']}  "
                     f"terminal_edge/ct={rt['terminal_edge_per_contract']}")
        lines.append(f"    settlement={rt['settlement_result']}  ercot_peak={rt['ercot_realized_peak_mw']}")
        mk = rt.get("markouts")
        if mk:
            for h in ("5s", "30s", "300s", "1800s"):
                m = mk.get(h)
                if not m:
                    continue
                if m["status"] == "available":
                    exit_s = f"{m['markout_exit']:+.4f}" if m["markout_exit"] is not None else "n/a"
                    lines.append(f"    markout {h:>5s}: mid={m['markout_mid']:+.4f}  exit={exit_s}  "
                                 f"[src={m['quote_source']}, +{m['elapsed_seconds']}s, no_bid={m['no_bid']}]")
                elif m["status"] == "available_no_exit_liquidity":
                    lines.append(f"    markout {h:>5s}: mid={m['markout_mid']:+.4f}  exit=NO_EXIT_LIQUIDITY "
                                 f"[src={m['quote_source']}, +{m['elapsed_seconds']}s]")
                else:
                    lines.append(f"    markout {h:>5s}: {m['status']}")
        else:
            lines.append("    markouts: not computed (skipped)")
    lines.append("")
    lines.append("DISPLAYED CAPACITY PROXY:")
    cp = sheet["capacity"]
    lines.append(f"  {cp['label']}")
    lines.append(f"    YES candidates depth: {cp['yes_candidates_depth']}")
    lines.append(f"    NO  candidates depth: {cp['no_candidates_depth']}")
    lines.append("")
    lines.append("RELATIVE VALUE (cross-strike monotonicity, per operating day):")
    for day, rv in sheet["relative_value"].items():
        lines.append(f"  {day}: n_strikes={rv['n_strikes']} monotone={rv['monotone']} violations={len(rv['violations'])}")
    lines.append("")
    lines.append("FILLABILITY:")
    f = sheet["execution"]["fillability"]
    lines.append(f"  candidate YES edges: {f['candidate_yes']}   candidate NO edges: {f['candidate_no']}")
    lines.append("")
    mp = sheet.get("markout_measurements") or {}
    if mp.get("path"):
        lines.append(f"MARKOUT MEASUREMENTS (append-only derived stream):")
        lines.append(f"  path={mp['path']}  derivation={mp.get('derivation_version')}")
        lines.append(f"  appended={mp['appended']}  seen={mp['seen']}  (idempotent; never rewritten)")
        lines.append("")
    conf = sheet["confidence"]
    lines.append(f"CONFIDENCE: decision-usable(calibration)={conf['calibration_decision_usable']}  "
                 f"decision-usable(ECR)={conf['ecr_decision_usable']}  n_resolved={conf['n_resolved']}")
    lines.append("")
    lines.append(f"VERDICT: {sheet['verdict']}")
    if sheet["verdict"] == STATUS_INSUFFICIENT:
        lines.append("  -> refuse to make an alpha verdict at this N; metrics not filled with zero.")
    elif sheet["verdict"] == STATUS_CONTINUE:
        lines.append("  -> accumulating evidence; no promotion/kill decision yet.")
    elif sheet["verdict"] == STATUS_PROMOTE:
        lines.append("  -> candidate for capital allocation (human review required).")
    elif sheet["verdict"] == STATUS_KILL:
        lines.append("  -> kill candidate (human review required).")
    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Energy strategy sheet (read-only)")
    ap.add_argument("--skip-markouts", action="store_true",
                    help="skip the WS quote scan for real-trade markouts (fast path)")
    ap.add_argument("--out", default=str(REPORT), help="report JSON path")
    args = ap.parse_args()

    observations = load_observations(OBSERVATIONS)
    resolutions = load_resolutions(RESOLUTIONS)
    trades, settlements = load_live_calibration(LIVE_CAL)

    ws_dir = None if args.skip_markouts else WS_DIR
    ladder_dir = None if args.skip_markouts else LADDERS
    markout_path = None if args.skip_markouts else MARKOUT_MEASUREMENTS
    sheet = build_strategy_sheet(
        observations, resolutions, trades, settlements,
        ws_dir=ws_dir, ladder_dir=ladder_dir, markout_path=markout_path,
    )

    print(_human_summary(sheet))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sheet, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(f"\nWROTE {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())