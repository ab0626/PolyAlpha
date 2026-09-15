"""Public-stream book reconstruction; disconnects invalidate all executable state."""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime

from .domain import Level, number
from .parsing import parse_book


class Reconstructor:
    def __init__(self, specifications):
        # token -> (condition ID, tick size, minimum order size), from known REST books
        self.specifications = dict(specifications)
        self.books = {}

    def invalidate(self):
        self.books.clear()

    def apply(self, event, received):
        kind = event.get("event_type")
        try:
            if kind == "book":
                token = event["asset_id"]
                condition, tick, minimum = self.specifications[token]
                raw = dict(event, tick_size=str(tick), min_order_size=str(minimum))
                book = parse_book(raw, received, token)
                if book.condition_id != condition:
                    raise ValueError("condition mismatch")
                prior = self.books.get(token)
                if prior is not None and book.source_at < prior.source_at:
                    raise ValueError("out of order snapshot")
                self.books[token] = book
                return [book]
            if kind == "tick_size_change":
                token = event["asset_id"]
                condition, _, minimum = self.specifications[token]
                self.specifications[token] = (
                    condition,
                    number(event["new_tick_size"]),
                    minimum,
                )
                self.books.pop(token, None)
                return []
            if kind != "price_change":
                return []
            at = datetime.fromtimestamp(int(event["timestamp"]) / 1000, UTC)
            updated = dict(self.books)
            changed = set()
            for change in event["price_changes"]:
                token = change["asset_id"]
                if token not in updated:
                    raise ValueError("delta before snapshot; resynchronize")
                book = updated[token]
                if book.source_at > at or book.condition_id != event["market"]:
                    raise ValueError("out of order or mismatched delta")
                side = change["side"]
                if side not in ("BUY", "SELL"):
                    raise ValueError("unknown side")
                bids, asks = (
                    dict((x.price, x.size) for x in book.bids),
                    dict((x.price, x.size) for x in book.asks),
                )
                levels = bids if side == "BUY" else asks
                price, size = number(change["price"]), number(change["size"])
                if not 0 <= price <= 1 or size < 0:
                    raise ValueError("invalid delta")
                if size == 0:
                    levels.pop(price, None)
                else:
                    levels[price] = size
                candidate = replace(
                    book,
                    source_at=at,
                    received_at=received,
                    bids=tuple(Level(p, q) for p, q in sorted(bids.items(), reverse=True)),
                    asks=tuple(Level(p, q) for p, q in sorted(asks.items())),
                    source_hash=change.get("hash", ""),
                )
                for field, actual in (
                    ("best_bid", candidate.best_bid),
                    ("best_ask", candidate.best_ask),
                ):
                    if field in change and actual is not None and number(change[field]) != actual:
                        raise ValueError("top-of-book mismatch; resynchronize")
                updated[token] = candidate
                changed.add(token)
            self.books = updated
            return [updated[t] for t in sorted(changed)]
        except (ValueError, KeyError, TypeError):
            self.invalidate()
            raise


def stream(specifications, store, seconds=60):
    """Bounded raw stream collector. No sequence numbers: gaps cannot be proven absent."""
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    if seconds <= 0 or not specifications:
        raise ValueError("positive duration and token specifications required")
    state = Reconstructor(specifications)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with connect(
                "wss://ws-subscriptions-clob.polymarket.com/ws/market", open_timeout=10
            ) as ws:
                state.invalidate()
                ws.send(
                    json.dumps(
                        {
                            "assets_ids": list(specifications),
                            "type": "market",
                            "custom_feature_enabled": True,
                        }
                    )
                )
                next_ping = time.monotonic()
                while time.monotonic() < deadline:
                    if time.monotonic() >= next_ping:
                        ws.send("PING")
                        next_ping = time.monotonic() + 10
                    try:
                        message = ws.recv(timeout=min(1, max(0.01, deadline - time.monotonic())))
                    except TimeoutError:
                        continue
                    if message == "PONG":
                        continue
                    received = datetime.now(UTC)
                    payload = json.loads(message)
                    for event in payload if isinstance(payload, list) else [payload]:
                        store.append("raw_stream", "market", received, event)
                        for book in state.apply(event, received):
                            payload = dict(
                                asset_id=book.token_id,
                                market=book.condition_id,
                                timestamp=str(int(book.source_at.timestamp() * 1000)),
                                bids=[
                                    dict(price=str(x.price), size=str(x.size)) for x in book.bids
                                ],
                                asks=[
                                    dict(price=str(x.price), size=str(x.size)) for x in book.asks
                                ],
                                tick_size=str(book.tick_size),
                                min_order_size=str(book.min_order_size),
                                hash=book.source_hash,
                            )
                            store.append("book", book.token_id, received, payload, book.source_at)
        except (ConnectionClosed, OSError, ValueError, KeyError) as error:
            state.invalidate()
            store.append("stream_gap", "market", datetime.now(UTC), {"reason": str(error)})
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        finally:
            state.invalidate()
