"""Release-required collection universe and coverage invariant.

One canonical helper defines which markets a scheduled release requires, so the
three collectors never duplicate discovery logic independently:

    U_collector = U_activity  U  U_release_required

Activity-ranked markets are useful for general collection but can NEVER displace
a required research contract. The mapping is point-in-time data: every discovery
is persisted with a receipt time, so a later /v1/search result cannot silently
redefine the universe after a release (no retrospective market selection).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from ..gov_releases import discover_release_contracts, load_gov_releases


@dataclass(frozen=True)
class RequiredMarket:
    release_id: str
    release_name: str
    release_at: datetime
    market_slug: str
    market_id: str  # "us:{slug}"

    def as_dict(self) -> dict:
        return {
            "release_id": self.release_id,
            "release_name": self.release_name,
            "release_at": self.release_at.isoformat(),
            "market_slug": self.market_slug,
            "market_id": self.market_id,
        }


def release_required_universe(
    schedule_path: str | Path,
    search_fn: Callable[[str], list[dict]],
    horizon_days: int = 60,
    now: datetime | None = None,
) -> list[RequiredMarket]:
    """Discover the release-required markets for upcoming scheduled releases.

    ``search_fn(term)`` returns event dicts (each with ``markets`` -> ``slug``).
    Only releases within ``horizon_days`` are considered (far-future contracts
    do not exist yet; additive rediscovery picks them up as they appear).
    """
    now = now or datetime.now(UTC)
    source = load_gov_releases(schedule_path)
    out: list[RequiredMarket] = []
    for rel in source.upcoming(now):
        if (rel.scheduled_at - now).days > horizon_days:
            continue
        for slug in discover_release_contracts(rel, search_fn):
            out.append(RequiredMarket(
                release_id=rel.event_id,
                release_name=rel.name,
                release_at=rel.scheduled_at,
                market_slug=slug,
                market_id=f"us:{slug}",
            ))
    return out


def load_persisted_mapping(path: str | Path) -> set[tuple[str, str]]:
    """Load (release_id, market_slug) pairs from the persisted point-in-time mapping."""
    path = Path(path)
    if not path.exists():
        return set()
    out: set[tuple[str, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            out.add((data["release_id"], data["market_slug"]))
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def merged_required_slugs(
    schedule_path: str | Path,
    search_fn: Callable[[str], list[dict]],
    mapping_path: str | Path,
    horizon_days: int = 60,
    now: datetime | None = None,
) -> set[str]:
    """Union of freshly-discovered and previously-persisted required slugs.

    Additive: previously-required contracts are retained even if /v1/search
    transiently fails, so the universe never churns on a search hiccup.
    """
    current = release_required_universe(schedule_path, search_fn, horizon_days, now)
    slugs = {rm.market_slug for rm in current}
    slugs |= {slug for _release_id, slug in load_persisted_mapping(mapping_path)}
    return slugs


def collector_universe(
    client,
    schedule_path: str | Path,
    mapping_path: str | Path,
    limit: int = 50,
    now: datetime | None = None,
) -> tuple[list[str], list[RequiredMarket], list[str], list[str]]:
    """Canonical collector universe: U_activity  U  U_release_required.

    Returns (activity_slugs, required_markets, required_slugs, universe).
    Required contracts can never be displaced by activity ranking.
    """
    body, _ = client.markets({"limit": limit, "closed": "false"})
    activity = [m["slug"] for m in body.get("markets", []) if m.get("slug")]

    def search_fn(term: str) -> list[dict]:
        raw, _ = client.search({"query": term, "status": "active", "limit": 20})
        return raw.get("events", [])

    required = release_required_universe(schedule_path, search_fn, now=now)
    required_slugs = merged_required_slugs(schedule_path, search_fn, mapping_path, now=now)
    universe = sorted(set(activity) | set(required_slugs))
    return activity, required, sorted(required_slugs), universe


def compute_coverage(
    required: list[RequiredMarket],
    rest_slugs: set[str],
    l2_slugs: set[str],
    trade_slugs: set[str],
) -> dict[str, Any]:
    """Coverage invariant: Coverage(r, c) = 1[c in REST AND L2 AND trades]."""

    per_market = []
    missing = 0
    for rm in required:
        cov = {
            "REST": rm.market_slug in rest_slugs,
            "L2": rm.market_slug in l2_slugs,
            "trade": rm.market_slug in trade_slugs,
        }
        ok = all(cov.values())
        if not ok:
            missing += 1
        per_market.append({**rm.as_dict(), "coverage": cov, "coverage_ok": ok})

    by_release: dict[str, list[dict]] = {}
    for m in per_market:
        by_release.setdefault(m["release_id"], []).append(m)

    # Next release = earliest upcoming release that has any discovered markets.
    next_release = None
    for release_id in sorted(by_release, key=lambda r: by_release[r][0]["release_at"]):
        if by_release[release_id]:
            next_release = release_id
            break

    next_release_ok = (
        next_release is not None
        and all(m["coverage_ok"] for m in by_release[next_release])
    )

    return {
        "required_release_markets": len(required),
        "required_markets_discovered": len(required),
        "required_markets_collecting": len(required) - missing,
        "required_markets_missing": missing,
        "next_release_id": next_release,
        "next_release_at": by_release[next_release][0]["release_at"] if next_release else None,
        "next_release_coverage_ok": next_release_ok,
        "per_market": per_market,
    }


def persist_mapping(
    required: list[RequiredMarket],
    path: str | Path,
    receipt_time: datetime | None = None,
) -> None:
    """Persist the point-in-time mapping (market_id, release_id, discovered_at).

    This freezes Mapping(r, t): if a later search returns a different contract
    set, the mapping used before the release remains in the evidence record.
    """
    receipt_time = receipt_time or datetime.now(UTC)
    lines = [
        json.dumps({
            "market_id": rm.market_id,
            "market_slug": rm.market_slug,
            "release_id": rm.release_id,
            "discovered_at": receipt_time.isoformat(),
        }, sort_keys=True)
        for rm in required
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        if lines:
            fh.write("\n".join(lines) + "\n")
