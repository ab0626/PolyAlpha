"""Polymarket US Retail execution client — AUTHENTICATED EXECUTION BOUNDARY.

This is a SCAFFOLD. It provides the venue-adapter seam the live-ready
execution gateway needs, but it is fail-closed: it transmits nothing unless
both (a) credentials are present and (b) ``live_enabled`` is True. Neither is
ever set automatically.

Credentials come from the environment (secrets boundary) and never enter
source, logs, manifests, or exception messages. The signing secret is held
only in memory and is never exposed as an attribute.

Auth (docs/POLYMARKET_US_API.md): headers ``X-PM-Access-Key``,
``X-PM-Timestamp`` (milliseconds), ``X-PM-Signature`` = Ed25519 over
``timestamp + method + path`` using a base64 secret, base64-encoded. The
timestamp must be within 30 seconds of server time. The exact canonical
string must be verified against the current venue docs before any real use.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_BASE = "https://api.polymarket.us"
ACCESS_KEY_ENV = "POLYMARKET_US_ACCESS_KEY"
SECRET_ENV = "POLYMARKET_US_SECRET"
REQUESTS_PER_SECOND = 20


class UsExecutionNotConfigured(RuntimeError):
    """Credentials or auth are absent."""


class UsExecutionDisabled(RuntimeError):
    """Execution is gated off; no transmission was attempted."""


def _signing_message(timestamp_ms: str, method: str, path: str) -> bytes:
    """Canonical message for the Retail Ed25519 signature.

    NOTE: the exact byte canonicalization (field order, separators, encoding)
    must be verified against the current venue documentation before real use.
    """
    return f"{timestamp_ms}{method}{path}".encode("utf-8")


class UsRetailAuth:
    """Retail API-key Ed25519 signer. Holds the secret only in memory."""

    def __init__(self, access_key: str, secret: str):
        if not access_key or not secret:
            raise UsExecutionNotConfigured("access key and secret are required")
        self.access_key = access_key
        self._secret = secret  # in-memory only; never logged or exposed

    def sign(self, method: str, path: str, timestamp_ms: str) -> str:
        try:
            from nacl.signing import SigningKey
        except ImportError as error:  # pragma: no cover - depends on env
            raise RuntimeError(
                "PyNaCl is required for Retail auth; install the 'execution' extra"
            ) from error
        raw = base64.b64decode(self._secret)
        # Polymarket US secret is base64(seed || public_key) (64 bytes) or a
        # bare 32-byte seed; Ed25519 signing only needs the seed.
        key = SigningKey(raw[:32])
        signed = key.sign(_signing_message(timestamp_ms, method, path))
        return base64.b64encode(signed.signature).decode("ascii")

    def headers(self, method: str, path: str, timestamp_ms: str) -> dict[str, str]:
        return {
            "X-PM-Access-Key": self.access_key,
            "X-PM-Timestamp": timestamp_ms,
            "X-PM-Signature": self.sign(method, path, timestamp_ms),
        }


class UsRetailExecutionClient:
    """Authenticated Retail trading client (preview/orders/cancel/open).

    Fail-closed: every endpoint raises ``UsExecutionDisabled`` unless
    ``live_enabled=True`` at construction, and raises
    ``UsExecutionNotConfigured`` without auth. Constructing this client is
    never sufficient to transmit an order.
    """

    def __init__(
        self,
        auth: UsRetailAuth | None = None,
        live_enabled: bool = False,
        base_url: str = API_BASE,
        timeout: float = 20,
        attempts: int = 3,
    ):
        if timeout <= 0 or attempts < 1:
            raise ValueError("invalid client settings")
        self.auth = auth
        self.live_enabled = live_enabled
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.attempts = attempts
        self._last_request_at = 0.0

    @classmethod
    def from_env(
        cls, live_enabled: bool = False, **kwargs: Any
    ) -> "UsRetailExecutionClient":
        """Build from environment secrets; never from source/logs."""
        access_key = os.environ.get(ACCESS_KEY_ENV, "")
        secret = os.environ.get(SECRET_ENV, "")
        if not access_key or not secret:
            raise UsExecutionNotConfigured(
                f"set {ACCESS_KEY_ENV} and {SECRET_ENV} in the environment"
            )
        return cls(
            UsRetailAuth(access_key, secret), live_enabled=live_enabled, **kwargs
        )

    def _pace(self) -> None:
        now = time.monotonic()
        delay = (1.0 / REQUESTS_PER_SECOND) - (now - self._last_request_at)
        if delay > 0:
            time.sleep(delay)
        self._last_request_at = time.monotonic()

    def _request(
        self, method: str, path: str, payload: dict | None = None
    ) -> tuple[Any, datetime]:
        if not self.live_enabled:
            raise UsExecutionDisabled("live execution is gated off")
        if self.auth is None:
            raise UsExecutionNotConfigured("no auth configured")
        self._pace()
        timestamp_ms = str(int(time.time() * 1000))
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.base_url + path,
            data=body,
            headers={
                **self.auth.headers(method, path, timestamp_ms),
                "Content-Type": "application/json",
            },
            method=method,
        )
        for attempt in range(self.attempts):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    received = datetime.now(UTC)
                return (json.loads(raw) if raw else {}), received
            except HTTPError as error:
                if error.code == 429 and attempt < self.attempts - 1:
                    time.sleep(2**attempt)
                elif error.code in (500, 502, 503, 504) and attempt < self.attempts - 1:
                    time.sleep(2**attempt)
                else:
                    raise
            except (URLError, TimeoutError):
                if attempt == self.attempts - 1:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")

    # ── Endpoints (docs/POLYMARKET_US_API.md §9) ──────────────────────────

    def order_preview(self, order: dict) -> tuple[Any, datetime]:
        """Read-only expected-execution preview (still gated for safety)."""
        return self._request("POST", "/v1/order/preview", order)

    def submit_order(self, order: dict) -> tuple[Any, datetime]:
        return self._request("POST", "/v1/orders", order)

    def cancel_order(self, order_id: str) -> tuple[Any, datetime]:
        return self._request("DELETE", f"/v1/orders/{order_id}")

    def open_orders(self) -> tuple[Any, datetime]:
        return self._request("GET", "/v1/orders/open")
