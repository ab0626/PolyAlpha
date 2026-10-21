"""Polymarket CLOB canonical microstructure layer (frozen contract).

Converts raw marketData snapshots -> PolymarketBookState and raw trade ->
PolymarketTradeEvent. Pure, deterministic, fail-closed. No Kalshi complement
logic; Polymarket books are native bid/offer snapshots.

Frozen contract: docs/POLYMARKET_MICROSTRUCTURE_EVIDENCE_V1.md
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

K_LEVELS = 5
FORWARD_BOUNDARY = "2026-09-22T00:00:00+00:00"
MARKOUT_HORIZONS = (0.1, 1.0, 5.0, 30.0, 300.0, 1800.0)
MARKOUT_TOLERANCE = {0.1: 0.05, 1.0: 0.2, 5.0: 0.5, 30.0: 2.0, 300.0: 10.0, 1800.0: 30.0}
# Taker fee is pinned from the venue fee schedule before evaluation (not inferred).
TAKER_FEE: Decimal | None = None

BOUNDARY_MS = int(datetime.fromisoformat(FORWARD_BOUNDARY).timestamp() * 1000)

D = Decimal


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return D(str(v))
    except (InvalidOperation, ValueError):
        return None


def _to_epoch_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def partition_of(venue_ts_ms: int) -> str:
    return "forward" if venue_ts_ms >= BOUNDARY_MS else "dev"


def _content_id(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PolymarketBookState:
    venue_ts_ms: int
    receive_ts_ns: int
    processed_ts_ns: int
    market_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    bid_depth_k: Decimal
    ask_depth_k: Decimal
    mid: Decimal | None
    spread: Decimal | None
    microprice: Decimal | None
    imbalance: Decimal | None
    gap_clean: bool
    source_sha256: str
    partition: str

    def to_record(self) -> dict:
        return {
            "venue_ts_ms": self.venue_ts_ms,
            "receive_ts_ns": self.receive_ts_ns,
            "processed_ts_ns": self.processed_ts_ns,
            "market_id": self.market_id,
            "best_bid": str(self.best_bid) if self.best_bid is not None else None,
            "best_ask": str(self.best_ask) if self.best_ask is not None else None,
            "bid_depth_k": str(self.bid_depth_k),
            "ask_depth_k": str(self.ask_depth_k),
            "mid": str(self.mid) if self.mid is not None else None,
            "spread": str(self.spread) if self.spread is not None else None,
            "microprice": str(self.microprice) if self.microprice is not None else None,
            "imbalance": str(self.imbalance) if self.imbalance is not None else None,
            "gap_clean": self.gap_clean,
            "source_sha256": self.source_sha256,
            "partition": self.partition,
        }


@dataclass(frozen=True)
class PolymarketTradeEvent:
    venue_ts_ms: int
    market_id: str
    price: Decimal
    size: Decimal
    taker_side: str
    taker_outcome: str
    taker_intent: str
    maker_side: str
    yes_direction: int
    trade_id: str
    gap_clean: bool
    source_sha256: str
    partition: str

    def to_record(self) -> dict:
        return {
            "venue_ts_ms": self.venue_ts_ms,
            "market_id": self.market_id,
            "price": str(self.price),
            "size": str(self.size),
            "taker_side": self.taker_side,
            "taker_outcome": self.taker_outcome,
            "taker_intent": self.taker_intent,
            "maker_side": self.maker_side,
            "yes_direction": self.yes_direction,
            "trade_id": self.trade_id,
            "gap_clean": self.gap_clean,
            "source_sha256": self.source_sha256,
            "partition": self.partition,
        }


def _levels(levels: list) -> list[tuple[Decimal, Decimal]]:
    """Parse [{'px':{value}, 'qty':...}, ...] -> [(price, size), ...], sorted ascending."""
    out = []
    for lvl in levels or []:
        px = _dec((lvl.get("px") or {}).get("value"))
        qty = _dec(lvl.get("qty"))
        if px is not None and qty is not None:
            out.append((px, qty))
    out.sort(key=lambda x: x[0])
    return out


def _top_depth(levels: list[tuple[Decimal, Decimal]], take_highest: bool, k: int = K_LEVELS) -> Decimal:
    """For bids (ascending), best = highest -> take last k. For offers, best = lowest -> take first k."""
    if take_highest:
        sel = levels[-k:]
    else:
        sel = levels[:k]
    return sum(sz for _, sz in sel)


def parse_book(outer: dict, inner: dict, canonical_json: str | None) -> PolymarketBookState | None:
    """Fail-closed: malformed/impossible books return None (never repaired)."""
    try:
        md = (inner.get("payload") or {}).get("marketData") or {}
        slug = md.get("marketSlug")
        venue = inner.get("exchange_timestamp_ms")
        recv = inner.get("received_at_ns")
        proc = inner.get("processed_at_ns")
        if not slug or not isinstance(venue, (int, float)) or not isinstance(recv, (int, float)):
            return None

        bids = _levels(md.get("bids"))
        offers = _levels(md.get("offers"))
        if not bids or not offers:
            return None  # one-sided -> fail closed for canonical mid

        best_bid = bids[-1][0]
        best_ask = offers[0][0]
        if not (D(0) < best_bid < best_ask < D(1)):
            return None  # impossible book

        bid_depth = _top_depth(bids, take_highest=True)
        ask_depth = _top_depth(offers, take_highest=False)
        mid = (best_bid + best_ask) / D(2)
        spread = best_ask - best_bid
        total = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total if total > 0 else D(0)
        microprice = (best_ask * bid_depth + best_bid * ask_depth) / total if total > 0 else None

        cid = _content_id(canonical_json) if canonical_json else ""
        hash_ok = bool(outer.get("sha256")) and outer["sha256"] == cid

        return PolymarketBookState(
            venue_ts_ms=int(venue), receive_ts_ns=int(recv), processed_ts_ns=int(proc or 0),
            market_id=slug, best_bid=best_bid, best_ask=best_ask,
            bid_depth_k=bid_depth, ask_depth_k=ask_depth, mid=mid, spread=spread,
            microprice=microprice, imbalance=imbalance, gap_clean=hash_ok,
            source_sha256=cid, partition=partition_of(int(venue)),
        )
    except Exception:
        return None


def yes_direction(taker_outcome: str, taker_side: str) -> int:
    """YES-direction sign from taker outcome + side. +1 = bullish YES."""
    o = (taker_outcome or "").upper()
    s = (taker_side or "").upper()
    if o == "YES" and s == "BUY":
        return 1
    if o == "YES" and s == "SELL":
        return -1
    if o == "NO" and s == "BUY":
        return -1
    if o == "NO" and s == "SELL":
        return 1
    return 0


def parse_trade(outer: dict, inner: dict, canonical_json: str | None) -> PolymarketTradeEvent | None:
    try:
        tr = (inner.get("payload") or {}).get("trade") or {}
        slug = tr.get("marketSlug")
        tid = tr.get("id")
        price = _dec((tr.get("price") or {}).get("value"))
        size = _dec((tr.get("quantity") or {}).get("value"))
        taker = tr.get("taker") or {}
        maker = tr.get("maker") or {}
        taker_outcome = (taker.get("outcomeSide") or "").replace("OUTCOME_SIDE_", "")
        taker_side = (taker.get("side") or "").replace("ORDER_SIDE_", "")
        taker_intent = (taker.get("intent") or "").replace("ORDER_INTENT_", "")
        maker_side = (maker.get("side") or "").replace("ORDER_SIDE_", "")
        trade_time = tr.get("tradeTime")
        venue = _to_epoch_ms(trade_time)
        if not slug or tid is None or price is None or size is None or venue is None:
            return None
        if not (D(0) < price < D(1)) or size <= 0:
            return None
        cid = _content_id(canonical_json) if canonical_json else ""
        hash_ok = bool(outer.get("sha256")) and outer["sha256"] == cid
        return PolymarketTradeEvent(
            venue_ts_ms=venue, market_id=slug, price=price, size=size,
            taker_side=taker_side, taker_outcome=taker_outcome, taker_intent=taker_intent,
            maker_side=maker_side, yes_direction=yes_direction(taker_outcome, taker_side),
            trade_id=tid, gap_clean=hash_ok, source_sha256=cid, partition=partition_of(venue),
        )
    except Exception:
        return None


def features(b: PolymarketBookState) -> dict:
    """Pure feature extraction from canonical state (no venue-specific logic)."""
    return {
        "mid": b.mid,
        "spread": b.spread,
        "imbalance": b.imbalance,
        "microprice": b.microprice,
        "best_bid": b.best_bid,
        "best_ask": b.best_ask,
        "bid_depth_k": b.bid_depth_k,
        "ask_depth_k": b.ask_depth_k,
    }


__all__ = [
    "PolymarketBookState", "PolymarketTradeEvent", "parse_book", "parse_trade",
    "features", "yes_direction", "partition_of", "K_LEVELS", "MARKOUT_HORIZONS",
    "MARKOUT_TOLERANCE", "FORWARD_BOUNDARY", "BOUNDARY_MS", "TAKER_FEE",
]