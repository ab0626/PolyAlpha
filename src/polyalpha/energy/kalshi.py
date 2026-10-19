"""Kalshi public market-data client + semantic energy ladder construction.

Uses only the standard library (matching the repo's core REST posture). Public
market-data endpoints require no auth:

    https://external-api.kalshi.com/trade-api/v2

Energy market discovery is semantic: a curated, provenance-bearing registry of
series tickers maps to energy taxonomy leaves — never keyword search over titles.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from . import taxonomy
from .schemas import EnergyMarketLink, EnergyUnderlying

BASE = "https://external-api.kalshi.com/trade-api/v2"
_UA = {"User-Agent": "polyalpha-energy/0.1", "Accept": "application/json"}

D = Decimal
ERCOT_TZ = ZoneInfo("America/Chicago")


def ercot_operating_day(close_time_iso: str) -> str:
    """Operating day (local ISO date) implied by a Kalshi ERCOT peak close time.

    These markets close at a fixed 04:55 UTC, which is late evening of the
    operating day in America/Chicago; the ERCOT actual-load report for that day
    publishes the following morning (~05:50 CT). Timezone-aware, DST-safe.
    """
    dt = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
    return dt.astimezone(ERCOT_TZ).date().isoformat()


def _dec(value: Any) -> Decimal | None:
    """Parse a dollars/price string or number to Decimal, or None."""
    if value is None or value == "":
        return None
    try:
        return D(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True)
class LadderRung:
    threshold: Decimal
    direction: str                # "greater" | "less"
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    yes_bid_size: Decimal | None
    yes_ask_size: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    ticker: str

    @property
    def mid(self) -> Decimal | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / D(2)
        return self.yes_bid if self.yes_bid is not None else self.yes_ask

    @property
    def spread(self) -> Decimal | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return self.yes_ask - self.yes_bid
        return None


@dataclass
class EnergyLadder:
    underlying: EnergyUnderlying
    series_ticker: str
    rungs: list[LadderRung]       # sorted ascending by threshold

    def survival(self, side: str = "mid") -> list[tuple[Decimal, Decimal]]:
        """Ordered (threshold, P(X > K)) pairs for the ladder."""
        out = []
        for r in self.rungs:
            p = getattr(r, side) if hasattr(r, side) else r.mid
            if p is not None:
                out.append((r.threshold, p))
        return out

    def midpoint_violations(self) -> list[tuple[Decimal, Decimal]]:
        """Adjacent (K1<K2) pairs where mid(K2) > mid(K1).

        DIAGNOSTIC ONLY: with wide spreads the midpoint ordering can be nonsense
        even when there is no executable arbitrage. Not a tradeable edge.
        """
        vals = [(r.threshold, r.mid) for r in self.rungs if r.mid is not None]
        return [(k2, p2) for (k1, p1), (k2, p2) in zip(vals, vals[1:]) if p2 > p1]

    def executable_violations(self) -> list[tuple[Decimal, Decimal]]:
        """Adjacent (K1<K2) pairs where yes_bid(K2) > yes_ask(K1).

        This is the executable no-arbitrage condition (buy YES@K1, sell YES@K2 is
        riskless iff yes_bid(K2) > yes_ask(K1)). Still BEFORE fees and leg risk.
        Returns (higher_strike, gross_edge).
        """
        out = []
        for r1, r2 in zip(self.rungs, self.rungs[1:]):
            if r1.yes_ask is not None and r2.yes_bid is not None and r2.yes_bid > r1.yes_ask:
                out.append((r2.threshold, r2.yes_bid - r1.yes_ask))
        return out


# ── Semantic energy series registry ──────────────────────────────────────
# series_ticker -> (taxonomy_leaf, underlying_kind). Curated, not keyword-derived.
ENERGY_SERIES: dict[str, tuple[str, str]] = {
    # Tier-1 wedge: ERCOT daily peak load (resolves to ERCOT "Actual System Load
    # by Forecast Zone" CSV, TOTAL column max for the operating day).
    "KXTXERCOTPEAKD": (taxonomy.power_leaf(taxonomy.ISO_ERCOT), "daily_peak_load"),
    "KXUTILITYERCOT": (taxonomy.power_leaf(taxonomy.ISO_ERCOT), "electricity_price"),
    "KXERCOTWESTM": (taxonomy.power_leaf(taxonomy.ISO_ERCOT), "hub_power_price"),
    "KXERCOTWESTW": (taxonomy.power_leaf(taxonomy.ISO_ERCOT), "hub_power_price"),
    "KXERCOTHOUM": (taxonomy.power_leaf(taxonomy.ISO_ERCOT), "hub_power_price"),
    "KXERCOTNORTHM": (taxonomy.power_leaf(taxonomy.ISO_ERCOT), "hub_power_price"),
    "KXNATGASD": (taxonomy.NATURAL_GAS, "natgas_spot"),
    "KXNATGASW": (taxonomy.NATURAL_GAS, "natgas_spot"),
    "KXWTI": (taxonomy.CRUDE_OIL, "wti_close"),
    "KXWTIW": (taxonomy.CRUDE_OIL, "wti_close"),
    "KXWTIE": (taxonomy.CRUDE_OIL, "wti_close"),
    "KXPJMAEPDAYM": (taxonomy.power_leaf(taxonomy.ISO_PJM), "hub_power_price"),
    "KXMISOINDM": (taxonomy.power_leaf(taxonomy.ISO_MISO), "hub_power_price"),
    "KXSPRLVL": (taxonomy.CRUDE_OIL, "spr_level"),
}


class KalshiPublicClient:
    def __init__(self, base_url: str = BASE, timeout: float = 30, min_spacing: float = 0.05):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_spacing = min_spacing
        self._last = 0.0

    def _get(self, path: str, params: dict | None = None) -> dict:
        now = time.monotonic()
        delay = self.min_spacing - (now - self._last)
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()
        url = self.base_url + path
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def markets(self, series_ticker: str | None = None, status: str | None = None,
                limit: int = 1000, cursor: str | None = None) -> dict:
        params: dict[str, str] = {"limit": str(limit)}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self._get("/markets", params)

    def market(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}")

    def orderbook(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}/orderbook")


def parse_threshold(market: dict) -> tuple[Decimal | None, str | None]:
    """Return (threshold, direction) from strike fields, or (None, None)."""
    strike_type = market.get("strike_type")
    if strike_type == "greater":
        return _dec(market.get("floor_strike")), "greater"
    if strike_type == "less":
        return _dec(market.get("cap_strike")), "less"
    return None, strike_type


def market_to_link(series_ticker: str, leaf: str, kind: str, market: dict) -> EnergyMarketLink | None:
    threshold, direction = parse_threshold(market)
    if threshold is None:
        return None
    occurrence = market.get("occurrence_datetime")
    close = market.get("close_time")
    return EnergyMarketLink(
        underlying_id=underlying_id_for(kind, occurrence),
        venue="kalshi",
        market_id=market.get("ticker") or "",
        series_id=series_ticker,
        threshold=threshold,
        direction=direction,
        strike_type=market.get("strike_type"),
        settlement_time=_parse_iso(close),
        resolution_source=market.get("rules_secondary") or market.get("rules_primary"),
        mapping_method="semantic_series",
        mapping_created_at=datetime.now().astimezone(),
    )


def underlying_id_for(kind: str, occurrence_iso: str | None) -> str:
    date = (occurrence_iso or "")[:10]
    return f"{kind}:{date}" if date else f"{kind}:unknown"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_ladder(series_ticker: str, markets: list[dict]) -> EnergyLadder:
    """Build a threshold ladder from a list of same-series markets.

    Groups by occurrence_datetime (the operating day) — all markets in the list
    are assumed to share one underlying day; sort strikes ascending.
    """
    leaf, kind = ENERGY_SERIES.get(series_ticker, (taxonomy.ENERGY_RELEASES, "unknown"))
    rungs: list[LadderRung] = []
    op_day = None
    for m in markets:
        threshold, direction = parse_threshold(m)
        if threshold is None:
            continue
        if op_day is None and m.get("close_time"):
            op_day = ercot_operating_day(m["close_time"])
        rungs.append(LadderRung(
            threshold=threshold,
            direction=direction or "greater",
            yes_bid=_dec(m.get("yes_bid_dollars")),
            yes_ask=_dec(m.get("yes_ask_dollars")),
            yes_bid_size=_dec(m.get("yes_bid_size_fp")),
            yes_ask_size=_dec(m.get("yes_ask_size_fp")),
            no_bid=_dec(m.get("no_bid_dollars")),
            no_ask=_dec(m.get("no_ask_dollars")),
            ticker=m.get("ticker") or "",
        ))
    rungs.sort(key=lambda r: r.threshold)
    underlying = EnergyUnderlying(
        underlying_id=underlying_id_for(kind, op_day),
        taxonomy_leaf=leaf,
        kind=kind,
        region=_region_from_leaf(leaf),
        resolution_source=(markets[0].get("rules_secondary") if markets else None),
        operating_day=op_day,
        observation_window_start=_parse_iso(markets[0].get("open_time")) if markets else None,
        observation_window_end=_parse_iso(markets[0].get("close_time")) if markets else None,
        market_timezone="America/Chicago" if "ERCOT" in leaf else None,
    )
    return EnergyLadder(underlying=underlying, series_ticker=series_ticker, rungs=rungs)


def _region_from_leaf(leaf: str) -> str | None:
    for iso in taxonomy.KNOWN_ISOS:
        if iso in leaf:
            return iso
    return None