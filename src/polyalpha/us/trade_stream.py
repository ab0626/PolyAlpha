"""Polymarket US retail trade-stream collector (authenticated WebSocket).

Produces the authoritative signed-flow tape for research families A6/A8: each
trade carries explicit maker/taker side and intent from the venue, so aggressor
flow is not inferred (Dubach 2026).

Layering:
  - ``UsTradeStream`` (transport-agnostic core): validate, dedupe, replay.
    Offline-testable — no websockets, no network.
  - ``UsTradeWsCollector`` (async live runner): authenticate, subscribe, receive,
    ingest, and store wire-exact raw to the RawStore.

Handshake auth (Ed25519 headers) and the subscribe envelope are confirmed
against the polymarket-us SDK / live feed; raw is stored wire-exact so any
future schema change is cheap to re-parse.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from ..rawstore import RawStore
from .execution import UsExecutionNotConfigured, UsRetailAuth
from .fills import FillRecord, parse_us_trade

TRADE_ENDPOINT = "wss://api.polymarket.us/v1/ws/markets"
SOURCE_US_TRADE = "polymarket_us_retail_trade"


class UsTradeStream:
    """Transport-agnostic trade-stream core: validate, dedupe, replay."""

    def __init__(self, dedupe_window: int = 100_000):
        self._seen: deque[str] = deque(maxlen=dedupe_window)
        self._seen_set: set[str] = set()
        self.records: list[FillRecord] = []

    def _key(self, raw: dict) -> str:
        return hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def ingest(self, raw: dict, received_at: datetime) -> FillRecord | None:
        """Parse and de-duplicate one trade message. Returns None on duplicate."""
        key = self._key(raw)
        if key in self._seen_set:
            return None
        if len(self._seen) == self._seen.maxlen and self._seen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)
        record = parse_us_trade(raw, received_at)
        self.records.append(record)
        return record


class UsTradeWsCollector:
    """Authenticated US retail trade WS collector (live runner)."""

    def __init__(
        self,
        auth: UsRetailAuth,
        raw: RawStore,
        endpoint: str = TRADE_ENDPOINT,
        dedupe_window: int = 100_000,
    ):
        self.auth = auth
        self.raw = raw
        self.endpoint = endpoint
        self.stream = UsTradeStream(dedupe_window=dedupe_window)
        self.stats = {"trades": 0, "duplicates": 0, "errors": 0}

    def _handshake_headers(self) -> dict[str, str]:
        # Confirmed: X-PM-* headers over Ed25519(timestamp + "GET" + path).
        timestamp_ms = str(int(time.time() * 1000))
        return self.auth.headers("GET", "/v1/ws/markets", timestamp_ms)

    def _subscribe_message(self, markets: list[str] | None) -> dict:
        # Confirmed envelope (polymarket-us SDK websocket/base.py):
        # {"subscribe": {"requestId", "subscriptionType", "marketSlugs"}}.
        payload: dict[str, Any] = {
            "requestId": "polyalpha-trades",
            "subscriptionType": "SUBSCRIPTION_TYPE_TRADE",
        }
        if markets:
            payload["marketSlugs"] = markets
        return {"subscribe": payload}

    async def run(
        self,
        markets: list[str] | None = None,
        duration: float | None = None,
        reconnect_seconds: float = 2.0,
    ) -> dict:
        """Run the trade stream. ``duration=None`` runs until cancelled."""
        import websockets

        deadline = time.monotonic() + duration if duration else None
        subscribe = self._subscribe_message(markets)

        while True:
            # Recompute the signed headers per connection: the signature embeds
            # a timestamp that must be fresh at handshake time.
            headers = self._handshake_headers()
            try:
                # VERIFY: websockets major-version connect signature.
                async with websockets.connect(
                    self.endpoint, additional_headers=headers
                ) as ws:
                    await ws.send(json.dumps(subscribe))
                    async for message in ws:
                        received = datetime.now(UTC)
                        if isinstance(message, bytes):
                            message = message.decode("utf-8")
                        try:
                            payload = json.loads(message)
                        except json.JSONDecodeError:
                            self.stats["errors"] += 1
                            continue
                        record = self.stream.ingest(payload, received)
                        if record is None:
                            self.stats["duplicates"] += 1
                            continue
                        self.raw.append(
                            SOURCE_US_TRADE, "trade_ws", payload, wire=message
                        )
                        self.stats["trades"] += 1
                        if deadline is not None and time.monotonic() >= deadline:
                            return dict(self.stats)
            except Exception:  # noqa: BLE001 - reconnect loop must survive any error
                self.stats["errors"] += 1
            if deadline is not None and time.monotonic() >= deadline:
                return dict(self.stats)
            await asyncio.sleep(reconnect_seconds)

    @classmethod
    def from_env(cls, raw: RawStore, **kwargs: Any) -> "UsTradeWsCollector":
        import os

        access_key = os.environ.get("POLYMARKET_US_ACCESS_KEY", "")
        secret = os.environ.get("POLYMARKET_US_SECRET", "")
        if not access_key or not secret:
            raise UsExecutionNotConfigured(
                "set POLYMARKET_US_ACCESS_KEY and POLYMARKET_US_SECRET"
            )
        return cls(UsRetailAuth(access_key, secret), raw, **kwargs)


def run_collector(
    raw_dir: str,
    markets: list[str] | None = None,
    duration: float | None = None,
    collector_version: str = "v0.4.1-us-research-baseline",
) -> dict:
    """Synchronous entry point: build from env creds, run the stream, return stats."""
    raw = RawStore(raw_dir, collector_version=collector_version)
    collector = UsTradeWsCollector.from_env(raw)
    try:
        return asyncio.run(collector.run(markets=markets, duration=duration))
    finally:
        raw.close()
