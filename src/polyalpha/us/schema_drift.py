"""US schema-drift detection — alert when the venue's payload shape changes.

The Polymarket US API moves quickly (new state enums, decimalization, partial
contracts, settlement-shape changes). A silent schema change is a long-term
bug source: the parser keeps running while mis-reading a renamed field or an
unknown enum.

This module compares a payload against a pinned expected schema and reports:
  * missing_fields  — required fields absent (renamed/removed)  -> actionable
  * unknown_fields  — fields the schema does not know           -> informational
  * unknown_enums   — enum values outside the known set         -> actionable

It never mutates or normalizes data; it only reports. Callers decide whether a
drift report is an alert.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Known US state spellings across all surfaces (retail, refdata, gRPC).
KNOWN_STATES = frozenset({
    "MARKET_STATE_OPEN", "MARKET_STATE_PREOPEN", "MARKET_STATE_SUSPENDED",
    "MARKET_STATE_HALTED", "MARKET_STATE_EXPIRED", "MARKET_STATE_TERMINATED",
    "MARKET_STATE_MATCH_AND_CLOSE_AUCTION",
    "INSTRUMENT_STATE_PENDING", "INSTRUMENT_STATE_PREOPEN", "INSTRUMENT_STATE_OPEN",
    "INSTRUMENT_STATE_SUSPENDED", "INSTRUMENT_STATE_HALTED",
    "INSTRUMENT_STATE_CLOSED", "INSTRUMENT_STATE_EXPIRED",
    "INSTRUMENT_STATE_TERMINATED", "INSTRUMENT_STATE_MATCH_AND_CLOSE_AUCTION",
    "PENDING", "PREOPEN", "OPEN", "SUSPENDED", "HALTED", "CLOSED",
    "EXPIRED", "TERMINATED", "MATCH_AND_CLOSE_AUCTION",
})

# Required fields per payload kind (fields the parser depends on).
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "market": frozenset({"slug", "question", "marketSides"}),
    "book": frozenset({"marketSlug", "bids", "offers", "state", "stats", "transactTime"}),
    "bbo": frozenset({"marketSlug", "bestBid", "bestAsk"}),
    "settlement": frozenset({"slug", "settlement"}),
    "instrument": frozenset({
        "symbol", "tickSize", "minimumTradeQty", "priceScale", "fractionalQtyScale",
        "state",
    }),
}

# Known fields per payload kind (for informational unknown-field reporting).
KNOWN_FIELDS: dict[str, frozenset[str]] = {
    "market": frozenset({
        "id", "slug", "question", "title", "description", "category", "tags",
        "state", "status", "endDate", "startDate", "createdAt", "updatedAt",
        "marketSides", "marketType", "sportsMarketType", "sportsMarketTypeV2",
        "orderPriceMinTickSize", "minimumTradeQty", "outcomePrices", "outcomes",
        "liquidity", "volume", "active", "archived", "closed", "hidden",
        "comboEnabled", "manualActivation", "gameStartTime", "ep3Status",
        "ep3SyncedAt", "feeCoefficient", "eventId", "resolutionSource",
    }),
    "book": frozenset({
        "marketSlug", "bids", "offers", "state", "stats", "transactTime", "hash",
        "marketData",
    }),
    "bbo": frozenset({
        "marketSlug", "bestBid", "bestAsk", "currentPx", "lastTradePx",
        "lastTradeQty", "longQuote", "shortQuote", "bidDepth", "askDepth",
        "sharesTraded", "openInterest", "state", "transactTime", "marketData",
    }),
    "settlement": frozenset({"slug", "settlement", "marketSlug", "settlementPx"}),
    "instrument": frozenset({
        "symbol", "tickSize", "minimumTradeQty", "priceScale", "fractionalQtyScale",
        "state", "question", "payoutValue", "outcome_type", "event_id",
        "event_series", "event_category", "event_subcategory", "event_start_time",
        "startDate", "expirationDate", "terminationDate", "long_participant_id",
        "long_participant_name", "short_participant_id", "short_participant_name",
        "instrument_rules", "description", "metadata", "eventAttributes",
        "priceLimit", "orderSizeLimit", "baseCurrency", "multiplier",
        "settlementCurrency", "settlementPriceScale", "productId",
        "tradingSchedule", "clearingHouse", "nonTradable", "jsonAttributes",
        "createTime", "updateTime",
    }),
}


@dataclass
class DriftReport:
    kind: str
    missing_fields: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    unknown_enums: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        """Missing fields and unknown enums are actionable; unknown fields are
        informational (the venue adds fields routinely)."""
        return bool(self.missing_fields or self.unknown_enums)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "missing_fields": sorted(self.missing_fields),
            "unknown_fields": sorted(self.unknown_fields),
            "unknown_enums": sorted(self.unknown_enums),
            "actionable": self.actionable,
        }


def _unwrap(payload: dict) -> dict:
    """Payloads are sometimes wrapped in {"marketData": {...}}."""
    if isinstance(payload, dict) and "marketData" in payload and isinstance(
        payload["marketData"], dict
    ):
        return payload["marketData"]
    return payload


def check_drift(kind: str, payload: dict) -> DriftReport:
    """Compare a payload against the pinned schema for `kind`."""
    report = DriftReport(kind=kind)
    data = _unwrap(payload)
    if not isinstance(data, dict):
        report.missing_fields.append("<not-a-dict>")
        return report

    keys = set(data.keys())
    required = REQUIRED_FIELDS.get(kind, frozenset())
    known = KNOWN_FIELDS.get(kind, frozenset())

    report.missing_fields = sorted(required - keys)
    report.unknown_fields = sorted(keys - known)

    state = data.get("state")
    if state is not None and state not in KNOWN_STATES:
        report.unknown_enums.append(str(state))

    return report