"""Measure a market's reaction to a scheduled gov release (family A10).

Joins a GovRelease's ground-truth t_0 to the two collected streams: the book
(quote revisions) and the fill tape (aggressive executions). Returns the
Information Incorporation Function R(h) (polyalpha.information) and the
fill-vs-quote race fraction (polyalpha.us.quote_race).
"""

from __future__ import annotations

from ..gov_releases import GovRelease
from ..information import incorporation_curve
from .quote_race import Quote, measure_quote_race

DEFAULT_HORIZONS = (5, 10, 30, 60, 300)


def measure_reaction(
    release: GovRelease,
    quotes: list[Quote],
    fills: list,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    stable_seconds: int = 1800,
) -> dict:
    """Compute R(h) and the fill-vs-quote race around the release time.

    ``quotes`` are (timestamp, best_bid, best_ask) snapshots for the relevant
    market; ``fills`` are FillRecords for the same market. Both should span
    [t_0 - pre, t_0 + stable].
    """
    mid_series = [(q.timestamp, q.mid) for q in quotes]
    curve = incorporation_curve(
        mid_series, release.scheduled_at, list(horizons), stable_seconds
    )
    race = measure_quote_race(fills, quotes)
    return {
        "event_id": release.event_id,
        "name": release.name,
        "scheduled_at": release.scheduled_at.isoformat(),
        "R": {h: (round(curve[h], 4) if curve[h] is not None else None) for h in horizons},
        "race": race.summary(),
    }
