"""Polymarket US market-data WebSocket collector (L2/BBO).

Separate from the trade WS and from the REST polling path. This is the finer
timestamp-resolution feed needed before any sub-2s reaction claim.

Guarantees:
  - Three clocks per raw record: venue/event time (``transactTime``), wall-clock
    receipt, and monotonic receipt.
  - Raw frame stored BEFORE normalization (wire-exact, append-only).
  - Deterministic L2/BBO state machine; the US retail WS sends full snapshots
    (no deltas), and no venue sequence number is exposed, so "gap" = reconnect /
    session boundary: books are marked STALE and re-baselined by a REST resync,
    never silently stitched over a missing update.
  - Feed-resolution label (``WS_L2`` / ``WS_BBO``) so downstream measurements
    only enable horizons the feed can actually resolve.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..rawstore import RawStore
from ..ws_frame import (
    GAP_DRAIN_TIMEOUT,
    GAP_QUEUE_OVERFLOW,
    GAP_WRITER_FAILURE,
    DataGapError,
    DerivedRecordWriter,
    WsFrameStore,
    WsFrameWriter,
    WsSession,
    capture_frame,
    fail_closed,
    frame_text,
)
from .adapter import amount_to_decimal
from .execution import UsExecutionNotConfigured, UsRetailAuth
from .states import UsMarketState

D = Decimal

MARKET_DATA_ENDPOINT = "wss://api.polymarket.us/v1/ws/markets"
SOURCE_US_MARKET_DATA = "polymarket_us_retail_market_data"
DEFAULT_MARKET_DATA_FRAMES = Path("data/us/market_data/ws_frames_v2")


class _DeadlineReached(Exception):
    """Internal sentinel: break the reconnect loop at the run deadline."""


@dataclass(frozen=True)
class MarketDataUpdate:
    """A normalized full-book (L2) or BBO (lite) update."""

    market_slug: str
    kind: str  # "L2" | "BBO"
    bids: tuple[tuple[Decimal, Decimal], ...]
    offers: tuple[tuple[Decimal, Decimal], ...]
    state: UsMarketState | None
    transact_time: datetime | None


@dataclass
class BookState:
    """Latest deterministic state for one market, plus staleness."""

    update: MarketDataUpdate | None = None
    stale: bool = False
    last_update_at: datetime | None = None


def parse_market_data(raw: dict) -> MarketDataUpdate | None:
    """Parse a marketData / marketDataLite WS message (SDK websocket/types.py)."""
    if "marketData" in raw:
        md = raw["marketData"]
        bids = tuple(
            (amount_to_decimal(level.get("px")), amount_to_decimal(level.get("qty")))
            for level in md.get("bids") or []
        )
        offers = tuple(
            (amount_to_decimal(level.get("px")), amount_to_decimal(level.get("qty")))
            for level in md.get("offers") or []
        )
        state = md.get("state")
        tt = md.get("transactTime")
        return MarketDataUpdate(
            market_slug=md.get("marketSlug") or "",
            kind="L2",
            bids=bids,
            offers=offers,
            state=UsMarketState.parse(state) if state else None,
            transact_time=datetime.fromisoformat(tt.replace("Z", "+00:00")) if tt else None,
        )
    if "marketDataLite" in raw:
        md = raw["marketDataLite"]
        bb = amount_to_decimal(md.get("bestBid"))
        ba = amount_to_decimal(md.get("bestAsk"))
        return MarketDataUpdate(
            market_slug=md.get("marketSlug") or "",
            kind="BBO",
            bids=((bb, D(0)),) if bb is not None else (),
            offers=((ba, D(0)),) if ba is not None else (),
            state=None,
            transact_time=None,
        )
    return None


class MarketDataStream:
    """Transport-agnostic, deterministic L2/BBO state machine.

    The US retail WS emits full snapshots (no deltas) and exposes no sequence
    number, so gap handling is session-boundary based: ``on_reconnect`` marks
    every book STALE; a fresh snapshot re-baselines it. Updates are never
    stitched over a missing one.
    """

    def __init__(self):
        self.books: dict[str, BookState] = {}
        self.session_id: str = ""
        self.gap_count = 0
        self.last_heartbeat_at: datetime | None = None

    def ingest(self, raw: dict, received_at: datetime) -> MarketDataUpdate | None:
        update = parse_market_data(raw)
        if update is None or not update.market_slug:
            return None
        state = self.books.setdefault(update.market_slug, BookState())
        state.update = update
        state.stale = False
        state.last_update_at = received_at
        return update

    def on_reconnect(self, session_id: str) -> None:
        """Session boundary: invalidate all books until re-baselined."""
        if session_id != self.session_id:
            self.session_id = session_id
            self.gap_count += 1
        for state in self.books.values():
            state.stale = True

    def on_heartbeat(self, received_at: datetime) -> None:
        self.last_heartbeat_at = received_at

    @property
    def stale_slugs(self) -> list[str]:
        return sorted(s for s, b in self.books.items() if b.stale)


class MarketDataWsCollector:
    """Authenticated US market-data WS collector (live runner)."""

    def __init__(
        self,
        auth: UsRetailAuth,
        raw: RawStore,
        endpoint: str = MARKET_DATA_ENDPOINT,
        frames_dir: str | Path | None = None,
    ):
        self.auth = auth
        self.raw = raw
        self.endpoint = endpoint
        self.stream = MarketDataStream()
        self.frame_store = WsFrameStore(frames_dir or DEFAULT_MARKET_DATA_FRAMES, SOURCE_US_MARKET_DATA)
        self.stats = {"updates": 0, "reconnects": 0, "errors": 0}

    def _handshake_headers(self) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        return self.auth.headers("GET", "/v1/ws/markets", timestamp_ms)

    def _subscribe_message(self, markets: list[str], lite: bool = False) -> dict:
        sub_type = "SUBSCRIPTION_TYPE_MARKET_DATA_LITE" if lite else "SUBSCRIPTION_TYPE_MARKET_DATA"
        return {"subscribe": {"requestId": "polyalpha-md", "subscriptionType": sub_type,
                              "marketSlugs": markets}}

    async def run(
        self,
        markets: list[str],
        lite: bool = False,
        duration: float | None = None,
        reconnect_seconds: float = 2.0,
    ) -> dict:
        """Run the market-data stream until cancelled or ``duration`` elapses."""
        import websockets

        deadline = time.monotonic() + duration if duration else None
        subscribe = self._subscribe_message(markets, lite)
        session = WsSession()
        self.frame_store.mark_cutover()

        frame_writer = WsFrameWriter(self.frame_store)
        frame_writer.start()

        def _persist(record: dict) -> None:
            self.raw.append(
                record["source"],
                record["session_id"],
                record["payload"],
                wire=record["wire"],
                received_at_ns=record["received_at_ns"],
                received_monotonic_ns=record["received_monotonic_ns"],
                message_sequence_local=record["session_seq"],
                exchange_timestamp_ms=record["exchange_timestamp_ms"],
            )

        derived_writer = DerivedRecordWriter(_persist)
        derived_writer.start()

        try:
            while True:
                # Recompute the signed headers per connection: the signature embeds
                # a timestamp that must be fresh at handshake time.
                headers = self._handshake_headers()
                try:
                    async with websockets.connect(
                        self.endpoint, additional_headers=headers
                    ) as ws:
                        self.stream.on_reconnect(session.on_connect())
                        await ws.send(json.dumps(subscribe))
                        while True:
                            try:
                                message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            except asyncio.TimeoutError:
                                # Bounded poll: notice a dead writer promptly.
                                if frame_writer.health()["fatal"]:
                                    fail_closed(self.frame_store, frame_writer, None, GAP_WRITER_FAILURE)
                                if derived_writer.health()["fatal"]:
                                    fail_closed(self.frame_store, derived_writer, None, GAP_WRITER_FAILURE)
                                continue

                            # Immutable transport frame at recv, BEFORE decode/parse.
                            wall_ns = time.time_ns()
                            mono_ns = time.monotonic_ns()
                            frame = capture_frame(SOURCE_US_MARKET_DATA, session, message, wall_ns, mono_ns)
                            if not frame_writer.try_submit(frame):
                                fail_closed(self.frame_store, frame_writer, frame, GAP_QUEUE_OVERFLOW)

                            try:
                                payload = json.loads(frame_text(frame))
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                self.stats["errors"] += 1
                                continue  # frame already enqueued; no parsed record

                            now_wall = datetime.fromtimestamp(wall_ns / 1e9, UTC)
                            if "heartbeat" in payload:
                                self.stream.on_heartbeat(now_wall)
                                continue  # control frame enqueued at transport layer

                            update = self.stream.ingest(payload, now_wall)
                            if update is None:
                                continue

                            # Three clocks: venue time (ms), wall ns, monotonic ns.
                            venue_ms = (
                                int(update.transact_time.timestamp() * 1000)
                                if update.transact_time is not None
                                else None
                            )
                            payload["frame_id"] = frame.frame_id
                            if not derived_writer.try_submit({
                                "frame_id": frame.frame_id,
                                "source": SOURCE_US_MARKET_DATA,
                                "session_id": frame.session_id,
                                "session_seq": frame.session_seq,
                                "payload": payload,
                                "wire": message,
                                "received_at_ns": wall_ns,
                                "received_monotonic_ns": mono_ns,
                                "exchange_timestamp_ms": venue_ms,
                            }):
                                fail_closed(self.frame_store, derived_writer, frame, GAP_QUEUE_OVERFLOW)
                            self.stats["updates"] += 1
                            if deadline is not None and time.monotonic() >= deadline:
                                raise _DeadlineReached
                except _DeadlineReached:
                    break
                except DataGapError:
                    raise
                except Exception:  # noqa: BLE001
                    self.stats["errors"] += 1
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self.stats["reconnects"] += 1
                await asyncio.sleep(reconnect_seconds)
        finally:
            if not frame_writer.close(drain=True):
                fail_closed(self.frame_store, frame_writer, None, GAP_DRAIN_TIMEOUT)
            if not derived_writer.close(drain=True):
                fail_closed(self.frame_store, derived_writer, None, GAP_DRAIN_TIMEOUT)
            self.frame_store.close()
        return dict(self.stats)

    @classmethod
    def from_env(cls, raw: RawStore, **kwargs: Any) -> "MarketDataWsCollector":
        import os

        access_key = os.environ.get("POLYMARKET_US_ACCESS_KEY", "")
        secret = os.environ.get("POLYMARKET_US_SECRET", "")
        if not access_key or not secret:
            raise UsExecutionNotConfigured(
                "set POLYMARKET_US_ACCESS_KEY and POLYMARKET_US_SECRET"
            )
        return cls(UsRetailAuth(access_key, secret), raw, **kwargs)


# Empirical, from scripts/smoke_market_data_ws.py (2026-09-21): WS_L2 median
# interarrival ~101ms, p95 218ms, p99 309ms. WS_BBO is the same feed (unmeasured;
# assumed comparable). Update these if the feed changes.
FEED_RESOLUTION = {"REST_2S": 2000, "WS_BBO": 100, "WS_L2": 100, "TRADE_WS": 0}


def resolvable_horizons(feed_label: str) -> list[int]:
    """Horizons (ms) a feed can resolve, from measured (not declared) cadence.

    Others must be marked UNRESOLVABLE, never zero or missing-alpha.
    """
    res = FEED_RESOLUTION.get(feed_label)
    if res is None:
        return []
    return [h for h in (100, 250, 500, 1000, 2000, 5000, 10000, 30000) if h >= res]
