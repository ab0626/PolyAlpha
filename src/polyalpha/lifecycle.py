"""Public lifecycle refresh and price history; never synthesize historical depth."""

from datetime import UTC, datetime
from urllib.parse import quote

from .parsing import parse_market


def refresh_market(transport, store, market_id):
    raw, at = transport.get(
        "https://gamma-api.polymarket.com/markets/" + quote(str(market_id), safe=""), {}
    )
    store.append("raw_market", str(market_id), at, raw)
    market = parse_market(raw, at)
    if market.market_id != str(market_id):
        raise ValueError("market mismatch")
    store.append("market", market.market_id, at, raw)
    return market


def refresh_tracked(transport, store):
    identifiers = {r.entity_id for r in store.replay(datetime.now(UTC), "market")}
    return [refresh_market(transport, store, m) for m in sorted(identifiers)]


def price_history(transport, store, token, start_seconds, end_seconds):
    if start_seconds >= end_seconds:
        raise ValueError("invalid history interval")
    raw, at = transport.get(
        "https://clob.polymarket.com/prices-history",
        {
            "market": token,
            "startTs": start_seconds,
            "endTs": end_seconds,
            "fidelity": 1,
        },
    )
    store.append("price_history", token, at, raw)
    return raw
