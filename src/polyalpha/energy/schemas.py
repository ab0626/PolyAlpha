"""Immutable point-in-time energy records.

The core discipline: ``X_t`` is the information actually available at ``t``, never
the latest corrected historical value. Energy data is revision-heavy (weather
forecast reruns, ERCOT preliminary->final LMPs, EIA weekly revisions), so every
record carries the full clock set and revision identity.

Fields are non-optional where their absence would invite silent lookahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..domain import utc


@dataclass(frozen=True)
class Clocks:
    """The five clocks every point-in-time observation must preserve."""

    source_event_time: datetime | None = None  # when the underlying thing happened
    published_at: datetime | None = None       # provider-claimed publish time
    first_seen_at: datetime | None = None      # first time this value entered our pipeline
    received_at: datetime | None = None        # local receipt (wall clock)
    processed_at: datetime | None = None       # local normalize/persist time


@dataclass(frozen=True)
class ForecastIdentity:
    """Identifies a specific model forecast vintage (revision-safe)."""

    forecast_run: datetime | None = None      # model initialization time
    forecast_valid_time: datetime | None = None
    forecast_horizon: int | None = None       # hours from run to valid time
    model: str | None = None
    revision_id: str | None = None
    supersedes_revision_id: str | None = None


@dataclass(frozen=True)
class EnergyUnderlying:
    """A physical energy outcome that prediction markets resolve against."""

    underlying_id: str
    taxonomy_leaf: str
    kind: str                       # e.g. "daily_peak_load", "natgas_spot", "wti_close"
    region: str | None = None
    resolution_source: str | None = None
    resolution_definition_hash: str | None = None
    operating_day: str | None = None        # physical operating day (local ISO date)
    observation_window_start: datetime | None = None
    observation_window_end: datetime | None = None
    market_timezone: str | None = None


@dataclass(frozen=True)
class EnergyMarketLink:
    """Provenance-bearing link from a physical underlying to a venue market."""

    underlying_id: str
    venue: str
    market_id: str                  # venue ticker / slug
    series_id: str | None = None
    threshold: Decimal | None = None
    direction: str | None = None    # "greater" | "less"
    strike_type: str | None = None
    settlement_time: datetime | None = None
    resolution_source: str | None = None
    mapping_method: str = "semantic_series"
    mapping_confidence: float = 1.0
    mapping_created_at: datetime | None = None


@dataclass(frozen=True)
class GridLoadObservation:
    """ERCOT/PJM/... load (or wind/solar/net-load) value for one interval.

    Power time is explicit: operating_date + UTC interval + local timezone, so
    DST's 23/25-hour days fail closed instead of mapping to the wrong hour.
    """

    iso: str
    variable: str                   # "load" | "wind" | "solar" | "net_load"
    value_mw: Decimal
    operating_date: str
    interval_start_utc: datetime
    interval_end_utc: datetime
    market_timezone: str
    utc_offset: str | None = None
    dst_fold: int | None = None
    preliminary_or_final: str | None = None  # "preliminary" | "final"
    clocks: Clocks | None = None
    forecast: ForecastIdentity | None = None


@dataclass(frozen=True)
class WeatherForecast:
    """One weather forecast variable at one location for one forecast vintage."""

    model: str
    model_run: datetime
    issued_at: datetime | None = None
    received_at: datetime | None = None
    valid_at: datetime | None = None
    forecast_hour: int | None = None
    location: str | None = None     # grid id / WFO / point key
    variable: str | None = None     # "temperature" | "wind_speed" | ...
    value: Decimal | None = None
    units: str | None = None
    ensemble_spread: Decimal | None = None
    revision_id: str | None = None
    supersedes_revision_id: str | None = None


@dataclass(frozen=True)
class EnergyRelease:
    """A scheduled energy release (EIA storage, petroleum status, ...)."""

    release_id: str
    name: str
    scheduled_at: datetime
    source: str | None = None
    published_at: datetime | None = None
    received_at: datetime | None = None
    processed_at: datetime | None = None
    revision: str | None = None
    value: Decimal | None = None
    units: str | None = None

    def __post_init__(self) -> None:
        utc(self.scheduled_at)