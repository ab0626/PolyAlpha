"""Energy taxonomy — semantic classification, never keyword matching.

A market is assigned to a taxonomy leaf via an explicit, provenance-bearing
mapping (``schemas.EnergyMarketLink``), never by fuzzy substring search over a
question title. Ambiguous mappings are excluded.
"""

from __future__ import annotations

# Top-level leaves.
POWER = "POWER"
NATURAL_GAS = "NATURAL_GAS"
CRUDE_OIL = "CRUDE_OIL"
REFINED_PRODUCTS = "REFINED_PRODUCTS"
GRID_RELIABILITY = "GRID_RELIABILITY"
ENERGY_RELEASES = "ENERGY_RELEASES"

# ISO/RTO identifiers.
ISO_ERCOT = "ERCOT"
ISO_PJM = "PJM"
ISO_MISO = "MISO"
ISO_SPP = "SPP"
ISO_CAISO = "CAISO"
ISO_NYISO = "NYISO"
ISO_ISONE = "ISO-NE"

KNOWN_ISOS = frozenset(
    {ISO_ERCOT, ISO_PJM, ISO_MISO, ISO_SPP, ISO_CAISO, ISO_NYISO, ISO_ISONE}
)


def power_leaf(iso: str) -> str:
    """Taxonomy leaf for a power ISO/RTO, e.g. ``POWER/ERCOT``."""
    return f"{POWER}/{iso}"


def ercot_peak_load_underlying_id(operating_date: str) -> str:
    """Canonical underlying id for the Kalshi ERCOT daily-peak-load ladder."""
    return f"ercot:daily-peak-load:{operating_date}"