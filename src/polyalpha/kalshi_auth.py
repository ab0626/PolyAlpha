"""Authenticated Kalshi Trade API client — read-only layer.

Implements Kalshi's request signing (Ed25519 or RSA-PSS) and an authenticated
REST client for read-only account and market data. Execution (create/cancel/
amend orders) is intentionally NOT in this module: that is a separate, disabled
layer until forward alpha survives.

Credentials are read from the environment (never hard-coded, never logged):

    KALSHI_API_KEY_ID         the API Key ID (looks like a UUID)
    KALSHI_PRIVATE_KEY_PATH   path to the downloaded .pem private key file
    KALSHI_ENV                "production" | "demo"

Signing (see docs.kalshi.com — API Keys):
    message = timestamp_ms + METHOD + path        (path = full path from API
    root, WITHOUT query parameters)
    Ed25519 signs the message directly; RSA uses RSA-PSS with SHA-256.

Endpoints (see docs.kalshi.com — API Environments and Endpoints):
    production REST  https://external-api.kalshi.com/trade-api/v2
    demo REST        https://external-api.demo.kalshi.co/trade-api/v2
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ENV_KEY_ID = "KALSHI_API_KEY_ID"
ENV_PRIVATE_KEY_PATH = "KALSHI_PRIVATE_KEY_PATH"
ENV_ENV = "KALSHI_ENV"

API_ROOT = "/trade-api/v2"
BASE_URLS = {
    "production": "https://external-api.kalshi.com" + API_ROOT,
    "demo": "https://external-api.demo.kalshi.co" + API_ROOT,
}


def load_private_key(path: str | Path):
    """Load a Kalshi private key from a PEM file (PKCS#8 Ed25519 or PKCS#1 RSA)."""
    with open(path, "rb") as fh:
        return serialization.load_pem_private_key(fh.read(), password=None)


def sign_request(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """Return the base64 signature for ``timestamp + METHOD + path``.

    ``path`` must be the full path from the API root (e.g.
    ``/trade-api/v2/portfolio/balance``) with query parameters stripped.
    """
    message = f"{timestamp_ms}{method}{path.split('?')[0]}".encode("utf-8")
    if isinstance(private_key, Ed25519PrivateKey):
        signature = private_key.sign(message)
    else:
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    return base64.b64encode(signature).decode("utf-8")


class KalshiAuthClient:
    """Authenticated, read-only Kalshi Trade API client (stdlib transport)."""

    def __init__(
        self,
        key_id: str | None = None,
        private_key_path: str | Path | None = None,
        env: str | None = None,
        timeout: float = 30,
    ):
        self.key_id = key_id or os.environ.get(ENV_KEY_ID, "")
        key_path = private_key_path or os.environ.get(ENV_PRIVATE_KEY_PATH, "")
        env = env or os.environ.get(ENV_ENV, "demo")
        self.base_url = BASE_URLS.get(env)
        if self.base_url is None:
            raise ValueError(f"KALSHI_ENV must be one of {sorted(BASE_URLS)}, got {env!r}")
        if not self.key_id or not key_path:
            raise ValueError(
                f"{ENV_KEY_ID} and {ENV_PRIVATE_KEY_PATH} must both be set"
            )
        self._private_key = load_private_key(key_path)
        self.timeout = timeout

    def _headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        signature = sign_request(self._private_key, timestamp, method, API_ROOT + path)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def request(self, method: str, path: str, params: dict | None = None) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        req = urllib.request.Request(url, headers=self._headers(method, path))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Kalshi HTTP {exc.code}: {body[:300]}") from exc

    # ── Read-only account endpoints ────────────────────────────────────────

    def get_balance(self) -> Any:
        return self.request("GET", "/portfolio/balance")

    def get_positions(self, **params: Any) -> Any:
        return self.request("GET", "/portfolio/positions", params or None)

    def get_orders(self, **params: Any) -> Any:
        return self.request("GET", "/portfolio/orders", params or None)

    def get_fills(self, **params: Any) -> Any:
        return self.request("GET", "/portfolio/fills", params or None)

    def get_settlements(self, **params: Any) -> Any:
        return self.request("GET", "/portfolio/settlements", params or None)

    # ── Market-data endpoints (also available unauthenticated) ─────────────

    def get_markets(self, **params: Any) -> Any:
        return self.request("GET", "/markets", params or None)

    def get_market(self, ticker: str) -> Any:
        return self.request("GET", f"/markets/{ticker}")

    def get_market_orderbook(self, ticker: str, **params: Any) -> Any:
        return self.request("GET", f"/markets/{ticker}/orderbook", params or None)

    def get_exchange_status(self) -> Any:
        return self.request("GET", "/exchange/status")