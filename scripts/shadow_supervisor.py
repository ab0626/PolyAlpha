#!/usr/bin/env python3
"""Durable shadow-external-v0 supervisor: source-specific scheduling.

Runs four independent loops so one broken provider never stalls the others:

    macro_slow    FRED + EIA            every 6h  (4x/day)
    primary_fast  SEC EDGAR + Fed RSS   every 30s
    weather       NWS alerts            every 30s (>= NWS guidance)
    breadth_slow  GDELT                 10m base, adaptive backoff on 429

Each loop owns its raw sub-directory, watermark, and status file. Observation
only — nothing feeds v0.4.
"""

import argparse
import json
import os
import random
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.rawstore import RawStore  # noqa: E402
from polyalpha.shadow import ExternalCollector, SourceClass  # noqa: E402
from polyalpha.shadow.sources import (  # noqa: E402
    EiaProvider,
    FredProvider,
    GdeltProvider,
    NwsProvider,
    RssProvider,
    SecProvider,
)


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


def _build_loops(cfg: dict) -> dict[str, dict]:
    providers = {"fred": [], "eia": [], "nws": [], "sec": [], "rss": [], "gdelt": []}
    for p in cfg.get("providers", []):
        kind = p["kind"]
        if kind == "fred":
            providers["fred"].append(FredProvider(p["series_ids"]))
        elif kind == "eia":
            providers["eia"].append(EiaProvider(p["routes"]))
        elif kind == "nws":
            providers["nws"].append(NwsProvider(p.get("state_codes")))
        elif kind == "sec":
            providers["sec"].append(SecProvider(p["ciks"]))
        elif kind == "rss":
            providers["rss"].append(RssProvider(p["name"], SourceClass[p["source_class"]], p["urls"]))
        elif kind == "gdelt":
            providers["gdelt"].append(GdeltProvider(p["query"]))

    return {
        "macro_slow": {"interval": 6 * 3600, "providers": providers["fred"] + providers["eia"]},
        "primary_fast": {"interval": 30, "providers": providers["sec"] + providers["rss"]},
        "weather": {"interval": 30, "providers": providers["nws"]},
        "breadth_slow": {"interval": 600, "providers": providers["gdelt"], "adaptive": True},
    }


def _run_loop(name: str, loop: dict, base_dir: Path, status_dir: Path) -> None:
    raw = RawStore(base_dir / "raw" / name, collector_version="shadow-external-v0")
    watermark = base_dir / "watermarks" / f"{name}.json"
    collector = ExternalCollector(loop["providers"], raw, watermark)

    interval = loop["interval"]
    failures = 0
    while True:
        try:
            stats = collector.collect()
            provider_errors = {
                k: v.get("error") for k, v in stats["providers"].items() if v.get("error")
            }
            if loop.get("adaptive") and provider_errors:
                # Provider errors (e.g. GDELT 429) are captured inside collect();
                # they must still drive backoff, not the success path.
                failures += 1
                interval = min(loop["interval"] * (2 ** failures) + random.uniform(0, 5), 3600)
                status = {
                    "loop": name, "ts": datetime.now(UTC).isoformat(),
                    "last_error": provider_errors,
                    "degraded": failures >= 5,
                }
            else:
                failures = 0
                interval = loop["interval"]
                status = {
                    "loop": name,
                    "ts": datetime.now(UTC).isoformat(),
                    "last_success": datetime.now(UTC).isoformat(),
                    "new": {k: v.get("new") for k, v in stats["providers"].items()},
                    "errors": provider_errors,
                }
        except Exception as error:  # noqa: BLE001
            status = {"loop": name, "ts": datetime.now(UTC).isoformat(),
                      "last_error": str(error)}
            if loop.get("adaptive"):
                failures += 1
                interval = min(loop["interval"] * (2 ** failures) + random.uniform(0, 5), 3600)
                status["degraded"] = failures >= 5
        (status_dir / f"{name}.json").write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow external supervisor")
    parser.add_argument("--providers", default=str(ROOT / "config" / "external_providers.json"))
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--base-dir", default="data/shadow")
    parser.add_argument("--status-dir", default="data/shadow/status")
    args = parser.parse_args()

    _load_env(Path(args.env))
    cfg = json.loads(Path(args.providers).read_text(encoding="utf-8"))
    loops = _build_loops(cfg)

    base_dir = Path(args.base_dir)
    status_dir = Path(args.status_dir)
    status_dir.mkdir(parents=True, exist_ok=True)

    threads = []
    for name, loop in loops.items():
        if not loop["providers"]:
            continue
        t = threading.Thread(target=_run_loop, args=(name, loop, base_dir, status_dir), daemon=True)
        t.start()
        threads.append(t)
        print(f"started loop {name} (interval {loop['interval']}s, {len(loop['providers'])} providers)", flush=True)

    while any(t.is_alive() for t in threads):
        time.sleep(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
