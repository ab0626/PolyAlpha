"""Public Polymarket US REST client — gateway.polymarket.us.

Read-only market data (no auth). Enforces the documented 20 requests/second
per-IP limit with pacing and exponential backoff on 429.

Public endpoints only; authenticated endpoints (api.polymarket.us) are handled
separately with API keys in the execution boundary.
"""

from __future__ import annotations

import socket
import ssl
import time
from datetime import UTC, datetime
from http.client import HTTPSConnection
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

GATEWAY = "https://gateway.polymarket.us"
REQUESTS_PER_SECOND = 20

# Pinned fallback IPs (Cloudflare anycast) for the gateway host, used only when
# system DNS resolution fails (flaky campus resolver). SNI and the Host header
# still use the real hostname, so TLS and CDN routing are unaffected. Normal
# resolution always wins; this is a last-resort dial path.
_FALLBACK_IPS = {
    "gateway.polymarket.us": ["104.18.38.40", "172.64.149.216"],
}


class _IPHTTPSConnection(HTTPSConnection):
    """HTTPSConnection that dials a pre-resolved IP while keeping the hostname
    for SNI and the Host header (required for cert validation + routing)."""

    def __init__(
        self,
        host: str,
        ip: str,
        port: int = 443,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ):
        self._ip = ip
        super().__init__(host, port=port, timeout=timeout, context=context)

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)
        if self._tunnel_host:
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


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
            except (URLError, TimeoutError) as error:
                # DNS resolution failure (flaky resolver): dial a pinned IP
                # directly so the request still goes out. SNI/Host stay on the
                # real hostname, so this is transparent to the server.
                if isinstance(getattr(error, "reason", None), socket.gaierror):
                    fallback = self._get_via_fallback_ip(path + query)
                    if fallback is not None:
                        return fallback
                if attempt == self.attempts - 1:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")

    def _get_via_fallback_ip(self, path_and_query: str) -> tuple[Any, datetime] | None:
        """GET the path over a pinned IP, using the real hostname for SNI/Host.

        Returns ``(body, received)`` on a 2xx, raises ``HTTPError`` on 4xx/5xx,
        and returns ``None`` if every pinned IP fails to connect. Only used when
        system DNS resolution has already failed.
        """
        host = urlparse(self.base_url).hostname or ""
        url = self.base_url + path_and_query
        for ip in _FALLBACK_IPS.get(host, []):
            try:
                conn = _IPHTTPSConnection(
                    host, ip, timeout=self.timeout, context=ssl.create_default_context()
                )
                conn.request(
                    "GET",
                    path_and_query,
                    headers={"User-Agent": "polyalpha-research/0.3"},
                )
                response = conn.getresponse()
                body = response.read()
                status = response.status
                headers = dict(response.getheaders())
                conn.close()
                if status >= 400:
                    raise HTTPError(url, status, response.reason, headers, None)
                import json

                return json.loads(body), datetime.now(UTC)
            except HTTPError:
                raise
            except OSError:
                continue
        return None

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