"""Polymarket market-stream collector with immutable raw capture.

Part of the v0.3.0 data-collection architecture. Consumes the public market
WebSocket (wss://ws-subscriptions-clob.polymarket.com/ws/market) and:

  * writes EVERY raw message to a RawStore before any interpretation,
  * sends the application-level `PING` every 10 seconds (server replies PONG),
  * subscribes with `custom_feature_enabled: true` to receive best-bid/ask,
    new-market, and market-resolved events in addition to the standard set,
  * keeps two clocks per record (exchange timestamp from the payload, local
    receive timestamp from the collector) — never collapsed,
  * detects dropped updates: a disconnect invalidates local book state and
    books are resynchronized from a full snapshot (never resumed by applying
    deltas onto stale state),
  * reconciles reconstructed books against REST periodically (see reconciler).

Lifecycle events (new_market, market_resolved) are captured raw and also
routed to the metadata/lifecycle store so the complete Market(t) history from
discovery through resolution is preserved.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .domain import Level, number
from .parsing import parse_book
from .rawstore import RawStore, SOURCE_MARKET_WS


@dataclass
class CollectorStats:
    started_at: float = 0.0
    connected_at: float = 0.0
    messages_received: int = 0
    book_events: int = 0
    price_change_events: int = 0
    last_trade_events: int = 0
    tick_size_events: int = 0
    best_bid_ask_events: int = 0
    new_market_events: int = 0
    market_resolved_events: int = 0
    reconnect_count: int = 0
    reconciliation_count: int = 0
    book_mismatch_count: int = 0
    last_message_at: float = 0.0
    last_book_snapshot_at: float = 0.0
    parse_errors: int = 0
    receive_lag_ns: list[int] = field(default_factory=list)

    def record(self, kind: str) -> None:
        setattr(self, kind, getattr(self, kind) + 1)

    def as_dict(self) -> dict:
        lag = self.receive_lag_ns
        return {
            "uptime_seconds": round(time.monotonic() - self.started_at, 1),
            "messages_received": self.messages_received,
            "book_events": self.book_events,
            "price_change_events": self.price_change_events,
            "last_trade_events": self.last_trade_events,
            "tick_size_events": self.tick_size_events,
            "best_bid_ask_events": self.best_bid_ask_events,
            "new_market_events": self.new_market_events,
            "market_resolved_events": self.market_resolved_events,
            "reconnect_count": self.reconnect_count,
            "reconciliation_count": self.reconciliation_count,
            "book_mismatch_count": self.book_mismatch_count,
            "last_message_at": self.last_message_at,
            "last_book_snapshot_at": self.last_book_snapshot_at,
            "parse_errors": self.parse_errors,
            "median_receive_lag_ms": (
                round(sorted(lag)[len(lag) // 2] / 1e6, 1) if lag else None
            ),
            "p95_receive_lag_ms": (
                round(sorted(lag)[int(len(lag) * 0.95)] / 1e6, 1) if lag else None
            ),
        }


class MarketCollector:
    """Long-running collector for the Polymarket public market stream."""

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    HEARTBEAT_SECONDS = 10

    def __init__(
        self,
        raw: RawStore,
        token_ids: list[str],
        metadata_store: Any | None = None,
        heartbeat_seconds: int = 10,
        collector_version: str = "unknown",
    ):
        self.raw = raw
        self.token_ids = list(token_ids)
        self.metadata_store = metadata_store
        self.heartbeat_seconds = heartbeat_seconds
        self.collector_version = collector_version
        self.stats = CollectorStats()
        self.books: dict[str, dict] = {}
        self.specifications: dict[str, tuple[str, Any, Any]] = {}
        self._connection_id: str | None = None
        self._sequence = 0
        self._invalidate_called = False

    # ── Connection lifecycle ────────────────────────────────────────────────

    def _new_connection_id(self) -> str:
        self._connection_id = uuid.uuid4().hex
        self._sequence = 0
        return self._connection_id

    def invalidate(self) -> None:
        """Drop all derived book state. Never resume deltas onto stale books."""
        self.books.clear()
        self._invalidate_called = True

    def _subscribe_frame(self, token_ids: list[str]) -> dict:
        frame = {"assets_ids": list(token_ids), "type": "market"}
        # Custom feature enables best_bid_ask / new_market / market_resolved.
        frame["custom_feature_enabled"] = True
        return frame

    def _delta_frame(self, token_ids: list[str], operation: str) -> dict:
        return {"assets_ids": list(token_ids), "operation": operation}

    # ── Raw capture ─────────────────────────────────────────────────────────

    def _capture(self, payload: dict, received_at_ns: int) -> None:
        """Write one raw message to the immutable store with full envelope."""
        self._sequence += 1
        self.raw.append(
            source=SOURCE_MARKET_WS,
            connection_id=self._connection_id or "none",
            payload=payload,
            received_at_ns=received_at_ns,
            message_sequence_local=self._sequence,
        )
        self.stats.messages_received += 1
        self.stats.last_message_at = received_at_ns / 1e9

    # ── Event dispatch ──────────────────────────────────────────────────────

    def process(self, raw_message: str) -> list[dict]:
        """Handle one raw WS text message. Returns derived book snapshots."""
        received_at_ns = time.time_ns()
        self._capture(json.loads(raw_message), received_at_ns)
        return self._process_events(json.loads(raw_message), received_at_ns)

    def _process_events(self, payload: dict, received_at_ns: int) -> list[dict]:
        events = payload if isinstance(payload, list) else [payload]
        derived: list[dict] = []
        for event in events:
            kind = event.get("event_type") or event.get("type")
            try:
                if kind == "book":
                    derived.extend(self._on_book(event, received_at_ns))
                elif kind == "price_change":
                    self._on_price_change(event, received_at_ns)
                elif kind == "last_trade_price":
                    self.stats.record("last_trade_events")
                elif kind == "tick_size_change":
                    self._on_tick_size(event)
                    self.stats.record("tick_size_events")
                elif kind == "best_bid_ask":
                    self.stats.record("best_bid_ask_events")
                elif kind == "new_market":
                    self._on_new_market(event)
                    self.stats.record("new_market_events")
                elif kind == "market_resolved":
                    self._on_market_resolved(event)
                    self.stats.record("market_resolved_events")
                elif kind in ("PONG", None, "pong"):
                    continue
                else:
                    # Unknown event type: still captured raw; do not fail the loop.
                    self.stats.record("parse_errors")
            except (KeyError, ValueError, TypeError) as error:
                self.stats.parse_errors += 1
                self.invalidate()
                raise
        return derived

    # ── Book handling ───────────────────────────────────────────────────────

    def _on_book(self, event: dict, received_at_ns: int) -> list[dict]:
        self.stats.book_events += 1
        self.stats.last_book_snapshot_at = received_at_ns / 1e9
        token = event["asset_id"]
        # Full snapshot replaces local state for this token.
        raw = dict(event)
        if token in self.specifications:
            _, tick, minimum = self.specifications[token]
            raw.setdefault("tick_size", str(tick))
            raw.setdefault("min_order_size", str(minimum))
        book = parse_book(raw, datetime.fromtimestamp(received_at_ns / 1e9, UTC), token)
        self.books[token] = {
            "asset_id": token,
            "market": book.condition_id,
            "timestamp": str(int(book.source_at.timestamp() * 1000)),
            "bids": [{"price": str(x.price), "size": str(x.size)} for x in book.bids],
            "asks": [{"price": str(x.price), "size": str(x.size)} for x in book.asks],
            "tick_size": str(book.tick_size),
            "min_order_size": str(book.min_order_size),
            "hash": book.source_hash,
            "received_at_ns": received_at_ns,
        }
        return [self.books[token]]

    def _on_price_change(self, event: dict, received_at_ns: int) -> None:
        self.stats.price_change_events += 1
        changes = event["price_changes"]
        if not changes:
            return
        market = event["market"]
        at = datetime.fromtimestamp(int(event["timestamp"]) / 1000, UTC)
        for change in changes:
            token = change["asset_id"]
            book = self.books.get(token)
            if book is None:
                # Delta before snapshot: resynchronize required.
                raise ValueError("delta before snapshot; resynchronize")
            if book.get("market") != market:
                raise ValueError("mismatched delta market")
            if book.get("received_at_ns", 0) > received_at_ns:
                raise ValueError("out of order delta")
            side = change["side"]
            if side not in ("BUY", "SELL"):
                raise ValueError("unknown side")
            bids, asks = (
                {(x["price"]): number(x["size"]) for x in book["bids"]},
                {(x["price"]): number(x["size"]) for x in book["asks"]},
            )
            levels = bids if side == "BUY" else asks
            price, size = number(change["price"]), number(change["size"])
            if not 0 <= price <= 1 or size < 0:
                raise ValueError("invalid delta")
            if size == 0:
                levels.pop(str(price), None)
            else:
                levels[str(price)] = size
            # Validate top-of-book if the feed reports it.
            for field, side_key in (("best_bid", "BUY"), ("best_ask", "SELL")):
                if field in change and change[field] is not None:
                    actual = number(change[field])
                    best = max(bids, key=lambda k: number(k)) if bids else None
                    if side_key == "BUY" and best is not None and number(best) != actual:
                        raise ValueError("top-of-book mismatch; resynchronize")
            book["bids"] = [
                {"price": p, "size": str(q)}
                for p, q in sorted(bids.items(), key=lambda kv: number(kv[0]), reverse=True)
            ]
            book["asks"] = [
                {"price": p, "size": str(q)}
                for p, q in sorted(asks.items(), key=lambda kv: number(kv[0]))
            ]
            book["timestamp"] = str(int(at.timestamp() * 1000))
            book["received_at_ns"] = received_at_ns
            book["hash"] = change.get("hash", book.get("hash", ""))

    def _on_tick_size(self, event: dict) -> None:
        token = event["asset_id"]
        self.books.pop(token, None)

    def _on_new_market(self, event: dict) -> None:
        if self.metadata_store is not None:
            self.metadata_store.record_lifecycle("new_market", event)

    def _on_market_resolved(self, event: dict) -> None:
        if self.metadata_store is not None:
            self.metadata_store.record_lifecycle("market_resolved", event)


def heartbeat_ping_seconds() -> int:
    return MarketCollector.HEARTBEAT_SECONDS


def run_collector(
    collector: MarketCollector,
    duration_seconds: float | None = None,
    reconnect_delay: float = 5.0,
    recv_timeout: float = 1.0,
) -> CollectorStats:
    """Run the market collector until duration_seconds elapses or interrupted.

    Uses the sync `websockets` client. Maintains the application-level
    heartbeat, invalidates book state on any disconnect, and only resumes
    after a fresh full book snapshot arrives. Returns the collector stats.

    NOTE: this is the live-network path; unit tests exercise `process()`
    directly with synthetic frames.
    """
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    collector.stats.started_at = time.monotonic()
    deadline = (
        time.monotonic() + duration_seconds if duration_seconds else float("inf")
    )

    while time.monotonic() < deadline:
        connection_id = collector._new_connection_id()
        if collector.stats.connected_at:
            collector.stats.reconnect_count += 1
        try:
            with connect(
                collector.WS_URL,
                open_timeout=10,
                additional_headers={"User-Agent": f"polyalpha/{collector.collector_version}"},
            ) as ws:
                collector.stats.connected_at = time.monotonic()
                ws.send(json.dumps(collector._subscribe_frame(collector.token_ids)))
                next_ping = time.monotonic() + collector.heartbeat_seconds
                while time.monotonic() < deadline:
                    if time.monotonic() >= next_ping:
                        ws.send("PING")
                        next_ping = time.monotonic() + collector.heartbeat_seconds
                    try:
                        message = ws.recv(timeout=recv_timeout)
                    except TimeoutError:
                        continue
                    if message == "PONG":
                        continue
                    for book in collector.process(message):
                        # Derived snapshots are intentionally NOT written to the
                        # raw store; the raw layer holds only upstream messages.
                        pass
        except (ConnectionClosed, OSError, ValueError, KeyError) as error:
            collector.stats.parse_errors += 1
            collector.invalidate()
            time.sleep(reconnect_delay)
        finally:
            collector.invalidate()

    return collector.stats