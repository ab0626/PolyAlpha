"""US market state model — faithful to the exchange lifecycle.

The canonical International Market.active: bool is too weak for Polymarket US.
Preserve the full lifecycle and derive helper predicates from it.

Retail enum:      MARKET_STATE_OPEN, PREOPEN, SUSPENDED, HALTED, EXPIRED,
                  TERMINATED, MATCH_AND_CLOSE_AUCTION
Exchange states:  PENDING, OPEN, CLOSED, EXPIRED, TERMINATED,
                  SUSPENDED, HALTED, PREOPEN, MATCH_AND_CLOSE_AUCTION
"""

from __future__ import annotations

from enum import Enum


class UsMarketState(str, Enum):
    PENDING = "PENDING"
    PREOPEN = "PREOPEN"
    OPEN = "OPEN"
    SUSPENDED = "SUSPENDED"
    HALTED = "HALTED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    TERMINATED = "TERMINATED"
    MATCH_AND_CLOSE_AUCTION = "MATCH_AND_CLOSE_AUCTION"

    @classmethod
    def parse(cls, raw: str) -> "UsMarketState":
        """Parse both retail (MARKET_STATE_*) and exchange (bare) spellings."""
        value = raw.strip().upper()
        if value.startswith("MARKET_STATE_"):
            value = value[len("MARKET_STATE_"):]
        if value.startswith("STATE_"):
            value = value[len("STATE_"):]
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"unknown US market state: {raw}")

    # ── Predicates ────────────────────────────────────────────────────────

    def is_pre_trade(self) -> bool:
        return self in (UsMarketState.PENDING, UsMarketState.PREOPEN)

    def is_tradable(self) -> bool:
        """Orders may rest/match while OPEN or in the closing auction."""
        return self in (UsMarketState.OPEN, UsMarketState.MATCH_AND_CLOSE_AUCTION)

    def is_trading_paused(self) -> bool:
        return self in (UsMarketState.SUSPENDED, UsMarketState.HALTED)

    def is_resolved(self) -> bool:
        return self in (UsMarketState.EXPIRED, UsMarketState.TERMINATED)

    def is_closed(self) -> bool:
        return self in (
            UsMarketState.CLOSED,
            UsMarketState.EXPIRED,
            UsMarketState.TERMINATED,
        )

    def is_active(self) -> bool:
        """Maps loosely to the canonical active flag: pre-trade or tradable."""
        return self.is_pre_trade() or self.is_tradable()

    def accepts_orders(self) -> bool:
        return self.is_tradable()

    def order_book_enabled(self) -> bool:
        return self.is_pre_trade() or self.is_tradable()

    def is_final(self) -> bool:
        return self in (
            UsMarketState.EXPIRED,
            UsMarketState.TERMINATED,
            UsMarketState.CLOSED,
        )