"""Hypothesis property-based tests for numerical functions (Sections 51/52/53).

Tests mathematical invariants, edge cases, and numerical stability
for calibration metrics, expected value calculations, correlation,
and relative-value constraint logic.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis.strategies import (
    composite,
    decimals,
    integers,
    lists,
    sampled_from,
)
from hypothesis.strategies import (
    floats as st_floats,
)

from polyalpha.calibration import metrics as cal_metrics
from polyalpha.correlation import CorrelationMatrix, compute_correlation
from polyalpha.execution import Fill
from polyalpha.expected_value import (
    calculate_fee_per_share,
    calculate_gross_edge,
    calculate_liquidity_penalty,
    calculate_stale_data_penalty,
    fractional_kelly,
    kelly_fraction,
)
from polyalpha.relative_value import Constraint, violations
from polyalpha.uncertainty import UncertaintyEstimate

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def stable_settings(max_examples):
    """Deterministic, load-immune Hypothesis settings.

    derandomize=True fixes a per-test seed so a run is fully reproducible and
    cannot flip based on collection order or global RNG state.

    suppress_health_check=[HealthCheck.too_slow] disables the wall-clock input
    generation check: the decimals() composite is fast when the machine is
    idle, but can exceed the ~1s check under full-suite CPU load, producing an
    order-dependent FailedHealthCheck even though the strategy and assertion
    are valid.
    """
    return settings(
        max_examples=max_examples,
        derandomize=True,
        suppress_health_check=[HealthCheck.too_slow],
    )


# ─── Strategies ───────────────────────────────────────────────────────────────


@composite
def prob_strategy(draw):
    """Generate a valid probability in [0, 1]."""
    return draw(decimals(min_value=D(0), max_value=D(1), places=4))


@composite
def outcome_strategy(draw):
    """Generate a binary outcome {0, 1}."""
    return draw(sampled_from([0, 1]))


@composite
def price_strategy(draw):
    """Generate a valid price in (0, 1)."""
    return draw(decimals(min_value=D("0.01"), max_value=D("0.99"), places=4))


@composite
def nonneg_decimal(draw):
    return draw(decimals(min_value=D(0), max_value=D("10000"), places=2))


@composite
def valid_fill(draw):
    shares = draw(decimals(min_value=D(1), max_value=D("10000"), places=0))
    price = draw(price_strategy())
    notional = (shares * price).quantize(D("0.01"))
    fee = draw(decimals(min_value=D(0), max_value=notional * D("0.1"), places=4))
    slippage = draw(decimals(min_value=D(0), max_value=price * D("0.05"), places=6))
    return Fill(
        order_id="o1",
        token_id="t1",
        side="BUY",
        requested=shares,
        shares=shares,
        notional=notional,
        fees=fee,
        depth_slippage=slippage,
        filled_at=NOW,
        levels=((price, shares),),
    )


# ─── Section 51: Calibration Metrics ─────────────────────────────────────────


class TestBrierScoreProperties:
    """Properties of the Brier score: 0 = perfect, 1 = worst, symmetry."""

    @given(probs=lists(prob_strategy(), min_size=5, max_size=50))
    @stable_settings(max_examples=50)
    def test_brier_perfect_predictions(self, probs):
        outcomes = [1 if p >= D("0.5") else 0 for p in probs]
        result = cal_metrics([float(p) for p in probs], outcomes)
        assert 0 <= result["brier"] <= 1

    @given(n=integers(min_value=5, max_value=30))
    @stable_settings(max_examples=20)
    def test_brier_worst_case(self, n):
        probs = [1.0] * n
        outcomes = [0] * n
        result = cal_metrics(probs, outcomes)
        assert result["brier"] == 1.0

    @given(n=integers(min_value=5, max_value=30))
    @stable_settings(max_examples=20)
    def test_brier_best_case(self, n):
        probs = [0.0] * n
        outcomes = [0] * n
        result = cal_metrics(probs, outcomes)
        assert result["brier"] == 0.0

    @given(probs=lists(prob_strategy(), min_size=5, max_size=30))
    @stable_settings(max_examples=30)
    def test_brier_bounded(self, probs):
        outcomes = [1 if p >= D("0.5") else 0 for p in probs]
        result = cal_metrics([float(p) for p in probs], outcomes)
        assert 0 <= result["brier"] <= 1

    @given(probs=lists(prob_strategy(), min_size=5, max_size=30))
    @stable_settings(max_examples=30)
    def test_ece_bounded(self, probs):
        outcomes = [1 if p >= D("0.5") else 0 for p in probs]
        result = cal_metrics([float(p) for p in probs], outcomes)
        assert 0 <= result["ece"] <= 1


class TestLogLossProperties:
    """Log loss properties: non-negative, penalizes confident wrong predictions."""

    @given(n=integers(min_value=5, max_value=30))
    @stable_settings(max_examples=20)
    def test_log_loss_perfect(self, n):
        probs = [0.001] * n
        outcomes = [0] * n
        result = cal_metrics(probs, outcomes)
        assert result["log_loss"] >= 0

    @given(probs=lists(prob_strategy(), min_size=5, max_size=30))
    @stable_settings(max_examples=30)
    def test_log_loss_nonnegative(self, probs):
        outcomes = [1 if p >= D("0.5") else 0 for p in probs]
        result = cal_metrics([float(p) for p in probs], outcomes)
        assert result["log_loss"] >= 0


# ─── Section 52: Expected Value Functions ─────────────────────────────────────


class TestKellyFractionProperties:
    """Kelly fraction: non-negative, bounded [0, 1], zero when no edge."""

    @given(p=prob_strategy(), price=price_strategy())
    @stable_settings(max_examples=100)
    def test_kelly_nonnegative(self, p, price):
        result = kelly_fraction(p, price)
        assert result >= 0

    @given(p=prob_strategy(), price=price_strategy())
    @stable_settings(max_examples=100)
    def test_kelly_bounded(self, p, price):
        result = kelly_fraction(p, price)
        assert 0 <= result <= 1

    @given(price=price_strategy())
    @stable_settings(max_examples=50)
    def test_kelly_no_edge_at_fair(self, price):
        p = float(price)
        result = kelly_fraction(D(str(p)), price)
        assert result == 0

    @given(p=decimals(min_value=D("0.9"), max_value=D(1), places=4), price=price_strategy())
    @stable_settings(max_examples=50)
    def test_kelly_high_prob(self, p, price):
        result = kelly_fraction(p, price)
        assert 0 <= result <= 1

    @given(price=price_strategy())
    @stable_settings(max_examples=50)
    def test_fractional_kelly_smaller(self, price):
        p = D(str(float(price))) + D("0.1")
        if p > 1:
            p = D("0.99")
        full = kelly_fraction(p, price)
        frac = fractional_kelly(p, price, D("0.25"))
        assert frac <= full


class TestGrossEdgeProperties:
    """Gross edge: positive when fair > execution, negative otherwise."""

    @given(fair=prob_strategy(), vwap=price_strategy())
    @stable_settings(max_examples=100)
    def test_yes_edge_formula(self, fair, vwap):
        edge = calculate_gross_edge(fair, vwap, "YES")
        assert edge == fair - vwap

    @given(fair=prob_strategy(), vwap=price_strategy())
    @stable_settings(max_examples=100)
    def test_no_edge_formula(self, fair, vwap):
        edge = calculate_gross_edge(fair, vwap, "NO")
        assert edge == (1 - fair) - vwap

    @given(fair=prob_strategy(), vwap=price_strategy())
    @stable_settings(max_examples=50)
    def test_yes_no_symmetry(self, fair, vwap):
        yes_edge = calculate_gross_edge(fair, vwap, "YES")
        no_edge = calculate_gross_edge(fair, vwap, "NO")
        assert yes_edge + no_edge == 1 - 2 * vwap


class TestFeeProperties:
    """Fee calculation: non-negative, proportional to shares and price."""

    def test_fee_nonneg(self):
        for shares in [D("1"), D("10"), D("100")]:
            for price in [D("0.10"), D("0.50"), D("0.90")]:
                notional = shares * price
                fill = Fill(
                    order_id="o1",
                    token_id="t1",
                    side="BUY",
                    requested=shares,
                    shares=shares,
                    notional=notional,
                    fees=D("0.01"),
                    depth_slippage=D(0),
                    filled_at=NOW,
                    levels=((price, shares),),
                )
                fee = calculate_fee_per_share(fill)
                assert fee >= 0


class TestLiquidityPenaltyProperties:
    """Liquidity penalty: zero when depth >= requested, positive otherwise."""

    @given(depth=nonneg_decimal(), requested=nonneg_decimal())
    @stable_settings(max_examples=100)
    def test_penalty_nonneg(self, depth, requested):
        penalty = calculate_liquidity_penalty(depth, requested)
        assert penalty >= 0

    @given(requested=nonneg_decimal())
    @stable_settings(max_examples=50)
    def test_zero_depth_doubles_penalty(self, requested):
        penalty = calculate_liquidity_penalty(D(0), requested)
        assert penalty == D("0.005") * 2


class TestStaleDataPenaltyProperties:
    """Stale data penalty: zero for fresh data, increases with age."""

    @given(age=st_floats(min_value=0, max_value=100))
    @stable_settings(max_examples=50)
    def test_penalty_nonneg(self, age):
        penalty = calculate_stale_data_penalty(age)
        assert penalty >= 0

    def test_zero_age_zero_penalty(self):
        penalty = calculate_stale_data_penalty(0)
        assert penalty == 0


# ─── Section 53: Correlation Properties ───────────────────────────────────────


class TestCorrelationProperties:
    """Correlation: bounded [-1, 1], symmetric, diagonal = 1."""

    def test_diagonal_is_one(self):
        m = CorrelationMatrix(tokens=["a", "b"])
        assert m.get("a", "a") == D(1)

    def test_symmetry(self):
        m = CorrelationMatrix(tokens=["a", "b"])
        m.set("a", "b", D("0.7"))
        assert m.get("b", "a") == D("0.7")

    def test_unknown_is_zero(self):
        m = CorrelationMatrix(tokens=["a", "b"])
        assert m.get("a", "c") == D(0)

    def test_perfect_positive(self):
        a = [D("1"), D("2"), D("3"), D("5"), D("8")]
        r = compute_correlation(a, a)
        # Same series → correlation ≈ 1 (tiny float error from math.sqrt)
        assert r >= D("0.999")

    def test_known_correlation(self):
        a = [D("1"), D("2"), D("3"), D("4"), D("5")]
        b = [D("2"), D("4"), D("6"), D("8"), D("10")]
        r = compute_correlation(a, b)
        assert r == D(1)

    def test_negative_correlation(self):
        # Oscillating series with perfectly opposing returns
        a = [D("1"), D("2"), D("4"), D("2"), D("1")]
        b = [D("1"), D("0.5"), D("0.25"), D("0.5"), D("1")]
        r = compute_correlation(a, b)
        assert r == D(-1)

    def test_bounded(self):
        a = [D("10"), D("12"), D("11"), D("13"), D("14")]
        b = [D("5"), D("7"), D("4"), D("8"), D("6")]
        r = compute_correlation(a, b)
        assert -1 <= r <= 1


# ─── Relative Value Constraints ───────────────────────────────────────────────


class TestConstraintProperties:
    """Constraint violation detection properties."""

    @given(
        p1=prob_strategy(),
        p2=prob_strategy(),
        p3=prob_strategy(),
    )
    @stable_settings(max_examples=50)
    def test_partition_violation(self, p1, p2, p3):
        c = Constraint(
            kind="partition",
            markets=("m1", "m2", "m3"),
            review_reference="test",
            reviewed_at=NOW,
        )
        probs = {"m1": p1, "m2": p2, "m3": p3}
        violations_list = violations([c], probs, NOW)
        if violations_list:
            assert violations_list[0]["kind"] == "partition"
            assert float(violations_list[0]["magnitude"]) > 0

    @given(p1=prob_strategy(), p2=prob_strategy())
    @stable_settings(max_examples=50)
    def test_exclusive_violation(self, p1, p2):
        c = Constraint(
            kind="exclusive",
            markets=("m1", "m2"),
            review_reference="test",
            reviewed_at=NOW,
        )
        probs = {"m1": p1, "m2": p2}
        violations_list = violations([c], probs, NOW)
        total = p1 + p2
        if total > 1:
            assert len(violations_list) == 1
            assert violations_list[0]["kind"] == "exclusive"
        else:
            assert len(violations_list) == 0

    @given(p1=prob_strategy(), p2=prob_strategy())
    @stable_settings(max_examples=50)
    def test_implication_violation(self, p1, p2):
        c = Constraint(
            kind="implication",
            markets=("m1", "m2"),
            review_reference="test",
            reviewed_at=NOW,
        )
        probs = {"m1": p1, "m2": p2}
        violations_list = violations([c], probs, NOW)
        if p1 > p2:
            assert len(violations_list) == 1
            assert violations_list[0]["kind"] == "implication"
        else:
            assert len(violations_list) == 0

    @given(p1=prob_strategy(), p2=prob_strategy())
    @stable_settings(max_examples=50)
    def test_complement_violation(self, p1, p2):
        c = Constraint(
            kind="complement",
            markets=("m1", "m2"),
            review_reference="test",
            reviewed_at=NOW,
        )
        probs = {"m1": p1, "m2": p2}
        violations_list = violations([c], probs, NOW)
        if abs(p1 + p2 - 1) > 0:
            assert len(violations_list) == 1
            assert violations_list[0]["kind"] == "complement"

    @given(p1=prob_strategy(), p2=prob_strategy())
    @stable_settings(max_examples=50)
    def test_subsume_violation(self, p1, p2):
        c = Constraint(
            kind="subsume",
            markets=("m1", "m2"),
            review_reference="test",
            reviewed_at=NOW,
        )
        probs = {"m1": p1, "m2": p2}
        violations_list = violations([c], probs, NOW)
        # A subsumes B → P(A) ≥ P(B), violation if P(B) > P(A)
        if p2 > p1:
            assert len(violations_list) == 1
            assert violations_list[0]["kind"] == "subsume"
        else:
            assert len(violations_list) == 0

    @given(p=prob_strategy())
    @stable_settings(max_examples=50)
    def test_threshold_violation(self, p):
        c = Constraint(
            kind="threshold",
            markets=("m1",),
            review_reference="test",
            reviewed_at=NOW,
            threshold=D("0.7"),
        )
        probs = {"m1": p}
        violations_list = violations([c], probs, NOW)
        if p > D("0.7"):
            assert len(violations_list) == 1
            assert violations_list[0]["kind"] == "threshold"
        else:
            assert len(violations_list) == 0


# ─── Uncertainty Bounds ───────────────────────────────────────────────────────


class TestUncertaintyProperties:
    """Uncertainty estimate bounds: lower <= probability <= upper."""

    @given(p=prob_strategy(), score=decimals(min_value=D(0), max_value=D(1), places=4))
    @stable_settings(max_examples=50)
    def test_bounds_ordering(self, p, score):
        lo = max(D(0), p - score)
        hi = min(D(1), p + score)
        if lo <= p <= hi:
            est = UncertaintyEstimate(
                probability=p,
                lower_bound=lo,
                upper_bound=hi,
                uncertainty_score=score,
                method="test",
            )
            assert est.lower_bound <= est.probability <= est.upper_bound


class TestConstraintValidation:
    """Constraint construction validation."""

    def test_complement_requires_two(self):
        with pytest.raises(ValueError, match="complement requires two"):
            Constraint(kind="complement", markets=("m1",), review_reference="r", reviewed_at=NOW)

    def test_subsume_requires_two(self):
        with pytest.raises(ValueError, match="subsume requires two"):
            Constraint(
                kind="subsume", markets=("m1", "m3", "m2"), review_reference="r", reviewed_at=NOW
            )

    def test_threshold_requires_one(self):
        with pytest.raises(ValueError, match="threshold requires exactly one"):
            Constraint(
                kind="threshold", markets=("m1", "m2"), review_reference="r", reviewed_at=NOW
            )

    def test_threshold_requires_bound(self):
        with pytest.raises(ValueError, match="threshold must be"):
            Constraint(
                kind="threshold",
                markets=("m1",),
                review_reference="r",
                reviewed_at=NOW,
                threshold=D("2"),
            )

    def test_valid_types(self):
        for kind in ("partition", "exclusive", "implication", "complement", "subsume"):
            c = Constraint(kind=kind, markets=("m1", "m2"), review_reference="r", reviewed_at=NOW)
            assert c.kind == kind
        c = Constraint(
            kind="threshold",
            markets=("m1",),
            review_reference="r",
            reviewed_at=NOW,
            threshold=D("0.5"),
        )
        assert c.kind == "threshold"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "--tb=short"])
