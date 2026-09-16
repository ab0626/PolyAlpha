"""Schema-drift tests: pinned payloads + drift detection.

Two jobs:
  1. Pinned representative payloads must show NO actionable drift. If the
     parser's expectations or the known schema change, this flags.
  2. Injected drift (renamed field, new enum) MUST be detected, proving the
     checker actually works (a drift detector that never fires is useless).

Optional live check: set POLYALPHA_LIVE_SCHEMA=1 to fetch a live payload and
report drift (skipped by default so the suite stays offline/deterministic).
"""

import os

import pytest

from polyalpha.us.schema_drift import check_drift

# ── Pinned representative payloads (documented / observed shapes) ────────────


PINNED_MARKET = {
    "id": "1001",
    "slug": "aec-nfl-kc-phi-2026-02-09",
    "question": "Chiefs win Super Bowl LX?",
    "title": "Chiefs win Super Bowl LX?",
    "category": "sports",
    "state": "MARKET_STATE_OPEN",
    "endDate": "2027-02-14T00:00:00Z",
    "startDate": "2026-09-01T00:00:00Z",
    "createdAt": "2026-09-01T00:00:00Z",
    "updatedAt": "2026-09-01T00:00:00Z",
    "orderPriceMinTickSize": 0.001,
    "minimumTradeQty": "1",
    "marketType": "moneyline",
    "marketSides": [
        {"id": "side-long", "long": True, "price": "0.555"},
        {"id": "side-short", "long": False, "price": "0.455"},
    ],
}

PINNED_BOOK = {
    "marketData": {
        "marketSlug": "aec-nfl-kc-phi-2026-02-09",
        "bids": [{"px": {"value": "0.550", "currency": "USD"}, "qty": "2.50"}],
        "offers": [{"px": {"value": "0.560", "currency": "USD"}, "qty": "1.20"}],
        "state": "MARKET_STATE_OPEN",
        "stats": {"currentPx": {"value": "0.555", "currency": "USD"}},
        "transactTime": "2026-09-15T12:00:00Z",
    }
}

PINNED_BBO = {
    "marketData": {
        "marketSlug": "aec-nfl-kc-phi-2026-02-09",
        "bestBid": {"value": "0.550", "currency": "USD"},
        "bestAsk": {"value": "0.560", "currency": "USD"},
        "currentPx": {"value": "0.555", "currency": "USD"},
        "bidDepth": 100,
        "askDepth": 100,
    }
}

PINNED_SETTLEMENT = {"slug": "aec-nfl-kc-phi-2026-02-09", "settlement": 1}

PINNED_INSTRUMENT = {
    "symbol": "aec-nfl-kc-phi-2026-02-09",
    "tickSize": 0.001,
    "minimumTradeQty": "1",
    "priceScale": 1000,
    "fractionalQtyScale": 1,
    "state": "INSTRUMENT_STATE_OPEN",
    "metadata": {"event_series": "nfl", "outcome_type": "moneyline"},
}


# ── No-drift assertions on pinned payloads ───────────────────────────────────


def test_pinned_market_no_actionable_drift():
    report = check_drift("market", PINNED_MARKET)
    assert report.missing_fields == [], report.as_dict()
    assert report.unknown_enums == [], report.as_dict()


def test_pinned_book_no_actionable_drift():
    report = check_drift("book", PINNED_BOOK)
    assert report.missing_fields == [], report.as_dict()
    assert report.unknown_enums == [], report.as_dict()


def test_pinned_bbo_no_actionable_drift():
    report = check_drift("bbo", PINNED_BBO)
    assert report.missing_fields == [], report.as_dict()


def test_pinned_settlement_no_actionable_drift():
    report = check_drift("settlement", PINNED_SETTLEMENT)
    assert report.missing_fields == [], report.as_dict()


def test_pinned_instrument_no_actionable_drift():
    report = check_drift("instrument", PINNED_INSTRUMENT)
    assert report.missing_fields == [], report.as_dict()
    assert report.unknown_enums == [], report.as_dict()


# ── The checker must actually detect drift ───────────────────────────────────


def test_missing_required_field_detected():
    payload = dict(PINNED_SETTLEMENT)
    del payload["settlement"]
    report = check_drift("settlement", payload)
    assert "settlement" in report.missing_fields
    assert report.actionable is True


def test_unknown_enum_detected():
    payload = dict(PINNED_MARKET)
    payload["state"] = "MARKET_STATE_SOMETHING_NEW"
    report = check_drift("market", payload)
    assert "MARKET_STATE_SOMETHING_NEW" in report.unknown_enums
    assert report.actionable is True


def test_unknown_field_is_informational_not_actionable():
    payload = dict(PINNED_MARKET)
    payload["brandNewField"] = "x"
    report = check_drift("market", payload)
    assert "brandNewField" in report.unknown_fields
    assert report.actionable is False


def test_new_state_prefix_detected():
    payload = dict(PINNED_INSTRUMENT)
    payload["state"] = "INSTRUMENT_STATE_SOMETHING_NEW"
    report = check_drift("instrument", payload)
    assert report.actionable is True


# ── Optional live drift check ────────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("POLYALPHA_LIVE_SCHEMA") != "1",
    reason="live schema check disabled (set POLYALPHA_LIVE_SCHEMA=1)",
)
def test_live_settlement_schema_no_actionable_drift():
    from polyalpha.us.rest import PublicUsClient

    client = PublicUsClient(timeout=15, attempts=1, min_spacing_seconds=0.05)
    payload, _ = client.market_settlement("aec-nfl-lac-ten-2025-11-02")
    report = check_drift("settlement", payload)
    assert report.missing_fields == [], report.as_dict()
    assert report.unknown_enums == [], report.as_dict()