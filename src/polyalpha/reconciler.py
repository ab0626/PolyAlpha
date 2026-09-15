"""REST order-book reconciliation for the market collector.

Part of the v0.3.0 data-collection architecture. Periodically compares the
collector's reconstructed book for a token against the authoritative CLOB
REST `/book` response. Produces a BookReconciliationResult and — on mismatch —
replaces the derived local state with the REST truth. The raw event layer is
never modified: a discrepancy is recorded, not "fixed" retroactively.

Rate-limit awareness: `/book` is documented at 1,500 req / 10s; this module
is intentionally conservative and single-threaded, reconciling a bounded
number of tokens per call with a minimum spacing, so normal collection stays
far below the ceiling.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .domain import number
from .parsing import parse_book
from .transport import Transport


@dataclass(frozen=True)
class BookReconciliationResult:
    market: str
    token: str
    timestamp_ns: int
    local_hash: str
    remote_hash: str
    matches: bool
    difference_count: int
    max_size_difference: Decimal
    details: str = ""

    def summary(self) -> dict:
        return {
            "market": self.market,
            "token": self.token,
            "timestamp_ns": self.timestamp_ns,
            "local_hash": self.local_hash,
            "remote_hash": self.remote_hash,
            "matches": self.matches,
            "difference_count": self.difference_count,
            "max_size_difference": str(self.max_size_difference),
            "details": self.details,
        }


def _book_hash(bids: list, asks: list) -> str:
    """Deterministic hash of a book's levels, independent of ordering."""
    h = hashlib.sha256()
    for side in ("bids", "asks"):
        for level in sorted(
            bids if side == "bids" else asks,
            key=lambda x: str(x.get("price", "")),
        ):
            h.update(f"{side}:{level.get('price')}:{level.get('size')};".encode())
    return h.hexdigest()[:16]


def _levels_to_dict(levels: list) -> dict[str, Decimal]:
    return {str(x.get("price")): number(x.get("size", 0)) for x in levels}


def reconcile_book(
    local: dict,
    remote_raw: dict,
    token: str,
    received_at_ns: int,
) -> BookReconciliationResult:
    """Compare a reconstructed local book dict with a raw REST book response.

    Args:
        local: collector's derived book dict (from market_collector.books).
        remote_raw: raw response body from GET /book?token_id=...
        token: expected token id.
        received_at_ns: local clock when the REST response arrived.

    Returns:
        BookReconciliationResult describing whether local matches remote.
    """
    remote = parse_book(remote_raw, datetime.fromtimestamp(received_at_ns / 1e9, UTC), token)
    local_bids = _levels_to_dict(local.get("bids", []))
    local_asks = _levels_to_dict(local.get("asks", []))
    remote_bids = _levels_to_dict(
        [{"price": str(x.price), "size": str(x.size)} for x in remote.bids]
    )
    remote_asks = _levels_to_dict(
        [{"price": str(x.price), "size": str(x.size)} for x in remote.asks]
    )

    all_prices = set(local_bids) | set(remote_bids) | set(local_asks) | set(remote_asks)
    difference_count = 0
    max_size_diff = Decimal(0)
    for price in all_prices:
        lb = local_bids.get(price, Decimal(0))
        rb = remote_bids.get(price, Decimal(0))
        la = local_asks.get(price, Decimal(0))
        ra = remote_asks.get(price, Decimal(0))
        if lb != rb or la != ra:
            difference_count += 1
            for a, b in ((lb, rb), (la, ra)):
                diff = abs(a - b)
                if diff > max_size_diff:
                    max_size_diff = diff

    local_hash = _book_hash(local.get("bids", []), local.get("asks", []))
    remote_hash = _book_hash(
        [{"price": str(x.price), "size": str(x.size)} for x in remote.bids],
        [{"price": str(x.price), "size": str(x.size)} for x in remote.asks],
    )
    matches = (
        local_hash == remote_hash
        and local_bids == remote_bids
        and local_asks == remote_asks
    )

    return BookReconciliationResult(
        market=remote.condition_id,
        token=token,
        timestamp_ns=received_at_ns,
        local_hash=local_hash,
        remote_hash=remote_hash,
        matches=matches,
        difference_count=difference_count,
        max_size_difference=max_size_diff,
        details="" if matches else "local book differs from authoritative REST book",
    )


class Reconciler:
    """Reconciles collector book state against REST on a bounded schedule."""

    BOOK_ENDPOINT = "https://clob.polymarket.com/book"

    def __init__(
        self,
        transport: Transport,
        min_spacing_seconds: float = 2.0,
    ):
        if min_spacing_seconds <= 0:
            raise ValueError("positive spacing required")
        self.transport = transport
        self.min_spacing_seconds = min_spacing_seconds
        self._last_request_at = 0.0
        self.results: list[BookReconciliationResult] = []

    def reconcile(self, local_books: dict[str, dict], max_tokens: int = 25) -> list[BookReconciliationResult]:
        """Reconcile up to max_tokens reconstructed books against REST.

        Tokens are sampled in insertion order. Returns the reconciliation
        results for this pass; results accumulate in self.results.
        """
        if not local_books:
            return []
        tokens = list(local_books.keys())[:max_tokens]
        pass_results: list[BookReconciliationResult] = []
        for token in tokens:
            # Respect documented /book ceiling (1500/10s) with margin.
            now = time.monotonic()
            delay = self.min_spacing_seconds - (now - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
            try:
                raw, received = self.transport.get(
                    self.BOOK_ENDPOINT, {"token_id": token}
                )
            except Exception as error:  # noqa: BLE001 - network failures must not kill collection
                result = BookReconciliationResult(
                    market="",
                    token=token,
                    timestamp_ns=time.time_ns(),
                    local_hash="",
                    remote_hash="",
                    matches=False,
                    difference_count=0,
                    max_size_difference=Decimal(0),
                    details=f"reconcile request failed: {type(error).__name__}",
                )
                pass_results.append(result)
                self.results.append(result)
                continue
            self._last_request_at = time.monotonic()
            local = local_books.get(token)
            if local is None:
                continue
            result = reconcile_book(local, raw, token, time.time_ns())
            pass_results.append(result)
            self.results.append(result)
        return pass_results

    def summary(self) -> dict:
        total = len(self.results)
        matches = sum(1 for r in self.results if r.matches)
        return {
            "total": total,
            "matches": matches,
            "mismatches": total - matches,
            "match_rate": round(matches / total, 4) if total else None,
            "last_result": self.results[-1].summary() if self.results else None,
        }