"""Tests for the provenance-gated Kalshi execution-friction model."""

from __future__ import annotations

from decimal import Decimal

from polyalpha.kalshi_fees import (
    FeeRule,
    conservative_edge_per_contract,
    fee_per_contract,
    prediction_market_fee,
    verify_fee_rule,
)


def make_rule(verified=True):
    rule = FeeRule(
        series_ticker="KXERCOT",
        taker_rate=Decimal("0.07"),
        maker_rate=Decimal("0.0175"),
        source="https://docs.kalshi.com/fees",
    )
    return verify_fee_rule(rule) if verified else rule


def test_fee_formula_is_ceiling_quantized():
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> ceil to 0.02.
    assert prediction_market_fee(Decimal("1"), Decimal("0.5"), Decimal("0.07")) == Decimal("0.02")


def test_fee_is_symmetric_in_price():
    # P(1-P) is the same for P and 1-P.
    a = prediction_market_fee(Decimal("2"), Decimal("0.30"), Decimal("0.07"))
    b = prediction_market_fee(Decimal("2"), Decimal("0.70"), Decimal("0.07"))
    assert a == b


def test_unverified_rule_fails_closed():
    assert conservative_edge_per_contract(
        Decimal("0.60"), Decimal("0.55"), make_rule(verified=False)
    ) is None


def test_verified_rule_computes_net_edge():
    edge = conservative_edge_per_contract(
        Decimal("0.60"), Decimal("0.55"), make_rule(verified=True), is_maker=True
    )
    fee = fee_per_contract(Decimal("0.55"), Decimal("0.0175"))
    expected = Decimal("0.60") - Decimal("0.55") - fee - Decimal("0.01")
    assert edge == expected


def test_edge_is_none_for_missing_rule():
    assert conservative_edge_per_contract(
        Decimal("0.60"), Decimal("0.55"), None
    ) is None