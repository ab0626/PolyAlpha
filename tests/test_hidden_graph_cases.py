"""Part 52 — Graph/relative-value hidden tests.

Empty graph, single node, self-edge, duplicate edge, A implies B and B implies A,
contradictory relationships, partition zero/one member, partition sum
exactly/below/above 1, missing executable price, incompatible resolution rules,
cycle in logical graph.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyalpha.relative_value import CONSTRAINT_TYPES, Constraint, violations

D = Decimal
TZ = timezone.utc
TS = datetime(2025, 6, 1, tzinfo=TZ)


def _constraint(kind, markets, threshold=None):
    return Constraint(
        kind=kind,
        markets=tuple(markets),
        review_reference="review1",
        reviewed_at=TS,
        threshold=D(str(threshold)) if threshold is not None else None,
    )


# ── Empty constraints ────────────────────────────────────────────────────


class TestEmptyConstraints:
    def test_no_constraints_no_violations(self):
        result = violations([], {"m1": D("0.5")}, TS)
        assert result == []

    def test_constraint_no_matching_probabilities(self):
        rule = _constraint("partition", ["m1", "m2"])
        result = violations([rule], {"m3": D("0.5")}, TS)
        assert result == []


# ── Single node / single market ──────────────────────────────────────────


class TestSingleNode:
    def test_threshold_single_market(self):
        rule = _constraint("threshold", ["m1"], threshold=0.7)
        result = violations([rule], {"m1": D("0.5")}, TS)
        assert result == []

    def test_threshold_violation(self):
        rule = _constraint("threshold", ["m1"], threshold=0.5)
        result = violations([rule], {"m1": D("0.7")}, TS)
        assert len(result) == 1
        assert result[0]["kind"] == "threshold"


# ── Self-edge / self-implication ─────────────────────────────────────────


class TestSelfImplication:
    def test_self_implication_rejected(self):
        with pytest.raises(ValueError, match="duplicate members"):
            _constraint("implication", ["m1", "m1"])


# ── Duplicate constraints ────────────────────────────────────────────────


class TestDuplicateConstraints:
    def test_duplicate_partition_constraints(self):
        rule = _constraint("partition", ["m1", "m2"])
        result = violations([rule, rule], {"m1": D("0.5"), "m2": D("0.5")}, TS)
        assert result == []


# ── A implies B and B implies A ──────────────────────────────────────────


class TestBidirectionalImplication:
    def test_mutual_implication_consistent(self):
        r1 = _constraint("implication", ["m1", "m2"])
        r2 = _constraint("implication", ["m2", "m1"])
        result = violations([r1, r2], {"m1": D("0.5"), "m2": D("0.5")}, TS)
        assert result == []

    def test_mutual_implication_inconsistent(self):
        r1 = _constraint("implication", ["m1", "m2"])
        r2 = _constraint("implication", ["m2", "m1"])
        result = violations([r1, r2], {"m1": D("0.3"), "m2": D("0.7")}, TS)
        assert len(result) == 1


# ── Contradictory relationships ──────────────────────────────────────────


class TestContradictoryRelationships:
    def test_partition_violation(self):
        rule = _constraint("partition", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.5"), "m2": D("0.6")}, TS)
        assert len(result) == 1
        assert D(result[0]["magnitude"]) == D("0.1")

    def test_exclusive_violation(self):
        rule = _constraint("exclusive", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.6"), "m2": D("0.6")}, TS)
        assert len(result) == 1

    def test_exclusive_no_violation(self):
        rule = _constraint("exclusive", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.4"), "m2": D("0.5")}, TS)
        assert result == []


# ── Partition edge cases ─────────────────────────────────────────────────


class TestPartitionEdgeCases:
    def test_partition_sum_exactly_one(self):
        rule = _constraint("partition", ["m1", "m2", "m3"])
        result = violations(
            [rule],
            {"m1": D("0.3"), "m2": D("0.3"), "m3": D("0.4")},
            TS,
        )
        assert result == []

    def test_partition_sum_below_one(self):
        rule = _constraint("partition", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.3"), "m2": D("0.3")}, TS)
        assert len(result) == 1
        assert D(result[0]["magnitude"]) == D("0.4")

    def test_partition_sum_above_one(self):
        rule = _constraint("partition", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.6"), "m2": D("0.6")}, TS)
        assert len(result) == 1
        assert D(result[0]["magnitude"]) == D("0.2")


# ── Implication ──────────────────────────────────────────────────────────


class TestImplication:
    def test_implication_satisfied(self):
        rule = _constraint("implication", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.3"), "m2": D("0.7")}, TS)
        assert result == []

    def test_implication_violation(self):
        rule = _constraint("implication", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.7"), "m2": D("0.3")}, TS)
        assert len(result) == 1
        assert D(result[0]["magnitude"]) == D("0.4")


# ── Complement ───────────────────────────────────────────────────────────


class TestComplement:
    def test_complement_satisfied(self):
        rule = _constraint("complement", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.3"), "m2": D("0.7")}, TS)
        assert result == []

    def test_complement_violation(self):
        rule = _constraint("complement", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.3"), "m2": D("0.3")}, TS)
        assert len(result) == 1


# ── Subsume ──────────────────────────────────────────────────────────────


class TestSubsume:
    def test_subsume_satisfied(self):
        rule = _constraint("subsume", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.7"), "m2": D("0.3")}, TS)
        assert result == []

    def test_subsume_violation(self):
        rule = _constraint("subsume", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.3"), "m2": D("0.7")}, TS)
        assert len(result) == 1


# ── Missing executable price ─────────────────────────────────────────────


class TestMissingPrice:
    def test_missing_market_skipped(self):
        rule = _constraint("partition", ["m1", "m2"])
        result = violations([rule], {"m1": D("0.5")}, TS)
        assert result == []


# ── Invalid probabilities ────────────────────────────────────────────────


class TestInvalidProbabilities:
    def test_nan_probability_rejected(self):
        rule = _constraint("partition", ["m1", "m2"])
        with pytest.raises(ValueError, match="invalid probabilities"):
            violations([rule], {"m1": D("NaN"), "m2": D("0.5")}, TS)

    def test_inf_probability_rejected(self):
        rule = _constraint("partition", ["m1", "m2"])
        with pytest.raises(ValueError, match="invalid probabilities"):
            violations([rule], {"m1": D("Infinity"), "m2": D("0.5")}, TS)

    def test_negative_probability_rejected(self):
        rule = _constraint("partition", ["m1", "m2"])
        with pytest.raises(ValueError, match="invalid probabilities"):
            violations([rule], {"m1": D("-0.1"), "m2": D("0.5")}, TS)

    def test_probability_above_one_rejected(self):
        rule = _constraint("partition", ["m1", "m2"])
        with pytest.raises(ValueError, match="invalid probabilities"):
            violations([rule], {"m1": D("1.5"), "m2": D("0.5")}, TS)


# ── Constraint validation errors ─────────────────────────────────────────


class TestConstraintValidation:
    def test_duplicate_members_rejected(self):
        with pytest.raises(ValueError, match="duplicate members"):
            Constraint(
                kind="partition",
                markets=("m1", "m1"),
                review_reference="ref",
                reviewed_at=TS,
            )

    def test_threshold_requires_one_market(self):
        with pytest.raises(ValueError, match="threshold requires exactly one"):
            Constraint(
                kind="threshold",
                markets=("m1", "m2"),
                review_reference="ref",
                reviewed_at=TS,
                threshold=D("0.5"),
            )

    def test_threshold_without_value_rejected(self):
        with pytest.raises(ValueError, match="threshold must be"):
            Constraint(
                kind="threshold",
                markets=("m1",),
                review_reference="ref",
                reviewed_at=TS,
            )

    def test_implication_requires_two(self):
        with pytest.raises(ValueError, match="implication requires two"):
            Constraint(
                kind="implication",
                markets=("m1",),
                review_reference="ref",
                reviewed_at=TS,
            )

    def test_partition_requires_two_plus(self):
        with pytest.raises(ValueError, match="at least two"):
            Constraint(
                kind="partition",
                markets=("m1",),
                review_reference="ref",
                reviewed_at=TS,
            )

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match="reviewed constraint required"):
            Constraint(
                kind="unknown_kind",
                markets=("m1", "m2"),
                review_reference="ref",
                reviewed_at=TS,
            )


# ── Cycle in logical graph ──────────────────────────────────────────────


class TestCycleInGraph:
    def test_three_way_cycle_detected(self):
        r1 = _constraint("implication", ["m1", "m2"])
        r2 = _constraint("implication", ["m2", "m3"])
        r3 = _constraint("implication", ["m3", "m1"])
        result = violations(
            [r1, r2, r3],
            {"m1": D("0.3"), "m2": D("0.5"), "m3": D("0.7")},
            TS,
        )
        # m3 -> m1 violated because 0.7 > 0.3
        assert len(result) >= 1

    def test_three_way_cycle_consistent(self):
        r1 = _constraint("implication", ["m1", "m2"])
        r2 = _constraint("implication", ["m2", "m3"])
        r3 = _constraint("implication", ["m3", "m1"])
        result = violations(
            [r1, r2, r3],
            {"m1": D("0.3"), "m2": D("0.3"), "m3": D("0.3")},
            TS,
        )
        assert result == []


# ── Reviewed-at constraint ───────────────────────────────────────────────


class TestReviewedAtConstraint:
    def test_future_review_ignored(self):
        future_rule = Constraint(
            kind="partition",
            markets=("m1", "m2"),
            review_reference="ref",
            reviewed_at=TS + timedelta(days=365),
        )
        result = violations(
            [future_rule],
            {"m1": D("0.5"), "m2": D("0.6")},
            TS,
        )
        assert result == []
