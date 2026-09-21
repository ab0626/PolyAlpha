"""Fill-vs-quote race measurement (aggressive execution before quote revision).

Directly operationalizes the Kalshi finding that aggressive executions occur
before quote revision in ~1/6 of news reactions: an aggressor fills against a
*stale* quote, and only afterward does the book revise in the aggressor's
direction. This is the TOCTOU / stale-quote signal, and it is measurable from
the two streams PolyAlpha already collects (books = quote revisions, fills =
aggressive executions), joined on venue timestamps.

Pure functions, offline-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .fills import FillRecord

D = Decimal


@dataclass(frozen=True)
class Quote:
    """A top-of-book snapshot for one market."""

    market_slug: str
    timestamp: datetime
    best_bid: Decimal
    best_ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.best_bid + self.best_ask) / D(2)


@dataclass(frozen=True)
class RaceResult:
    total_fills: int
    race_fills: int
    stale_fills: int
    fraction: float

    def summary(self) -> dict:
        return {
            "total_fills": self.total_fills,
            "race_fills": self.race_fills,
            "stale_fills": self.stale_fills,
            "fraction": round(self.fraction, 4),
        }


def _executable_quote(quote: Quote, side: str) -> Decimal:
    return quote.best_ask if side == "BUY" else quote.best_bid


def measure_quote_race(
    fills: list[FillRecord],
    quotes: list[Quote],
    stale_eps: Decimal = D("0.005"),
    move_min: Decimal = D("0.001"),
) -> RaceResult:
    """Classify each fill as a "before-revision" race against a stale quote.

    A fill is a race when: (1) it printed at (approximately) the pre-fill
    executable quote, and (2) the very next quote revised *in the fill's
    direction*, i.e. the aggressor captured the stale price before the book
    repriced. Returns aggregate counts + fraction.
    """
    by_market: dict[str, list[Quote]] = {}
    for q in quotes:
        by_market.setdefault(q.market_slug, []).append(q)
    for qs in by_market.values():
        qs.sort(key=lambda q: q.timestamp)

    total = 0
    race = 0
    stale = 0
    for f in fills:
        qs = by_market.get(f.market_slug)
        if not qs:
            continue
        # Last quote at or before the fill, and first quote strictly after.
        pre = None
        post = None
        for q in qs:
            if q.timestamp <= f.trade_time:
                pre = q
            else:
                post = q
                break
        if pre is None or post is None:
            continue

        exec_q = _executable_quote(pre, f.aggressor_side)
        post_q = _executable_quote(post, f.aggressor_side)
        is_stale = abs(f.price - exec_q) <= stale_eps
        if f.aggressor_side == "BUY":
            moved = post_q - pre.best_ask >= move_min  # ask revised up after the buy
        else:
            moved = pre.best_bid - post_q >= move_min  # bid revised down after the sell

        total += 1
        if is_stale:
            stale += 1
        if is_stale and moved:
            race += 1

    return RaceResult(
        total_fills=total,
        race_fills=race,
        stale_fills=stale,
        fraction=(race / total) if total else 0.0,
    )
