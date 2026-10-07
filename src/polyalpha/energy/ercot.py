"""ERCOT Public Data API client (OAuth2 ROPC + Azure subscription key).

Auth (verified against the ERCOT developer docs):

    POST https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token
      username, password, grant_type=password,
      scope=openid <client_id> offline_access, client_id=<client_id>, response_type=id_token
      -> returns ``id_token`` (a JWT, valid ~1 hour)

Every API call then carries BOTH credentials:

    Ocp-Apim-Subscription-Key: <subscription key>
    Authorization: Bearer <id_token>

Tokens are minted automatically at runtime and cached until ~60s before expiry.

Credentials are read from the environment (never hard-coded):
    ERCOT_SUBSCRIPTION_KEY, ERCOT_USERNAME, ERCOT_PASSWORD
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TOKEN_URL = (
    "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
    "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
)
API_BASE = "https://api.ercot.com/api"
CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"
SCOPE = f"openid {CLIENT_ID} offline_access"

ENV_SUBSCRIPTION_KEY = "ERCOT_SUBSCRIPTION_KEY"
ENV_USERNAME = "ERCOT_USERNAME"
ENV_PASSWORD = "ERCOT_PASSWORD"

# Product names relevant to the ERCOT peak-load wedge (resolve EMIL ids via
# list_products(); do not hard-code ids without verifying the live listing).
PRODUCT_ACTUAL_LOAD_FORECAST_ZONE = "Actual System Load by Forecast Zone"
PRODUCT_ACTUAL_LOAD_WEATHER_ZONE = "Actual System Load by Weather Zone"


def _jwt_exp(token: str) -> float:
    """Extract the ``exp`` claim (unix seconds) from a JWT id_token."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    data = json.loads(base64.urlsafe_b64decode(payload))
    return float(data.get("exp", time.time() + 3600))


class ErcotClient:
    def __init__(
        self,
        subscription_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 60,
    ):
        self.subscription_key = subscription_key or os.environ.get(ENV_SUBSCRIPTION_KEY, "")
        self.username = username or os.environ.get(ENV_USERNAME, "")
        self.password = password or os.environ.get(ENV_PASSWORD, "")
        self.timeout = timeout
        self._token: str | None = None
        self._token_expiry: float = 0.0
        missing = [n for n, v in (
            ("ERCOT_SUBSCRIPTION_KEY", self.subscription_key),
            ("ERCOT_USERNAME", self.username),
            ("ERCOT_PASSWORD", self.password),
        ) if not v]
        if missing:
            raise ValueError("ERCOT credentials missing: " + ", ".join(missing))

    def _fetch_token(self) -> tuple[str, float]:
        params = urllib.parse.urlencode({
            "username": self.username,
            "password": self.password,
            "grant_type": "password",
            "scope": SCOPE,
            "client_id": CLIENT_ID,
            "response_type": "id_token",
        })
        req = urllib.request.Request(
            TOKEN_URL + "?" + params, data=b"",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ERCOT token HTTP {exc.code}: {body[:300]}") from exc
        token = data.get("id_token")
        if not token:
            raise RuntimeError(f"ERCOT token request failed: {data}")
        return token, _jwt_exp(token)

    def _ensure_token(self) -> str:
        if self._token is None or time.time() > self._token_expiry - 60:
            self._token, self._token_expiry = self._fetch_token()
        return self._token

    def get(self, path: str, params: dict | None = None) -> Any:
        token = self._ensure_token()
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Ocp-Apim-Subscription-Key": self.subscription_key,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def list_products(self) -> Any:
        """Return the live EMIL product listing (sanity check)."""
        return self.get("/public-reports")

    def get_report(self, emil_id: str, params: dict | None = None) -> Any:
        return self.get(f"/public-reports/{emil_id}", params)


def load_env_file(path: str) -> None:
    """Load credentials from a .env-style file into os.environ (no overwrite).

    Accepts both ``KEY=VALUE`` and ``name: value`` lines; maps the ERCOT portal's
    "primary key"/"secondary key" labels to ``ERCOT_SUBSCRIPTION_KEY``.
    """
    aliases = {
        "primary key": ENV_SUBSCRIPTION_KEY,
        "primary_key": ENV_SUBSCRIPTION_KEY,
        "secondary key": ENV_SUBSCRIPTION_KEY,
        "secondary_key": ENV_SUBSCRIPTION_KEY,
    }
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = "", ""
        if "=" in line:
            key, _, value = line.partition("=")
        elif ":" in line:
            key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        key = aliases.get(key, key.upper())
        os.environ.setdefault(key, value)
