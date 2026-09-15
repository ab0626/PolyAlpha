"""USRetailAdapter — normalize Polymarket US retail data into canonical domain.

Part of the US adapter family. Consumes the public gateway API
(https://gateway.polymarket.us) and the authenticated market WebSocket, and
produces canonical Market / Book / Level objects with Decimal probabilities.

The canonical core must never care whether 0.555 came from
    {"px": {"value": "0.555", "currency": "USD"}}   (retail Amount)
    px = 555, price_scale = 1000                     (exchange scaled int)
    "0.555"                                          (international token price)
All three normalize to Decimal at this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..domain import Book, Level, Market, number, utc
from .identifiers import UsIdentifier
from .states import UsMarketState

D = Decimal


def amount_to_decimal(value: object, currency: str | None = None) -> Decimal:
    """Normalize a retail Amount: {"value": "0.555", "currency": "USD"}."""
    if isinstance(value, dict):
        v = value.get("value")
        cur = value.get("currency")
        if cur and currency and cur != currency:
            raise ValueError(f"currency mismatch: {cur} != {currency}")
        value = v
    return number(value)


def scaled_to_decimal(px: int, price_scale: int) -> Decimal:
    """Normalize an exchange scaled integer: px / price_scale."""
    if not isinstance(px, int) and not (isinstance(px, float) and px.is_integer()):
        raise ValueError(f"exchange price must be an integer, got {px!r}")
    if price_scale <= 0:
        raise ValueError("price_scale must be positive")
    return number(px) / number(price_scale)


def retail_qty(value: object) -> Decimal:
    """Normalize a retail qty decimal string (may be fractional contracts)."""
    return number(value)


def parse_market(raw: dict, received_at: datetime, identifier: UsIdentifier) -> Market:
    """Build a canonical Market from a US retail market object.

    The US market carries marketSides[] (long/short) rather than
    yes/no token ids; the identifier supplies venue-neutral internal side ids.
    """
    received = utc(received_at)
    state = UsMarketState.parse(raw.get("state", "OPEN"))
    end_date = raw.get("endDate")
    deadline = utc(datetime.fromisoformat(end_date.replace("Z", "+00:00"))) if end_date else None

    def amount(key: str) -> Decimal | None:
        value = raw.get(key)
        return amount_to_decimal(value) if value is not None else None

    return Market(
        market_id=identifier.internal_market_id,
        condition_id=identifier.condition_id,
        event_ids=(raw.get("eventId") or identifier.internal_market_id,),
        question=raw.get("question") or raw.get("title") or "",
        description=raw.get("description") or "",
        resolution_source=raw.get("resolutionSource"),
        deadline=deadline,
        active=state.is_active(),
        closed=state.is_closed(),
        accepting_orders=state.accepts_orders(),
        enable_order_book=state.order_book_enabled(),
        liquidity=amount("liquidity"),
        volume=amount("volume"),
        fees_enabled=None,
        fee_parameters_json=None,
        yes_token_id=identifier.long_side_id,
        no_token_id=identifier.short_side_id,
        received_at=received,
        category=raw.get("category") or "unknown",
    )


def parse_book(
    raw: dict,
    received_at: datetime,
    identifier: UsIdentifier,
    tick_size: Decimal | None = None,
    min_order_size: Decimal | None = None,
) -> Book:
    """Build a canonical Book from a US retail /book or market WS payload.

    US shape:
      {"marketData": {"marketSlug": ..., "bids": [{px, qty}], "offers": [{px, qty}],
                      "state": ..., "transactTime": ...}}
    """
    received = utc(received_at)
    data = raw.get("marketData", raw)

    def levels(key: str, reverse: bool) -> tuple[Level, ...]:
        result = []
        for entry in data.get(key, []):
            px = amount_to_decimal(entry.get("px"))
            qty = retail_qty(entry.get("qty"))
            if qty <= 0:
                continue  # zero-size level is not a level
            result.append(Level(px, qty))
        return tuple(sorted(result, key=lambda x: x.price, reverse=reverse))

    bids = levels("bids", True)
    offers = levels("offers", False)

    transact_time = data.get("transactTime")
    source_at = (
        utc(datetime.fromisoformat(transact_time.replace("Z", "+00:00")))
        if transact_time
        else received
    )
    ts = tick_size if tick_size is not None else D("0.001")  # provisional fallback only
    mos = min_order_size if min_order_size is not None else D("1")
    source_hash = str(data.get("state", "")) if not raw.get("hash") else raw.get("hash")

    return Book(
        token_id=identifier.long_side_id,
        condition_id=identifier.condition_id,
        source_at=source_at,
        received_at=received,
        bids=bids,
        asks=offers,
        tick_size=ts,
        min_order_size=mos,
        source_hash=source_hash,
    )


def parse_events(raw: dict, received_at: datetime, registry) -> list[Market]:
    """Build canonical Markets from a US /v1/events response."""
    result = []
    for event in raw.get("events", []):
        for market in event.get("markets", []):
            slug = market.get("slug")
            if not slug:
                continue
            identifier = registry.by_slug(slug)
            if identifier is None:
                continue
            result.append(parse_market(market, received_at, identifier))
    return result


@dataclass(frozen=True)
class PriceHistoryPoint:
    timestamp: int  # Unix seconds
    long_price: Decimal
    short_price: Decimal

    @property
    def sum_prices(self) -> Decimal:
        return self.long_price + self.short_price


def parse_price_history(raw: dict) -> list[PriceHistoryPoint]:
    """Parse /v1/price-history.

    WARNING: longPrice/shortPrice are DISPLAY prices that preserve the
    bid-ask spread (they can sum to > 1). They are NOT historical trade
    prices and must never be used as a trade tape.
    """
    points = []
    for item in raw.get("history", []):
        points.append(
            PriceHistoryPoint(
                timestamp=int(item["timestamp"]),
                long_price=number(item.get("longPrice", 0)),
                short_price=number(item.get("shortPrice", 0)),
            )
        )
    return points


def parse_settlement(raw: dict, identifier: UsIdentifier) -> dict:
    """Normalize a settlement payload into a flat dict with a finality flag."""
    data = raw.get("marketData", raw)
    settlement = data.get("stats", {}).get("settlementPx") or data.get("settlementPx")
    return {
        "internal_market_id": identifier.internal_market_id,
        "settlement_px": amount_to_decimal(settlement) if settlement is not None else None,
        "settlement_preliminary": bool(
            data.get("stats", {}).get("settlementPreliminaryFlag", False)
        ),
        "settlement_calculation_method": data.get("stats", {}).get(
            "settlementPriceCalculationMethod"
        ),
        "is_final": not bool(data.get("stats", {}).get("settlementPreliminaryFlag", False)),
    }


def parse_bbo(raw: dict, identifier: UsIdentifier) -> dict:
    """Normalize a /bbo payload into a flat dict of Decimals."""
    data = raw.get("marketData", raw)

    def amount(key: str) -> Decimal | None:
        value = data.get(key)
        return amount_to_decimal(value) if value is not None else None

    return {
        "internal_market_id": identifier.internal_market_id,
        "best_bid": amount("bestBid"),
        "best_ask": amount("bestAsk"),
        "current_px": amount("currentPx"),
        "last_trade_px": amount("lastTradePx"),
        "long_quote": amount("longQuote"),
        "short_quote": amount("shortQuote"),
        "bid_depth": data.get("bidDepth"),
        "ask_depth": data.get("askDepth"),
        "shares_traded": data.get("sharesTraded"),
        "open_interest": data.get("openInterest"),
    }


def parse_exchange_book(
    raw: dict,
    received_at: datetime,
    identifier: UsIdentifier,
    price_scale: int,
    tick_size: Decimal | None = None,
    min_order_size: Decimal | None = None,
) -> Book:
    """Build a canonical Book from a Direct Exchange order book.

    Exchange representation differs again: prices are scaled integers
    (px / priceScale), not retail Amount objects. `price_scale` MUST come from
    the authoritative refdata instrument, never a default.
    """
    from .instruments import scaled_to_decimal

    received = utc(received_at)
    data = raw.get("marketData", raw)

    def levels(key: str, reverse: bool) -> tuple[Level, ...]:
        result = []
        for entry in data.get(key, []):
            px = scaled_to_decimal(int(entry["px"]), price_scale)
            qty = retail_qty(entry.get("qty"))
            if qty <= 0:
                continue
            result.append(Level(px, qty))
        return tuple(sorted(result, key=lambda x: x.price, reverse=reverse))

    bids = levels("bids", True)
    offers = levels("offers", False)

    transact_time = data.get("transactTime")
    source_at = (
        utc(datetime.fromisoformat(transact_time.replace("Z", "+00:00")))
        if transact_time
        else received
    )
    ts = tick_size if tick_size is not None else D("0.001")  # provisional fallback only
    mos = min_order_size if min_order_size is not None else D("1")
    source_hash = str(data.get("state", "")) if not raw.get("hash") else raw.get("hash")

    return Book(
        token_id=identifier.long_side_id,
        condition_id=identifier.condition_id,
        source_at=source_at,
        received_at=received,
        bids=bids,
        asks=offers,
        tick_size=ts,
        min_order_size=mos,
        source_hash=source_hash,
    )