"""Tests for the E3 -> screen-edge -> TAKER intent wiring (single-day, fail-closed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from polyalpha.energy.kalshi import EnergyLadder, LadderRung
from polyalpha.kalshi_exec import OrderSide
from polyalpha.kalshi_exec_prod import MAX_NOTIONAL_PER_ORDER, MIN_SCREEN_EDGE
from polyalpha.kalshi_fees import FeeRule, verify_fee_rule
from polyalpha.kalshi_oms_prod import ProdOrderIntent
from polyalpha.kalshi_signal_e3 import (
    REASON_EDGE_BELOW,
    REASON_ELIGIBLE,
    REASON_FEED_UNVERIFIED,
    REASON_NO_QUOTE,
    REASON_REALIZED_CROSSED,
    REASON_SIGNAL_STALE,
    REASON_ZERO_SIZE,
    build_signal_intents,
    build_single_day_ladder,
    evaluate_ladder,
    revalidate_quote,
)

D = Decimal
AS_OF = datetime(2026, 10, 18, 0, 0, tzinfo=UTC)


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
        no_bid=None,
        no_ask=None,
        ticker="KXTXERCOTPEAKD-100",
    )
    base.update(kwargs)
    return LadderRung(**base)


def make_ladder(*rungs):
    underlying = SimpleNamespace(
        operating_day="2026-10-18",
        observation_window_start=None,
        observation_window_end=datetime(2026, 10, 18, 4, 55, tzinfo=UTC),
    )
    return EnergyLadder(underlying=underlying, series_ticker="KXTXERCOTPEAKD", rungs=list(rungs))


def ercot_rule(verified=True):
    rule = FeeRule(
        series_ticker="KXTXERCOTPEAKD",
        taker_rate=D("0.07"),
        maker_rate=D("0"),
        source="inferred",
    )
    return verify_fee_rule(rule) if verified else rule


def eval_one(rung, p="0.60", fee_rule=None, as_of=AS_OF, realized_running_max=Decimal("0")):
    model = FakeModel(p)
    ladder = make_ladder(rung)
    return evaluate_ladder(
        model, ladder, fee_rule if fee_rule is not None else ercot_rule(), as_of,
        realized_running_max=realized_running_max,
    )


# ── Single operating-day scope ────────────────────────────────────────────

def test_mixed_day_ladder_rejected():
    markets = [
        {"ticker": "A", "close_time": "2026-10-18T04:55:00Z"},
        {"ticker": "B", "close_time": "2026-10-19T04:55:00Z"},
    ]
    with pytest.raises(ValueError, match="mixed operating days"):
        build_single_day_ladder(markets, "KXTXERCOTPEAKD")


def test_ladder_without_operating_day_rejected():
    underlying = SimpleNamespace(
        operating_day=None,
        observation_window_start=None,
        observation_window_end=datetime(2026, 10, 18, 4, 55, tzinfo=UTC),
    )
    ladder = EnergyLadder(underlying=underlying, series_ticker="KX", rungs=[make_rung()])
    with pytest.raises(ValueError, match="operating_day"):
        evaluate_ladder(FakeModel("0.60"), ladder, ercot_rule(), AS_OF)


# ── Taker-only + mapping ──────────────────────────────────────────────────

def test_unverified_rule_emits_nothing():
    model = FakeModel("0.60")
    ladder = make_ladder(make_rung())
    assert build_signal_intents(model, ladder, ercot_rule(verified=False), AS_OF,
                                realized_running_max=Decimal("0")) == []


def test_yes_taker_intent_is_bid_at_ask_not_post_only():
    evals = eval_one(make_rung(no_ask=None), p="0.60")
    e = [e for e in evals if e.side == "YES"][0]
    assert e.eligibility_reason == REASON_ELIGIBLE
    assert e.intent.side == OrderSide.BID
    assert e.intent.price == D("0.50")
    assert e.intent.post_only is False
    assert e.intent.screen_edge >= MIN_SCREEN_EDGE


def test_no_taker_intent_is_ask_at_yes_bid():
    # p_hat_E3 = 0.30 -> P(NO) = 0.70; effective NO price = 1 - 0.48 = 0.52.
    evals = eval_one(make_rung(yes_ask=None, yes_bid=D("0.48")), p="0.30")
    e = [e for e in evals if e.side == "NO"][0]
    assert e.eligibility_reason == REASON_ELIGIBLE
    assert e.intent.side == OrderSide.ASK
    assert e.intent.price == D("0.48")  # sell YES at the bid == buy NO at 1 - bid
    assert e.executable_price == D("1") - D("0.48")


# ── Fail-closed on degenerate books ───────────────────────────────────────

def test_zero_ask_is_no_executable_quote():
    evals = eval_one(make_rung(yes_ask=D("0.00"), no_ask=None))
    e = [e for e in evals if e.side == "YES"][0]
    assert e.eligibility_reason == REASON_NO_QUOTE
    assert e.intent is None


def test_one_ask_is_no_executable_quote():
    evals = eval_one(make_rung(yes_ask=D("1.00"), no_ask=None))
    e = [e for e in evals if e.side == "YES"][0]
    assert e.eligibility_reason == REASON_NO_QUOTE


def test_zero_size_is_not_eligible():
    evals = eval_one(make_rung(yes_ask_size=D("0"), no_ask=None))
    e = [e for e in evals if e.side == "YES"][0]
    assert e.eligibility_reason == REASON_ZERO_SIZE
    assert e.intent is None


def test_edge_below_hurdle_is_not_eligible():
    evals = eval_one(make_rung(no_ask=None), p="0.53")
    e = [e for e in evals if e.side == "YES"][0]
    assert e.eligibility_reason == REASON_EDGE_BELOW
    assert e.intent is None


def test_count_capped_at_max_notional():
    evals = eval_one(make_rung(yes_ask_size=D("100"), no_ask=None), p="0.60")
    e = [e for e in evals if e.side == "YES"][0]
    assert e.intent.count == MAX_NOTIONAL_PER_ORDER


# ── Pre-submit quote revalidation ─────────────────────────────────────────

def _intent(side, price):
    return ProdOrderIntent(
        ticker="KXTXERCOTPEAKD-100",
        side=side,
        price=D(price),
        count=D("1"),
        client_order_id="cid-1",
    )


def test_revalidate_bid_same_or_better():
    intent = _intent(OrderSide.BID, "0.50")
    assert revalidate_quote(intent, D("0.50")) is True   # same
    assert revalidate_quote(intent, D("0.49")) is True   # better
    assert revalidate_quote(intent, D("0.52")) is False  # ask moved up -> reject


def test_revalidate_ask_same_or_better():
    intent = _intent(OrderSide.ASK, "0.48")
    assert revalidate_quote(intent, D("0.48")) is True   # same
    assert revalidate_quote(intent, D("0.49")) is True   # better
    assert revalidate_quote(intent, D("0.46")) is False  # bid moved down -> reject


def test_revalidate_rejects_degenerate_quote():
    intent = _intent(OrderSide.BID, "0.50")
    assert revalidate_quote(intent, None) is False
    assert revalidate_quote(intent, D("0.00")) is False
    assert revalidate_quote(intent, D("1.00")) is False
    assert revalidate_quote(intent, D("0.50"), fresh_size=D("0")) is False


# ── Realized-path dominance + signal-age gates ────────────────────────────

def test_feed_unverified_blocks_all():
    evals = eval_one(make_rung(), p="0.60", realized_running_max=None)
    assert all(e.eligibility_reason == REASON_FEED_UNVERIFIED for e in evals)
    assert all(e.intent is None for e in evals)


def test_realized_crossing_blocks_no_but_not_yes():
    evals = eval_one(make_rung(), p="0.60", realized_running_max=Decimal("150"))
    e_yes = [e for e in evals if e.side == "YES"][0]
    e_no = [e for e in evals if e.side == "NO"][0]
    assert e_yes.eligibility_reason == REASON_ELIGIBLE
    assert e_no.eligibility_reason == REASON_REALIZED_CROSSED
    assert e_no.intent is None


def test_signal_stale_blocks_all():
    stale_ts = AS_OF - timedelta(hours=7)  # > 6h frozen window
    model = FakeModel("0.60")
    ladder = make_ladder(make_rung())
    evals = evaluate_ladder(
        model, ladder, ercot_rule(), AS_OF,
        realized_running_max=Decimal("0"),
        signal_timestamp=stale_ts,
        max_signal_age_seconds=6 * 3600,
    )
    assert all(e.eligibility_reason == REASON_SIGNAL_STALE for e in evals)
    assert all(e.intent is None for e in evals)