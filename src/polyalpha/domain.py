"""Immutable normalized records; monetary quantities use decimal arithmetic."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(UTC)


def number(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non-finite numeric value")
    return result


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        if not self.price.is_finite() or not 0 <= self.price <= 1:
            raise ValueError("price outside [0,1]")
        if not self.size.is_finite() or self.size <= 0:
            raise ValueError("size must be positive and finite")


@dataclass(frozen=True)
class Book:
    token_id: str
    condition_id: str
    source_at: datetime
    received_at: datetime
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    tick_size: Decimal
    min_order_size: Decimal
    source_hash: str

    def __post_init__(self) -> None:
        utc(self.source_at)
        utc(self.received_at)
        if not self.token_id or not self.condition_id:
            raise ValueError("book identifiers required")
        if not self.tick_size.is_finite() or not 0 < self.tick_size <= 1:
            raise ValueError("invalid tick size")
        if not self.min_order_size.is_finite() or self.min_order_size < 0:
            raise ValueError("invalid minimum order size")
        for levels, reverse in ((self.bids, True), (self.asks, False)):
            prices = [level.price for level in levels]
            if prices != sorted(set(prices), reverse=reverse):
                raise ValueError("levels must be unique and sorted")
            if any(price % self.tick_size != 0 for price in prices):
                raise ValueError("price off tick grid")

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> Decimal | None:
        if self.spread is None:
            return None
        return (self.best_bid + self.best_ask) / 2


@dataclass(frozen=True)
class Market:
    market_id: str
    condition_id: str
    event_ids: tuple[str, ...]
    question: str
    description: str
    resolution_source: str | None
    deadline: datetime | None
    active: bool
    closed: bool
    accepting_orders: bool
    enable_order_book: bool
    liquidity: Decimal | None
    volume: Decimal | None
    fees_enabled: bool | None
    fee_parameters_json: str | None
    yes_token_id: str
    no_token_id: str
    received_at: datetime
    category: str = "unknown"

    def __post_init__(self) -> None:
        utc(self.received_at)
        if self.deadline is not None:
            utc(self.deadline)
        if not all((self.market_id, self.condition_id, self.yes_token_id, self.no_token_id)):
            raise ValueError("market identifiers required")
        if self.yes_token_id == self.no_token_id:
            raise ValueError("outcome tokens must differ")
        for value in (self.liquidity, self.volume):
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError("invalid liquidity/volume")
