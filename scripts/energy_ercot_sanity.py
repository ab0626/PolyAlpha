"""ERCOT auth sanity check.

Loads ERCOT credentials from an env file and calls GET /api/public-reports to
confirm the subscription key + auto-minted bearer token work. Also prints the
EMIL products relevant to the peak-load wedge so ids can be resolved from the
live listing rather than hard-coded.

    .venv/Scripts/python.exe scripts/energy_ercot_sanity.py [--env PATH]

Default env path: C:/Users/adith/ercot.env (also loads the repo .env).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.config import load_env  # noqa: E402
from polyalpha.energy.ercot import ErcotClient, load_env_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=r"C:\Users\adith\ercot.env")
    args = parser.parse_args()

    load_env(str(ROOT / ".env"))       # repo env (POLYMARKET/FRED/EIA + any ERCOT)
    load_env_file(args.env)            # the ERCOT credentials file

    try:
        client = ErcotClient()
    except ValueError as e:
        print("ERROR:", e)
        print()
        print("Add these to your env file (keep the values out of chat):")
        print("  ERCOT_SUBSCRIPTION_KEY=<primary key>")
        print("  ERCOT_USERNAME=<your ERCOT developer email>")
        print("  ERCOT_PASSWORD=<your ERCOT developer password>")
        print()
        print("A token is minted automatically at runtime (valid ~1 hour).")
        return 2

    products = client.list_products()
    embedded = products.get("_embedded", {})
    items = embedded.get("products", embedded.get("publicReports", [])) if isinstance(embedded, dict) else []
    print("sanity OK: /api/public-reports returned", len(items), "products")
    wanted = ("Actual System Load", "Load Forecast", "Wind Power Production",
              "Solar Power Production", "Resource Outage")
    for p in items:
        name = p.get("name") or p.get("description") or ""
        if any(w.lower() in name.lower() for w in wanted):
            print("  -", p.get("emilId"), "|", name[:80])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())