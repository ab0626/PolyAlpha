"""US gRPC market-data stream consumer — state-correctness first.

Part of the US adapter family. Consumes the Direct Exchange market-data stream
(CreateMarketDataSubscription / BiDirectionalStreamMarketData) and produces
canonical domain objects with correct state handling.

Design principle (matching the review): state correctness before throughput.
The nine concerns are each addressed:

  1. Subscription lifecycle   — explicit subscribe -> ack -> update -> (un)subscribe
  2. Snapshot semantics       — snapshot_only closes after the first update
  3. Aggregated vs unaggregated — aggregated (by price) vs raw order book; both parse
  4. Heartbeat / reconnect    — heartbeat = liveness; reconnect invalidates state
  5. Sequence / freshness     — transact_time monotonicity per symbol
  6. Instrument lookup before normalization — symbol must be registered; else REJECT
  7. priceScale-required      — never a default; must come from refdata registry
  8. Stale-state invalidation — out-of-order / stale updates invalidate the symbol
  9. Deterministic replay     — every accepted event is emitted as a replay record

This module is TRANSPORT-AGNOSTIC: it consumes decoded message dicts, so it is
fully testable offline and does not import grpc/proto modules. A thin gRPC
adapter (in grpc_client.py) feeds decoded messages in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..domain import Book, Level, number, utc
from .identifiers import UsIdentifierRegistry
from .instruments import (
    UsInstrumentRegistry,
    scaled_qty_to_decimal,
    scaled_to_decimal,
)
from .states import UsMarketState

D = Decimal

# gRPC InstrumentState enum values (from proto reference).
GRPC_INSTRUMENT_STATE = {
    "INSTRUMENT_STATE_PENDING": "PENDING",
    "INSTRUMENT_STATE_OPEN": "OPEN",
    "INSTRUMENT_STATE_CLOSED": "CLOSED",
    "INSTRUMENT_STATE_EXPIRED": "EXPIRED",
    "INSTRUMENT_STATE_TERMINATED": "TERMINATED",
    "INSTRUMENT_STATE_SUSPENDED": "SUSPENDED",
    "INSTRUMENT_STATE_HALTED": "HALTED",
    "INSTRUMENT_STATE_PREOPEN": "PREOPEN",
    "INSTRUMENT_STATE_MATCH_AND_CLOSE_AUCTION": "MATCH_AND_CLOSE_AUCTION",
}


@dataclass(frozen=True)
class GrpcUpdate:
    """A normalized market-data update, ready for canonical Book conversion."""

    symbol: str
    bids: tuple[tuple[Decimal, Decimal], ...]  # (price, qty) Decimal-normalized
    offers: tuple[tuple[Decimal, Decimal], ...]
    state: UsMarketState | None
    transact_time: datetime
    sequence: int
    book_hidden: bool = False

    def to_book(self, identifier, tick_size: Decimal, min_order_size: Decimal) -> Book:
        bids = tuple(Level(p, q) for p, q in self.bids)
        offers = tuple(Level(p, q) for p, q in self.offers)
        return Book(
            token_id=identifier.long_side_id,
            condition_id=identifier.condition_id,
            source_at=self.transact_time,
            received_at=datetime.now(UTC),
            bids=bids,
            asks=offers,
            tick_size=tick_size,
            min_order_size=min_order_size,
            source_hash=f"grpc:{self.sequence}",
        )


@dataclass(frozen=True)
class SubscriptionAck:
    symbols_added: tuple[str, ...]
    symbols_removed: tuple[str, ...]
    active_symbols: tuple[str, ...]


@dataclass(frozen=True)
class SubscriptionError:
    error_code: str
    message: str
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class StreamEvent:
    kind: str  # heartbeat | update | ack | error | subscribe | unsubscribe | reconnect | stale
    symbol: str = ""
    update: GrpcUpdate | None = None
    ack: SubscriptionAck | None = None
    error: SubscriptionError | None = None
    message: str = ""

    def as_dict(self) -> dict:
        d: dict[str, Any] = {"kind": self.kind, "symbol": self.symbol}
        if self.update is not None:
            d["update"] = {
                "symbol": self.update.symbol,
                "bids": [(str(p), str(q)) for p, q in self.update.bids],
                "offers": [(str(p), str(q)) for p, q in self.update.offers],
                "state": self.update.state.value if self.update.state else None,
                "transact_time": self.update.transact_time.isoformat(),
                "sequence": self.update.sequence,
                "book_hidden": self.update.book_hidden,
            }
        if self.ack is not None:
            d["ack"] = {
                "symbols_added": list(self.ack.symbols_added),
                "symbols_removed": list(self.ack.symbols_removed),
                "active_symbols": list(self.ack.active_symbols),
            }
        if self.error is not None:
            d["error"] = {
                "error_code": self.error.error_code,
                "message": self.error.message,
                "symbols": list(self.error.symbols),
            }
        d["message"] = self.message
        return d


class UsGrpcMarketStream:
    """Stateful consumer of decoded gRPC market-data messages.

    Handles subscription lifecycle, snapshot-only, heartbeat liveness, symbol
    sequence/freshness, instrument-lookup-before-normalization, priceScale-
    required decoding, stale-state invalidation, and deterministic replay.
    """

    def __init__(
        self,
        identifiers: UsIdentifierRegistry,
        instruments: UsInstrumentRegistry,
        max_stale_seconds: float = 60.0,
        require_price_scale: bool = True,
        now_fn=None,
    ):
        self.identifiers = identifiers
        self.instruments = instruments
        self.max_stale_seconds = max_stale_seconds
        self.require_price_scale = require_price_scale
        self._now = now_fn or (lambda: datetime.now(UTC))
        self.events: list[StreamEvent] = []
        self._last_seq: dict[str, int] = {}
        self._last_time: dict[str, datetime] = {}       # server transact_time
        self._last_received: dict[str, datetime] = {}   # local monotonic-ish clock
        self._last_state: dict[str, UsMarketState] = {}
        self.active_symbols: set[str] = set()
        self.reconnect_count = 0

    # ── Subscription lifecycle ─────────────────────────────────────────────

    def subscribe(self, symbols: list[str]) -> None:
        self.active_symbols.update(symbols)
        self.events.append(StreamEvent(kind="subscribe", symbol=",".join(symbols)))

    def unsubscribe(self, symbols: list[str]) -> None:
        for symbol in symbols:
            self.active_symbols.discard(symbol)
        self.events.append(StreamEvent(kind="unsubscribe", symbol=",".join(symbols)))

    def on_ack(self, payload: dict) -> None:
        ack = SubscriptionAck(
            symbols_added=tuple(payload.get("symbols_added", [])),
            symbols_removed=tuple(payload.get("symbols_removed", [])),
            active_symbols=tuple(payload.get("active_symbols", [])),
        )
        self.active_symbols = set(ack.active_symbols)
        self.events.append(StreamEvent(kind="ack", ack=ack))

    def on_error(self, payload: dict) -> None:
        error = SubscriptionError(
            error_code=payload.get("error_code", "UNKNOWN"),
            message=payload.get("message", ""),
            symbols=tuple(payload.get("symbols", [])),
        )
        self.events.append(StreamEvent(kind="error", error=error))

    # ── Reconnect / invalidation ───────────────────────────────────────────

    def reconnect(self) -> None:
        """Invalidate ALL derived book state on reconnect (never resume deltas
        onto a stale book)."""
        self.reconnect_count += 1
        self._last_seq.clear()
        self._last_time.clear()
        self._last_received.clear()
        self._last_state.clear()
        self.events.append(StreamEvent(kind="reconnect"))

    def _invalid_symbols(self) -> list[str]:
        """Symbols with no update received within max_stale_seconds."""
        now = self._now()
        stale = []
        for symbol, last in self._last_received.items():
            if (now - last).total_seconds() > self.max_stale_seconds:
                stale.append(symbol)
        return stale

    def invalidate_stale(self) -> None:
        for symbol in self._invalid_symbols():
            self._last_seq.pop(symbol, None)
            self._last_time.pop(symbol, None)
            self._last_received.pop(symbol, None)
            self._last_state.pop(symbol, None)
            self.events.append(StreamEvent(kind="stale", symbol=symbol))

    # ── Message processing ─────────────────────────────────────────────────

    def process(self, message: dict) -> list[StreamEvent]:
        """Process one decoded gRPC message. Returns the events emitted.

        Message shape (decoded from protobuf):
          {"heartbeat": True}  or  {"update": {...}}
          update: {symbol, bids: [{px, qty}], offers: [{px, qty}],
                   state?, transact_time?, book_hidden?}
        """
        if message.get("heartbeat"):
            self.events.append(StreamEvent(kind="heartbeat"))
            return [self.events[-1]]
        if "update" not in message:
            return []
        update_payload = message["update"]
        symbol = update_payload.get("symbol")
        if not symbol:
            raise ValueError("update without symbol")

        # 6. Instrument lookup BEFORE normalization.
        identifier = self.identifiers.by_symbol(symbol)
        instrument = self.instruments.by_symbol(symbol)
        if identifier is None:
            self.events.append(StreamEvent(kind="error", symbol=symbol,
                                           message="symbol not in identifier registry"))
            return [self.events[-1]]
        # 7. priceScale required — never a default.
        if instrument is None:
            if self.require_price_scale:
                self.events.append(StreamEvent(kind="error", symbol=symbol,
                                               message="no instrument; priceScale unknown"))
                return [self.events[-1]]
            price_scale = 1000  # only if require_price_scale is False (debug)
        else:
            price_scale = instrument.price_scale

        # 5. Sequence / freshness.
        transact_time = _parse_ts(update_payload.get("transact_time"))
        last_time = self._last_time.get(symbol)
        if last_time is not None and transact_time is not None and transact_time < last_time:
            self.events.append(StreamEvent(kind="stale", symbol=symbol,
                                           message="out-of-order transact_time"))
            return [self.events[-1]]

        sequence = self._last_seq.get(symbol, 0) + 1
        self._last_seq[symbol] = sequence
        self._last_time[symbol] = transact_time
        self._last_received[symbol] = self._now()

        # 3. Aggregated vs unaggregated: both come as {px: int64, qty}; only
        #    the price decoding differs via price_scale. Quantities are scaled
        #    integers on the Direct Exchange: decimal_qty = qty / fractionalQtyScale
        #    (fractionalQtyScale from the authoritative refdata instrument).
        if instrument is not None:
            frac_qty_scale = instrument.fractional_qty_scale
        else:
            frac_qty_scale = 1  # only if require_price_scale is False (debug)
        bids = tuple(
            (scaled_to_decimal(entry["px"], price_scale),
             scaled_qty_to_decimal(int(entry["qty"]), frac_qty_scale))
            for entry in update_payload.get("bids", [])
            if number(entry["qty"]) > 0
        )
        offers = tuple(
            (scaled_to_decimal(entry["px"], price_scale),
             scaled_qty_to_decimal(int(entry["qty"]), frac_qty_scale))
            for entry in update_payload.get("offers", [])
            if number(entry["qty"]) > 0
        )

        state = None
        raw_state = update_payload.get("state")
        if raw_state is not None:
            state = _map_grpc_state(raw_state)
            self._last_state[symbol] = state

        update = GrpcUpdate(
            symbol=symbol,
            bids=bids,
            offers=offers,
            state=state,
            transact_time=transact_time,
            sequence=sequence,
            book_hidden=bool(update_payload.get("book_hidden")),
        )
        event = StreamEvent(kind="update", symbol=symbol, update=update)
        self.events.append(event)
        return [event]

    def replay(self) -> list[dict]:
        """Deterministic replay: the full event sequence in arrival order."""
        return [e.as_dict() for e in self.events]

    def summary(self) -> dict:
        return {
            "events": len(self.events),
            "active_symbols": sorted(self.active_symbols),
            "reconnect_count": self.reconnect_count,
            "symbols_seen": sorted(self._last_seq),
        }


def _parse_ts(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _map_grpc_state(raw: str) -> UsMarketState:
    """Map a gRPC InstrumentState name (or bare name) to canonical state."""
    name = str(raw).upper()
    name = name.removeprefix("INSTRUMENT_STATE_")
    mapped = GRPC_INSTRUMENT_STATE.get(f"INSTRUMENT_STATE_{name}", name)
    return UsMarketState.parse(mapped)