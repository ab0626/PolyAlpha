"""US raw collector — writes exact US retail payloads to the immutable RawStore.

Part of the US adapter family. US has its own raw lineage (data/us/...) and
its own source labels; it does NOT reuse International manifests or
REAL_DATA_START. This collector only gathers data; it never trades.

Wire fidelity: the exact JSON text received from gateway.polymarket.us is
preserved verbatim via RawStore's wire field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..rawstore import RawStore
from .identifiers import UsIdentifierRegistry
from .rest import PublicUsClient

SOURCE_US_RETAIL_MARKETS = "polymarket_us_retail_markets"
SOURCE_US_RETAIL_BOOK = "polymarket_us_retail_book"
SOURCE_US_RETAIL_BBO = "polymarket_us_retail_bbo"
SOURCE_US_RETAIL_EVENTS = "polymarket_us_retail_events"
SOURCE_US_RETAIL_PRICE_HISTORY = "polymarket_us_retail_price_history"
SOURCE_US_RETAIL_SETTLEMENT = "polymarket_us_retail_settlement"


@dataclass
class UsCollectStats:
    markets: int = 0
    books: int = 0
    bbos: int = 0
    events: int = 0
    price_history: int = 0
    settlements: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return {
            "markets": self.markets,
            "books": self.books,
            "bbos": self.bbos,
            "events": self.events,
            "price_history": self.price_history,
            "settlements": self.settlements,
            "errors": self.errors,
        }


class UsRawCollector:
    """Collect US retail data into the raw store. Venue-specific lineage."""

    def __init__(
        self,
        client: PublicUsClient,
        raw: RawStore,
        registry: UsIdentifierRegistry,
        collector_version: str = "unknown",
    ):
        self.client = client
        self.raw = raw
        self.registry = registry
        self.collector_version = collector_version
        self.stats = UsCollectStats()

    def _capture(self, source: str, connection_id: str, payload: Any, wire: str | None) -> None:
        self.raw.append(
            source=source,
            connection_id=connection_id,
            payload=payload,
            wire=wire,
        )

    def discover_markets(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """Fetch /v1/markets, register identifiers, and capture raw."""
        raw_payload, received = self.client.markets({"limit": limit, "offset": offset})
        self._capture(SOURCE_US_RETAIL_MARKETS, "discovery", raw_payload, None)
        markets = raw_payload.get("markets", [])
        for market in markets:
            slug = market.get("slug")
            internal = f"us:{slug}" if slug else None
            if slug and internal and self.registry.by_slug(slug) is None:
                self.registry.register(internal, slug)
            self.stats.markets += 1
        return markets

    def collect_books(self, slugs: list[str], tick_size: Any = None, min_size: Any = None) -> None:
        from .adapter import parse_book

        for slug in slugs:
            identifier = self.registry.by_slug(slug)
            if identifier is None:
                self.stats.errors += 1
                continue
            try:
                raw_payload, received = self.client.market_book(slug)
                self._capture(SOURCE_US_RETAIL_BOOK, slug, raw_payload, None)
                parse_book(raw_payload, received, identifier, tick_size, min_size)
                self.stats.books += 1
            except Exception as error:  # noqa: BLE001
                self.stats.errors += 1
                self.raw.append(
                    SOURCE_US_RETAIL_BOOK,
                    slug,
                    {"error_type": type(error).__name__, "reason": str(error)},
                )

    def collect_events(self, params: dict | None = None) -> list[dict]:
        raw_payload, received = self.client.events(params)
        self._capture(SOURCE_US_RETAIL_EVENTS, "events", raw_payload, None)
        events = raw_payload.get("events", [])
        for event in events:
            for market in event.get("markets", []):
                slug = market.get("slug")
                internal = f"us:{slug}" if slug else None
                if slug and internal and self.registry.by_slug(slug) is None:
                    self.registry.register(internal, slug)
        self.stats.events += len(events)
        return events