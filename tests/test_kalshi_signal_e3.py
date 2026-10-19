"""Tests for the E3 -> screen-edge -> ProdOrderIntent wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from polyalpha.energy.kalshi import EnergyLadder, LadderRung
from polyalpha.kalshi_exec import OrderSide
from polyalpha.kalshi_exec_prod import MAX_NOTIONAL_PER_ORDER, MIN_SCREEN_EDGE
from polyalpha.kalshi_fees import FeeRule, verify_fee_rule
from polyalpha.kalshi_signal_e3 import build_signal_intents, screen_edges

D = Decimal


class FakeModel:
    def __init__(self, p: str):
        self.p = D(p)

    def p_peak_gt(self, target_day, as_of, strike):
        return self.p


def make_rung(**kwargs):
    base = dict(
        threshold=D("100"),
        direction="greater",
        yes_bid=D("0.48"),
        yes_ask=D("0.50"),
        yes_bid_size=D("10"),
        yes_ask_size=D("10"),
        no_bid=D("0.28"),
        no_ask=D("0.30"),
        ticker="KXTXERCOTPEAKD-100",
    )
    base.update(kwargs)
    return LadderRung(**base)


def make_ladder(*rungs):
    underlying = SimpleNamespace(
        operating_day="2026-10-18",
        observation_window_end=datetime(2026, 10, 18, 4, 55, tzinfo=UTC),
    )
    return EnergyLadder(underlying=underlying, series_ticker="KXTXERCOTPEAKD", rungs=list(rungs))


def ercot_rule(verified=True):
    rule = FeeRule(
        series_ticker="KXTXERCOTPEAKD",
        taker_rate=D("0.07"),
        maker_rate=D("0"),  # inferred default maker multiplier
        source="inferred",
    )
    return verify_fee_rule(rule) if verified else rule


def test_unverified_rule_emits_nothing():
    model = FakeModel("0.60")
    ladder = make_ladder(make_rung())
    assert build_signal_intents(model, ladder, ercot_rule(verified=False), datetime.now(UTC)) == []


def test_yes_intent_when_screen_edge_clears():
    model = FakeModel("0.60")  # p_model 0.60 vs yes_ask 0.50 -> edge 0.08 (>= 0.05)
    ladder = make_ladder(make_rung(no_ask=None, no_bid=None))
    intents = build_signal_intents(model, ladder, ercot_rule(), datetime.now(UTC))
    assert len(intents) == 1
    assert intents[0].side == OrderSide.BID
    assert intents[0].price == D("0.50")
    assert intents[0].screen_edge is not None and intents[0].screen_edge >= MIN_SCREEN_EDGE


def test_no_intent_sells_yes_at_one_minus_no_ask():
    model = FakeModel("0.60")  # P(NO)=0.40 vs no_ask 0.30 -> edge 0.08 -> ASK @ 0.70
    ladder = make_ladder(make_rung(yes_ask=None, yes_bid=None))
    intents = build_signal_intents(model, ladder, ercot_rule(), datetime.now(UTC))
    no_intents = [i for i in intents if i.side == OrderSide.ASK]
    assert len(no_intents) == 1
    assert no_intents[0].price == D("1") - D("0.30")


def test_edge_below_hurdle_emits_nothing():
    model = FakeModel("0.53")  # 0.53 - 0.50 - 0.02 = 0.01 < 0.05
    ladder = make_ladder(make_rung(no_ask=None, no_bid=None))
    assert build_signal_intents(model, ladder, ercot_rule(), datetime.now(UTC)) == []


def test_count_is_capped_and_floored():
    model = FakeModel("0.60")
    rung = make_rung(yes_ask_size=D("100"), no_ask=None, no_bid=None)
    ladder = make_ladder(rung)
    intents = build_signal_intents(model, ladder, ercot_rule(), datetime.now(UTC))
    assert len(intents) == 1
    assert intents[0].count == MAX_NOTIONAL_PER_ORDER


def test_insufficient_size_emits_nothing():
    model = FakeModel("0.60")
    rung = make_rung(yes_ask_size=D("0.004"), no_ask=None, no_bid=None)
    ladder = make_ladder(rung)
    assert build_signal_intents(model, ladder, ercot_rule(), datetime.now(UTC)) == []


def test_screen_edges_unverified_is_none():
    model = FakeModel("0.60")
    rung = make_rung()
    e_yes, e_no = screen_edges(
        model, rung, "2026-10-18", datetime.now(UTC), ercot_rule(verified=False)
    )
    assert e_yes is None
    assert e_no is None