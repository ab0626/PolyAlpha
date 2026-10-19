"""ERCOT NP6-905-CD Settlement Point Price acquisition (E4-v0 data lineage).

Implements the data-capture side of ``docs/ENERGY_E4_SPEC.md`` — nothing else.
This module fetches, parses, filters, and identity-classifies ERCOT real-time
Settlement Point Prices for a single hub (HB_WEST). It has NO model logic and NO
execution authority, and imports nothing from E3 or the production-submit path.

Auth (ERCOT Public API, documented flow):
  - ID token via ROPC flow, valid 1 hour, cannot be refreshed (re-acquire).
  - subscription key via ``Ocp-Apim-Subscription-Key`` header.
Credentials are read from environment only (never stored in the repo). Fail
closed: any auth/fetch error propagates so the collector can record it rather
than silently falling back to a different semantic source.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

TOKEN_URL = (
    "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
    "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
)
BASE_URL = "https://api.ercot.com/api/public-reports"
SPP_PATH = "np6-905-cd/spp_node_zone_hub"
# Fixed for every ERCOT public-API user (NOT a secret).
CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"
SCOPE = "openid fec253ea-0d06-4272-a5e6-b478baeecd70 offline_access"
HUB = "HB_WEST"
_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


class SppAuthError(RuntimeError):
    """Raised when the ERCOT ID token cannot be acquired (fail closed)."""


def acquire_id_token(username: str, password: str, timeout: float = 30.0) -> str:
    """Obtain an ERCOT public-API ID token via the ROPC flow.

    Raises :class:`SppAuthError` on any failure so callers never proceed with a
    missing/expired token.
    """
    params = {
        "username": username,
        "password": password,
        "grant_type": "password",
        "scope": SCOPE,
        "client_id": CLIENT_ID,
        "response_type": "id_token",
    }
    url = f"{TOKEN_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="POST", headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — fail closed on any transport error
        raise SppAuthError(f"token acquisition failed: {exc}") from exc
    token = data.get("id_token")
    if not token:
        raise SppAuthError(f"token response missing id_token: {list(data.keys())}")
    return token


class SppClient:
    """Caches the 1-hour ID token and fetches raw NP6-905-CD payloads."""

    def __init__(self, subscription_key: str, username: str, password: str) -> None:
        self.subscription_key = subscription_key
        self.username = username
        self.password = password
        self._token: str | None = None
        self._token_expires: float = 0.0

    def _ensure_token(self) -> str:
        # 60s safety margin before the documented 1-hour expiry.
        if self._token is not None and time.time() < self._token_expires - 60:
            return self._token
        self._token = acquire_id_token(self.username, self.password)
        self._token_expires = time.time() + 3600.0
        return self._token

    def fetch_raw(self, delivery_date: str) -> bytes:
        """Fetch the raw JSON payload for one delivery date, filtered to the hub.

        ``settlementPoint=HB_WEST`` is applied server-side so we pull only the hub's
        intervals (<= 96 rows) rather than the entire settlement-point universe,
        which would require pagination.
        """
        token = self._ensure_token()
        params = {
            "deliveryDateFrom": delivery_date,
            "deliveryDateTo": delivery_date,
            "settlementPoint": HUB,
        }
        url = f"{BASE_URL}/{SPP_PATH}?{urllib.parse.urlencode(params)}"
        headers = dict(_UA)
        headers["Ocp-Apim-Subscription-Key"] = self.subscription_key
        headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()


def parse_spp(raw: bytes) -> list[dict]:
    """Parse the NP6-905-CD payload into normalized HB_WEST observations.

    Handles both ``data`` as a list of dicts and ``data`` as a list of rows with a
    separate ``fields`` metadata block. Filters to the hub (``HB_WEST``). Each
    returned observation is a dict with normalized keys (see the collector).
    """
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid NP6-905-CD JSON: {exc}") from exc

    rows = payload.get("data", [])
    if not isinstance(rows, list):
        raise ValueError("NP6-905-CD payload has no data[] rows")

    fields = payload.get("fields") or []
    field_names = [str(f.get("name") if isinstance(f, dict) else f).lower() for f in fields]

    out: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            obs = {str(k).lower(): v for k, v in row.items()}
        elif isinstance(row, list):
            if not field_names or len(field_names) != len(row):
                continue
            obs = dict(zip(field_names, row))
        else:
            continue

        point = str(obs.get("settlementpoint") or "").strip().upper()
        if point != HUB:
            continue

        dst = obs.get("dstflag")
        if isinstance(dst, bool):
            dst_str = "Y" if dst else "N"
        else:
            dst_str = str(dst) if dst is not None else ""

        out.append({
            "operating_day": str(obs.get("deliverydate") or ""),
            "delivery_hour": str(obs.get("deliveryhour") or ""),
            "delivery_interval": str(obs.get("deliveryinterval") or ""),
            "repeated_hour_flag": dst_str,
            "hub": HUB,
            "price": str(obs.get("settlementpointprice") or ""),
        })
    return out


def natural_interval_key(obs: dict) -> tuple:
    """Identity of an interval under the E4-v0 settlement semantics.

    Includes the repeated-hour flag because Kalshi's rule counts repeated DST
    intervals separately; identifying by interval-ending alone would collapse them.
    """
    return (
        obs.get("operating_day", ""),
        obs.get("delivery_hour", ""),
        obs.get("delivery_interval", ""),
        obs.get("repeated_hour_flag", ""),
        obs.get("hub", HUB),
    )