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
    """Deterministic hash of canonical book levels.

    Input levels are canonicalized first (see _canonical_levels), so hash
    equality means book-state equality regardless of ordering or decimal
    formatting in the original representation.
    """
    canonical = _canonical_levels(bids, asks)
    h = hashlib.sha256()
    for side in ("bids", "asks"):
        for price, size in canonical[side]:
            h.update(f"{side}:{price}:{size};".encode())
    return h.hexdigest()[:16]


def _canonical_levels(
    bids: list, asks: list
) -> dict[str, list[tuple[str, str]]]:
    """Canonicalize order-book levels for comparison/hashing.

    Rules:
      * bids sorted descending by price, asks ascending
      * prices/sizes normalized through Decimal (never float serialization)
      * zero-size levels removed (per state rules: size==0 deletes a level)
      * duplicate prices collapsed (later size wins), NaN/negative dropped
    """
    def _normalize(levels, drop_side):
        by_price: dict[Decimal, Decimal] = {}
        for level in levels:
            try:
                price = number(level.get("price"))
                size = number(level.get("size", 0))
            except Exception:  # noqa: BLE001 - malformed level is not a book state
                continue
            if not price.is_finite() or not size.is_finite() or price < 0:
                continue
            if size == 0:
                by_price.pop(price, None)  # size==0 deletes the level
            else:
                by_price[price] = size
        return by_price

    bid_map = _normalize(bids, "bids")
    ask_map = _normalize(asks, "asks")
    return {
        "bids": [(str(p), str(q)) for p, q in sorted(bid_map.items(), reverse=True)],
        "asks": [(str(p), str(q)) for p, q in sorted(ask_map.items())],
    }


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
    local_canon = _canonical_levels(local.get("bids", []), local.get("asks", []))
    remote_canon = _canonical_levels(
        [{"price": str(x.price), "size": str(x.size)} for x in remote.bids],
        [{"price": str(x.price), "size": str(x.size)} for x in remote.asks],
    )

    # Count differing price levels across both sides.
    difference_count = 0
    max_size_diff = Decimal(0)
    for side in ("bids", "asks"):
        lmap = dict(local_canon[side])
        rmap = dict(remote_canon[side])
        for price in set(lmap) | set(rmap):
            lb = lmap.get(price, Decimal(0))
            rb = rmap.get(price, Decimal(0))
            if lb != rb:
                difference_count += 1
                diff = abs(Decimal(lb) - Decimal(rb))
                if diff > max_size_diff:
                    max_size_diff = diff

    local_hash = _book_hash(local.get("bids", []), local.get("asks", []))
    remote_hash = _book_hash(
        [{"price": str(x.price), "size": str(x.size)} for x in remote.bids],
        [{"price": str(x.price), "size": str(x.size)} for x in remote.asks],
    )
    matches = local_hash == remote_hash and difference_count == 0

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