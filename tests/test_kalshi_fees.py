"""Tests for the provenance-gated Kalshi execution-friction model."""

from __future__ import annotations

from decimal import Decimal

from polyalpha.kalshi_fees import (
    SLIPPAGE_ADVERSE_SELECTION_BUFFER,
    FeeRule,
    fee_per_contract,
    prediction_market_fee,
    screen_edge_per_contract,
    verify_fee_rule,
)


def make_rule(verified=True, maker_rate="0.0175"):
    rule = FeeRule(
        series_ticker="KXERCOT",
        taker_rate=Decimal("0.07"),
        maker_rate=Decimal(maker_rate),
        source="https://docs.kalshi.com/fees",
    )
    return verify_fee_rule(rule) if verified else rule


def test_fee_formula_is_ceiling_quantized():
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> ceil to 0.02.
    assert prediction_market_fee(Decimal("1"), Decimal("0.5"), Decimal("0.07")) == Decimal("0.02")


def test_fee_is_symmetric_in_price():
    a = prediction_market_fee(Decimal("2"), Decimal("0.30"), Decimal("0.07"))
    b = prediction_market_fee(Decimal("2"), Decimal("0.70"), Decimal("0.07"))
    assert a == b


def test_unverified_rule_fails_closed():
    assert screen_edge_per_contract(
        Decimal("0.60"), Decimal("0.55"), make_rule(verified=False)
    ) is None


def test_verified_rule_computes_net_edge():
    edge = screen_edge_per_contract(
        Decimal("0.60"), Decimal("0.55"), make_rule(verified=True), is_maker=True
    )
    fee = fee_per_contract(Decimal("0.55"), Decimal("0.0175"))
    expected = Decimal("0.60") - Decimal("0.55") - fee - Decimal("0.02")
    assert edge == expected


def test_edge_is_none_for_missing_rule():
    assert screen_edge_per_contract(Decimal("0.60"), Decimal("0.55"), None) is None


def test_ercot_zero_maker_rate_means_no_maker_fee():
    # Inferred ERCOT: maker multiplier 0 -> fee 0 -> screen edge = p - exec - B.
    rule = make_rule(verified=True, maker_rate="0")
    edge = screen_edge_per_contract(
        Decimal("0.60"), Decimal("0.55"), rule, is_maker=True
    )
    assert edge == Decimal("0.60") - Decimal("0.55") - SLIPPAGE_ADVERSE_SELECTION_BUFFER