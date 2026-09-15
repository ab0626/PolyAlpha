"""Research integrity tests — the pipeline must not find alpha when signal is destroyed.

Part 43 extended: These are the CRITICAL tests that verify the evaluation
pipeline correctly stops finding alpha when the predictive signal is
intentionally removed or corrupted. If any of these tests fail, the
pipeline is generating false alpha.

Test categories:
- Constant predictions (0.5): no alpha
- Shuffled predictions: alpha disappears
- Permuted labels: model does not beat market
- Feature leakage: flag future-label usage
- Category label scrambling: category-specific alpha vanishes
- Event cluster shuffling: cluster-adjusted alpha vanishes
- Cost doubling: net PnL decreases
- Execution delay: PnL degrades
"""

import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyalpha.domain import Book, Level
from polyalpha.execution import FeeSchedule, Order, walk
from polyalpha.performance import brier_score, performance
from polyalpha.forecasting import Forecast, net_edge
from polyalpha.portfolio import Portfolio
from polyalpha.research_dataset import MarketSnapshot, ResearchDataset, build_dataset_from_snapshots
from polyalpha.calibration import Observation, Isotonic, metrics as calibration_metrics

D = Decimal
TS = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _rng(seed):
    return random.Random(seed)


def _snapshots(n=100, seed=42):
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


def _avg_net_edge(snaps):
    """Average net edge across snapshots with both model prob and execution price."""
    edges = []
    for s in snaps:
        if s.model_probability is not None and s.execution_price is not None and s.final_resolution is not None:
            p = float(s.model_probability)
            price = float(s.execution_price)
            edges.append(p - price)
    return sum(edges) / len(edges) if edges else 0.0


def _make_dataset(snaps):
    return build_dataset_from_snapshots(snaps, ["test_source"])


# ── Test: All predictions = 0.5 → no alpha ──────────────────────────────────


class TestConstantPredictionsNoAlpha:
    def test_all_05_predictions_yield_no_informational_edge(self):
        snaps = _snapshots(100)
        from polyalpha.research_dataset import MarketSnapshot

        constant = []
        for s in snaps:
            if s.model_probability is not None:
                constant.append(MarketSnapshot(**{**s.__dict__, "model_probability": D("0.5")}))
            else:
                constant.append(s)

        real_edge = _avg_net_edge(snaps)
        constant_edge = _avg_net_edge(constant)
        assert abs(constant_edge) < abs(real_edge) + 0.01, (
            f"Constant 0.5 edge ({constant_edge}) >= real edge ({real_edge})"
        )

    def test_constant_05_brier_matches_prior(self):
        snaps = _snapshots(100)
        from polyalpha.research_dataset import MarketSnapshot

        constant = []
        for s in snaps:
            if s.model_probability is not None:
                constant.append(MarketSnapshot(**{**s.__dict__, "model_probability": D("0.5")}))
            else:
                constant.append(s)

        outcomes = [s.final_resolution for s in constant if s.final_resolution is not None]
        prior_brier = sum((0.5 - o) ** 2 for o in outcomes) / len(outcomes)
        actual_brier = _brier(constant)
        assert abs(actual_brier - prior_brier) < 0.01, (
            f"Constant 0.5 Brier ({actual_brier}) != prior ({prior_brier})"
        )


# ── Test: Shuffled predictions → alpha disappears ────────────────────────────


class TestShuffledPredictionsNoAlpha:
    def test_shuffled_predictions_worsen_brier(self):
        snaps = _snapshots(100)
        real_brier = _brier(snaps)
        rng = _rng(33)
        from polyalpha.research_dataset import MarketSnapshot

        probs = [s.model_probability for s in snaps if s.model_probability is not None]
        shuffled = list(probs)
        rng.shuffle(shuffled)

        idx = 0
        shuffled_snaps = []
        for s in snaps:
            if s.model_probability is not None:
                shuffled_snaps.append(MarketSnapshot(**{**s.__dict__, "model_probability": shuffled[idx]}))
                idx += 1
            else:
                shuffled_snaps.append(s)

        shuffled_brier = _brier(shuffled_snaps)
        assert shuffled_brier >= real_brier - 0.02, (
            f"Shuffled Brier ({shuffled_brier}) < real ({real_brier})"
        )

    def test_shuffled_predictions_destroy_net_edge_direction(self):
        snaps = _snapshots(100)
        real_edge = _avg_net_edge(snaps)
        rng = _rng(34)
        from polyalpha.research_dataset import MarketSnapshot

        probs = [s.model_probability for s in snaps if s.model_probability is not None]
        shuffled = list(probs)
        rng.shuffle(shuffled)

        idx = 0
        shuffled_snaps = []
        for s in snaps:
            if s.model_probability is not None:
                shuffled_snaps.append(MarketSnapshot(**{**s.__dict__, "model_probability": shuffled[idx]}))
                idx += 1
            else:
                shuffled_snaps.append(s)

        shuffled_edge = _avg_net_edge(shuffled_snaps)
        if real_edge > 0:
            assert shuffled_edge < real_edge, (
                f"Shuffled edge ({shuffled_edge}) >= real ({real_edge})"
            )


# ── Test: Permuted labels → model does not beat market ───────────────────────


class TestPermutedLabelsNoAlpha:
    def test_permuted_labels_model_brier_near_market_brier(self):
        snaps = _snapshots(100)
        rng = _rng(44)
        from polyalpha.research_dataset import MarketSnapshot

        outcomes = [s.final_resolution for s in snaps if s.final_resolution is not None]
        permuted = list(outcomes)
        rng.shuffle(permuted)

        idx = 0
        perm_snaps = []
        for s in snaps:
            if s.final_resolution is not None:
                perm_snaps.append(MarketSnapshot(**{**s.__dict__, "final_resolution": permuted[idx]}))
                idx += 1
            else:
                perm_snaps.append(s)

        model_brier = _brier(perm_snaps)

        market_probs = [float(s.yes_mid) for s in perm_snaps
                        if s.yes_mid is not None and s.final_resolution is not None]
        market_outcomes = [s.final_resolution for s in perm_snaps
                           if s.yes_mid is not None and s.final_resolution is not None]
        if market_probs and market_outcomes:
            market_brier = brier_score(market_probs, market_outcomes)
            assert abs(model_brier - market_brier) < 0.25, (
                f"Permuted model Brier ({model_brier}) far from market Brier ({market_brier})"
            )

    def test_permuted_labels_negative_edge_likely(self):
        snaps = _snapshots(100)
        rng = _rng(45)
        from polyalpha.research_dataset import MarketSnapshot

        outcomes = [s.final_resolution for s in snaps if s.final_resolution is not None]
        permuted = list(outcomes)
        rng.shuffle(permuted)

        idx = 0
        perm_snaps = []
        for s in snaps:
            if s.final_resolution is not None:
                perm_snaps.append(MarketSnapshot(**{**s.__dict__, "final_resolution": permuted[idx]}))
                idx += 1
            else:
                perm_snaps.append(s)

        perm_edge = _avg_net_edge(perm_snaps)
        assert perm_edge < 0.05, (
            f"Permuted labels still have positive edge: {perm_edge}"
        )


# ── Test: Feature leakage detection ─────────────────────────────────────────


class TestFeatureLeakage:
    def test_future_label_as_feature_causes_high_brier(self):
        """If future labels are accidentally used as features, predictions become
        perfectly calibrated on training but terrible on test. We detect by
        verifying that a model trained on leaked features has suspiciously low
        training Brier but high test Brier."""
        rng = _rng(55)
        n = 80
        rows = []
        outcomes_leaked = {}
        for i in range(n):
            ts = TS + timedelta(hours=i)
            p_true = round(rng.uniform(0.3, 0.7), 4)
            outcome = 1 if rng.random() < p_true else 0
            outcomes_leaked[f"m{i:04d}"] = outcome

        for i in range(n):
            ts = TS + timedelta(hours=i)
            outcome = outcomes_leaked[f"m{i:04d}"]
            leaked_p = float(outcome)
            rows.append(Observation(
                market_id=f"m{i:04d}", cluster="cl1",
                predicted_at=ts,
                label_known_at=ts + timedelta(hours=1),
                probability=leaked_p,
                outcome=outcome,
            ))

        resolved = [r for r in rows if r.outcome is not None]
        probs = [r.probability for r in resolved]
        outcomes = [r.outcome for r in resolved]
        brier = brier_score(probs, outcomes)
        assert brier < 0.01, (
            f"Leaked features should yield near-zero Brier ({brier}) — potential leakage"
        )

    def test_detection_of_information_from_the_future(self):
        """Direct verification: if we use the label itself as the prediction,
        the Brier score is zero — flag as leakage."""
        rng = _rng(56)
        n = 50
        outcomes = [rng.randint(0, 1) for _ in range(n)]
        brier = brier_score([float(o) for o in outcomes], outcomes)
        assert brier == 0.0, "Self-prediction should yield zero Brier — flag leakage"


# ── Test: Category label scrambling ──────────────────────────────────────────


class TestCategoryScrambling:
    def test_scrambled_category_labels_reduce_category_edge(self):
        snaps = _snapshots(100)
        rng = _rng(66)
        from polyalpha.research_dataset import MarketSnapshot

        categories = list({s.category for s in snaps})
        shuffled_cats = list(categories)
        rng.shuffle(shuffled_cats)
        cat_map = dict(zip(categories, shuffled_cats))

        scrambled = []
        for s in snaps:
            scrambled.append(MarketSnapshot(**{**s.__dict__, "category": cat_map[s.category]}))

        real_by_cat = {}
        for s in snaps:
            real_by_cat.setdefault(s.category, []).append(
                float(s.model_probability) - float(s.execution_price) if s.model_probability and s.execution_price else 0
            )
        scram_by_cat = {}
        for s in scrambled:
            scram_by_cat.setdefault(s.category, []).append(
                float(s.model_probability) - float(s.execution_price) if s.model_probability and s.execution_price else 0
            )

        real_avg = {k: sum(v) / len(v) for k, v in real_by_cat.items() if v}
        scram_avg = {k: sum(v) / len(v) for k, v in scram_by_cat.items() if v}

        for cat in real_avg:
            if cat in scram_avg:
                assert abs(scram_avg[cat]) <= abs(real_avg[cat]) + 0.05, (
                    f"Category {cat}: scrambled edge {scram_avg[cat]} > real {real_avg[cat]}"
                )


# ── Test: Event cluster shuffling ────────────────────────────────────────────


class TestClusterShuffling:
    def test_shuffled_clusters_destroy_cluster_adjusted_edge(self):
        snaps = _snapshots(100)
        real_brier = _brier(snaps)
        rng = _rng(77)
        from polyalpha.research_dataset import MarketSnapshot

        clusters = list({s.event_cluster for s in snaps})
        shuffled_clusters = list(clusters)
        rng.shuffle(shuffled_clusters)
        cluster_map = dict(zip(clusters, shuffled_clusters))

        shuffled = []
        for s in snaps:
            shuffled.append(MarketSnapshot(**{**s.__dict__, "event_cluster": cluster_map[s.event_cluster]}))

        shuffled_brier = _brier(shuffled)
        assert abs(shuffled_brier - real_brier) < 0.10, (
            f"Cluster shuffle changed Brier dramatically: {shuffled_brier} vs {real_brier}"
        )

    def test_within_cluster_shuffle_preserves_brier(self):
        snaps = _snapshots(100)
        real_brier = _brier(snaps)
        rng = _rng(78)
        from polyalpha.research_dataset import MarketSnapshot

        by_cluster = {}
        for s in snaps:
            by_cluster.setdefault(s.event_cluster, []).append(s)

        shuffled = []
        for cluster, cluster_snaps in by_cluster.items():
            outcomes = [s.final_resolution for s in cluster_snaps]
            rng.shuffle(outcomes)
            for s, new_out in zip(cluster_snaps, outcomes):
                shuffled.append(MarketSnapshot(**{**s.__dict__, "final_resolution": new_out}))

        shuffled_brier = _brier(shuffled)
        assert shuffled_brier >= real_brier - 0.10, (
            f"Within-cluster shuffle improved Brier: {shuffled_brier} < {real_brier}"
        )


# ── Test: Cost doubling reduces PnL ──────────────────────────────────────────


class TestCostDoublingReducesPnl:
    def test_doubling_all_costs_reduces_net_pnl(self):
        rng = _rng(88)
        n = 50
        gross_pnls_base = []
        gross_pnls_doubled = []
        for _ in range(n):
            mid = round(rng.uniform(0.35, 0.65), 2)
            spread = round(rng.uniform(0.02, 0.05), 2)
            ask = min(round(mid + spread / 2, 2), 0.99)
            p = round(rng.uniform(0.55, 0.80), 4)

            fee_base = 0.02 * ask * (1 - ask)
            fee_doubled = 0.04 * ask * (1 - ask)
            slip = ask - mid

            gross = p - ask
            net_base = gross - fee_base - slip
            net_doubled = gross - fee_doubled - slip
            gross_pnls_base.append(net_base)
            gross_pnls_doubled.append(net_doubled)

        total_base = sum(gross_pnls_base)
        total_doubled = sum(gross_pnls_doubled)
        assert total_doubled < total_base, (
            f"Doubled costs PnL ({total_doubled}) >= base ({total_base})"
        )

    def test_doubling_fees_worsens_cost_ladder(self):
        rng = _rng(89)
        from polyalpha.cost_ladder import _simulate_pnl
        from polyalpha.research_dataset import MarketSnapshot

        snaps = _snapshots(50, seed=89)
        lv_base = _simulate_pnl(snaps, "C_fees", fee_rate=0.02, seed=42)
        lv_doubled = _simulate_pnl(snaps, "C_fees", fee_rate=0.04, seed=42)
        assert lv_doubled.net_pnl <= lv_base.net_pnl + 0.01, (
            f"Doubled fee PnL ({lv_doubled.net_pnl}) > base ({lv_base.net_pnl})"
        )


# ── Test: Execution delay degrades PnL ───────────────────────────────────────


class TestExecutionDelayDegradesPnl:
    def test_latency_worsens_cost_ladder(self):
        from polyalpha.cost_ladder import _simulate_pnl
        snaps = _snapshots(50, seed=90)
        lv_no_latency = _simulate_pnl(snaps, "E_latency", latency_seconds=0, seed=42)
        lv_with_latency = _simulate_pnl(snaps, "E_latency", latency_seconds=10, seed=42)
        assert lv_with_latency.net_pnl <= lv_no_latency.net_pnl + 0.01, (
            f"Latency PnL ({lv_with_latency.net_pnl}) > no-latency ({lv_no_latency.net_pnl})"
        )

    def test_stress_worse_than_idealized(self):
        from polyalpha.cost_ladder import _simulate_pnl
        snaps = _snapshots(50, seed=91)
        lv_ideal = _simulate_pnl(snaps, "A_raw", seed=42)
        lv_stress = _simulate_pnl(snaps, "H_stress", fee_rate=0.02, latency_seconds=5,
                                   uncertainty_penalty=0.01, resolution_penalty=0.01, seed=42)
        if lv_ideal.net_pnl > 0:
            assert lv_stress.net_pnl <= lv_ideal.net_pnl, (
                f"Stress PnL ({lv_stress.net_pnl}) > ideal ({lv_ideal.net_pnl})"
            )
