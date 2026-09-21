"""Venue model: hard-coded US-executable / International-research split.

Polymarket US and Kalshi are US-accessible, executable venues. Polymarket
International (Gamma/CLOB, docs.polymarket.com) is research-data only: its
collector is never an executable path. This distinction is a code invariant,
never operator convention.
"""

from enum import Enum


class Venue(str, Enum):
    POLYMARKET_US = "polymarket_us"
    KALSHI = "kalshi"
    POLYMARKET_INTERNATIONAL = "polymarket_international"


EXECUTABLE_VENUES = frozenset({Venue.POLYMARKET_US, Venue.KALSHI})
RESEARCH_ONLY_VENUES = frozenset({Venue.POLYMARKET_INTERNATIONAL})


def is_executable(venue: Venue) -> bool:
    """True only for venues where orders may be transmitted (still gated)."""
    return venue in EXECUTABLE_VENUES


def is_research_only(venue: Venue) -> bool:
    """True for data-only venues whose adapter must never execute."""
    return venue in RESEARCH_ONLY_VENUES
