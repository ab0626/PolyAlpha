"""shadow-external-v0: observation-only external information bus.

Collects point-in-time external data (macro, energy, weather, regulatory,
government, equity proxies, news) into a SEPARATE raw lineage. Observation-only:
nothing here feeds EventShock, Z_lag, propagation, market selection, or any
frozen v0.4 methodology. Reads_v0.4(shadow_external) must be 0.
"""

from .core import (
    AlertValue,
    Coverage,
    ExternalCollector,
    ExternalObservation,
    ExternalValue,
    FilingValue,
    Freshness,
    MarketValue,
    ScalarValue,
    SourceClass,
    TextValue,
    observation_id,
    revision_key,
    value_hash,
)

__all__ = [
    "AlertValue",
    "Coverage",
    "ExternalCollector",
    "ExternalObservation",
    "ExternalValue",
    "FilingValue",
    "Freshness",
    "MarketValue",
    "ScalarValue",
    "SourceClass",
    "TextValue",
    "observation_id",
    "revision_key",
    "value_hash",
]
