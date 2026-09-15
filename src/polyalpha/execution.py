"""Deterministic taker simulation. There is deliberately no real order client."""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from .domain import Book, Market, number, utc

D = Decimal


@dataclass(frozen=True)
class FeeSchedule:
    rate: Decimal
    known_at: datetime
    version: str

    def __post_init__(self):
        utc(self.known_at)
        if not self.rate.is_finite() or self.rate < 0 or not self.version:
            raise ValueError("invalid fee schedule")

    @classmethod
    def from_market(cls, market: Market):
        if market.fees_enabled is False:
            return cls(D(0), market.received_at, "explicit-fee-free")
        if market.fees_enabled is not True or not market.fee_parameters_json:
            raise ValueError("unknown fees")
        raw = json.loads(market.fee_parameters_json)
        if raw.get("exponent") != 1 or raw.get("takerOnly") is not True:
            raise ValueError("unsupported fee schedule")
        return cls(number(raw["rate"]), market.received_at, market.fee_parameters_json)

    def fee(self, shares: Decimal, price: Decimal) -> Decimal:
        if not shares.is_finite() or shares < 0 or not price.is_finite() or not 0 <= price <= 1:
            raise ValueError("invalid fee inputs")
        # Cash-equivalent taker fee. Round per consumed level, not at VWAP.
        return (shares * self.rate * price * (1 - price)).quantize(
            D("0.00001"), rounding=ROUND_HALF_UP
        )


@dataclass(frozen=True)
class Order:
    order_id: str
    token_id: str
    side: str
    shares: Decimal
    submitted_at: datetime
    limit: Decimal | None = None
    allow_partial: bool = False

    def __post_init__(self):
        utc(self.submitted_at)
        if not self.order_id or self.side not in ("BUY", "SELL"):
            raise ValueError("invalid order")
        if not self.shares.is_finite() or self.shares <= 0:
            raise ValueError("invalid order size")
        if self.limit is not None and (not self.limit.is_finite() or not 0 <= self.limit <= 1):
            raise ValueError("invalid limit")


@dataclass(frozen=True)
class Fill:
    order_id: str
    token_id: str
    side: str
    requested: Decimal
    shares: Decimal
    notional: Decimal
    fees: Decimal
    depth_slippage: Decimal
    filled_at: datetime
    levels: tuple[tuple[Decimal, Decimal], ...]

    def __post_init__(self):
        utc(self.filled_at)
        if self.side not in ("BUY", "SELL") or not self.order_id or not self.token_id:
            raise ValueError("invalid fill identity")
        if any(
            not x.is_finite() or x < 0
            for x in (
                self.requested,
                self.shares,
                self.notional,
                self.fees,
                self.depth_slippage,
            )
        ):
            raise ValueError("invalid fill quantities")
        if self.requested <= 0 or self.shares > self.requested:
            raise ValueError("overfill")
        if any(
            not p.is_finite() or not q.is_finite() or not 0 <= p <= 1 or q <= 0
            for p, q in self.levels
        ):
            raise ValueError("invalid fill legs")
        if (
            sum((q for p, q in self.levels), D(0)) != self.shares
            or sum((p * q for p, q in self.levels), D(0)) != self.notional
        ):
            raise ValueError("fill does not conserve quantity/notional")

    @property
    def vwap(self):
        return self.notional / self.shares if self.shares else None


def walk(
    book: Book,
    order: Order,
    fees: FeeSchedule,
    at: datetime,
    consumed: dict | None = None,
    max_age: float = 30,
) -> Fill:
    utc(at)
    if order.token_id != book.token_id:
        raise ValueError("token mismatch")
    if (
        at < order.submitted_at
        or book.received_at > at
        or book.source_at > at
        or fees.known_at > at
    ):
        raise ValueError("future information or execution before submission")
    if (at - book.source_at).total_seconds() > max_age:
        raise ValueError("stale book")
    if book.spread is None or book.spread <= 0:
        raise ValueError("unusable book")
    if order.shares < book.min_order_size:
        raise ValueError("below minimum order size")
    if order.limit is not None and order.limit % book.tick_size:
        raise ValueError("limit off tick grid")
    remaining, legs = order.shares, []
    levels = book.asks if order.side == "BUY" else book.bids
    for level in levels:
        if order.limit is not None and (
            (order.side == "BUY" and level.price > order.limit)
            or (order.side == "SELL" and level.price < order.limit)
        ):
            break
        available = max(D(0), level.size - (consumed or {}).get((order.side, level.price), D(0)))
        quantity = min(remaining, available)
        if quantity:
            legs.append((level.price, quantity))
            remaining -= quantity
        if not remaining:
            break
    if remaining and not order.allow_partial:
        raise ValueError("insufficient depth")
    shares = order.shares - remaining
    notional = sum((p * q for p, q in legs), D(0))
    charge = sum((fees.fee(q, p) for p, q in legs), D(0))
    impact = (
        notional - shares * levels[0].price
        if order.side == "BUY"
        else shares * levels[0].price - notional
    )
    return Fill(
        order.order_id,
        order.token_id,
        order.side,
        order.shares,
        shares,
        notional,
        charge,
        impact,
        at,
        tuple(legs),
    )


class Simulator:
    """Conservative lifetime price-level depletion; replenishment is never inferred."""

    def __init__(self):
        self.consumed = {}
        self.orders = {}

    def quote(self, book, order, fees, at):
        if order.order_id in self.orders:
            raise ValueError("duplicate order ID")
        return walk(book, order, fees, at, self.consumed.get(book.token_id, {}))

    def commit(self, fill):
        if fill.order_id in self.orders:
            raise ValueError("duplicate order ID")
        used = self.consumed.setdefault(fill.token_id, {})
        for price, quantity in fill.levels:
            key = (fill.side, price)
            used[key] = used.get(key, D(0)) + quantity
        self.orders[fill.order_id] = fill
