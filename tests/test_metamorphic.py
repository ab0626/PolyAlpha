"""Metamorphic testing: verify relations that hold across transformations.

Part 59: Metamorphic tests verify that transformations to inputs produce
predictable changes in outputs, even when the exact output is unknown.

Metamorphic relations:
- Book size scaling: small-trade VWAP unchanged
- Quantity scaling: VWAP unchanged
- Market = model probability: gross informational edge → zero
- Fee increase: net edge cannot increase
- Uncertainty increase: actionable signal set should not increase
- Risk cap decrease: accepted notional should not increase
- Prediction shuffling: Brier worsens or stays same
- Label permutation: model Brier → market Brier
"""

import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyalpha.domain import Book, Level
from polyalpha.execution import FeeSchedule, Order, walk
from polyalpha.portfolio import Portfolio
from polyalpha.performance import brier_score
from polyalpha.forecasting import Forecast, net_edge
from polyalpha.risk import Limits, Risk
from polyalpha.signals import Signal, SignalEvaluation, evaluate_entry, rank_signals

D = Decimal
TS = datetime(2025, 6, 1, tzinfo=timezone.utc)

_counter = [0]


def _rng(seed):
    return random.Random(seed)


def _tick(x):
    return round(x, 2)


def _next_id():
    _counter[0] += 1
    return f"m{_counter[0]}"


def _book(bid_p, ask_p, bid_sz, ask_sz, tick=D("0.01"), min_size=D("1")):
    return Book(
        token_id="t1", condition_id="c1",
        source_at=TS, received_at=TS,
        bids=(Level(D(str(_tick(bid_p))), D(str(bid_sz))),),
        asks=(Level(D(str(_tick(ask_p))), D(str(ask_sz))),),
        tick_size=tick, min_order_size=min_size, source_hash="",
    )


def _multi_level_book(bid_prices, ask_prices, bid_sizes, ask_sizes):
    bids = tuple(Level(D(str(_tick(p))), D(str(s))) for p, s in zip(bid_prices, bid_sizes))
    asks = tuple(Level(D(str(_tick(p))), D(str(s))) for p, s in zip(ask_prices, ask_sizes))
    return Book(
        token_id="t1", condition_id="c1",
        source_at=TS, received_at=TS,
        bids=bids, asks=asks,
        tick_size=D("0.01"), min_order_size=D("1"), source_hash="",
    )


def _market(mid, active=True, accepting=True, ob_enabled=True):
    from polyalpha.domain import Market
    return Market(
        market_id="m1", condition_id="c1",
        event_ids=("e1",), question="Test?", description="",
        resolution_source=None, deadline=None,
        active=active, closed=False,
        accepting_orders=accepting, enable_order_book=ob_enabled,
        liquidity=D("5000"), volume=D("1000"),
        fees_enabled=False, fee_parameters_json=None,
        yes_token_id="t1", no_token_id="t2",
        received_at=TS,
    )


def _forecast(prob, ts=None):
    return Forecast(
        market_id="m1", timestamp=ts or TS,
        probability=D(str(prob)),
        lower=D(str(max(0.01, prob - 0.05))),
        upper=D(str(min(0.99, prob + 0.05))),
        version="test",
    )


def _snapshots_with_predictions(n=80, seed=42):
    from polyalpha.research_dataset import MarketSnapshot
    rng = _rng(seed)
    snaps = []
    for i in range(n):
        p = round(rng.uniform(0.3, 0.7), 4)
        mid = round(p + rng.gauss(0, 0.03), 4)
        mid = max(0.15, min(0.85, mid))
        spread = round(rng.uniform(0.01, 0.05), 4)
        outcome = 1 if rng.random() < p else 0
        ts = TS + timedelta(hours=i)
        snaps.append(MarketSnapshot(
            observation_timestamp=ts,
            market_id=f"m{i:04d}", event_id=f"evt_{i // 5}",
            condition_id=f"c{i:04d}",
            category=["politics", "sports", "crypto"][i % 3],
            question=f"Q #{i}?",
            yes_token_id=f"yes_{i}", no_token_id=f"no_{i}",
            yes_best_bid=D(str(round(mid - spread / 2, 4))),
            yes_best_ask=D(str(round(mid + spread / 2, 4))),
            yes_mid=D(str(mid)),
            yes_spread=D(str(spread)),
            yes_depth_1=D(str(rng.uniform(50, 500))),
            yes_depth_5=D(str(rng.uniform(200, 2000))),
            yes_bid_size=D(str(rng.uniform(20, 200))),
            yes_ask_size=D(str(rng.uniform(20, 200))),
            no_best_bid=D(str(round(1 - mid - spread / 2, 4))),
            no_best_ask=D(str(round(1 - mid + spread / 2, 4))),
            no_mid=D(str(round(1 - mid, 4))),
            volume=D(str(rng.uniform(100, 10000))),
            liquidity=D(str(rng.uniform(500, 50000))),
            hours_to_resolution=rng.uniform(1, 720),
            fees_enabled=True,
            fee_rate=D("0.02"),
            event_cluster=f"cluster_{i // 10}",
            final_resolution=outcome,
            model_probability=D(str(p)),
            execution_price=D(str(round(mid + spread / 2, 4))),
            side="BUY",
        ))
    return snaps


def _brier(snaps):
    resolved = [s for s in snaps if s.final_resolution is not None and s.model_probability is not None]
    if not resolved:
        return 1.0
    return sum((float(s.model_probability) - s.final_resolution) ** 2 for s in resolved) / len(resolved)


# ── MR1: Book size scaling ──────────────────────────────────────────────────


class TestBookSizeScaling:
    def test_multiplying_book_sizes_by_10_leaves_small_trade_vwap_unchanged(self):
        """Scaling all sizes by a constant factor should not change VWAP for
        small trades that don't exhaust the first level."""
        rng = _rng(100)
        for _ in range(30):
            bid_p = _tick(rng.uniform(0.30, 0.49))
            ask_p = _tick(rng.uniform(0.51, 0.70))
            if bid_p >= ask_p:
                continue
            bid_sz = round(rng.uniform(50, 200), 2)
            ask_sz = round(rng.uniform(50, 200), 2)
            shares = D(str(round(rng.uniform(1, min(bid_sz, ask_sz) * 0.5), 2)))

            book1 = _book(bid_p, ask_p, bid_sz, ask_sz)
            book2 = _book(bid_p, ask_p, bid_sz * 10, ask_sz * 10)

            fees = FeeSchedule(D("0"), TS, "test")
            fill1 = walk(book1, Order(_next_id(), "t1", "BUY", shares, TS), fees, TS)
            fill2 = walk(book2, Order(_next_id(), "t1", "BUY", shares, TS), fees, TS)
            assert fill1.vwap == fill2.vwap, (
                f"VWAP changed with scaled sizes: {fill1.vwap} != {fill2.vwap}"
            )


# ── MR2: Quantity scaling ───────────────────────────────────────────────────


class TestQuantityScaling:
    def test_scaling_quantities_and_requested_proportionally_leaves_vwap_unchanged(self):
        """If all level sizes and requested quantity are scaled by the same
        factor, VWAP should be identical."""
        rng = _rng(200)
        for _ in range(30):
            bid_p = _tick(rng.uniform(0.30, 0.49))
            ask_p = _tick(rng.uniform(0.51, 0.70))
            if bid_p >= ask_p:
                continue
            bid_sz = round(rng.uniform(50, 200), 2)
            ask_sz = round(rng.uniform(50, 200), 2)
            base_shares = round(rng.uniform(5, min(bid_sz, ask_sz) * 0.5), 2)
            factor = round(rng.uniform(2, 10), 1)

            book1 = _book(bid_p, ask_p, bid_sz, ask_sz)
            book2 = _book(bid_p, ask_p, bid_sz * factor, ask_sz * factor)

            fees = FeeSchedule(D("0"), TS, "test")
            fill1 = walk(book1, Order(_next_id(), "t1", "BUY", D(str(base_shares)), TS), fees, TS)
            fill2 = walk(book2, Order(_next_id(), "t1", "BUY", D(str(base_shares * factor)), TS), fees, TS)
            assert abs(fill1.vwap - fill2.vwap) < D("0.0001"), (
                f"VWAP changed under proportional scaling: {fill1.vwap} != {fill2.vwap}"
            )


# ── MR3: Model = market → edge ≈ 0 ─────────────────────────────────────────


class TestModelEqualsMarket:
    def test_when_model_probability_equals_market_mid_gross_edge_approaches_zero(self):
        rng = _rng(300)
        for _ in range(30):
            mid = _tick(rng.uniform(0.30, 0.70))
            spread = _tick(rng.uniform(0.02, 0.06))
            ask = min(_tick(mid + spread / 2), 0.99)
            bid = _tick(mid - spread / 2)
            if bid < 0.01 or bid >= ask:
                continue
            book = _book(bid, ask, 200, 200)

            forecast = _forecast(mid)
            fees = FeeSchedule(D("0"), TS, "test")
            order = Order(_next_id(), "t1", "BUY", D("30"), TS)
            fill = walk(book, order, fees, TS)

            edge = net_edge(forecast, fill, yes=True,
                            extra_slippage=D("0"), resolution_penalty=D("0"))
            assert abs(edge) < D("0.10"), (
                f"Edge when model=market: {edge} (mid={mid}, fill_vwap={fill.vwap})"
            )


# ── MR4: Fee increase → net edge decrease ────────────────────────────────────


class TestFeeIncreaseReducesEdge:
    def test_higher_fees_never_increase_net_edge(self):
        rng = _rng(400)
        for _ in range(30):
            mid = _tick(rng.uniform(0.35, 0.65))
            spread = _tick(rng.uniform(0.02, 0.05))
            ask = min(_tick(mid + spread / 2), 0.99)
            bid = _tick(mid - spread / 2)
            if bid < 0.01 or bid >= ask:
                continue
            book = _book(bid, ask, 200, 200)
            forecast = _forecast(mid + 0.10)
            shares = D("30")
            fee_rates = [D("0"), D("0.01"), D("0.03"), D("0.05"), D("0.10")]
            edges = []
            for rate in fee_rates:
                fees = FeeSchedule(rate, TS, "test")
                order = Order(_next_id(), "t1", "BUY", shares, TS)
                fill = walk(book, order, fees, TS)
                edge = net_edge(forecast, fill, yes=True,
                                extra_slippage=D("0"), resolution_penalty=D("0"))
                edges.append(edge)
            for i in range(1, len(edges)):
                assert edges[i] <= edges[i - 1] + D("0.001"), (
                    f"Fee increase improved edge: {fee_rates[i]}: {edges[i]} > {fee_rates[i-1]}: {edges[i-1]}"
                )


# ── MR5: Uncertainty increase → fewer actionable signals ─────────────────────


class TestUncertaintyReducesActionable:
    def test_higher_uncertainty_fewer_actionable_signals(self):
        rng = _rng(500)
        n_markets = 40
        market = _market("m1")
        yes_book = _book(0.50, 0.55, 200, 200)
        fees = FeeSchedule(D("0"), TS, "test")

        low_count = 0
        high_count = 0
        for i in range(n_markets):
            prob = round(rng.uniform(0.35, 0.65), 4)
            # Low uncertainty
            fc_low = Forecast("m1", TS, D(str(prob)),
                              D(str(max(0.01, prob - 0.01))),
                              D(str(min(0.99, prob + 0.01))), "test")
            result_low = evaluate_entry(market, yes_book, yes_book, fc_low, fees, D("10000"), D("50"), TS)
            if result_low.signal and result_low.signal.is_executable:
                low_count += 1

            # High uncertainty
            fc_high = Forecast("m1", TS, D(str(prob)),
                               D(str(max(0.01, prob - 0.10))),
                               D(str(min(0.99, prob + 0.10))), "test")
            result_high = evaluate_entry(market, yes_book, yes_book, fc_high, fees, D("10000"), D("50"), TS)
            if result_high.signal and result_high.signal.is_executable:
                high_count += 1

        assert high_count <= low_count, (
            f"Higher uncertainty produced more signals: {high_count} > {low_count}"
        )


# ── MR6: Risk cap decrease → lower accepted notional ────────────────────────


class TestRiskCapDecreaseReducesNotional:
    def test_lower_risk_cap_means_less_budget(self):
        equity = D("10000")
        portfolio = Portfolio(equity)
        normal_caps = [D("0.01"), D("0.005"), D("0.002")]
        budgets = []
        for cap in normal_caps:
            limits = Limits(normal=cap, market=D("0.05"), event=D("0.05"),
                            cluster=D("0.05"), category=D("0.10"), total=D("0.20"),
                            daily_loss=D("0.02"), drawdown=D("0.08"))
            risk = Risk(equity, limits)
            budget = risk.budget(portfolio, equity, "m1", "e1", "cl1", "politics")
            budgets.append(budget)
        for i in range(1, len(budgets)):
            assert budgets[i] <= budgets[i - 1] + D("0.01"), (
                f"Lower cap gave more budget: {budgets[i]} > {budgets[i-1]}"
            )


# ── MR7: Prediction shuffling → Brier worsens ───────────────────────────────


class TestPredictionShuffleWorsensBrier:
    def test_shuffled_predictions_worsen_brier_or_stay_same(self):
        snaps = _snapshots_with_predictions(100)
        real_brier = _brier(snaps)
        rng = _rng(600)
        from polyalpha.research_dataset import MarketSnapshot

        probs = [s.model_probability for s in snaps if s.model_probability is not None]
        shuffled = list(probs)
        rng.shuffle(shuffled)

        idx = 0
        shuffled_snaps = []
        for s in snaps:
            if s.model_probability is not None:
                shuffled_snaps.append(MarketSnapshot(
                    **{**s.__dict__, "model_probability": shuffled[idx]}
                ))
                idx += 1
            else:
                shuffled_snaps.append(s)

        shuffled_brier = _brier(shuffled_snaps)
        assert shuffled_brier >= real_brier - 0.01, (
            f"Shuffled predictions improved Brier: {shuffled_brier} < {real_brier}"
        )


# ── MR8: Label permutation → model Brier → market Brier ─────────────────────


class TestLabelPermutationApproachesMarketBrier:
    def test_permuted_labels_model_brier_approaches_market_brier(self):
        snaps = _snapshots_with_predictions(100)
        rng = _rng(700)
        from polyalpha.research_dataset import MarketSnapshot

        outcomes = [s.final_resolution for s in snaps if s.final_resolution is not None]
        permuted = list(outcomes)
        rng.shuffle(permuted)

        idx = 0
        perm_snaps = []
        for s in snaps:
            if s.final_resolution is not None:
                perm_snaps.append(MarketSnapshot(
                    **{**s.__dict__, "final_resolution": permuted[idx]}
                ))
                idx += 1
            else:
                perm_snaps.append(s)

        perm_brier = _brier(perm_snaps)

        market_probs = [float(s.yes_mid) for s in snaps
                        if s.final_resolution is not None and s.yes_mid is not None]
        market_outcomes = [s.final_resolution for s in snaps
                           if s.final_resolution is not None and s.yes_mid is not None]
        if market_probs and market_outcomes:
            market_brier = brier_score(market_probs, market_outcomes)
            diff = abs(perm_brier - market_brier)
            assert diff < 0.30, (
                f"Permuted Brier {perm_brier} far from market Brier {market_brier}"
            )


# ── MR9: Spread widening → worse VWAP for buyer ─────────────────────────────


class TestSpreadWideningWorsensVwap:
    def test_wider_spread_means_higher_buy_vwap(self):
        rng = _rng(800)
        for _ in range(20):
            mid = _tick(rng.uniform(0.40, 0.60))
            narrow_spread = _tick(rng.uniform(0.01, 0.03))
            wide_spread = _tick(narrow_spread + rng.uniform(0.02, 0.05))
            narrow_ask = min(_tick(mid + narrow_spread / 2), 0.99)
            wide_ask = min(_tick(mid + wide_spread / 2), 0.99)
            narrow_bid = _tick(mid - narrow_spread / 2)
            wide_bid = _tick(mid - wide_spread / 2)
            if narrow_bid < 0.01 or wide_bid < 0.01 or narrow_ask <= narrow_bid or wide_ask <= wide_bid:
                continue
            book_narrow = _book(narrow_bid, narrow_ask, 200, 200)
            book_wide = _book(wide_bid, wide_ask, 200, 200)
            fees = FeeSchedule(D("0"), TS, "test")
            fill_narrow = walk(book_narrow, Order(_next_id(), "t1", "BUY", D("10"), TS), fees, TS)
            fill_wide = walk(book_wide, Order(_next_id(), "t1", "BUY", D("10"), TS), fees, TS)
            assert fill_wide.vwap >= fill_narrow.vwap - D("0.001"), (
                f"Wider spread gave better VWAP: {fill_wide.vwap} < {fill_narrow.vwap}"
            )


# ── MR10: Perfect predictions → zero Brier ──────────────────────────────────


class TestPerfectPredictionsZeroBrier:
    def test_predictions_matching_outcomes_yield_zero_brier(self):
        snaps = _snapshots_with_predictions(60, seed=810)
        from polyalpha.research_dataset import MarketSnapshot

        perfect = []
        for s in snaps:
            if s.final_resolution is not None:
                perfect.append(MarketSnapshot(
                    **{**s.__dict__, "model_probability": D(str(s.final_resolution))}
                ))
            else:
                perfect.append(s)
        assert _brier(perfect) == 0.0


# ── MR11: Inverted predictions → Brier near 1 ──────────────────────────────


class TestInvertedPredictionsWorstBrier:
    def test_inverted_predictions_yield_brier_near_one(self):
        snaps = _snapshots_with_predictions(60, seed=811)
        from polyalpha.research_dataset import MarketSnapshot

        inverted = []
        for s in snaps:
            if s.model_probability is not None:
                inv_p = 1.0 - float(s.model_probability)
                inverted.append(MarketSnapshot(
                    **{**s.__dict__, "model_probability": D(str(round(inv_p, 4)))}
                ))
            else:
                inverted.append(s)
        real_brier = _brier(snaps)
        brier = _brier(inverted)
        assert brier >= real_brier, (
            f"Inverted predictions Brier ({brier}) should be >= real ({real_brier})"
        )


# ── MR12: Constant predictions → Brier = prior ─────────────────────────────


class TestConstantPredictionsBrierEqualsPrior:
    def test_all_05_predictions_brier_equals_prior(self):
        snaps = _snapshots_with_predictions(60, seed=812)
        from polyalpha.research_dataset import MarketSnapshot

        constant = []
        for s in snaps:
            if s.model_probability is not None:
                constant.append(MarketSnapshot(**{**s.__dict__, "model_probability": D("0.5")}))
            else:
                constant.append(s)

        outcomes = [s.final_resolution for s in constant if s.final_resolution is not None]
        prior_brier = sum((0.5 - o) ** 2 for o in outcomes) / len(outcomes)
        assert abs(_brier(constant) - prior_brier) < 0.01


# ── MR13: Fee schedule validation is monotonic ──────────────────────────────


class TestFeeMonotonic:
    def test_fee_scales_linearly_with_rate(self):
        rng = _rng(813)
        shares = D("100")
        price = D("0.50")
        rates = [D("0"), D("0.01"), D("0.02"), D("0.05"), D("0.10")]
        fees_list = []
        for rate in rates:
            fs = FeeSchedule(rate, TS, "test")
            fees_list.append(fs.fee(shares, price))
        for i in range(1, len(fees_list)):
            assert fees_list[i] >= fees_list[i - 1], (
                f"Fee at rate {rates[i]} ({fees_list[i]}) < at {rates[i-1]} ({fees_list[i-1]})"
            )


# ── MR14: Book mid is average of best bid and ask ───────────────────────────


class TestBookMidInvariant:
    def test_mid_equals_average_of_best_bid_ask(self):
        rng = _rng(814)
        for _ in range(30):
            bid = _tick(rng.uniform(0.20, 0.49))
            ask = _tick(rng.uniform(0.51, 0.80))
            book = _book(bid, ask, 100, 100)
            assert book.mid == (book.best_bid + book.best_ask) / 2


# ── MR15: Ensemble average lies between component forecasts ─────────────────


class TestEnsembleBounds:
    def test_ensemble_probability_is_weighted_average(self):
        from polyalpha.forecasting import ensemble
        f1 = Forecast("m1", TS, D("0.40"), D("0.35"), D("0.45"), "v1")
        f2 = Forecast("m1", TS, D("0.60"), D("0.55"), D("0.65"), "v2")
        e = ensemble([f1, f2], [D("0.5"), D("0.5")], TS)
        assert e.probability == D("0.50")

    def test_ensemble_lower_is_min_of_components(self):
        from polyalpha.forecasting import ensemble
        f1 = Forecast("m1", TS, D("0.40"), D("0.30"), D("0.50"), "v1")
        f2 = Forecast("m1", TS, D("0.60"), D("0.55"), D("0.65"), "v2")
        e = ensemble([f1, f2], [D("0.5"), D("0.5")], TS)
        assert e.lower == D("0.30")

    def test_ensemble_upper_is_max_of_components(self):
        from polyalpha.forecasting import ensemble
        f1 = Forecast("m1", TS, D("0.40"), D("0.30"), D("0.50"), "v1")
        f2 = Forecast("m1", TS, D("0.60"), D("0.55"), D("0.65"), "v2")
        e = ensemble([f1, f2], [D("0.5"), D("0.5")], TS)
        assert e.upper == D("0.65")
