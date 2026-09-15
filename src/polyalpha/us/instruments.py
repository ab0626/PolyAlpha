"""USExchangeAdapter instrument definitions — authoritative venue semantics.

Part of the US adapter family. The Direct Exchange reference-data API
(POST /v1/refdata/instruments) is the authoritative source of per-instrument
semantics: symbol, tickSize, minimumTradeQty, priceScale, state lifecycle,
rules, event metadata, participants, and payout value.

This is where provisional adapter defaults (e.g. tick size 0.001) get replaced
by authoritative per-instrument values. The canonical core still only sees
Decimal probabilities and normalized domain objects; these instruments are the
adapter's source of truth for validating books and normalizing scaled prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from ..domain import number
from .states import UsMarketState

D = Decimal


@dataclass(frozen=True)
class UsInstrument:
    """Authoritative instrument definition from refdata/instruments."""

    symbol: str
    tick_size: Decimal
    minimum_trade_qty: Decimal
    price_scale: int
    state: UsMarketState
    # Optional / richer fields
    question: str | None = None
    payout_value: Decimal | None = None
    outcome_type: str | None = None
    event_id: str | None = None
    event_series: str | None = None
    event_category: str | None = None
    event_subcategory: str | None = None
    event_start_time: datetime | None = None
    start_date: datetime | None = None
    expiration_date: datetime | None = None
    termination_date: datetime | None = None
    long_participant_id: str | None = None
    long_participant_name: str | None = None
    short_participant_id: str | None = None
    short_participant_name: str | None = None
    instrument_rules: str | None = None

    def scaled_to_probability(self, scaled_px: int) -> Decimal:
        """Convert an exchange scaled integer (px / priceScale) to Decimal."""
        return scaled_to_decimal(scaled_px, self.price_scale)


def scaled_to_decimal(px: int, price_scale: int) -> Decimal:
    """Normalize an exchange scaled integer: px / price_scale."""
    if isinstance(px, bool) or not isinstance(px, int):
        raise ValueError(f"exchange price must be an integer, got {px!r}")
    if price_scale <= 0:
        raise ValueError("price_scale must be positive")
    return number(px) / number(price_scale)


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def parse_instrument(raw: dict) -> UsInstrument:
    """Parse a refdata/instruments entry into an authoritative UsInstrument.

    Expects keys matching the documented exchange reference data: symbol,
    tickSize, minimumTradeQty, priceScale, state, question, payoutValue,
    outcome_type, event_id, event_series, event_category, event_subcategory,
    event_start_time, startDate, expirationDate, terminationDate,
    long_participant_id/name, short_participant_id/name, instrument_rules.
    """
    if not raw.get("symbol"):
        raise ValueError("instrument requires symbol")
    tick = raw.get("tickSize")
    min_qty = raw.get("minimumTradeQty")
    scale = raw.get("priceScale")
    if tick is None or min_qty is None or scale is None:
        raise ValueError("instrument requires tickSize, minimumTradeQty, priceScale")
    return UsInstrument(
        symbol=str(raw["symbol"]),
        tick_size=number(tick),
        minimum_trade_qty=number(min_qty),
        price_scale=int(scale),
        state=UsMarketState.parse(raw.get("state", "OPEN")),
        question=raw.get("question"),
        payout_value=number(raw["payoutValue"]) if raw.get("payoutValue") is not None else None,
        outcome_type=raw.get("outcome_type"),
        event_id=str(raw["event_id"]) if raw.get("event_id") is not None else None,
        event_series=str(raw["event_series"]) if raw.get("event_series") is not None else None,
        event_category=raw.get("event_category"),
        event_subcategory=raw.get("event_subcategory"),
        event_start_time=_ts(raw.get("event_start_time")),
        start_date=_ts(raw.get("startDate")),
        expiration_date=_ts(raw.get("expirationDate")),
        termination_date=_ts(raw.get("terminationDate")),
        long_participant_id=str(raw["long_participant_id"]) if raw.get("long_participant_id") is not None else None,
        long_participant_name=raw.get("long_participant_name"),
        short_participant_id=str(raw["short_participant_id"]) if raw.get("short_participant_id") is not None else None,
        short_participant_name=raw.get("short_participant_name"),
        instrument_rules=raw.get("instrument_rules"),
    )


class UsInstrumentRegistry:
    """Registry keyed by exchange symbol; authoritative semantics lookup.

    The retail registry maps internal <-> slug <-> symbol. This registry holds
    the authoritative per-instrument values (tickSize, priceScale,
    minimumTradeQty) so book validation and scaled-price normalization use the
    true venue semantics rather than provisional defaults.
    """

    FALLBACK_TICK_SIZE = D("0.001")  # provisional only; never permanent truth

    def __init__(self) -> None:
        self._by_symbol: dict[str, UsInstrument] = {}

    def register(self, instrument: UsInstrument) -> None:
        if instrument.symbol in self._by_symbol:
            raise ValueError(f"duplicate instrument symbol: {instrument.symbol}")
        self._by_symbol[instrument.symbol] = instrument

    def by_symbol(self, symbol: str) -> UsInstrument | None:
        return self._by_symbol.get(symbol)

    def tick_size_for(self, symbol: str) -> Decimal | None:
        instrument = self.by_symbol(symbol)
        return instrument.tick_size if instrument else None

    def price_scale_for(self, symbol: str) -> int | None:
        instrument = self.by_symbol(symbol)
        return instrument.price_scale if instrument else None

    def min_qty_for(self, symbol: str) -> Decimal | None:
        instrument = self.by_symbol(symbol)
        return instrument.minimum_trade_qty if instrument else None

    def __len__(self) -> int:
        return len(self._by_symbol)