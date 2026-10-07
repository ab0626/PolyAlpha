"""ERCOT public data client (key-gated).

The Kalshi ``KXTXERCOTPEAKD`` ladder resolves to the ERCOT **"Actual System Load
by Forecast Zone"** CSV: expiration value = the highest value in the ``TOTAL``
column of the first complete report for the operating day. Later revisions do
not affect the expiration value (verified from Kalshi ``rules_secondary``).

ERCOT's Public Data API requires a free subscription key + OAuth2 token:

    https://developer.ercot.com

There is no anonymous access (verified: ``api.ercot.com`` returns 401/403
without credentials). This module is therefore a key-ready client, not a toy
scraper: raw responses must be preserved with source/receipt clocks, and
preliminary vs. final load values must never be conflated.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from typing import Any

API_BASE = "https://api.ercot.com/api/public-reports"

# Product names relevant to the ERCOT peak-load wedge. EMIL IDs should be
# resolved at integration time via GET /api/public-reports (they are stable but
# versioned); do not hard-code without verifying against the live listing.
PRODUCT_ACTUAL_LOAD_FORECAST_ZONE = "Actual System Load by Forecast Zone"
PRODUCT_ACTUAL_LOAD_WEATHER_ZONE = "Actual System Load by Weather Zone"


class ErcotClient:
    """Minimal ERCOT Public Data API client (OAuth2 + subscription key)."""

    def __init__(self, subscription_key: str, token: str, timeout: float = 30):
        if not subscription_key or not token:
            raise ValueError("ERCOT requires a subscription key and an OAuth2 token")
        self.subscription_key = subscription_key
        self.token = token
        self.timeout = timeout

    def get_report(self, emil_id: str, params: dict | None = None) -> tuple[dict, datetime]:
        """GET a public report by EMIL id; returns (payload, received_at)."""
        url = f"{API_BASE}/{emil_id}"
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        req = urllib.request.Request(
            url,
            headers={
                "Ocp-Apim-Subscription-Key": self.subscription_key,
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body, datetime.now().astimezone()


def list_products(client: ErcotClient) -> Any:
    """Return the live EMIL product listing (resolve ids at integration time)."""
    body, _ = client.get_report("")  # placeholder; list endpoint varies
    return body