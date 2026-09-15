"""External observations carry public source, publication and retrieval provenance."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ExternalFact:
    event_id: str
    source_url: str
    published_at: datetime
    retrieved_at: datetime
    features: dict[str, float]

    def __post_init__(self):
        import math

        from .domain import utc

        utc(self.published_at)
        utc(self.retrieved_at)
        if (
            not self.event_id
            or not self.source_url
            or any(not math.isfinite(x) for x in self.features.values())
        ):
            raise ValueError("invalid external provenance/features")


class ExternalSource(Protocol):
    def available(self, event_id: str, at: datetime) -> list[ExternalFact]: ...


class FixtureSource:
    """Explicit injected fixtures, never an invented live API."""

    def __init__(self, facts):
        self.facts = tuple(facts)

    def available(self, event_id, at):
        return [
            f
            for f in self.facts
            if f.event_id == event_id and max(f.published_at, f.retrieved_at) <= at
        ]
