"""US book reconciliation — gRPC / Exchange REST / Retail.

Part of the US adapter family. Compares canonical normalized books across the
three US data surfaces and answers FOUR separate questions, never one generic
"books match" boolean:

    1. Identity     — are we comparing the same instrument?
    2. Semantics    — same priceScale / tickSize / state?
    3. Structure    — same normalized price levels and quantities?
    4. Freshness    — are differences explainable by timestamp/transport skew?

Authority hierarchy:
    PRIMARY          Direct Exchange gRPC
                     Direct Exchange REST L2
    SECONDARY CROSS  Retail /book
                     Retail /bbo

CRITICAL: reconciliation compares CANONICAL normalized Books (Decimal levels),
never raw transport representations. gRPC int64 px, Exchange REST scaled px,
and Retail px.value all pass through adapter normalization to Decimal levels
BEFORE any comparison.

Reason codes and gate classification:
    MATCH, MATCH_WITHIN_TOLERANCE, {REST,GRPC,RETAIL}_LAG  → operational noise
    STATE_MISMATCH, PRICE_SCALE_MISMATCH, LEVEL_MISMATCH,
    UNKNOWN_SYMBOL, UNRESOLVED                               → burn-in hard failure
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from ..domain import Book
from .identifiers import UsIdentifierRegistry
from .states import UsMarketState

D = Decimal


class UsSourceKind(str, Enum):
    GRPC = "GRPC"
    EXCHANGE_REST = "EXCHANGE_REST"
    RETAIL = "RETAIL"


class ReconciliationReason(str, Enum):
    MATCH = "MATCH"
    MATCH_WITHIN_TOLERANCE = "MATCH_WITHIN_TOLERANCE"
    REST_LAG = "REST_LAG"
    GRPC_LAG = "GRPC_LAG"
    RETAIL_LAG = "RETAIL_LAG"
    STATE_MISMATCH = "STATE_MISMATCH"
    PRICE_SCALE_MISMATCH = "PRICE_SCALE_MISMATCH"
    LEVEL_MISMATCH = "LEVEL_MISMATCH"
    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"
    UNRESOLVED = "UNRESOLVED"

    def is_hard_failure(self) -> bool:
        """Unresolved semantic or book mismatches fail the burn-in gate."""
        return self in (
            ReconciliationReason.STATE_MISMATCH,
            ReconciliationReason.PRICE_SCALE_MISMATCH,
            ReconciliationReason.LEVEL_MISMATCH,
            ReconciliationReason.UNKNOWN_SYMBOL,
            ReconciliationReason.UNRESOLVED,
        )

    def is_operational_noise(self) -> bool:
        return not self.is_hard_failure()


@dataclass(frozen=True)
class SourceBook:
    """A canonical Book from one US surface, with its provenance."""

    source_kind: UsSourceKind
    symbol: str
    book: Book
    price_scale: int | None = None
    tick_size: Decimal | None = None
    state: UsMarketState | None = None
    transact_time: datetime | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    symbol: str
    primary: UsSourceKind
    cross: UsSourceKind
    reason: ReconciliationReason
    identity_ok: bool
    semantics_ok: bool
    structure_ok: bool
    freshness_ok: bool
    details: str = ""

    @property
    def is_hard_failure(self) -> bool:
        return self.reason.is_hard_failure()

    @property
    def is_operational_noise(self) -> bool:
        return self.reason.is_operational_noise()

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "primary": self.primary.value,
            "cross": self.cross.value,
            "reason": self.reason.value,
            "hard_failure": self.is_hard_failure,
            "identity_ok": self.identity_ok,
            "semantics_ok": self.semantics_ok,
            "structure_ok": self.structure_ok,
            "freshness_ok": self.freshness_ok,
            "details": self.details,
        }


def _levels_map(book: Book) -> dict[Decimal, Decimal]:
    """Canonical price -> qty map (Decimal keys/values)."""
    result: dict[Decimal, Decimal] = {}
    for level in list(book.bids) + list(book.asks):
        result[level.price] = result.get(level.price, D("0")) + level.size
    return result


def _level_differences(primary: Book, cross: Book) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    """Return (side, price, primary_qty, cross_qty) for differing levels."""
    diffs = []
    p_bids = _levels_map(primary)
    c_bids = _levels_map(cross)
    p_asks = {level.price: level.size for level in primary.asks}
    c_asks = {level.price: level.size for level in cross.asks}
    all_prices = set(p_bids) | set(c_bids) | set(p_asks) | set(c_asks)
    for price in all_prices:
        pb, cb = p_bids.get(price, D("0")), c_bids.get(price, D("0"))
        pa, ca = p_asks.get(price, D("0")), c_asks.get(price, D("0"))
        if pb != cb:
            diffs.append(("bid", price, pb, cb))
        if pa != ca:
            diffs.append(("ask", price, pa, ca))
    return diffs


def reconcile(
    primary: SourceBook,
    cross: SourceBook,
    identifiers: UsIdentifierRegistry,
    lag_threshold_seconds: float = 5.0,
    qty_tolerance: Decimal = D("0.01"),
) -> ReconciliationResult:
    """Reconcile a primary book against a cross-check book.

    Answers the four questions in order; the most specific reason wins.
    """
    symbol = primary.symbol if primary.symbol == cross.symbol else f"{primary.symbol}|{cross.symbol}"

    # ── 1. Identity ─────────────────────────────────────────────────────────
    p_ident = identifiers.by_symbol(primary.symbol)
    c_ident = identifiers.by_symbol(cross.symbol)
    if p_ident is None or c_ident is None:
        return ReconciliationResult(
            symbol=symbol, primary=primary.source_kind, cross=cross.source_kind,
            reason=ReconciliationReason.UNKNOWN_SYMBOL,
            identity_ok=False, semantics_ok=False, structure_ok=False, freshness_ok=False,
            details="symbol not in identifier registry",
        )
    if p_ident.internal_market_id != c_ident.internal_market_id:
        return ReconciliationResult(
            symbol=symbol, primary=primary.source_kind, cross=cross.source_kind,
            reason=ReconciliationReason.UNRESOLVED,
            identity_ok=False, semantics_ok=False, structure_ok=False, freshness_ok=False,
            details="primary and cross resolve to different internal market ids",
        )

    # ── 2. Semantics ────────────────────────────────────────────────────────
    if (
        primary.price_scale is not None
        and cross.price_scale is not None
        and primary.price_scale != cross.price_scale
    ):
        return ReconciliationResult(
            symbol=symbol, primary=primary.source_kind, cross=cross.source_kind,
            reason=ReconciliationReason.PRICE_SCALE_MISMATCH,
            identity_ok=True, semantics_ok=False, structure_ok=False, freshness_ok=False,
            details=f"priceScale {primary.price_scale} != {cross.price_scale}",
        )
    if (
        primary.state is not None
        and cross.state is not None
        and primary.state != cross.state
    ):
        return ReconciliationResult(
            symbol=symbol, primary=primary.source_kind, cross=cross.source_kind,
            reason=ReconciliationReason.STATE_MISMATCH,
            identity_ok=True, semantics_ok=False, structure_ok=False, freshness_ok=False,
            details=f"state {primary.state.value} != {cross.state.value}",
        )
    if (
        primary.tick_size is not None
        and cross.tick_size is not None
        and primary.tick_size != cross.tick_size
    ):
        return ReconciliationResult(
            symbol=symbol, primary=primary.source_kind, cross=cross.source_kind,
            reason=ReconciliationReason.PRICE_SCALE_MISMATCH,
            identity_ok=True, semantics_ok=False, structure_ok=False, freshness_ok=False,
            details=f"tickSize {primary.tick_size} != {cross.tick_size}",
        )

    # ── 3. Structure ────────────────────────────────────────────────────────
    diffs = _level_differences(primary.book, cross.book)
    structure_ok = len(diffs) == 0
    if structure_ok:
        return ReconciliationResult(
            symbol=symbol, primary=primary.source_kind, cross=cross.source_kind,
            reason=ReconciliationReason.MATCH,
            identity_ok=True, semantics_ok=True, structure_ok=True, freshness_ok=True,
        )

    # Small qty differences within tolerance → MATCH_WITHIN_TOLERANCE.
    max_diff = max(abs(p - c) for _, _, p, c in diffs) if diffs else D("0")
    if max_diff <= qty_tolerance:
        return ReconciliationResult(
            symbol=symbol, primary=primary.source_kind, cross=cross.source_kind,
            reason=ReconciliationReason.MATCH_WITHIN_TOLERANCE,
            identity_ok=True, semantics_ok=True, structure_ok=True, freshness_ok=True,
            details=f"qty diff {max_diff} within tolerance",
        )

    # ── 4. Freshness ────────────────────────────────────────────────────────
    # A structural difference explainable by timestamp/transport skew is a
    # LAG reason (operational noise), not a hard failure.
    if (
        primary.transact_time is not None
        and cross.transact_time is not None
    ):
        skew = abs((primary.transact_time - cross.transact_time).total_seconds())
        if skew > lag_threshold_seconds:
            older = primary if primary.transact_time < cross.transact_time else cross
            reason = {
                UsSourceKind.GRPC: ReconciliationReason.GRPC_LAG,
                UsSourceKind.EXCHANGE_REST: ReconciliationReason.REST_LAG,
                UsSourceKind.RETAIL: ReconciliationReason.RETAIL_LAG,
            }[older.source_kind]
            return ReconciliationResult(
                symbol=symbol, primary=primary.source_kind, cross=cross.source_kind,
                reason=reason,
                identity_ok=True, semantics_ok=True, structure_ok=False, freshness_ok=False,
                details=f"older source {older.source_kind.value} lags by {skew:.1f}s",
            )

    return ReconciliationResult(
        symbol=symbol, primary=primary.source_kind, cross=cross.source_kind,
        reason=ReconciliationReason.LEVEL_MISMATCH,
        identity_ok=True, semantics_ok=True, structure_ok=False, freshness_ok=False,
        details=f"{len(diffs)} level(s) differ",
    )


class UsReconciliationReport:
    """Aggregates pairwise reconciliations; gate-friendly summary."""

    def __init__(self) -> None:
        self.results: list[ReconciliationResult] = []

    def add(self, result: ReconciliationResult) -> None:
        self.results.append(result)

    @property
    def hard_failures(self) -> list[ReconciliationResult]:
        return [r for r in self.results if r.is_hard_failure]

    @property
    def operational_noise(self) -> list[ReconciliationResult]:
        return [r for r in self.results if r.is_operational_noise]

    def summary(self) -> dict:
        by_reason: dict[str, int] = {}
        for r in self.results:
            by_reason[r.reason.value] = by_reason.get(r.reason.value, 0) + 1
        return {
            "total": len(self.results),
            "hard_failures": len(self.hard_failures),
            "operational_noise": len(self.operational_noise),
            "by_reason": by_reason,
        }