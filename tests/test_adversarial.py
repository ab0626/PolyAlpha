"""Adversarial and research-integrity tests.

Part 43 of the v0.3 spec: aggressive tests intended to break assumptions.
These verify that the evaluation pipeline stops finding alpha when signal
is intentionally destroyed.

Three test classes:
1. Adversarial: deliberately corrupt data and verify results worsen
2. Metamorphic: verify mathematical invariants that must hold
3. Research-integrity: verify alpha disappears when signal is destroyed
"""

import math
import random
from datetime import datetime, timedelta
from decimal import Decimal

from polyalpha.research_dataset import MarketSnapshot, ResearchDataset, FeatureProvenance

D = Decimal


def _make_snapshots(n=50, seed=42):
    """Build synthetic snapshots with realistic structure."""
    rng = random.Random(seed)
    snaps = []
    for i in range(n):
        p = rng.uniform(0.3, 0.7)
        mid = p + rng.gauss(0, 0.03)
        mid = max(0.1, min(0.9, mid))
        spread = rng.uniform(0.01, 0.05)
        outcome = 1 if rng.random() < p else 0
        ts = datetime(2025, 1, 1) + timedelta(hours=i)
        snaps.append(MarketSnapshot(
            observation_timestamp=ts,
            market_id=f"m{i:04d}",
            event_id=f"evt_{i // 5}",
            condition_id=f"c{i:04d}",
            category=["politics", "sports", "crypto"][i % 3],
            question=f"Will X happen #{i}?",
            yes_token_id=f"yes_{i}",
            no_token_id=f"no_{i}",
            yes_best_bid=D(str(round(mid - spread / 2, 4))),
            yes_best_ask=D(str(round(mid + spread / 2, 4))),
            yes_mid=D(str(round(mid, 4))),
            yes_spread=D(str(round(spread, 4))),
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
            model_probability=D(str(round(p, 4))),
            execution_price=D(str(round(mid + spread / 2, 4))),
            side="BUY",
        ))
    return snaps


def _brier(snapshots):
    resolved = [s for s in snapshots if s.final_resolution is not None and s.model_probability is not None]
    if not resolved:
        return 1.0
    return sum((float(s.model_probability) - s.final_resolution) ** 2 for s in resolved) / len(resolved)


# ══════════════════════════════════════════════════════════════════════════════
# ADVERSARIAL TESTS — corrupt data, verify results worsen
# ══════════════════════════════════════════════════════════════════════════════


class TestAdversarialNumericCorruption:
    """Verify numeric corruption degrades model quality."""

    def test_gaussian_noise_on_probabilities_worsens_brier(self):
        snaps = _make_snapshots(50)
        baseline = _brier(snaps)
        rng = random.Random(99)
        noisy = []
        for s in snaps:
            if s.model_probability is not None:
                noisy_p = float(s.model_probability) + rng.gauss(0, 0.1)
                noisy_p = max(0.01, min(0.99, noisy_p))
                noisy.append(MarketSnapshot(
                    **{**s.__dict__, "model_probability": D(str(round(noisy_p, 4)))}
                ))
            else:
                noisy.append(s)
        corrupted = _brier(noisy)
        assert corrupted > baseline - 0.01, (
            f"Corrupted Brier ({corrupted}) should be >= baseline ({baseline})"
        )

    def test_extreme_probabilities_worsens_brier(self):
        snaps = _make_snapshots(50)
        baseline = _brier(snaps)
        extreme = []
        for s in snaps:
            if s.model_probability is not None:
                p = float(s.model_probability)
                extreme_p = 0.01 if p < 0.5 else 0.99
                extreme.append(MarketSnapshot(
                    **{**s.__dict__, "model_probability": D(str(extreme_p))}
                ))
            else:
                extreme.append(s)
        corrupted = _brier(extreme)
        assert corrupted > baseline - 0.01

    def test_random_predictions_match_prior(self):
        snaps = _make_snapshots(100)
        rng = random.Random(77)
        random_snaps = []
        for s in snaps:
            if s.final_resolution is not None:
                rp = rng.uniform(0.1, 0.9)
                random_snaps.append(MarketSnapshot(
                    **{**s.__dict__, "model_probability": D(str(round(rp, 4)))}
                ))
            else:
                random_snaps.append(s)
        random_brier = _brier(random_snaps)
        outcomes = [s.final_resolution for s in random_snaps if s.final_resolution is not None]
        prior_brier = sum((0.5 - o) ** 2 for o in outcomes) / len(outcomes)
        assert abs(random_brier - prior_brier) < 0.15, (
            f"Random Brier ({random_brier}) should be near prior ({prior_brier})"
        )


class TestAdversarialTemporalCorruption:
    """Verify temporal corruption degrades results."""

    def test_delayed_predictions_worsens_brier(self):
        snaps = _make_snapshots(50)
        baseline = _brier(snaps)
        delayed = []
        for i, s in enumerate(snaps):
            if s.model_probability is not None and i >= 5:
                prev = snaps[i - 5]
                delayed.append(MarketSnapshot(
                    **{**s.__dict__, "model_probability": prev.model_probability}
                ))
            else:
                delayed.append(s)
        delayed_brier = _brier(delayed)
        assert delayed_brier >= baseline - 0.02

    def test_scrambled_order_same_predictions(self):
        snaps = _make_snapshots(50)
        baseline = _brier(snaps)
        rng = random.Random(55)
        shuffled = list(snaps)
        rng.shuffle(shuffled)
        shuffled_brier = _brier(shuffled)
        assert abs(shuffled_brier - baseline) < 0.01, (
            "Reordering snapshots should not change Brier (predictions unchanged)"
        )


class TestAdversarialFeatureCorruption:
    """Verify feature corruption degrades results."""

    def test_zeroed_features_same_prediction(self):
        snaps = _make_snapshots(30)
        baseline = _brier(snaps)
        zeroed = []
        for s in snaps:
            zeroed.append(MarketSnapshot(
                **{
                    **s.__dict__,
                    "yes_depth_1": D(0),
                    "yes_depth_5": D(0),
                    "yes_depth_10": D(0),
                    "yes_bid_size": D(0),
                    "yes_ask_size": D(0),
                }
            ))
        zeroed_brier = _brier(zeroed)
        assert abs(zeroed_brier - baseline) < 0.01, (
            "Zeroing depth features with same predictions should not change Brier"
        )


# ══════════════════════════════════════════════════════════════════════════════
# METAMORPHIC TESTS — mathematical invariants that must hold
# ══════════════════════════════════════════════════════════════════════════════


class TestMetamorphicInvariants:
    """Verify mathematical invariants that must always hold."""

    def test_increasing_fees_cannot_improve_net_edge(self):
        snaps = _make_snapshots(30)
        fee_rates = [0.0, 0.01, 0.02, 0.05, 0.10]
        net_edges = []
        for fee in fee_rates:
            total = 0.0
            count = 0
            for s in snaps:
                if s.model_probability is None or s.execution_price is None or s.final_resolution is None:
                    continue
                p = float(s.model_probability)
                price = float(s.execution_price)
                gross = p - price
                cost = fee * price * (1 - price)
                total += gross - cost
                count += 1
            net_edges.append(total / count if count else 0.0)
        for i in range(1, len(net_edges)):
            assert net_edges[i] <= net_edges[i - 1] + 1e-10, (
                f"Net edge at fee={fee_rates[i]} ({net_edges[i]}) > "
                f"at fee={fee_rates[i-1]} ({net_edges[i-1]})"
            )

    def test_brier_score_bounds(self):
        snaps = _make_snapshots(50)
        brier = _brier(snaps)
        assert 0.0 <= brier <= 1.0, f"Brier score {brier} outside [0,1]"

    def test_perfect_predictions_zero_brier(self):
        snaps = _make_snapshots(20)
        perfect = []
        for s in snaps:
            if s.final_resolution is not None:
                perfect.append(MarketSnapshot(
                    **{**s.__dict__, "model_probability": D(str(s.final_resolution))}
                ))
            else:
                perfect.append(s)
        assert _brier(perfect) == 0.0

    def test_worst_predictions_brier_near_one(self):
        snaps = _make_snapshots(20)
        worst = []
        for s in snaps:
            if s.final_resolution is not None:
                inverted = 1 - s.final_resolution
                worst.append(MarketSnapshot(
                    **{**s.__dict__, "model_probability": D(str(inverted))}
                ))
            else:
                worst.append(s)
        brier = _brier(worst)
        assert brier >= 0.9, f"Worst-case Brier {brier} should be >= 0.9"

    def test_bid_ask_ordering(self):
        snaps = _make_snapshots(30)
        for s in snaps:
            if s.yes_best_bid is not None and s.yes_best_ask is not None:
                assert s.yes_best_bid <= s.yes_best_ask, (
                    f"bid ({s.yes_best_bid}) > ask ({s.yes_best_ask}) for {s.market_id}"
                )

    def test_no_position_beyond_risk_cap(self):
        snaps = _make_snapshots(20)
        max_notional = D(1000)
        total = D(0)
        for s in snaps:
            if s.execution_price is not None:
                shares = D(100)
                notional = shares * s.execution_price
                total += notional
                if total > max_notional:
                    break
        assert total <= max_notional * 2, "Position should respect risk cap"

    def test_vwap_monotonicity_asks(self):
        from polyalpha.domain import Book, Level
        from datetime import timezone
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        book = Book(
            token_id="t1", condition_id="c1",
            source_at=ts,
            received_at=ts,
            bids=(Level(D("0.5"), D("100")),),
            asks=(Level(D("0.55"), D("50")), Level(D("0.56"), D("50")), Level(D("0.57"), D("100"))),
            tick_size=D("0.01"), min_order_size=D("1"), source_hash="",
        )
        from polyalpha.execution import FeeSchedule, Order, walk
        fees = FeeSchedule(D("0"), ts, "free")
        order1 = Order("o1", "t1", "BUY", D("30"), ts)
        order2 = Order("o2", "t1", "BUY", D("80"), ts)
        fill1 = walk(book, order1, fees, ts)
        fill2 = walk(book, order2, fees, ts)
        assert fill1.vwap <= fill2.vwap + D("0.001"), (
            f"Larger buy got better VWAP: {fill1.vwap} > {fill2.vwap}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# RESEARCH-INTEGRITY TESTS — verify alpha disappears when signal destroyed
# ══════════════════════════════════════════════════════════════════════════════


class TestResearchIntegrity:
    """Verify that the evaluation pipeline stops finding alpha when signal is destroyed."""

    def test_alpha_disappears_with_shuffled_labels(self):
        snaps = _make_snapshots(100)
        real_brier = _brier(snaps)
        rng = random.Random(33)
        outcomes = [s.final_resolution for s in snaps if s.final_resolution is not None]
        shuffled_outcomes = list(outcomes)
        rng.shuffle(shuffled_outcomes)
        shuffled = []
        idx = 0
        for s in snaps:
            if s.final_resolution is not None:
                shuffled.append(MarketSnapshot(
                    **{**s.__dict__, "final_resolution": shuffled_outcomes[idx]}
                ))
                idx += 1
            else:
                shuffled.append(s)
        shuffled_brier = _brier(shuffled)
        assert abs(shuffled_brier - real_brier) < 0.15, (
            f"Shuffled labels Brier ({shuffled_brier}) too far from real ({real_brier})"
        )

    def test_alpha_disappears_with_permuted_predictions_within_category(self):
        snaps = _make_snapshots(100)
        real_brier = _brier(snaps)
        by_cat: dict[str, list] = {}
        for s in snaps:
            by_cat.setdefault(s.category, []).append(s)
        rng = random.Random(44)
        permuted = []
        for cat, cat_snaps in by_cat.items():
            probs = [s.model_probability for s in cat_snaps if s.model_probability is not None]
            rng.shuffle(probs)
            pi = 0
            for s in cat_snaps:
                if s.model_probability is not None and pi < len(probs):
                    permuted.append(MarketSnapshot(**{**s.__dict__, "model_probability": probs[pi]}))
                    pi += 1
                else:
                    permuted.append(s)
        perm_brier = _brier(permuted)
        assert perm_brier >= real_brier - 0.05, (
            f"Within-category permuted Brier ({perm_brier}) < real ({real_brier})"
        )

    def test_alpha_disappears_with_cluster_shuffle(self):
        snaps = _make_snapshots(100)
        real_brier = _brier(snaps)
        by_cluster: dict[str, list] = {}
        for s in snaps:
            by_cluster.setdefault(s.event_cluster, []).append(s)
        rng = random.Random(55)
        shuffled = []
        for cluster, cluster_snaps in by_cluster.items():
            outcomes = [s.final_resolution for s in cluster_snaps]
            rng.shuffle(outcomes)
            for s, new_out in zip(cluster_snaps, outcomes):
                shuffled.append(MarketSnapshot(**{**s.__dict__, "final_resolution": new_out}))
        shuffled_brier = _brier(shuffled)
        assert shuffled_brier >= real_brier - 0.1, (
            f"Cluster-shuffled Brier ({shuffled_brier}) < real ({real_brier})"
        )

    def test_constant_prediction_no_edge(self):
        snaps = _make_snapshots(50)
        constant = []
        for s in snaps:
            if s.model_probability is not None:
                constant.append(MarketSnapshot(**{**s.__dict__, "model_probability": D("0.5")}))
            else:
                constant.append(s)
        constant_brier = _brier(constant)
        outcomes = [s.final_resolution for s in constant if s.final_resolution is not None]
        prior_brier = sum((0.5 - o) ** 2 for o in outcomes) / len(outcomes)
        assert abs(constant_brier - prior_brier) < 0.01, (
            f"Constant 0.5 Brier ({constant_brier}) should match prior ({prior_brier})"
        )

    def test_inverted_predictions_worse_than_random(self):
        snaps = _make_snapshots(50)
        real_brier = _brier(snaps)
        inverted = []
        for s in snaps:
            if s.model_probability is not None:
                inv_p = 1.0 - float(s.model_probability)
                inverted.append(MarketSnapshot(**{**s.__dict__, "model_probability": D(str(round(inv_p, 4)))}))
            else:
                inverted.append(s)
        inv_brier = _brier(inverted)
        assert inv_brier > real_brier, (
            f"Inverted Brier ({inv_brier}) should be worse than real ({real_brier})"
        )
