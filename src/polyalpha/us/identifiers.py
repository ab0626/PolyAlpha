"""Canonical identifier mapping for Polymarket US.

Part of the US adapter family. Internal IDs must be venue-independent:

    internal_market_id
        ↕
    us_market_slug      (retail / gateway REST + WS)
        ↕
    us_exchange_symbol  (direct Exchange REST/gRPC)

Plus internal long/short side IDs synthesized from the internal market id, so
the canonical Market domain object (which requires yes_token_id / no_token_id)
never sees venue-specific identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UsIdentifier:
    """One US market's three-way identity."""

    internal_market_id: str
    us_market_slug: str
    us_exchange_symbol: str | None = None

    @property
    def long_side_id(self) -> str:
        return f"{self.internal_market_id}:LONG"

    @property
    def short_side_id(self) -> str:
        return f"{self.internal_market_id}:SHORT"

    @property
    def condition_id(self) -> str:
        return f"us:{self.internal_market_id}"


class UsIdentifierRegistry:
    """Bidirectional registry: internal <-> slug <-> symbol."""

    def __init__(self) -> None:
        self._by_internal: dict[str, UsIdentifier] = {}
        self._by_slug: dict[str, UsIdentifier] = {}
        self._by_symbol: dict[str, UsIdentifier] = {}

    def register(
        self,
        internal_market_id: str,
        us_market_slug: str,
        us_exchange_symbol: str | None = None,
    ) -> UsIdentifier:
        if not internal_market_id or not us_market_slug:
            raise ValueError("internal market id and slug are required")
        identifier = UsIdentifier(
            internal_market_id=internal_market_id,
            us_market_slug=us_market_slug,
            us_exchange_symbol=us_exchange_symbol,
        )
        if internal_market_id in self._by_internal:
            raise ValueError(f"duplicate internal market id: {internal_market_id}")
        if us_market_slug in self._by_slug:
            raise ValueError(f"duplicate slug: {us_market_slug}")
        if us_exchange_symbol and us_exchange_symbol in self._by_symbol:
            raise ValueError(f"duplicate symbol: {us_exchange_symbol}")
        self._by_internal[internal_market_id] = identifier
        self._by_slug[us_market_slug] = identifier
        if us_exchange_symbol:
            self._by_symbol[us_exchange_symbol] = identifier
        return identifier

    def by_internal(self, internal_market_id: str) -> UsIdentifier | None:
        return self._by_internal.get(internal_market_id)

    def by_slug(self, slug: str) -> UsIdentifier | None:
        return self._by_slug.get(slug)

    def by_symbol(self, symbol: str) -> UsIdentifier | None:
        return self._by_symbol.get(symbol)

    def __len__(self) -> int:
        return len(self._by_internal)

    def symbols(self) -> list[str]:
        return sorted(self._by_symbol)

    def slugs(self) -> list[str]:
        return sorted(self._by_slug)