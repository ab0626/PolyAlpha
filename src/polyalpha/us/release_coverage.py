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


def stratum_for(slug: str, category: str | None, strata: dict) -> str:
    """Map a market to a frozen category stratum (venue category, else slug prefix)."""
    if category:
        for name, cfg in strata.items():
            if category in cfg.get("categories", []):
                return name
    for name, cfg in strata.items():
        if any(slug.startswith(p) for p in cfg.get("slug_prefixes", [])):
            return name
    return "other"


def stratified_universe(client, strata_cfg: dict, limit: int = 500) -> list[tuple[str, str]]:
    """Sample ``count_per_stratum`` markets per frozen category stratum.

    Returns (slug, stratum) pairs. Deterministic (sort by the frozen key, take
    top N) and data-quality-gated (active/accepting/min_volume) before sampling.
    """
    sampling = strata_cfg.get("sampling", {})
    strata = strata_cfg.get("category_strata", {})
    body, _ = client.markets({"limit": limit, "closed": "false"})
    by_stratum: dict[str, list[tuple[str, float]]] = {}
    for m in body.get("markets", []):
        slug = m.get("slug")
        if not slug:
            continue
        if sampling.get("min_active") and not m.get("active", True):
            continue
        if sampling.get("min_accepting_orders") and not m.get("accepting_orders", True):
            continue
        try:
            vol = float(m.get("volume") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        if vol < sampling.get("min_volume", 0):
            continue
        stratum = stratum_for(slug, m.get("category"), strata)
        by_stratum.setdefault(stratum, []).append((slug, vol))

    sampled: list[tuple[str, str]] = []
    count = sampling.get("count_per_stratum", 10)
    desc = sampling.get("descending", True)
    for stratum in sorted(by_stratum):
        items = sorted(by_stratum[stratum], key=lambda x: x[1], reverse=desc)
        for slug, _ in items[:count]:
            sampled.append((slug, stratum))
    return sampled


def persist_stratified_membership(
    sampled: list[tuple[str, str]], path: str | Path, receipt_time: datetime | None = None
) -> None:
    """Persist point-in-time stratified membership (slug, stratum, sampled_at)."""
    receipt_time = receipt_time or datetime.now(UTC)
    lines = [
        json.dumps({"market_slug": slug, "stratum": stratum,
                    "sampled_at": receipt_time.isoformat()}, sort_keys=True)
        for slug, stratum in sampled
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if lines:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def collector_universe(
    client,
    schedule_path: str | Path,
    mapping_path: str | Path,
    limit: int = 50,
    now: datetime | None = None,
    strata_path: str | Path | None = None,
    membership_path: str | Path = "data/us/stratified_membership.jsonl",
) -> tuple[list[str], list[RequiredMarket], list[str], list[str]]:
    """Canonical collector universe: U_activity  U  U_required  U  U_stratified.

    Returns (activity_slugs, required_markets, required_slugs, universe).
    Required and stratified contracts can never be displaced by activity
    ranking. Stratified sampling is frozen in ``strata_path`` and its membership
    is persisted point-in-time.
    """
    body, _ = client.markets({"limit": limit, "closed": "false"})
    activity = [m["slug"] for m in body.get("markets", []) if m.get("slug")]

    def search_fn(term: str) -> list[dict]:
        raw, _ = client.search({"query": term, "status": "active", "limit": 20})
        return raw.get("events", [])

    required = release_required_universe(schedule_path, search_fn, now=now)
    required_slugs = merged_required_slugs(schedule_path, search_fn, mapping_path, now=now)

    stratified_slugs: set[str] = set()
    if strata_path:
        cfg = json.loads(Path(strata_path).read_text(encoding="utf-8"))
        sampled = stratified_universe(client, cfg)
        stratified_slugs = {slug for slug, _ in sampled}
        persist_stratified_membership(sampled, membership_path)

    universe = sorted(set(activity) | set(required_slugs) | stratified_slugs)
    return activity, required, sorted(required_slugs), universe


def compute_coverage(
    required: list[RequiredMarket],
    rest_slugs: set[str],
    l2_slugs: set[str],
    trade_slugs: set[str],
    rest_last: dict[str, datetime] | None = None,
    l2_last: dict[str, datetime] | None = None,
    trade_collector_age_seconds: float | None = None,
    now: datetime | None = None,
    rest_max_age_seconds: float = 300.0,
    l2_max_age_seconds: float = 90.0,
    trade_max_age_seconds: float = 300.0,
) -> dict[str, Any]:
    """Coverage = membership AND freshness.

    Coverage(c, t) = Subscribed(c) AND Age_last_observation(c) < tau. A market
    seen yesterday does not count as covered today. Sparse trades are not
    required: trade coverage is the subscription invariant + collector liveness.
    """
    now = now or datetime.now(UTC)

    def _age(last: dict[str, datetime] | None, slug: str) -> float | None:
        t = last.get(slug) if last else None
        return (now - t).total_seconds() if t is not None else None

    per_market = []
    missing = 0
    for rm in required:
        slug = rm.market_slug
        rest_age = _age(rest_last, slug)
        l2_age = _age(l2_last, slug)
        rest_ok = slug in rest_slugs and (rest_age is None or rest_age < rest_max_age_seconds)
        l2_ok = slug in l2_slugs and (l2_age is None or l2_age < l2_max_age_seconds)
        trade_ok = slug in trade_slugs and (
            trade_collector_age_seconds is None
            or trade_collector_age_seconds < trade_max_age_seconds
        )
        cov = {"REST": rest_ok, "L2": l2_ok, "trade": trade_ok}
        ok = all(cov.values())
        if not ok:
            missing += 1
        per_market.append({
            **rm.as_dict(), "coverage": cov, "coverage_ok": ok,
            "rest_age_seconds": round(rest_age, 1) if rest_age is not None else None,
            "l2_age_seconds": round(l2_age, 1) if l2_age is not None else None,
        })

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
