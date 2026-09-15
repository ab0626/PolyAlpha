"""Public discovery and snapshot ingestion. Errors and exclusions are auditable."""

from datetime import UTC, datetime
from decimal import InvalidOperation
from urllib.error import HTTPError, URLError

from .parsing import parse_book, parse_market
from .quality import Filter
from .storage import Store
from .transport import Transport


class Collector:
    def __init__(self, transport: Transport, store: Store, quality: Filter):
        self.transport, self.store, self.quality = transport, store, quality

    def collect(self, page_size: int = 100, max_pages: int = 1) -> dict:
        if not 1 <= page_size <= 100 or max_pages < 1:
            raise ValueError("invalid pagination limits")
        stats = dict(pages=0, markets=0, books=0, rejected=0, errors=0, truncated=False)
        cursor = None
        cursors, seen = set(), set()
        for _ in range(max_pages):
            params = {"limit": page_size, "closed": "false"}
            if cursor:
                params["after_cursor"] = cursor
            raw, received = self.transport.get(
                "https://gamma-api.polymarket.com/markets/keyset", params
            )
            self.store.append("raw_markets_page", cursor or "first", received, raw)
            if not isinstance(raw, dict) or not isinstance(raw.get("markets"), list):
                raise ValueError("unexpected discovery response schema")
            stats["pages"] += 1
            for item in raw["markets"]:
                try:
                    market = parse_market(item, received)
                except (KeyError, ValueError, TypeError, InvalidOperation) as error:
                    self.store.append(
                        "parse_error",
                        "market",
                        received,
                        {"reason": str(error), "raw": item},
                    )
                    stats["errors"] += 1
                    continue
                self.store.append("market", market.market_id, received, item)
                if market.market_id in seen:
                    continue
                seen.add(market.market_id)
                stats["markets"] += 1
                reasons = self.quality.market_reasons(market, received)
                self.store.append(
                    "market_quality", market.market_id, received, {"reasons": reasons}
                )
                if reasons:
                    stats["rejected"] += 1
                    continue
                for token in (market.yes_token_id, market.no_token_id):
                    self.collect_book(token, market.condition_id, stats)
            cursor = raw.get("next_cursor")
            if not cursor:
                return stats
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("invalid or repeated discovery cursor")
            cursors.add(cursor)
        stats["truncated"] = bool(cursor)
        return stats

    def collect_book(self, token: str, condition_id: str, stats: dict) -> None:
        try:
            raw, received = self.transport.get(
                "https://clob.polymarket.com/book", {"token_id": token}
            )
            self.store.append("raw_book", token, received, raw)
            book = parse_book(raw, received, token)
            if book.condition_id != condition_id:
                raise ValueError("condition ID mismatch")
            self.store.append("book", token, received, raw, book.source_at)
            reasons = self.quality.book_reasons(book, received)
            self.store.append("book_quality", token, received, {"reasons": reasons})
            stats["books"] += 1
            if reasons:
                stats["rejected"] += 1
        except (
            HTTPError,
            URLError,
            TimeoutError,
            KeyError,
            ValueError,
            TypeError,
            InvalidOperation,
        ) as error:
            self.store.append(
                "book_error",
                token,
                datetime.now(UTC),
                {"error_type": type(error).__name__, "reason": str(error)},
            )
            stats["errors"] += 1
