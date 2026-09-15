"""Public Data API v2 adapters; pagination and payout units follow its OpenAPI schema."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .domain import number, utc
from .parsing import array, identifier, parse_market

BASE = "https://data-api.polymarket.com"


@dataclass(frozen=True)
class PublicTrade:
    token_id: str
    condition_id: str
    side: str
    shares: Decimal
    price: Decimal
    source_at: datetime
    received_at: datetime
    transaction_hash: str

    def __post_init__(self):
        utc(self.source_at)
        utc(self.received_at)
        if (
            self.side not in ("BUY", "SELL")
            or not self.shares.is_finite()
            or self.shares <= 0
            or not self.price.is_finite()
            or not 0 <= self.price <= 1
        ):
            raise ValueError("invalid public trade")


def parse_trade(raw, received):
    seconds = number(raw["timestamp"])
    if seconds != seconds.to_integral_value() or seconds < 0:
        raise ValueError("invalid block timestamp")
    return PublicTrade(
        identifier(raw["token_id"]),
        identifier(raw["condition_id"]),
        raw["side"],
        number(raw["size"]),
        number(raw["price"]),
        datetime.fromtimestamp(int(seconds), UTC),
        utc(received),
        identifier(raw["transaction_hash"]),
    )


def collect_trades(transport, store, condition, limit=100, max_pages=1):
    if not 1 <= limit <= 1000 or max_pages < 1:
        raise ValueError("invalid page bounds")
    cursor = None
    cursors = set()
    count = 0
    for _ in range(max_pages):
        params = {"condition": condition, "limit": limit, "taker_only": "true"}
        if cursor:
            params["cursor"] = cursor
        raw, received = transport.get(BASE + "/v2/trades", params)
        store.append("raw_trades_page", condition, received, raw)
        if not isinstance(raw.get("data"), list) or not isinstance(raw.get("pagination"), dict):
            raise ValueError("invalid v2 trades envelope")
        for item in raw["data"]:
            trade = parse_trade(item, received)
            if trade.condition_id != condition:
                raise ValueError("trade condition mismatch")
            store.append("trade", trade.token_id, received, item, trade.source_at)
            count += 1
        pagination = raw["pagination"]
        if pagination.get("has_more") is False:
            return dict(rows=count, truncated=False)
        cursor = pagination.get("next_cursor")
        if (
            pagination.get("has_more") is not True
            or not isinstance(cursor, str)
            or not cursor
            or cursor in cursors
        ):
            raise ValueError("invalid trade cursor")
        cursors.add(cursor)
    return dict(rows=count, truncated=True)


def collect_resolution(transport, store, raw_market):
    raw, received = transport.get(
        BASE + "/v2/resolutions", {"condition": raw_market["conditionId"]}
    )
    market = parse_market(raw_market, received)
    store.append("raw_resolution", market.market_id, received, raw)
    if not isinstance(raw.get("data"), list):
        raise ValueError("invalid v2 resolution envelope")
    tokens = [identifier(x) for x in array(raw_market["clobTokenIds"])]
    emitted = 0
    for resolution in raw["data"]:
        if resolution.get("condition_id") != market.condition_id:
            raise ValueError("resolution condition mismatch")
        # Only terminal, explicit binary payouts with source provenance are accepted.
        if resolution.get("status") != "resolved" or resolution.get("extended_review") is not False:
            continue
        if (
            resolution.get("market_type") != "BINARY"
            or resolution.get("resolution_source") != "reported"
        ):
            continue
        payouts = resolution.get("payouts")
        if (
            not isinstance(payouts, list)
            or len(payouts) != 2
            or any(type(p) is not int or not 0 <= p <= 1000000 for p in payouts)
            or sum(payouts) != 1000000
        ):
            raise ValueError("invalid micro-USDC payout vector")
        if not resolution.get("transaction_hash") or not resolution.get("resolved_at"):
            continue
        resolved = utc(datetime.fromisoformat(resolution["resolved_at"].replace("Z", "+00:00")))
        if resolved > received:
            raise ValueError("future resolution timestamp")
        for token, payout in zip(tokens, payouts):
            previous = store.latest("settlement", token, received)
            if (
                previous is not None
                and previous.payload.get("transaction_hash") == resolution["transaction_hash"]
            ):
                if Decimal(previous.payload["payout"]) != Decimal(payout) / 1000000:
                    raise ValueError("conflicting payout for an existing resolution transaction")
                continue
            payload = dict(
                token_id=token,
                payout=str(Decimal(payout) / 1000000),
                known_at=received.isoformat(),
                source_url=BASE + "/v2/resolutions",
                verified=True,
                verification="terminal API status and explicit binary payout vector",
                transaction_hash=resolution["transaction_hash"],
                condition_id=market.condition_id,
            )
            store.append("settlement", token, received, payload, resolved)
            emitted += 1
    return emitted
