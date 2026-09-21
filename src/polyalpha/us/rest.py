"""Public Polymarket US REST client — gateway.polymarket.us.

Read-only market data (no auth). Enforces the documented 20 requests/second
per-IP limit with pacing and exponential backoff on 429.

Public endpoints only; authenticated endpoints (api.polymarket.us) are handled
separately with API keys in the execution boundary.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GATEWAY = "https://gateway.polymarket.us"
REQUESTS_PER_SECOND = 20


class UsRateLimited(RuntimeError):
    pass


class PublicUsClient:
    def __init__(
        self,
        base_url: str = GATEWAY,
        timeout: float = 20,
        attempts: int = 4,
        min_spacing_seconds: float = 1.0 / REQUESTS_PER_SECOND,
    ):
        if timeout <= 0 or attempts < 1 or min_spacing_seconds <= 0:
            raise ValueError("invalid client settings")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.attempts = attempts
        self.min_spacing_seconds = min_spacing_seconds
        self._last_request_at = 0.0

    def _pace(self) -> None:
        now = time.monotonic()
        delay = self.min_spacing_seconds - (now - self._last_request_at)
        if delay > 0:
            time.sleep(delay)
        self._last_request_at = time.monotonic()

    def get(self, path: str, params: dict | None = None) -> tuple[Any, datetime]:
        self._pace()
        query = "?" + urlencode(params) if params else ""
        request = Request(
            self.base_url + path + query,
            headers={"User-Agent": "polyalpha-research/0.3"},
        )
        for attempt in range(self.attempts):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    received = datetime.now(UTC)
                import json
                return json.loads(body), received
            except HTTPError as error:
                if error.code == 429:
                    if attempt == self.attempts - 1:
                        raise UsRateLimited(
                            f"US public rate limit exceeded after {self.attempts} attempts"
                        )
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

    # ── Typed helpers ─────────────────────────────────────────────────────

    def markets(self, params: dict | None = None) -> tuple[Any, datetime]:
        return self.get("/v1/markets", params)

    def market_book(self, slug: str) -> tuple[Any, datetime]:
        return self.get(f"/v1/markets/{slug}/book")

    def market_bbo(self, slug: str) -> tuple[Any, datetime]:
        return self.get(f"/v1/markets/{slug}/bbo")

    def market_settlement(self, slug: str) -> tuple[Any, datetime]:
        return self.get(f"/v1/markets/{slug}/settlement")

    def price_history(
        self, slug: str, fixed_interval: str, fidelity: int
    ) -> tuple[Any, datetime]:
        return self.get(
            "/v1/price-history",
            {"symbol": slug, "fixedInterval": fixed_interval, "fidelity": fidelity},
        )

    def events(self, params: dict | None = None) -> tuple[Any, datetime]:
        return self.get("/v1/events", params)

    def search(self, params: dict | None = None) -> tuple[Any, datetime]:
        """Public /v1/search: text search over events/markets."""
        return self.get("/v1/search", params)