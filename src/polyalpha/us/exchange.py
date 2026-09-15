"""USExchangeAdapter — Direct Polymarket Exchange reference-data client.

Part of the US adapter family. Consumes the Direct Exchange reference-data API
(POST /v1/refdata/instruments) which is the authoritative source of
per-instrument semantics (symbol, tickSize, minimumTradeQty, priceScale,
lifecycle, rules, participants, payout value).

AUTH BOUNDARY: the Direct Exchange requires private-key JWT -> Auth0 access
token (expires_in 180s, refresh every 3 min) with scopes such as
read:instruments, read:l2marketdata, read:marketdata. This module is a pure
client stub: it does NOT store credentials and does NOT implement JWT signing.
A caller supplies an `access_token`; the token lifecycle lives in the
isolated execution boundary, never in this code.

gRPC host naming is inconsistent across the docs (grpc-prod vs grpc-api.prod);
the gRPC host must be configuration, not code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .instruments import UsInstrument, UsInstrumentRegistry, parse_instrument

EXCHANGE_PROD = "https://api.prod.polymarketexchange.com"
EXCHANGE_PREPROD = "https://api.preprod.polymarketexchange.com"


class ExchangeRefDataClient:
    """Read-only Direct Exchange refdata client (JWT auth required)."""

    def __init__(
        self,
        base_url: str = EXCHANGE_PROD,
        access_token: str | None = None,
        timeout: float = 20,
        attempts: int = 3,
    ):
        if timeout <= 0 or attempts < 1:
            raise ValueError("invalid client settings")
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout
        self.attempts = attempts

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def post(self, path: str, payload: dict | None = None) -> tuple[Any, datetime]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload or {}).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        for attempt in range(self.attempts):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    received = datetime.now(UTC)
                return json.loads(body), received
            except HTTPError as error:
                if error.code in (429, 500, 502, 503, 504) and attempt < self.attempts - 1:
                    import time
                    time.sleep(2**attempt)
                else:
                    raise
            except (URLError, TimeoutError):
                if attempt == self.attempts - 1:
                    raise
                import time
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")

    def instruments(
        self, symbols: list[str] | None = None, registry: UsInstrumentRegistry | None = None
    ) -> list[UsInstrument]:
        """POST /v1/refdata/instruments; register parsed instruments.

        `symbols` is a filter list if the API supports it; the documented
        endpoint accepts a request body. If `registry` is given, each parsed
        instrument is registered.
        """
        payload = {"symbols": symbols} if symbols else {}
        body, _ = self.post("/v1/refdata/instruments", payload)
        items = body if isinstance(body, list) else body.get("instruments", [])
        instruments = [parse_instrument(item) for item in items]
        if registry is not None:
            for instrument in instruments:
                registry.register(instrument)
        return instruments