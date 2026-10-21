"""Polymarket CLOB canonicalization layer (frozen contract).

Converts raw marketData snapshots -> PolymarketBookState and raw trade ->
PolymarketTradeEvent. This is canonicalization, not reconstruction: every
marketData record is a complete snapshot, so there is no delta replay.

Frozen contract: docs/POLYMARKET_MICROSTRUCTURE_EVIDENCE_V1.md
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

K_LEVELS = 5
TICK_SIZE = Decimal("0.001")
FORWARD_BOUNDARY = "2026-09-22T00:00:00+00:00"
MARKOUT_HORIZONS = (0.1, 1.0, 5.0, 30.0, 300.0, 1800.0)
MARKOUT_TOLERANCE = {0.1: 0.05, 1.0: 0.2, 5.0: 0.5, 30.0: 2.0, 300.0: 10.0, 1800.0: 30.0}
# Frozen Polymarket US fee model (docs.polymarket.us/fees): taker coefficient
# Θ = 0.0695, Tier-1 maker rebate = 0, Tier-1 taker rebate = 0. Fee is
# price-symmetric: fee = Θ * C * p * (1 - p).
TAKER_FEE_COEFFICIENT = Decimal("0.0695")


def taker_fee(price: Decimal) -> Decimal:
    """One-leg taker fee in USDC per contract at price p."""
    return TAKER_FEE_COEFFICIENT * price * (Decimal(1) - price)

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


def content_hash(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def off_tick(price: Decimal) -> bool:
    """True if price is not on the 0.001 grid (more than 3 decimals)."""
    return (price * 1000) % 1 != 0


def _native_levels(levels) -> tuple[tuple[Decimal, Decimal], ...]:
    """Native [{'px':{value},'qty':...}, ...] -> ((price, size), ...) sorted ascending.

    Preserves exact Decimal values; does not drop or reinterpret any level."""
    out = []
    for lvl in levels or []:
        px = _dec((lvl.get("px") or {}).get("value"))
        qty = _dec(lvl.get("qty"))
        if px is not None and qty is not None:
            out.append((px, qty))
    out.sort(key=lambda x: x[0])
    return tuple(out)


@dataclass(frozen=True)
class PolymarketBookState:
    market_slug: str
    exchange_timestamp_ms: int
    received_at_ns: int
    processed_at_ns: int
    bids: tuple[tuple[Decimal, Decimal], ...]     # native YES/long bids, ascending
    offers: tuple[tuple[Decimal, Decimal], ...]   # native YES/long offers, ascending
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid: Decimal | None
    spread: Decimal | None
    top5_bid_depth: Decimal
    top5_ask_depth: Decimal
    imbalance: Decimal | None
    microprice: Decimal | None
    integrity_hash: str
    integrity_verified: bool
    message_sequence_local: int | None
    partition: str

    @property
    def gap_clean(self) -> bool:
        return self.integrity_verified

    @property
    def source_record_id(self) -> str:
        return self.integrity_hash

    def to_record(self) -> dict:
        return {
            "market_slug": self.market_slug,
            "exchange_timestamp_ms": self.exchange_timestamp_ms,
            "received_at_ns": self.received_at_ns,
            "processed_at_ns": self.processed_at_ns,
            "bids": [[str(p), str(s)] for p, s in self.bids],
            "offers": [[str(p), str(s)] for p, s in self.offers],
            "best_bid": str(self.best_bid) if self.best_bid is not None else None,
            "best_ask": str(self.best_ask) if self.best_ask is not None else None,
            "mid": str(self.mid) if self.mid is not None else None,
            "spread": str(self.spread) if self.spread is not None else None,
            "top5_bid_depth": str(self.top5_bid_depth),
            "top5_ask_depth": str(self.top5_ask_depth),
            "imbalance": str(self.imbalance) if self.imbalance is not None else None,
            "microprice": str(self.microprice) if self.microprice is not None else None,
            "tick_size": str(TICK_SIZE),
            "integrity_hash": self.integrity_hash,
            "integrity_verified": self.integrity_verified,
            "message_sequence_local": self.message_sequence_local,
            "partition": self.partition,
        }


@dataclass(frozen=True)
class PolymarketTradeEvent:
    market_slug: str
    trade_time_ms: int
    received_at_ns: int | None
    processed_at_ns: int | None
    price: Decimal
    size: Decimal
    maker_side: str
    taker_side: str
    taker_outcome: str
    taker_intent: str
    maker_taker_complete: bool
    classification_status: str
    yes_direction: int
    trade_id: str
    integrity_hash: str
    integrity_verified: bool
    partition: str

    def to_record(self) -> dict:
        return {
            "market_slug": self.market_slug,
            "trade_time_ms": self.trade_time_ms,
            "received_at_ns": self.received_at_ns,
            "processed_at_ns": self.processed_at_ns,
            "price": str(self.price),
            "size": str(self.size),
            "maker_side": self.maker_side,
            "taker_side": self.taker_side,
            "taker_outcome": self.taker_outcome,
            "taker_intent": self.taker_intent,
            "maker_taker_complete": self.maker_taker_complete,
            "classification_status": self.classification_status,
            "yes_direction": self.yes_direction,
            "trade_id": self.trade_id,
            "integrity_hash": self.integrity_hash,
            "integrity_verified": self.integrity_verified,
            "partition": self.partition,
        }


def yes_direction(taker_outcome: str, taker_side: str) -> int:
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


def _classify(outcome: str, side: str, intent: str) -> tuple[str, bool]:
    o_ok = outcome in ("YES", "NO")
    s_ok = side in ("BUY", "SELL")
    i_ok = intent not in ("", "UNDEFINED")
    if o_ok and s_ok and i_ok:
        return "COMPLETE", True
    if o_ok and s_ok and not i_ok:
        return "MISSING_INTENT", False
    if not o_ok:
        return "MISSING_OUTCOME", False
    if not s_ok:
        return "MISSING_SIDE", False
    return "UNKNOWN", False


def parse_book(outer: dict, inner: dict, canonical_json: str | None) -> PolymarketBookState | None:
    """Fail-closed: malformed/impossible books return None (never repaired)."""
    try:
        md = (inner.get("payload") or {}).get("marketData") or {}
        slug = md.get("marketSlug")
        venue = inner.get("exchange_timestamp_ms")
        recv = inner.get("received_at_ns")
        proc = inner.get("processed_at_ns")
        seq = inner.get("message_sequence_local")
        if not slug or not isinstance(venue, (int, float)) or not isinstance(recv, (int, float)):
            return None

        bids = _native_levels(md.get("bids"))
        offers = _native_levels(md.get("offers"))
        if not bids or not offers:
            return None  # one-sided/empty -> no canonical mid, fail closed

        best_bid = bids[-1][0]
        best_ask = offers[0][0]
        if not (D(0) < best_bid < best_ask < D(1)):
            return None  # crossed or impossible book

        top5_bid = sum(sz for _, sz in bids[-K_LEVELS:])
        top5_ask = sum(sz for _, sz in offers[:K_LEVELS])
        mid = (best_bid + best_ask) / D(2)
        spread = best_ask - best_bid
        total = top5_bid + top5_ask
        imbalance = (top5_bid - top5_ask) / total if total > 0 else D(0)
        microprice = (best_ask * top5_bid + best_bid * top5_ask) / total if total > 0 else None

        cid = content_hash(canonical_json) if canonical_json else ""
        verified = bool(outer.get("sha256")) and outer["sha256"] == cid

        return PolymarketBookState(
            market_slug=slug,
            exchange_timestamp_ms=int(venue),
            received_at_ns=int(recv),
            processed_at_ns=int(proc or 0),
            bids=bids, offers=offers,
            best_bid=best_bid, best_ask=best_ask, mid=mid, spread=spread,
            top5_bid_depth=top5_bid, top5_ask_depth=top5_ask,
            imbalance=imbalance, microprice=microprice,
            integrity_hash=cid, integrity_verified=verified,
            message_sequence_local=int(seq) if isinstance(seq, int) else None,
            partition=partition_of(int(venue)),
        )
    except Exception:
        return None


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
        venue = _to_epoch_ms(tr.get("tradeTime"))
        recv = inner.get("received_at_ns")
        proc = inner.get("processed_at_ns")
        if not slug or not tid or price is None or size is None or venue is None:
            return None
        if not (D(0) < price < D(1)) or size <= 0:
            return None
        cid = content_hash(canonical_json) if canonical_json else ""
        verified = bool(outer.get("sha256")) and outer["sha256"] == cid
        status, complete = _classify(taker_outcome, taker_side, taker_intent)
        direction = yes_direction(taker_outcome, taker_side) if (taker_outcome in ("YES", "NO") and taker_side in ("BUY", "SELL")) else 0
        return PolymarketTradeEvent(
            market_slug=slug, trade_time_ms=venue,
            received_at_ns=int(recv) if isinstance(recv, (int, float)) else None,
            processed_at_ns=int(proc) if isinstance(proc, (int, float)) else None,
            price=price, size=size, maker_side=maker_side, taker_side=taker_side,
            taker_outcome=taker_outcome, taker_intent=taker_intent,
            maker_taker_complete=complete, classification_status=status,
            yes_direction=direction, trade_id=tid,
            integrity_hash=cid, integrity_verified=verified,
            partition=partition_of(venue),
        )
    except Exception:
        return None


def features(b: PolymarketBookState) -> dict:
    """Pure feature extraction from canonical state (no venue-specific logic)."""
    return {
        "mid": b.mid, "spread": b.spread, "imbalance": b.imbalance,
        "microprice": b.microprice, "best_bid": b.best_bid, "best_ask": b.best_ask,
        "top5_bid_depth": b.top5_bid_depth, "top5_ask_depth": b.top5_ask_depth,
    }


__all__ = [
    "PolymarketBookState", "PolymarketTradeEvent", "parse_book", "parse_trade",
    "features", "yes_direction", "partition_of", "content_hash", "off_tick",
    "taker_fee", "K_LEVELS", "TICK_SIZE", "TAKER_FEE_COEFFICIENT",
    "MARKOUT_HORIZONS", "MARKOUT_TOLERANCE", "FORWARD_BOUNDARY", "BOUNDARY_MS",
]