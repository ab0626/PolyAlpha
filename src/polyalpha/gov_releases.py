"""Scheduled US macro/government release source (authoritative timestamps).

A scheduled release has a *known-in-advance* public time t_0 (BLS CPI/NFP at
08:30 ET, FOMC statement at 14:00 ET), so the information-incorporation clock
can be measured against a ground-truth event time rather than an inferred one.
This is the cleanest input for the reaction curve R(h) (polyalpha.information)
and the fill-vs-quote race (polyalpha.us.quote_race).

Implements the ExternalSource protocol: a fact becomes available only at/after
``scheduled_at``. ``source_authority=1.0`` and ``primary_source=True`` mark the
official first publication, so a wire copy (Reuters) can never masquerade as
the primary event.

Limitation (noted, not hidden): ``scheduled_at`` is the *scheduled* time; a
delayed release shifts the true t_0. A delayed release manifests as "no price
reaction at t_0" in the measurement, which is itself detectable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .domain import utc
from .external import ExternalFact


@dataclass(frozen=True)
class GovRelease:
    event_id: str
    name: str
    scheduled_at: datetime
    category: str
    source_url: str
    related_market_slugs: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()

    def __post_init__(self):
        utc(self.scheduled_at)

    def to_external_fact(self, retrieved_at: datetime) -> ExternalFact:
        """The release as a primary, authoritative ExternalFact (empty features:
        the released value is enriched separately, post-release)."""
        return ExternalFact(
            event_id=self.event_id,
            source_url=self.source_url,
            published_at=self.scheduled_at,
            retrieved_at=retrieved_at,
            features={},
            source_authority=1.0,
            primary_source=True,
        )


class GovReleaseSource:
    """A calendar of scheduled releases, queryable by time and by id."""

    def __init__(self, releases: list[GovRelease]):
        self._releases = tuple(sorted(releases, key=lambda r: r.scheduled_at))
        self._by_id = {r.event_id: r for r in self._releases}

    def by_id(self, event_id: str) -> GovRelease | None:
        return self._by_id.get(event_id)

    def releases_between(self, start: datetime, end: datetime) -> list[GovRelease]:
        return [r for r in self._releases if start <= r.scheduled_at < end]

    def upcoming(self, at: datetime) -> list[GovRelease]:
        return [r for r in self._releases if r.scheduled_at > at]

    def available(self, event_id: str, at: datetime) -> list[ExternalFact]:
        """ExternalSource protocol: the release is public only at/after t_0."""
        release = self._by_id.get(event_id)
        if release is None or at < release.scheduled_at:
            return []
        return [release.to_external_fact(retrieved_at=at)]


def load_gov_releases(path: str | Path) -> GovReleaseSource:
    """Load a schedule from JSON: {"releases": [{event_id, name, scheduled_at,
    category, source_url, related_market_slugs, search_terms}, ...]}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    releases = [
        GovRelease(
            event_id=r["event_id"],
            name=r["name"],
            scheduled_at=datetime.fromisoformat(r["scheduled_at"]),
            category=r.get("category", "macro"),
            source_url=r.get("source_url", ""),
            related_market_slugs=tuple(r.get("related_market_slugs", [])),
            search_terms=tuple(r.get("search_terms", [])),
        )
        for r in data["releases"]
    ]
    return GovReleaseSource(releases)


def discover_release_contracts(release: GovRelease, search_fn) -> list[str]:
    """Discover relevant market slugs for a release via keyword search + date key.

    ``search_fn(term)`` returns a list of event dicts (each with ``markets``,
    each market with a ``slug``). A slug is relevant when it embeds the release
    date (YYYY-MM-DD), which Polymarket US slugs do for macro contracts.
    """
    date_key = release.scheduled_at.strftime("%Y-%m-%d")
    slugs: set[str] = set()
    for term in release.search_terms:
        try:
            events = search_fn(term)
        except Exception:  # noqa: BLE001 - discovery is best-effort
            continue
        for event in events:
            for market in event.get("markets", []):
                slug = market.get("slug")
                if slug and date_key in slug:
                    slugs.add(slug)
    return sorted(slugs)
