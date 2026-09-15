"""Strict API boundary. Preserve the original payload separately before parsing."""

import json
from datetime import UTC, datetime
from typing import Any

from .domain import Book, Level, Market, number, utc


def boolean(raw: dict, key: str, default: bool | None = None) -> bool | None:
    value = raw.get(key, default)
    if value is not None and type(value) is not bool:
        raise ValueError(f"{key} must be boolean")
    return value


def array(value: Any) -> list:
    result = json.loads(value) if isinstance(value, str) else value
    if not isinstance(result, list):
        raise ValueError("expected array")
    return result


def identifier(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value):
        raise ValueError("invalid identifier")
    return str(value)


def parse_market(raw: dict, received_at: datetime) -> Market:
    outcomes = array(raw["outcomes"])
    tokens = array(raw["clobTokenIds"])
    labels = [str(x).casefold() for x in outcomes]
    if len(tokens) != 2 or sorted(labels) != ["no", "yes"]:
        raise ValueError("only explicitly labelled binary YES/NO markets supported")
    deadline = raw.get("endDate")
    return Market(
        market_id=identifier(raw["id"]),
        condition_id=identifier(raw["conditionId"]),
        event_ids=tuple(identifier(e["id"]) for e in raw.get("events", [])),
        question=raw["question"],
        description=raw.get("description") or "",
        resolution_source=raw.get("resolutionSource"),
        deadline=utc(datetime.fromisoformat(deadline.replace("Z", "+00:00"))) if deadline else None,
        active=boolean(raw, "active") is True,
        closed=boolean(raw, "closed", True) is not False,
        accepting_orders=boolean(raw, "acceptingOrders") is True,
        enable_order_book=boolean(raw, "enableOrderBook") is True,
        liquidity=number(raw["liquidity"]) if raw.get("liquidity") is not None else None,
        volume=number(raw["volume"]) if raw.get("volume") is not None else None,
        fees_enabled=boolean(raw, "feesEnabled"),
        fee_parameters_json=json.dumps(raw["feeSchedule"], sort_keys=True)
        if raw.get("feeSchedule") is not None
        else None,
        yes_token_id=identifier(tokens[labels.index("yes")]),
        no_token_id=identifier(tokens[labels.index("no")]),
        received_at=utc(received_at),
        category=raw.get("category") or "unknown",
    )


def parse_book(raw: dict, received_at: datetime, expected_token: str | None = None) -> Book:
    token = identifier(raw["asset_id"])
    if expected_token is not None and token != expected_token:
        raise ValueError("response token mismatch")
    # CLOB emits Unix milliseconds; do not silently guess seconds from magnitude.
    milliseconds = number(raw["timestamp"])
    if milliseconds != milliseconds.to_integral_value() or milliseconds < 0:
        raise ValueError("invalid millisecond timestamp")

    def levels(key: str, reverse: bool) -> tuple[Level, ...]:
        result = [Level(number(x["price"]), number(x["size"])) for x in array(raw[key])]
        return tuple(sorted(result, key=lambda x: x.price, reverse=reverse))

    return Book(
        token,
        identifier(raw["market"]),
        datetime.fromtimestamp(int(milliseconds) / 1000, UTC),
        utc(received_at),
        levels("bids", True),
        levels("asks", False),
        number(raw["tick_size"]),
        number(raw["min_order_size"]),
        str(raw.get("hash", "")),
    )
