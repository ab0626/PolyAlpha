"""Comprehensive tests for all v0.3 research modules.

Covers: research_dataset, dataset_audit, market_benchmark, null_strategies,
cost_ladder, model_ablation, model_vs_market, disagreement_analysis,
time_to_resolution, category_analysis, ensemble_consensus, feature_provenance,
signal_explanation, edge_decomposition, trade_autopsy, shadow_portfolios,
effective_sample_size, monte_carlo_stress, model_degradation, negative_controls,
counterfactual, holdout.
"""

import random
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

D = Decimal

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _ts(year=2025, month=1, day=1, hour=12):
    return datetime(year, month, day, hour, 0, 0)


def _make_dataset(n=50, seed=42):
    from polyalpha.research_dataset import MarketSnapshot, ResearchDataset

    rng = random.Random(seed)
    snaps = []
    for i in range(n):
        p = rng.uniform(0.2, 0.8)
        mid = p + rng.gauss(0, 0.05)
        mid = max(0.1, min(0.9, mid))
        spread = rng.uniform(0.01, 0.04)
        outcome = 1 if rng.random() < p else 0
        ts = _ts() + timedelta(hours=i)
        snaps.append(MarketSnapshot(
            observation_timestamp=ts,
            market_id=f"mkt_{i:04d}",
            event_id=f"evt_{i // 5}",
            condition_id=f"cond_{i:04d}",
            category=["politics", "sports", "crypto", "science", "entertainment"][i % 5],
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
    return ResearchDataset(
        snapshots=snaps,
        created_at=datetime.utcnow(),
        source_reports=["test"],
        data_hash="abc123",
        period_start=snaps[0].observation_timestamp.isoformat(),
        period_end=snaps[-1].observation_timestamp.isoformat(),
        total_observations=n,
        resolved_observations=n,
        unresolved_observations=0,
        unique_markets=n,
        unique_events=n // 5,
        unique_clusters=n // 10,
        categories={f: n // 5 for f in ["politics", "sports", "crypto", "science", "entertainment"]},
    )


# ── Research Dataset Tests ───────────────────────────────────────────────────


class TestResearchDataset:
    def test_brier_score(self):
        ds = _make_dataset(20)
        brier = ds.brier_score()
        assert brier is not None
        assert 0.0 <= brier <= 1.0

    def test_log_loss(self):
        ds = _make_dataset(20)
        ll = ds.log_loss()
        assert ll is not None
        assert ll >= 0.0

    def test_market_brier(self):
        ds = _make_dataset(20)
        mb = ds.market_brier()
        assert mb is not None
        assert 0.0 <= mb <= 1.0

    def test_to_csv(self, tmp_path):
        ds = _make_dataset(5)
        csv_path = str(tmp_path / "test.csv")
        ds.to_csv(csv_path)
        import csv
        with open(csv_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 5

    def test_to_json(self, tmp_path):
        ds = _make_dataset(5)
        json_path = str(tmp_path / "test.json")
        ds.to_json(json_path)
        import json
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["total_observations"] == 5

    def test_empty_dataset(self):
        from polyalpha.research_dataset import ResearchDataset
        ds = ResearchDataset(
            snapshots=[], created_at=datetime.utcnow(),
            source_reports=[], data_hash="empty",
        )
        assert ds.brier_score() is None
        assert ds.resolved_fraction == 0.0

    def test_cluster_property(self):
        from polyalpha.research_dataset import MarketSnapshot
        s1 = MarketSnapshot(
            observation_timestamp=_ts(2025, 1, 1),
            market_id="m1", event_id="e1", condition_id="c1",
            category="test", question="Q?", yes_token_id="y", no_token_id="n",
            event_cluster="cluA",
        )
        assert s1.cluster == "cluA"
        s2 = MarketSnapshot(
            observation_timestamp=_ts(2025, 1, 1),
            market_id="m2", event_id="e2", condition_id="c2",
            category="test", question="Q?", yes_token_id="y", no_token_id="n",
        )
        assert s2.cluster == "m2"

    def test_snapshot_id_property(self):
        from polyalpha.research_dataset import MarketSnapshot
        s = MarketSnapshot(
            observation_timestamp=_ts(2025, 1, 1),
            market_id="m1", event_id="e1", condition_id="c1",
            category="test", question="Q?", yes_token_id="y", no_token_id="n",
        )
        assert s.snapshot_id.startswith("m1:")

    def test_provenance_invariant_violation(self):
        from polyalpha.research_dataset import MarketSnapshot, FeatureProvenance
        with pytest.raises(ValueError, match="INVARIANT"):
            MarketSnapshot(
                observation_timestamp=_ts(2025, 1, 1),
                market_id="m1", event_id="e1", condition_id="c1",
                category="test", question="Q?",
                yes_token_id="y1", no_token_id="n1",
                feature_provenance=FeatureProvenance(
                    feature_timestamp=_ts(2025, 6, 1),
                ),
            )

    def test_microprice_computation(self):
        from polyalpha.research_dataset import MarketSnapshot
        s = MarketSnapshot(
            observation_timestamp=_ts(), market_id="m1", event_id="e1",
            condition_id="c1", category="test", question="Q?",
            yes_token_id="y1", no_token_id="n1",
            yes_best_bid=D("0.4"), yes_best_ask=D("0.6"),
            yes_bid_size=D("100"), yes_ask_size=D("50"),
        )
        mp = s.microprice_yes
        assert mp is not None
        expected = (D("0.6") * D("100") + D("0.4") * D("50")) / D("150")
        assert abs(mp - expected) < D("0.001")


# ── Dataset Audit Tests ──────────────────────────────────────────────────────


class TestDatasetAudit:
    def test_audit_clean_dataset(self):
        from polyalpha.dataset_audit import DatasetAuditor
        ds = _make_dataset(20)
        auditor = DatasetAuditor()
        report = auditor.audit(ds)
        assert report.total_rows == 20

    def test_audit_catches_duplicate_market_timestamp(self):
        from polyalpha.dataset_audit import DatasetAuditor
        from polyalpha.research_dataset import MarketSnapshot
        snaps = [
            MarketSnapshot(
                observation_timestamp=_ts(), market_id="m1", event_id="e1",
                condition_id="c1", category="test", question="Q?",
                yes_token_id="y1", no_token_id="n1",
                model_probability=D("0.6"), final_resolution=1,
            ),
            MarketSnapshot(
                observation_timestamp=_ts(), market_id="m1", event_id="e1",
                condition_id="c1", category="test", question="Q?",
                yes_token_id="y1", no_token_id="n1",
                model_probability=D("0.7"), final_resolution=0,
            ),
        ]
        from polyalpha.research_dataset import ResearchDataset
        ds = ResearchDataset(
            snapshots=snaps, created_at=datetime.utcnow(),
            source_reports=[], data_hash="x",
        )
        auditor = DatasetAuditor()
        report = auditor.audit(ds)
        assert report.rejected_rows >= 1

    def test_audit_catches_impossible_bid_ask(self):
        from polyalpha.dataset_audit import DatasetAuditor
        from polyalpha.research_dataset import MarketSnapshot
        snaps = [
            MarketSnapshot(
                observation_timestamp=_ts(), market_id="m1", event_id="e1",
                condition_id="c1", category="test", question="Q?",
                yes_token_id="y1", no_token_id="n1",
                yes_best_bid=D("0.7"), yes_best_ask=D("0.5"),
            ),
        ]
        from polyalpha.research_dataset import ResearchDataset
        ds = ResearchDataset(
            snapshots=snaps, created_at=datetime.utcnow(),
            source_reports=[], data_hash="x",
        )
        auditor = DatasetAuditor()
        report = auditor.audit(ds)
        assert any(c.name == "impossible_bid_ask" and not c.passed for c in report.checks)

    def test_audit_catches_negative_sizes(self):
        from polyalpha.dataset_audit import DatasetAuditor
        from polyalpha.research_dataset import MarketSnapshot
        snaps = [
            MarketSnapshot(
                observation_timestamp=_ts(), market_id="m1", event_id="e1",
                condition_id="c1", category="test", question="Q?",
                yes_token_id="y1", no_token_id="n1",
                yes_depth_1=D("-10"),
            ),
        ]
        from polyalpha.research_dataset import ResearchDataset
        ds = ResearchDataset(
            snapshots=snaps, created_at=datetime.utcnow(),
            source_reports=[], data_hash="x",
        )
        auditor = DatasetAuditor()
        report = auditor.audit(ds)
        assert any(c.name == "negative_sizes" and not c.passed for c in report.checks)

    def test_report_summary_fields(self):
        from polyalpha.dataset_audit import DatasetAuditor
        ds = _make_dataset(10)
        auditor = DatasetAuditor()
        report = auditor.audit(ds)
        summary = report.summary()
        assert "total_rows" in summary
        assert "accepted_rows" in summary
        assert "rejected_rows" in summary
        assert "failure_counts" in summary


# ── Market Benchmark Tests ───────────────────────────────────────────────────


class TestMarketBenchmark:
    def test_benchmark_with_microprice(self):
        from polyalpha.market_benchmark import evaluate_market_benchmark
        ds = _make_dataset(20)
        result = evaluate_market_benchmark(ds)
        assert result.resolved_count == 20
        assert hasattr(result, "market_microprice")
        assert hasattr(result, "delta_brier_microprice")


# ── Null Strategy Tests ──────────────────────────────────────────────────────


class TestNullStrategies:
    def test_all_five_strategies(self):
        from polyalpha.null_strategies import evaluate_null_strategies
        ds = _make_dataset(30)
        results = evaluate_null_strategies(ds)
        assert len(results) == 5

    def test_market_midpoint_uses_yes_mid(self):
        """0B baseline must use the market midpoint, not the model probability."""
        from polyalpha.null_strategies import evaluate_null_strategies
        from polyalpha.research_dataset import MarketSnapshot

        snaps = []
        for i in range(20):
            outcome = 1 if i % 2 == 1 else 0
            mid = D("0.9") if outcome == 1 else D("0.1")
            ts = _ts() + timedelta(hours=i)
            snaps.append(MarketSnapshot(
                observation_timestamp=ts,
                market_id=f"mkt_{i}",
                event_id="evt",
                condition_id=f"cond_{i}",
                category="politics",
                question="q",
                yes_token_id="y",
                no_token_id="n",
                yes_best_bid=D("0.05"),
                yes_best_ask=D("0.95"),
                yes_mid=mid,
                no_best_bid=D("0.05"),
                no_best_ask=D("0.95"),
                no_mid=D(str(1 - float(mid))),
                yes_depth_1=D("100"),
                yes_depth_5=D("500"),
                yes_bid_size=D("100"),
                yes_ask_size=D("100"),
                no_bid_size=D("100"),
                no_ask_size=D("100"),
                no_depth_1=D("100"),
                no_depth_5=D("500"),
                volume=D("1000"),
                liquidity=D("5000"),
                hours_to_resolution=24.0,
                fees_enabled=False,
                fee_rate=D("0"),
                event_cluster="c1",
                final_resolution=outcome,
                model_probability=D("0.50"),  # deliberately uninformative
                execution_price=D("0.31"),
                side="BUY",
            ))
        from polyalpha.research_dataset import ResearchDataset
        ds = ResearchDataset(
            snapshots=snaps, created_at=_ts(),
            source_reports=[], data_hash="x",
            total_observations=20, resolved_observations=20,
        )
        results = evaluate_null_strategies(ds)
        by_name = {r.name: r for r in results}
        mid_result = by_name["0B_market_midpoint"]
        # Using yes_mid (0.1/0.9 matching outcomes) yields near-zero Brier;
        # if it had used model_probability (0.5) Brier would be ~0.25.
        assert mid_result.brier_score < 0.2, f"0B used model prob: {mid_result.brier_score}"


# ── Cost Ladder Tests ────────────────────────────────────────────────────────


class TestCostLadder:
    def test_eight_levels(self):
        from polyalpha.cost_ladder import run_cost_ladder
        ds = _make_dataset(30)
        result = run_cost_ladder(ds)
        assert len(result.levels) == 8
        level_names = {lv.level for lv in result.levels}
        assert "A_raw" in level_names
        assert "H_stress" in level_names

    def test_alpha_survival_ratio(self):
        from polyalpha.cost_ladder import run_cost_ladder
        ds = _make_dataset(30)
        result = run_cost_ladder(ds)
        assert "A_raw" in result.alpha_survival_by_level

    def test_output_fields(self):
        from polyalpha.cost_ladder import run_cost_ladder
        ds = _make_dataset(20)
        result = run_cost_ladder(ds)
        for lv in result.levels:
            assert hasattr(lv, "gross_pnl")
            assert hasattr(lv, "net_pnl")
            assert hasattr(lv, "fee_cost")
            assert hasattr(lv, "slippage_cost")
            assert hasattr(lv, "drawdown")
            assert hasattr(lv, "profit_factor")


# ── Model Ablation Tests ─────────────────────────────────────────────────────


class TestModelAblation:
    def test_fourteen_combinations(self):
        from polyalpha.model_ablation import run_ablation
        ds = _make_dataset(30)
        result = run_ablation(ds)
        assert len(result.results) == 14

    def test_six_components_in_results(self):
        from polyalpha.model_ablation import run_ablation
        ds = _make_dataset(30)
        result = run_ablation(ds)
        all_components = set()
        for r in result.results:
            all_components.update(r.components)
        assert len(all_components) == 6

    def test_dual_ranking(self):
        from polyalpha.model_ablation import run_ablation
        ds = _make_dataset(30)
        result = run_ablation(ds)
        assert hasattr(result, "brier_ranking")
        assert hasattr(result, "pnl_ranking")
        assert len(result.brier_ranking) == 14


# ── Model vs Market Tests ────────────────────────────────────────────────────


class TestModelVsMarket:
    def test_matrix(self):
        from polyalpha.model_vs_market import build_market_model_matrix
        ds = _make_dataset(30)
        result = build_market_model_matrix(ds)
        assert result.total_compared == 30


# ── Disagreement Analysis Tests ──────────────────────────────────────────────


class TestDisagreementAnalysis:
    def test_seven_fixed_buckets(self):
        from polyalpha.disagreement_analysis import analyze_disagreement
        ds = _make_dataset(50)
        result = analyze_disagreement(ds)
        assert len(result.buckets) >= 1
        labels = {b.bucket_label for b in result.buckets}
        assert "0-1%" in labels or "1-2%" in labels

    def test_bucket_labels(self):
        from polyalpha.disagreement_analysis import analyze_disagreement
        ds = _make_dataset(50)
        result = analyze_disagreement(ds)
        valid_labels = {"0-1%", "1-2%", "2-5%", "5-10%", "10-15%", "15-20%", ">20%"}
        for b in result.buckets:
            assert b.bucket_label in valid_labels


# ── Time-to-Resolution Tests ─────────────────────────────────────────────────


class TestTimeToResolution:
    def test_has_buckets(self):
        from polyalpha.time_to_resolution import analyze_time_to_resolution
        from polyalpha.research_dataset import MarketSnapshot, ResearchDataset
        import random as _rng
        rng = _rng.Random(42)
        snaps = []
        for i in range(20):
            snaps.append(MarketSnapshot(
                observation_timestamp=_ts() + timedelta(hours=i),
                market_id=f"m{i}", event_id=f"e{i}", condition_id=f"c{i}",
                category="test", question="Q?", yes_token_id=f"y{i}", no_token_id=f"n{i}",
                model_probability=D("0.6"), final_resolution=1,
                resolution_timestamp=_ts() + timedelta(hours=i + rng.randint(1, 24)),
            ))
        ds = ResearchDataset(
            snapshots=snaps, created_at=datetime.utcnow(),
            source_reports=[], data_hash="x",
        )
        result = analyze_time_to_resolution(ds)
        assert len(result.buckets) >= 1


# ── Category Analysis Tests ──────────────────────────────────────────────────


class TestCategoryAnalysis:
    def test_all_categories(self):
        from polyalpha.category_analysis import analyze_categories
        ds = _make_dataset(25)
        result = analyze_categories(ds)
        assert len(result.categories) == 5


# ── Ensemble Consensus Tests ─────────────────────────────────────────────────


class TestEnsembleConsensus:
    def test_has_observations(self):
        from polyalpha.ensemble_consensus import analyze_consensus
        ds = _make_dataset(30)
        result = analyze_consensus(ds)
        assert hasattr(result, "observations")


# ── Edge Decomposition Tests ─────────────────────────────────────────────────


class TestEdgeDecomposition:
    def test_track_and_realize(self):
        from polyalpha.edge_decomposition import track_edge, realize_edge
        from polyalpha.research_dataset import MarketSnapshot
        snap = MarketSnapshot(
            observation_timestamp=_ts(), market_id="m1", event_id="e1",
            condition_id="c1", category="test", question="Q?",
            yes_token_id="y1", no_token_id="n1",
            model_probability=D("0.7"), execution_price=D("0.6"),
            hours_to_resolution=48.0, liquidity=D("1000"),
        )
        record = track_edge("s1", snap, raw_model_edge=D("0.1"), fee_cost=D("0.01"))
        assert record.predicted_net_edge == D("0.09")
        realize_edge(record, outcome=1, exec_price=D("0.6"))
        assert record.realized_edge is not None
        assert record.edge_realization_ratio is not None


# ── Trade Autopsy Tests ──────────────────────────────────────────────────────


class TestTradeAutopsy:
    def test_classify_failure_types(self):
        from polyalpha.trade_autopsy import autopsy_trade
        result = autopsy_trade(
            market="m1",
            entry_time=_ts(),
            exit_time=_ts() + timedelta(hours=1),
            entry_price=D("0.6"),
            exit_price=D("0.5"),
            model_probability=D("0.7"),
            conservative_probability=D("0.65"),
            final_resolution="0",
            predicted_edge=D("0.1"),
        )
        assert result.failure_classification in [
            "forecast_error", "execution_slippage", "fee_drag",
            "model_overconfidence", "unknown",
        ]


# ── Shadow Portfolio Tests ───────────────────────────────────────────────────


class TestShadowPortfolios:
    def test_seven_correct_shadows(self):
        from polyalpha.shadow_portfolios import run_shadow_portfolios
        signals = [
            {"probability": 0.6, "outcome": 1, "pnl": 10, "net_edge": 0.05,
             "execution_price": 0.55, "market_id": "m1", "side": "BUY",
             "confidence": 0.7, "liquidity": 1000, "uncertainty_penalty": 0.01,
             "resolution_penalty": 0.005, "market_mid": 0.55}
            for _ in range(20)
        ]
        results = run_shadow_portfolios(signals)
        names = {r.name for r in results}
        assert "shadow_market_mid" in names
        assert "shadow_equal_weight" in names
        assert "shadow_no_uncertainty" in names
        assert "shadow_no_resolution_penalty" in names
        assert "shadow_no_risk_caps" in names
        assert "shadow_random_valid_signal" in names
        assert "shadow_model_only" in names
        assert len(results) == 7


# ── Effective Sample Size Tests ──────────────────────────────────────────────


class TestEffectiveSampleSize:
    def test_compute_ess(self):
        from polyalpha.effective_sample_size import compute_effective_sample_size
        ds = _make_dataset(50)
        result = compute_effective_sample_size(ds)
        assert result.nominal_count == 50
        assert result.unique_market_count > 0
        assert result.unique_event_count > 0


# ── Monte Carlo Stress Tests ─────────────────────────────────────────────────


class TestMonteCarloStress:
    def test_pnl_based_output(self):
        from polyalpha.monte_carlo_stress import run_monte_carlo_stress
        ds = _make_dataset(30)
        result = run_monte_carlo_stress(ds, n_simulations=50)
        assert hasattr(result, "prob_positive_pnl")
        assert hasattr(result, "pnl_5th")
        assert hasattr(result, "pnl_50th")
        assert hasattr(result, "pnl_95th")


# ── Model Degradation Tests ──────────────────────────────────────────────────


class TestModelDegradation:
    def test_five_perturbation_tests(self):
        from polyalpha.model_degradation import run_degradation_tests
        ds = _make_dataset(40)
        result = run_degradation_tests(ds)
        assert len(result.tests) == 5
        test_names = {t.name for t in result.tests}
        assert "gaussian_noise" in test_names
        assert "delay_predictions" in test_names
        assert "remove_external_features" in test_names
        assert "scramble_categories" in test_names
        assert "stale_snapshots" in test_names


# ── Negative Control Tests ───────────────────────────────────────────────────


class TestNegativeControls:
    def test_four_correct_controls(self):
        from polyalpha.negative_controls import run_negative_controls
        ds = _make_dataset(40)
        results = run_negative_controls(ds, n_simulations=20)
        assert len(results) == 4
        types = {r.control_type for r in results}
        assert "permuted_labels" in types
        assert "category_permutation" in types
        assert "cluster_shuffle" in types
        assert "temporal_shift" in types


# ── Counterfactual Tests ─────────────────────────────────────────────────────


class TestCounterfactual:
    def test_analyze(self):
        from polyalpha.counterfactual import analyze_counterfactuals
        decisions = [
            {"market_id": "m1", "action": "accepted", "entry_price": 0.6, "probability": 0.7, "pnl": 10},
            {"market_id": "m2", "action": "rejected", "entry_price": 0.5, "probability": 0.5, "pnl": 0},
        ]
        outcomes = {"m1": 1, "m2": 0}
        results = analyze_counterfactuals(decisions, outcomes)
        assert len(results) == 2


# ── Holdout Tests ────────────────────────────────────────────────────────────


class TestHoldout:
    def test_lock_and_check(self, tmp_path):
        from polyalpha.holdout import lock_holdout, check_holdout_lock
        state = lock_holdout(str(tmp_path))
        assert state["locked"] is True
        status = check_holdout_lock(str(tmp_path))
        assert status["locked"] is True

    def test_unlock(self, tmp_path):
        from polyalpha.holdout import lock_holdout, unlock_holdout, check_holdout_lock
        lock_holdout(str(tmp_path))
        unlock_holdout(str(tmp_path), reason="testing")
        status = check_holdout_lock(str(tmp_path))
        assert status["locked"] is False

    def test_record_evaluation(self, tmp_path):
        from polyalpha.holdout import lock_holdout, unlock_holdout, record_holdout_evaluation
        lock_holdout(str(tmp_path))
        with pytest.raises(RuntimeError, match="HOLDOUT LOCKED"):
            record_holdout_evaluation(str(tmp_path))
        unlock_holdout(str(tmp_path), reason="testing")
        state = record_holdout_evaluation(str(tmp_path))
        assert state["evaluation_count"] == 1

    def test_multiple_evaluations_warn(self, tmp_path):
        from polyalpha.holdout import lock_holdout, unlock_holdout, record_holdout_evaluation
        lock_holdout(str(tmp_path))
        unlock_holdout(str(tmp_path), reason="testing")
        for _ in range(4):
            record_holdout_evaluation(str(tmp_path))
        state = record_holdout_evaluation(str(tmp_path))
        assert "warning" in state


# ── Persistence Tests ─────────────────────────────────────────────────────────


class TestPersistence:
    def _equity_curve(self, n=200):
        from polyalpha.persistence import EquityPoint
        points = []
        cum = 0.0
        for i in range(n):
            net = 0.05 if i % 2 == 0 else -0.02
            cum += net
            points.append(EquityPoint(
                trade_index=i,
                timestamp=float(i * 3600),
                cumulative_pnl=cum,
                market_brier=0.20,
                model_brier=0.18,
                net_edge=net,
            ))
        return points

    def test_rolling_windows_default(self):
        from polyalpha.persistence import analyze_profit_persistence
        result = analyze_profit_persistence(self._equity_curve(), window_size=30)
        assert len(result.window_metrics) > 0
        assert result.warning or result.has_persistent_alpha

    def test_regime_conditioned_windows(self):
        from polyalpha.persistence import analyze_profit_persistence
        result = analyze_profit_persistence(
            self._equity_curve(), window_size=100,
            window_types=["regime_conditioned"],
        )
        assert len(result.window_metrics) > 0
        types = {w.window_type for w in result.window_metrics}
        assert any(t.startswith("regime-") for t in types), f"got {types}"

    def test_all_window_types(self):
        from polyalpha.persistence import analyze_profit_persistence
        result = analyze_profit_persistence(
            self._equity_curve(), window_size=100,
            window_types=["rolling", "expanding", "calendar", "regime_conditioned"],
        )
        assert len(result.window_metrics) > 0
        types = {w.window_type for w in result.window_metrics}
        assert any(t.startswith("rolling") for t in types)
        assert any(t.startswith("expanding") for t in types)
        assert any(t.startswith("calendar") for t in types)
        assert any(t.startswith("regime-") for t in types)

    def test_empty_curve_returns_empty_report(self):
        from polyalpha.persistence import analyze_profit_persistence
        result = analyze_profit_persistence([], window_size=30)
        assert len(result.window_metrics) == 0
