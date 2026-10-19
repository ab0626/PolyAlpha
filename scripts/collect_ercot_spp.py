#!/usr/bin/env python3
"""Prospective ERCOT NP6-905-CD Settlement Point Price collector (E4-v0 lineage).

Append-only capture of ERCOT West Hub (HB_WEST) 15-minute real-time SPPs. Raw
response bytes are persisted and hashed BEFORE parsing; parsed observations are
classified first-seen vs. revision against a natural interval key that includes
the repeated-hour flag. Never backfills pre-spec data, never replaces a price.

Lineage:
    data/energy/e4/ercot_spp/raw/<ts>_<sha8>.json   raw responses (bytes preserved)
    data/energy/e4/ercot_spp/observations.jsonl     parsed append-only observations
    data/energy/e4/ercot_spp/heartbeat.json         freshness / health

Credentials are read from environment only (fail closed if absent):
    ERCOT_SUBSCRIPTION_KEY, ERCOT_USERNAME, ERCOT_PASSWORD

Usage:
    .venv/Scripts/python.exe scripts/collect_ercot_spp.py --interval 300
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.ercot_spp import (  # noqa: E402
    HUB,
    SppClient,
    natural_interval_key,
    parse_spp,
)

DATA = ROOT / "data" / "energy" / "e4" / "ercot_spp"
RAW_DIR = DATA / "raw"
OBS_PATH = DATA / "observations.jsonl"
HEARTBEAT_PATH = DATA / "heartbeat.json"
CENTRAL = ZoneInfo("America/Chicago")
DEFAULT_INTERVAL = 300.0  # seconds; 15-min product cadence + retry


def _load_seen() -> dict[tuple, str]:
    """Reconstruct {natural_key: last_price} from the existing append-only store.

    Deterministic restart/replay: a key already present is never re-classified as
    first-seen, and the recorded last price decides whether a re-observation is a
    duplicate (skip) or a revision (append with is_first_seen=false).
    """
    seen: dict[tuple, str] = {}
    if not OBS_PATH.exists():
        return seen
    for line in OBS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = tuple(d.get("natural_key", "").split("|")) if d.get("natural_key") else ()
        if len(key) == 5:
            seen[key] = str(d.get("price", ""))
    return seen


def _write_heartbeat(status: dict) -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEARTBEAT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(HEARTBEAT_PATH)


def _append_observations(obs_list: list[dict], raw_sha: str, received_at: str, seen: dict[tuple, str]) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    appended = 0
    with OBS_PATH.open("a", encoding="utf-8") as fh:
        for obs in obs_list:
            key = natural_interval_key(obs)
            price = obs.get("price", "")
            if key in seen:
                if price == seen[key]:
                    continue  # unchanged duplicate: neither first-seen nor revision
                is_first = False
            else:
                is_first = True
            seen[key] = price
            rec = {
                **obs,
                "natural_key": "|".join(key),
                "raw_payload_sha256": raw_sha,
                "received_at": received_at,
                "is_first_seen": is_first,
            }
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            appended += 1
    return appended


def main() -> int:
    ap = argparse.ArgumentParser(description="E4 ERCOT SPP collector")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    ap.add_argument("--once", action="store_true", help="single poll then exit")
    args = ap.parse_args()

    sub_key = os.environ.get("ERCOT_SUBSCRIPTION_KEY", "")
    username = os.environ.get("ERCOT_USERNAME", "")
    password = os.environ.get("ERCOT_PASSWORD", "")
    if not (sub_key and username and password):
        print("ERROR: ERCOT_SUBSCRIPTION_KEY/ERCOT_USERNAME/ERCOT_PASSWORD required (fail closed)", file=sys.stderr)
        return 1

    client = SppClient(sub_key, username, password)
    seen = _load_seen()
    status = {
        "started_at": datetime.now(UTC).isoformat(),
        "interval_seconds": args.interval,
        "hub": HUB,
        "last_successful_poll_at": None,
        "last_new_observation_at": None,
        "total_observations": len(seen),
        "last_error": None,
        "last_error_at": None,
    }
    _write_heartbeat(status)

    run = 0
    while args.cycles == 0 or run < args.cycles:
        now = datetime.now(UTC)
        today = now.astimezone(CENTRAL).date().isoformat()
        # Also catch late-published intervals from the previous operating day.
        prev = (now.astimezone(CENTRAL).date() - timedelta(days=1)).isoformat()
        try:
            appended = 0
            for delivery_date in (prev, today):
                raw = client.fetch_raw(delivery_date)
                raw_sha = hashlib.sha256(raw).hexdigest()
                raw_path = RAW_DIR / f"{delivery_date}_{raw_sha[:12]}.json"
                if not raw_path.exists():
                    raw_path.write_bytes(raw)
                obs_list = parse_spp(raw)
                received_at = datetime.now(UTC).isoformat()
                appended += _append_observations(obs_list, raw_sha, received_at, seen)

            status["last_successful_poll_at"] = datetime.now(UTC).isoformat()
            status["total_observations"] = len(seen)
            if appended:
                status["last_new_observation_at"] = status["last_successful_poll_at"]
            status["last_error"] = None
            status["last_error_at"] = None
            print(f"[{status['last_successful_poll_at']}] {today}: +{appended} new/revised obs (total {len(seen)})", flush=True)
        except Exception as exc:  # noqa: BLE001 — durable: record + retry
            status["last_error"] = f"{type(exc).__name__}: {exc}"
            status["last_error_at"] = datetime.now(UTC).isoformat()
            print(f"[{status['last_error_at']}] ERROR {status['last_error']}", file=sys.stderr, flush=True)

        _write_heartbeat(status)
        run += 1
        if args.once or (args.cycles != 0 and run >= args.cycles):
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())