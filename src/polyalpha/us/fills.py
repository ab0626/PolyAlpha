"""Canonical US trade/fill records for order-flow research (A6/A8).

Consumes decoded Polymarket US retail trade messages
(`SUBSCRIPTION_TYPE_TRADE`, docs/POLYMARKET_US_API.md) and produces a venue-free
``FillRecord`` with explicit maker/taker side and intent — so signed flow is
sourced from the venue's own aggressor attribution, never inferred from the
public book feed (Dubach 2026: feed-inferred direction is ~59% accurate).

The raw message is authoritative; the record is derived and re-derivable from
raw. This module is offline-testable (no network, no websockets).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

D = Decimal


def _amount(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or value == "":
        return None
    try:
        return D(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:  # milliseconds
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class FillRecord:
    """One US retail trade, with explicit aggressor (taker) attribution."""

    market_slug: str | None
    price: Decimal
    size: Decimal
    trade_time: datetime
    received_at: datetime
    maker_side: str  # "BUY" | "SELL"
    maker_intent: str | None  # ORDER_INTENT_*
    taker_side: str  # "BUY" | "SELL"
    taker_intent: str | None
    trade_id: str | None = None

    @property
    def aggressor_side(self) -> str:
        """The taker crossed the spread; taker side is the aggressor direction."""
        return self.taker_side

    @property
    def signed_size(self) -> Decimal:
        """Signed flow: +size for an aggressive buy, -size for an aggressive sell."""
        return self.size if self.taker_side == "BUY" else -self.size

    @property
    def signed_yes_flow(self) -> Decimal:
        """Signed flow on the YES token, from taker intent.

        BUY_LONG and SELL_SHORT both add YES pressure (+); SELL_LONG and
        BUY_SHORT remove it (-). Falls back to taker side when intent is absent.
        """
        if self.taker_intent:
            buy_yes = ("BUY_LONG" in self.taker_intent) or ("SELL_SHORT" in self.taker_intent)
            sell_yes = ("SELL_LONG" in self.taker_intent) or ("BUY_SHORT" in self.taker_intent)
            if buy_yes and not sell_yes:
                return self.size
            if sell_yes and not buy_yes:
                return -self.size
        return self.signed_size


def _normalize_side(side: object) -> str:
    """Normalize the venue's ORDER_SIDE_* enum to BUY/SELL."""
    s = str(side).upper()
    if "BUY" in s or s == "1":
        return "BUY"
    if "SELL" in s or s == "2":
        return "SELL"
    raise ValueError(f"invalid trade side: {side!r}")


def parse_us_trade(raw: dict, received_at: datetime) -> FillRecord:
    """Parse a US retail trade message into a FillRecord.

    Confirmed shape (polymarket-us SDK websocket/types.py):
      {"requestId": ..., "subscriptionType": "SUBSCRIPTION_TYPE_TRADE",
       "trade": {"marketSlug": ..., "price": Amount, "quantity": Amount,
                 "tradeTime": ..., "maker": {"side": "ORDER_SIDE_*",
                 "intent": "ORDER_INTENT_*"}, "taker": {...}}}
    """
    payload = raw.get("trade", raw) if isinstance(raw.get("trade"), dict) else raw

    price = _amount(payload.get("price"))
    size = _amount(payload.get("quantity")) or _amount(payload.get("size"))
    if price is None or size is None or size <= 0 or not (0 <= price <= 1):
        raise ValueError("invalid US trade message")

    trade_time = _parse_time(payload.get("tradeTime")) or received_at
    maker = payload.get("maker") or {}
    taker = payload.get("taker") or {}

    maker_side = _normalize_side(maker.get("side"))
    taker_side = _normalize_side(taker.get("side"))

    return FillRecord(
        market_slug=payload.get("marketSlug") or payload.get("slug"),
        price=price,
        size=size,
        trade_time=trade_time,
        received_at=received_at,
        maker_side=maker_side,
        maker_intent=maker.get("intent"),
        taker_side=taker_side,
        taker_intent=taker.get("intent"),
        trade_id=payload.get("tradeId") or payload.get("id"),
    )
