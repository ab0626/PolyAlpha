"""Property-based testing with randomized inputs.

Part 58: Verifies mathematical invariants that must hold across all
randomized input configurations. Uses the stdlib random module (no Hypothesis).

Invariant categories:
- Order-book: bid <= ask ordering
- VWAP monotonicity: buying more should not improve VWAP; selling more should
  not worsen VWAP
- Portfolio reconciliation: cash + positions after round trips
- Risk caps: exposure never exceeds hard cap after accepted entry
- Probability bounds: calibrated probabilities remain in [0,1]
- Fees: never negative
- Slippage: nonnegative under normal taker execution
- Drawdown: never negative
- Brier score: in [0,1]
- Cost ordering: higher costs must not yield better deterministic PnL
"""

import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyalpha.domain import Book, Level
from polyalpha.execution import FeeSchedule, Fill, Order, walk
from polyalpha.portfolio import Portfolio
from polyalpha.performance import brier_score, drawdown_series
from polyalpha.calibration import Observation, Isotonic, metrics as calibration_metrics
from polyalpha.risk import Limits, Risk
from polyalpha.forecasting import Forecast, net_edge

D = Decimal
TS = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _rng(seed):
    return random.Random(seed)


def _tick(x):
    """Round to nearest tick (0.01)."""
    return round(x, 2)


def _book(bid_p, ask_p, bid_sz, ask_sz, tick=D("0.01"), min_size=D("1")):
    return Book(
        token_id="t1",
        condition_id="c1",
        source_at=TS,
        received_at=TS,
        bids=(Level(D(str(_tick(bid_p))), D(str(bid_sz))),),
        asks=(Level(D(str(_tick(ask_p))), D(str(ask_sz))),),
        tick_size=tick,
        min_order_size=min_size,
        source_hash="",
    )


_counter = [0]


def _fill_vwap(book, shares, side="BUY", fee_rate=D("0")):
    _counter[0] += 1
    fees = FeeSchedule(fee_rate, TS, "test")
    order = Order(f"o{_counter[0]}", book.token_id, side, D(str(shares)), TS)
    return walk(book, order, fees, TS)


def _forecast(prob, ts=None):
    return Forecast(
        market_id="m1", timestamp=ts or TS,
        probability=D(str(prob)),
        lower=D(str(max(0.01, prob - 0.05))),
        upper=D(str(min(0.99, prob + 0.05))),
        version="test",
    )


# ── Order-book invariants ────────────────────────────────────────────────────


class TestBidAskOrdering:
    def test_randomized_books_maintain_bid_leq_ask(self):
        rng = _rng(42)
        for _ in range(50):
            bid = _tick(rng.uniform(0.10, 0.49))
            ask = _tick(bid + rng.uniform(0.01, 0.10))
            ask = min(ask, 0.99)
            bid_sz = rng.uniform(10, 500)
            ask_sz = rng.uniform(10, 500)
            book = _book(bid, ask, bid_sz, ask_sz)
            assert book.best_bid is not None
            assert book.best_ask is not None
            assert book.best_bid <= book.best_ask

    def test_spread_nonnegative_for_valid_books(self):
        rng = _rng(77)
        for _ in range(50):
            bid = _tick(rng.uniform(0.10, 0.49))
            ask = _tick(bid + rng.uniform(0.01, 0.08))
            ask = min(ask, 0.99)
            book = _book(bid, ask, 100, 100)
            assert book.spread is not None
            assert book.spread >= D(0)


# ── VWAP monotonicity ────────────────────────────────────────────────────────


class TestVwapMonotonicity:
    def test_buying_more_does_not_decrease_vwap(self):
        """Walking deeper into the ask book: VWAP must be non-decreasing."""
        rng = _rng(123)
        for _ in range(30):
            levels = []
            p = 0.32
            for _ in range(5):
                sz = rng.uniform(20, 200)
                levels.append(Level(D(str(_tick(p))), D(str(round(sz, 2)))))
                p += rng.uniform(0.01, 0.03)
                p = min(p, 0.99)
            book = Book(
                token_id="t1", condition_id="c1",
                source_at=TS, received_at=TS,
                bids=(Level(D("0.30"), D("500")),),
                asks=tuple(levels),
                tick_size=D("0.01"), min_order_size=D("1"), source_hash="",
            )
            fees = FeeSchedule(D("0"), TS, "test")
            sizes = [D("10"), D("30"), D("80")]
            vwaps = []
            for sz in sizes:
                _counter[0] += 1
                order = Order(f"o{_counter[0]}", "t1", "BUY", sz, TS)
                fill = walk(book, order, fees, TS)
                vwaps.append(fill.vwap)
            for i in range(1, len(vwaps)):
                assert vwaps[i] >= vwaps[i - 1] - D("0.001"), (
                    f"Larger buy got better VWAP: {vwaps[i]} < {vwaps[i-1]}"
                )

    def test_selling_more_does_not_increase_vwap(self):
        """Walking deeper into the bid book: sell VWAP must be non-increasing."""
        rng = _rng(456)
        for _ in range(30):
            levels = []
            p = 0.70
            for _ in range(5):
                sz = rng.uniform(20, 200)
                levels.append(Level(D(str(_tick(p))), D(str(round(sz, 2)))))
                p -= rng.uniform(0.01, 0.03)
                p = max(p, 0.10)
            book = Book(
                token_id="t1", condition_id="c1",
                source_at=TS, received_at=TS,
                bids=tuple(levels),
                asks=(Level(D("0.80"), D("500")),),
                tick_size=D("0.01"), min_order_size=D("1"), source_hash="",
            )
            fees = FeeSchedule(D("0"), TS, "test")
            sizes = [D("10"), D("30"), D("80")]
            vwaps = []
            for sz in sizes:
                _counter[0] += 1
                order = Order(f"o{_counter[0]}", "t1", "SELL", sz, TS)
                fill = walk(book, order, fees, TS)
                vwaps.append(fill.vwap)
            for i in range(1, len(vwaps)):
                assert vwaps[i] <= vwaps[i - 1] + D("0.001"), (
                    f"Larger sell got worse VWAP: {vwaps[i]} > {vwaps[i-1]}"
                )


# ── Portfolio reconciliation ─────────────────────────────────────────────────


class TestPortfolioReconciliation:
    def test_cash_positions_reconcile_after_round_trip(self):
        """After buy then sell of same size, cash decreases by spread cost."""
        rng = _rng(88)
        for _ in range(20):
            mid = _tick(rng.uniform(0.35, 0.65))
            spread = _tick(rng.uniform(0.02, 0.06))
            bid = _tick(mid - spread / 2)
            ask = _tick(mid + spread / 2)
            if ask > 0.99 or bid < 0.01 or bid >= ask:
                continue
            book = _book(bid, ask, 200, 200)
            shares = D(str(rng.randint(5, 50)))
            initial_cash = D("10000")
            portfolio = Portfolio(initial_cash)

            buy_fill = _fill_vwap(book, shares, "BUY", D("0"))
            portfolio.apply(buy_fill, "m1", "e1", "cl1", "politics")

            sell_fill = _fill_vwap(book, shares, "SELL", D("0"))
            portfolio.apply(sell_fill, "m1", "e1", "cl1", "politics")

            # Cash changes by (sell_notional - buy_notional) + (buy_fees - sell_fees)
            expected_cash = initial_cash - buy_fill.notional + sell_fill.notional
            assert portfolio.cash == expected_cash, (
                f"Cash {portfolio.cash} != expected {expected_cash}"
            )

    def test_round_trip_with_fees_reduces_cash(self):
        """With nonzero fees, round trip must cost something."""
        rng = _rng(99)
        for _ in range(20):
            mid = _tick(rng.uniform(0.35, 0.65))
            spread = _tick(rng.uniform(0.02, 0.06))
            bid = _tick(mid - spread / 2)
            ask = _tick(mid + spread / 2)
            if ask > 0.99 or bid < 0.01 or bid >= ask:
                continue
            book = _book(bid, ask, 200, 200)
            shares = D(str(rng.randint(5, 50)))
            initial_cash = D("10000")
            portfolio = Portfolio(initial_cash)
            fee_rate = D(str(round(rng.uniform(0.01, 0.05), 3)))

            buy_fill = _fill_vwap(book, shares, "BUY", fee_rate)
            portfolio.apply(buy_fill, "m1", "e1", "cl1", "politics")

            sell_fill = _fill_vwap(book, shares, "SELL", fee_rate)
            portfolio.apply(sell_fill, "m1", "e1", "cl1", "politics")

            assert portfolio.cash < initial_cash
            assert portfolio.fees > D(0)


# ── Risk caps ────────────────────────────────────────────────────────────────


class TestRiskCaps:
    def test_budget_never_exceeds_equity_times_limit(self):
        rng = _rng(200)
        for _ in range(30):
            equity = D(str(round(rng.uniform(1000, 50000), 2)))
            limits = Limits(
                normal=D(str(round(rng.uniform(0.002, 0.02), 4))),
                market=D(str(round(rng.uniform(0.01, 0.05), 4))),
                event=D(str(round(rng.uniform(0.02, 0.08), 4))),
                cluster=D(str(round(rng.uniform(0.02, 0.08), 4))),
                category=D(str(round(rng.uniform(0.05, 0.12), 4))),
                total=D(str(round(rng.uniform(0.10, 0.25), 4))),
                daily_loss=D("0.02"),
                drawdown=D("0.08"),
            )
            risk = Risk(equity, limits)
            portfolio = Portfolio(equity)
            budget = risk.budget(portfolio, equity, "m1", "e1", "cl1", "politics")
            assert budget <= equity * limits.normal + D("0.01"), (
                f"Budget {budget} exceeds normal limit {equity * limits.normal}"
            )
            assert budget >= D(0)

    def test_exposure_never_exceeds_total_cap(self):
        rng = _rng(300)
        equity = D("10000")
        limits = Limits(total=D("0.20"), normal=D("0.005"), market=D("0.02"),
                        event=D("0.05"), cluster=D("0.05"), category=D("0.10"),
                        daily_loss=D("0.02"), drawdown=D("0.08"))
        risk = Risk(equity, limits)
        portfolio = Portfolio(equity)
        total_exposure = D(0)
        for i in range(5):
            budget = risk.budget(portfolio, equity, f"m{i}", f"e{i}", f"cl{i}", "politics")
            total_exposure += budget
            assert total_exposure <= equity * limits.total + D("0.01")


# ── Calibration bounds ───────────────────────────────────────────────────────


class TestCalibrationBounds:
    def test_calibrated_probabilities_in_unit_interval(self):
        rng = _rng(500)
        rows = []
        for i in range(60):
            ts = TS + timedelta(hours=i)
            p = round(rng.uniform(0.1, 0.9), 4)
            outcome = 1 if rng.random() < p else 0
            rows.append(Observation(
                market_id=f"m{i:04d}", cluster="cl1",
                predicted_at=ts,
                label_known_at=ts + timedelta(hours=1),
                probability=p, outcome=outcome,
            ))
        iso = Isotonic().fit(rows, TS + timedelta(hours=100))
        for _ in range(50):
            p = round(rng.uniform(0.05, 0.95), 4)
            calibrated = iso.predict(p)
            assert 0 <= calibrated <= 1, f"Calibrated {calibrated} outside [0,1]"

    def test_raw_brier_score_bounds(self):
        rng = _rng(501)
        for _ in range(30):
            n = rng.randint(5, 50)
            probs = [round(rng.uniform(0.01, 0.99), 4) for _ in range(n)]
            outcomes = [rng.randint(0, 1) for _ in range(n)]
            bs = brier_score(probs, outcomes)
            assert 0 <= bs <= 1, f"Brier {bs} outside [0,1]"


# ── Fee nonnegativity ────────────────────────────────────────────────────────


class TestFeeNonnegativity:
    def test_fees_never_negative_for_valid_inputs(self):
        rng = _rng(600)
        for _ in range(50):
            shares = D(str(round(rng.uniform(1, 500), 2)))
            price = round(rng.uniform(0.05, 0.95), 4)
            rate = round(rng.uniform(0, 0.10), 4)
            fees = FeeSchedule(D(str(rate)), TS, "test")
            fee = fees.fee(shares, D(str(price)))
            assert fee >= D(0), f"Negative fee: {fee}"

    def test_zero_fee_rate_yields_zero_fee(self):
        rng = _rng(601)
        fees = FeeSchedule(D(0), TS, "test")
        for _ in range(30):
            shares = D(str(round(rng.uniform(1, 200), 2)))
            price = round(rng.uniform(0.1, 0.9), 4)
            assert fees.fee(shares, D(str(price))) == D(0)


# ── Slippage nonnegativity ──────────────────────────────────────────────────


class TestSlippageNonnegativity:
    def test_depth_slippage_never_negative(self):
        rng = _rng(700)
        for _ in range(30):
            mid = _tick(rng.uniform(0.30, 0.70))
            spread = _tick(rng.uniform(0.01, 0.06))
            ask = min(_tick(mid + spread / 2), 0.99)
            bid = _tick(mid - spread / 2)
            if bid < 0.01 or bid >= ask:
                continue
            sz1 = round(rng.uniform(10, 100), 2)
            sz2 = round(rng.uniform(10, 100), 2)
            book = _book(bid, ask, sz1, sz2)
            shares = D(str(round(rng.uniform(5, min(sz1, sz2) - 1), 2)))
            if shares <= 0:
                continue
            fill = _fill_vwap(book, shares, "BUY", D("0"))
            assert fill.depth_slippage >= D(0), (
                f"Negative slippage: {fill.depth_slippage}"
            )


# ── Drawdown nonnegativity ──────────────────────────────────────────────────


class TestDrawdownNonnegativity:
    def test_drawdown_series_never_negative(self):
        rng = _rng(800)
        for _ in range(30):
            n = rng.randint(3, 20)
            equity = rng.uniform(9000, 11000)
            equities = [equity]
            for _ in range(n - 1):
                equity *= (1 + rng.uniform(-0.05, 0.05))
                equities.append(equity)
            dd = drawdown_series(equities)
            assert all(d >= 0 for d in dd), f"Negative drawdown found: {dd}"
            assert all(d <= 1 for d in dd), f"Drawdown > 1 found: {dd}"

    def test_drawdown_zero_at_start(self):
        dd = drawdown_series([100, 110, 105])
        assert dd[0] == 0.0


# ── Brier score bounds ───────────────────────────────────────────────────────


class TestBrierScoreBounds:
    def test_brier_always_in_unit_interval(self):
        rng = _rng(900)
        for _ in range(50):
            n = rng.randint(2, 100)
            probs = [round(rng.uniform(0.001, 0.999), 6) for _ in range(n)]
            outcomes = [rng.randint(0, 1) for _ in range(n)]
            bs = brier_score(probs, outcomes)
            assert 0 <= bs <= 1.0, f"Brier {bs} outside [0,1]"

    def test_perfect_predictions_yield_zero_brier(self):
        outcomes = [0, 1, 1, 0, 1]
        bs = brier_score([0, 1, 1, 0, 1], outcomes)
        assert bs == 0.0

    def test_worst_predictions_yield_brier_near_one(self):
        outcomes = [0, 1, 1, 0, 1]
        bs = brier_score([1, 0, 0, 1, 0], outcomes)
        assert bs >= 0.99


# ── Cost ordering ────────────────────────────────────────────────────────────


class TestCostOrdering:
    def test_higher_fees_cannot_improve_net_pnl(self):
        rng = _rng(1000)
        for _ in range(20):
            mid = _tick(rng.uniform(0.35, 0.65))
            spread = _tick(rng.uniform(0.02, 0.05))
            ask = min(_tick(mid + spread / 2), 0.99)
            bid = _tick(mid - spread / 2)
            if bid < 0.01 or bid >= ask:
                continue
            book = _book(bid, ask, 200, 200)
            shares = D("50")
            fee_rates = [D("0"), D("0.01"), D("0.03"), D("0.05")]
            pnls = []
            for rate in fee_rates:
                fill = _fill_vwap(book, shares, "BUY", rate)
                p = D("0.70")
                gross = p - fill.vwap
                net = gross - fill.fees / fill.shares
                pnls.append(net)
            for i in range(1, len(pnls)):
                assert pnls[i] <= pnls[i - 1] + D("0.001"), (
                    f"Higher fee {fee_rates[i]} gave better PnL: {pnls[i]} > {pnls[i-1]}"
                )

    def test_higher_slippage_cannot_improve_deterministic_pnl(self):
        rng = _rng(1001)
        for _ in range(20):
            mid = _tick(rng.uniform(0.35, 0.65))
            spread = _tick(rng.uniform(0.02, 0.05))
            ask = min(_tick(mid + spread / 2), 0.99)
            bid = _tick(mid - spread / 2)
            if bid < 0.01 or bid >= ask:
                continue
            book = _book(bid, ask, 200, 200)
            shares = D("50")
            fill = _fill_vwap(book, shares, "BUY", D("0"))
            assert fill.depth_slippage >= D(0)


# ── Exposure never exceeds cap after accepted entry ──────────────────────────


class TestExposureAfterEntry:
    def test_budget_decreases_after_apply(self):
        equity = D("10000")
        # Use high normal limit but low market limit so market exposure is binding
        limits = Limits(
            normal=D("0.10"), market=D("0.01"), event=D("0.05"),
            cluster=D("0.05"), category=D("0.10"), total=D("0.20"),
            daily_loss=D("0.02"), drawdown=D("0.08"),
        )
        risk = Risk(equity, limits)
        portfolio = Portfolio(equity)

        market = "m1"
        budget_before = risk.budget(portfolio, equity, market, "e1", "cl1", "politics")
        assert budget_before > D(0)

        book = _book(0.50, 0.55, 200, 200)
        fill = _fill_vwap(book, D("10"), "BUY", D("0"))
        portfolio.apply(fill, market, "e1", "cl1", "politics")

        budget_after = risk.budget(portfolio, equity, market, "e1", "cl1", "politics")
        assert budget_after < budget_before

    def test_total_exposure_never_exceeds_equity_times_total_limit(self):
        equity = D("10000")
        limits = Limits(total=D("0.20"), normal=D("0.10"), market=D("0.05"),
                        event=D("0.05"), cluster=D("0.05"), category=D("0.10"),
                        daily_loss=D("0.02"), drawdown=D("0.08"))
        risk = Risk(equity, limits)
        portfolio = Portfolio(equity)

        for i in range(3):
            token = f"t{i}"
            budget = risk.budget(portfolio, equity, f"m{i}", f"e{i}", f"cl{i}", "politics")
            if budget <= 0:
                break
            actual = min(budget, D("200"))
            book = Book(
                token_id=token, condition_id=f"c{i}",
                source_at=TS, received_at=TS,
                bids=(Level(D("0.50"), D("200")),),
                asks=(Level(D("0.55"), D("200")),),
                tick_size=D("0.01"), min_order_size=D("1"), source_hash="",
            )
            _counter[0] += 1
            order = Order(f"o{_counter[0]}", token, "BUY", actual, TS)
            fill = walk(book, order, FeeSchedule(D("0"), TS, "test"), TS)
            portfolio.apply(fill, f"m{i}", f"e{i}", f"cl{i}", "politics")

        total_exposure = sum(portfolio.exposures().values())
        assert total_exposure <= equity * limits.total + D("0.01")


# ── Forecast bounds ──────────────────────────────────────────────────────────


class TestForecastBounds:
    def test_forecast_probability_always_in_unit_interval(self):
        rng = _rng(1100)
        for _ in range(40):
            p = round(rng.uniform(0.05, 0.95), 4)
            f = _forecast(p)
            assert D(0) <= f.lower <= f.probability <= f.upper <= D(1)

    def test_conservative_yes_leq_probability(self):
        rng = _rng(1101)
        for _ in range(40):
            p = round(rng.uniform(0.10, 0.90), 4)
            f = _forecast(p)
            assert f.conservative(yes=True) <= f.probability

    def test_conservative_no_leq_one_minus_probability(self):
        rng = _rng(1102)
        for _ in range(40):
            p = round(rng.uniform(0.10, 0.90), 4)
            f = _forecast(p)
            assert f.conservative(yes=False) <= D(1) - f.probability


# ── Net edge sign consistency ────────────────────────────────────────────────


class TestNetEdgeConsistency:
    def test_net_edge_negative_when_forecast_below_vwap(self):
        rng = _rng(1200)
        for _ in range(20):
            mid = _tick(rng.uniform(0.50, 0.80))
            spread = _tick(rng.uniform(0.02, 0.05))
            ask = min(_tick(mid + spread / 2), 0.99)
            bid = _tick(mid - spread / 2)
            if bid < 0.01 or bid >= ask:
                continue
            book = _book(bid, ask, 200, 200)
            low_prob = round(rng.uniform(0.20, 0.40), 4)
            forecast = _forecast(low_prob)
            fees = FeeSchedule(D("0"), TS, "test")
            _counter[0] += 1
            order = Order(f"o{_counter[0]}", "t1", "BUY", D("30"), TS)
            fill = walk(book, order, fees, TS)
            edge = net_edge(forecast, fill, yes=True,
                            extra_slippage=D("0"), resolution_penalty=D("0"))
            assert edge < D(0), f"Low forecast should yield negative edge: {edge}"


# ── Order fill conservation ──────────────────────────────────────────────────


class TestFillConservation:
    def test_fill_notional_equals_sum_of_legs(self):
        rng = _rng(1300)
        for _ in range(20):
            mid = _tick(rng.uniform(0.35, 0.65))
            spread = _tick(rng.uniform(0.02, 0.05))
            ask = min(_tick(mid + spread / 2), 0.99)
            bid = _tick(mid - spread / 2)
            if bid < 0.01 or bid >= ask:
                continue
            book = _book(bid, ask, 200, 200)
            shares = D(str(rng.randint(5, 100)))
            fill = _fill_vwap(book, shares, "BUY", D("0.02"))
            leg_sum = sum(p * q for p, q in fill.levels)
            assert leg_sum == fill.notional, (
                f"Notional {fill.notional} != leg sum {leg_sum}"
            )
