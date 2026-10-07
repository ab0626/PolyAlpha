"""PolyAlpha energy namespace — alpha-first ERCOT + gas wedge.

Isolated from the frozen Polymarket-US forward experiment: this namespace has its
own raw lineage, dataset builder, market mappings, research agenda, and forward
evidence. It must not read or mutate ``polyalpha.us`` research state.

The first wedge:

    HRRR weather forecast revisions + ERCOT load information
        -> P(ERCOT daily peak load > K)
        -> Kalshi threshold-ladder mispricing.
"""

from . import kalshi, schemas, taxonomy

__all__ = ["kalshi", "schemas", "taxonomy"]