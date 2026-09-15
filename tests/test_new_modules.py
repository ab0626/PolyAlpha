"""Tests for market_making, alpha_decay, edge_metrics, feature_importance, sizing, pipeline, strategies, experiments."""

from datetime import UTC, datetime
from decimal import Decimal

D = Decimal

# ── market_making tests ──────────────────────────────────────────────────────


class TestMakerQuoter:
    def test_bid_below_fair(self):
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter(base_half_spread=D("0.02"), risk_adjustment=D("0.005"))
        inv = MakerInventory(token_id="t1", side="NONE")
        q = quoter.quote(D("0.60"), inv)
        assert q.side == "BUY"
        assert q.price < D("0.60")
        assert q.price >= D("0.01")

    def test_long_inventory_skews_down(self):
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter(
            base_half_spread=D("0.02"),
            inventory_skew_per_share=D("0.001"),
            risk_adjustment=D("0.005"),
        )
        inv_flat = MakerInventory(token_id="t1", side="NONE")
        inv_long = MakerInventory(token_id="t1", side="BUY", shares=D("500"))
        q_flat = quoter.quote(D("0.60"), inv_flat)
        q_long = quoter.quote(D("0.60"), inv_long)
        assert q_long.price <= q_flat.price

    def test_zero_inventory_no_skew(self):
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter(base_half_spread=D("0.02"), risk_adjustment=D("0.005"))
        inv = MakerInventory(token_id="t1", side="NONE")
        q = quoter.quote(D("0.60"), inv)
        assert q.inventory_skew == D(0)

    def test_maker_inventory_capacity(self):
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter(max_inventory=D("100"))
        inv = MakerInventory(token_id="t1", side="NONE", shares=D("90"))
        q = quoter.quote(D("0.60"), inv)
        assert q.size <= D("100")


class TestMakerFillAnalysis:
    def test_adverse_selection_buy(self):
        from polyalpha.market_making import MakerFillAnalysis

        fill = MakerFillAnalysis(
            token_id="t1",
            side="BUY",
            fill_price=D("0.55"),
            fill_size=D("100"),
            pre_fill_mid=D("0.54"),
            post_fill_mid_1s=D("0.52"),
            post_fill_mid_5s=D("0.51"),
            post_fill_mid_60s=None,
        )
        assert fill.adverse_selection_1s == D("0.02")
        assert fill.adverse_selection_5s == D("0.03")
        assert fill.adverse_selection_60s is None

    def test_adverse_selection_sell(self):
        from polyalpha.market_making import MakerFillAnalysis

        fill = MakerFillAnalysis(
            token_id="t1",
            side="SELL",
            fill_price=D("0.55"),
            fill_size=D("100"),
            pre_fill_mid=D("0.54"),
            post_fill_mid_1s=D("0.56"),
            post_fill_mid_5s=None,
            post_fill_mid_60s=None,
        )
        assert fill.adverse_selection_1s == D("0.02")


class TestMakerPnL:
    def test_estimate_fill_pnl(self):
        from polyalpha.market_making import MakerFillAnalysis, MakerPnLEstimator

        fill = MakerFillAnalysis(
            token_id="t1",
            side="BUY",
            fill_price=D("0.55"),
            fill_size=D("100"),
            pre_fill_mid=D("0.54"),
            post_fill_mid_1s=D("0.52"),
            post_fill_mid_5s=None,
            post_fill_mid_60s=None,
        )
        estimator = MakerPnLEstimator()
        pnl = estimator.estimate_fill_pnl(fill, spread_at_fill=D("0.02"))
        assert D(pnl["spread_capture"]) == D("0.02") * D("100")
        assert D(pnl["net_pnl"]) < D("0.02") * D("100")  # adverse selection reduces pnl

    def test_aggregate_pnl(self):
        from polyalpha.market_making import MakerPnLEstimator

        estimator = MakerPnLEstimator()
        pnls = [
            {
                "spread_capture": "0.50",
                "adverse_selection": "0.20",
                "rebate": "0",
                "net_pnl": "0.30",
            },
            {
                "spread_capture": "0.30",
                "adverse_selection": "0.10",
                "rebate": "0",
                "net_pnl": "0.20",
            },
        ]
        agg = estimator.aggregate_pnl(pnls)
        assert agg["fill_count"] == 2
        assert D(agg["total_spread_capture"]) == D("0.80")


class TestMakerQueue:
    def test_fill_matching_aggressor(self):
        from polyalpha.market_making import MakerQueue

        q = MakerQueue(side="BUY", price=D("0.55"), remaining=D("100"), queue_ahead=D("50"))
        filled = q.on_trade("SELL", D("0.55"), D("80"))
        assert filled == D("30")  # 80 - 50 ahead = 30 for us
        assert q.remaining == D("70")
        assert q.queue_ahead == D(0)

    def test_no_fill_wrong_price(self):
        from polyalpha.market_making import MakerQueue

        q = MakerQueue(side="BUY", price=D("0.55"), remaining=D("100"), queue_ahead=D("0"))
        filled = q.on_trade("SELL", D("0.56"), D("100"))
        assert filled == D(0)


# ── alpha_decay tests ────────────────────────────────────────────────────────


class TestAlphaDecay:
    def test_analyze_alpha_decay_basic(self):
        from polyalpha.alpha_decay import AlphaDecayObservation, analyze_alpha_decay

        obs = [
            AlphaDecayObservation(
                market_id="m1",
                signal_time=datetime(2025, 1, 1, tzinfo=UTC),
                signal_side="YES",
                signal_probability=D("0.65"),
                entry_price=D("0.57"),
                price_observations={30: D("0.58"), 60: D("0.59"), 300: D("0.60")},
            ),
            AlphaDecayObservation(
                market_id="m2",
                signal_time=datetime(2025, 1, 1, tzinfo=UTC),
                signal_side="NO",
                signal_probability=D("0.35"),
                entry_price=D("0.43"),
                price_observations={30: D("0.42"), 60: D("0.41"), 300: D("0.40")},
            ),
        ]
        results = analyze_alpha_decay(obs, horizons=(30, 60, 300))
        assert len(results) == 3
        for r in results:
            assert r.sample_size == 2
            assert 0 <= r.directional_accuracy <= 1

    def test_decay_summary(self):
        from polyalpha.alpha_decay import AlphaDecayResult, decay_summary

        results = [
            AlphaDecayResult(30, 0.005, 0.004, 0.6, 10, 2.1, 0.05),
            AlphaDecayResult(300, 0.002, 0.001, 0.52, 10, 0.5, 0.6),
        ]
        summary = decay_summary(results)
        assert summary["total_horizons"] == 2
        assert summary["best_horizon_seconds"] == 30

    def test_empty_observations(self):
        from polyalpha.alpha_decay import analyze_alpha_decay, decay_summary

        results = analyze_alpha_decay([])
        assert results == []
        summary = decay_summary([])
        assert summary["status"] == "no_data"


# ── edge_metrics tests ───────────────────────────────────────────────────────


class TestEdgeMetrics:
    def test_edge_realization_ratio(self):
        from polyalpha.edge_metrics import EdgeObservation, edge_realization_ratio

        obs = [
            EdgeObservation(
                "m1", "2025-01-01", D("0.64"), D("0.57"), "YES", D("0.07"), 1, D("0.05")
            ),
            EdgeObservation(
                "m2", "2025-01-01", D("0.60"), D("0.55"), "YES", D("0.05"), 1, D("-0.02")
            ),
            EdgeObservation(
                "m3", "2025-01-01", D("0.58"), D("0.52"), "YES", D("0.06"), 0, D("0.08")
            ),
        ]
        result = edge_realization_ratio(obs)
        assert result["ratio"] is not None
        assert result["ratio"] == 2 / 3

    def test_realized_edge_stats(self):
        from polyalpha.edge_metrics import EdgeObservation, realized_edge_stats

        obs = [
            EdgeObservation(
                "m1", "2025-01-01", D("0.64"), D("0.57"), "YES", D("0.07"), 1, D("0.05")
            ),
            EdgeObservation(
                "m2", "2025-01-01", D("0.60"), D("0.55"), "YES", D("0.05"), 1, D("-0.02")
            ),
        ]
        stats = realized_edge_stats(obs)
        assert stats["sample_size"] == 2
        assert stats["win_rate"] == 0.5

    def test_edge_by_bucket(self):
        from polyalpha.edge_metrics import EdgeObservation, edge_by_confidence_bucket

        obs = [
            EdgeObservation("m1", "t", D("0.7"), D("0.6"), "YES", D("0.10"), 1, D("0.05")),
            EdgeObservation("m2", "t", D("0.6"), D("0.55"), "YES", D("0.05"), 1, D("0.02")),
        ]
        buckets = edge_by_confidence_bucket(obs)
        assert len(buckets) >= 1


# ── feature_importance tests ─────────────────────────────────────────────────


class TestFeatureImportance:
    def test_permutation_importance(self):
        from polyalpha.feature_importance import permutation_importance

        class SimpleModel:
            def score(self, features):
                return features.get("x1", 0.5)

        model = SimpleModel()
        X = [{"x1": float(i), "x2": float(i * 2)} for i in range(20)]
        y = [1 if i > 10 else 0 for i in range(20)]

        result = permutation_importance(model, ["x1", "x2"], X, y, n_repeats=3, random_state=42)
        assert len(result) == 2
        # x1 should have higher importance since model depends on it
        assert result[0].feature_name == "x1"
        assert result[0].importance > 0

    def test_detect_leakage(self):
        from polyalpha.feature_importance import FeatureImportance, detect_leakage

        imps = [
            FeatureImportance("good_feature", 0.05, 0.25, 0.20, "positive"),
            FeatureImportance("leaky_feature", -0.10, 0.25, 0.15, "negative"),
        ]
        suspects = detect_leakage(imps, threshold=0.05)
        assert len(suspects) == 1
        assert suspects[0].feature_name == "leaky_feature"

    def test_feature_summary(self):
        from polyalpha.feature_importance import FeatureImportance, feature_summary

        imps = [
            FeatureImportance("f1", 0.10, 0.25, 0.15, "positive"),
            FeatureImportance("f2", -0.02, 0.25, 0.23, "negative"),
        ]
        summary = feature_summary(imps)
        assert summary["total_features"] == 2
        assert len(summary["top_positive_features"]) == 1


# ── sizing tests ─────────────────────────────────────────────────────────────


class TestSizing:
    def test_fixed_fractional(self):
        from polyalpha.sizing import fixed_fractional_sizing

        result = fixed_fractional_sizing(D("10000"), D("0.005"))
        assert result == D("50.00")

    def test_fixed_fractional_max_cap(self):
        from polyalpha.sizing import fixed_fractional_sizing

        result = fixed_fractional_sizing(D("10000"), D("0.005"), max_shares=D("30"))
        assert result == D("30")

    def test_kelly_sizing(self):
        from polyalpha.sizing import kelly_sizing

        result = kelly_sizing(D("0.64"), D("0.57"), D("10000"))
        assert result.shares > 0
        assert result.notional > 0
        assert result.method == "fractional_kelly"
        assert result.kelly_full is not None

    def test_constrained_sizing_market_cap(self):
        from polyalpha.sizing import constrained_sizing

        result = constrained_sizing(
            equity=D("10000"),
            risk_budget=D("500"),
            price=D("0.57"),
            max_market_fraction=D("0.02"),
        )
        assert result.notional <= D("10000") * D("0.02")

    def test_constrained_sizing_risk_budget(self):
        from polyalpha.sizing import constrained_sizing

        result = constrained_sizing(
            equity=D("10000"),
            risk_budget=D("50"),
            price=D("0.57"),
            max_market_fraction=D("0.02"),
        )
        # Risk budget caps notional; small rounding tolerance from quantize
        assert result.notional <= D("51")


# ── pipeline tests ───────────────────────────────────────────────────────────


class TestPipeline:
    def test_claim_deduplication(self):
        from polyalpha.pipeline import ClaimDeduplicator, ClaimExtraction, ExtractedEntity

        dedup = ClaimDeduplicator()
        claim = ClaimExtraction(
            claim_text="Test claim",
            source_url="http://example.com",
            published_at=datetime(2025, 1, 1, tzinfo=UTC),
            retrieved_at=datetime(2025, 1, 1, 0, 1, tzinfo=UTC),
            entities=(ExtractedEntity("person", "Alice", D("0.9")),),
            probability_impact=D("0.02"),
            impact_confidence=D("0.7"),
            category="politics",
            content_hash="abc123",
        )
        assert dedup.is_duplicate(claim) is False
        assert dedup.is_duplicate(claim) is True  # same hash within window

    def test_information_pipeline(self):
        from polyalpha.pipeline import ClaimExtraction, InformationPipeline

        pipeline = InformationPipeline()
        claim = ClaimExtraction(
            claim_text="Breaking news",
            source_url="http://news.com",
            published_at=datetime(2025, 1, 1, tzinfo=UTC),
            retrieved_at=datetime(2025, 1, 1, 0, 5, tzinfo=UTC),
            entities=(),
            probability_impact=D("0.02"),
            impact_confidence=D("0.7"),
            category="crypto",
            content_hash="hash1",
        )
        result = pipeline.process_claim(claim, D("0.50"))
        assert result is not None
        assert D(result["new_probability"]) > D("0.50")
        # Duplicate should return None
        result2 = pipeline.process_claim(claim, D("0.52"))
        assert result2 is None
        summary = pipeline.summary()
        assert summary["claims_processed"] == 2
        assert summary["claims_deduplicated"] == 1

    def test_compute_content_hash(self):
        from polyalpha.pipeline import compute_content_hash

        h1 = compute_content_hash("Hello World", "http://example.com")
        h2 = compute_content_hash("hello world", "http://example.com")
        h3 = compute_content_hash("Different text", "http://example.com")
        assert h1 == h2  # case-insensitive
        assert h1 != h3


# ── strategies tests ─────────────────────────────────────────────────────────


class TestStrategies:
    def test_compare_strategies(self):
        from polyalpha.strategies import StrategyResult, compare_strategies

        r1 = StrategyResult(
            name="A",
            description="baseline",
            brier_score=0.22,
            log_loss=0.5,
            net_pnl=100.0,
            gross_pnl=120.0,
            max_drawdown=0.05,
            sharpe_ratio=1.5,
            total_fills=50,
            fill_rate=0.8,
            average_predicted_edge=0.03,
            calibration_error=0.02,
            resolved_markets=40,
            independent_clusters=10,
            period_start="2025-01-01",
            period_end="2025-06-30",
        )
        r2 = StrategyResult(
            name="B",
            description="ensemble",
            brier_score=0.20,
            log_loss=0.45,
            net_pnl=150.0,
            gross_pnl=175.0,
            max_drawdown=0.03,
            sharpe_ratio=2.0,
            total_fills=60,
            fill_rate=0.85,
            average_predicted_edge=0.04,
            calibration_error=0.015,
            resolved_markets=50,
            independent_clusters=12,
            period_start="2025-01-01",
            period_end="2025-06-30",
        )
        comp = compare_strategies([r1, r2])
        assert comp["strategies"] == 2
        assert "brier_score" in comp["rankings"]
        assert comp["rankings"]["brier_score"][0]["name"] == "B"

    def test_strategy_report(self):
        from polyalpha.strategies import StrategyResult, strategy_report

        r = StrategyResult(
            name="Test",
            description="test strategy",
            brier_score=0.25,
            log_loss=0.6,
            net_pnl=50.0,
            gross_pnl=60.0,
            max_drawdown=0.08,
            sharpe_ratio=1.0,
            total_fills=20,
            fill_rate=0.75,
            average_predicted_edge=0.035,
            calibration_error=0.03,
            resolved_markets=15,
            independent_clusters=5,
            period_start="2025-01-01",
            period_end="2025-06-30",
        )
        report = strategy_report([r])
        assert "Test" in report
        assert "Brier Score" in report

    def test_result_from_report(self):
        from polyalpha.strategies import result_from_report

        report = {
            "net_pnl": "100.50",
            "gross_pnl": "120.00",
            "max_drawdown": "0.05",
            "fills": 30,
            "resolved_markets": 20,
            "calibration": {"brier": 0.22, "log_loss": 0.5, "ece": 0.02, "independent_clusters": 8},
            "decisions": [
                {"reason": "queued", "net_edge": "0.03"},
                {"reason": "filled", "net_edge": "0.04"},
                {"reason": "rejected"},
                {"reason": "filled", "net_edge": "0.02"},
            ],
        }
        result = result_from_report(report, "test")
        assert result.name == "test"
        assert result.total_fills == 30
        assert result.brier_score == 0.22


# ── experiments tests ─────────────────────────────────────────────────────────


class TestExperiments:
    def test_experiment_tracker(self, tmp_path):
        from polyalpha.experiments import ExperimentTracker

        tracker = ExperimentTracker(str(tmp_path))
        exp = tracker.record(
            config={"lr": 0.01},
            metrics={"brier": 0.25},
            model_version="v1.0",
        )
        assert exp.experiment_id
        assert exp.source_sha256
        assert exp.model_version == "v1.0"

        loaded = tracker.load(exp.experiment_id)
        assert loaded is not None
        assert loaded["model_version"] == "v1.0"

    def test_list_experiments(self, tmp_path):
        from polyalpha.experiments import ExperimentTracker

        tracker = ExperimentTracker(str(tmp_path))
        tracker.record(config={}, metrics={})
        tracker.record(config={}, metrics={})
        experiments = tracker.list_experiments()
        assert len(experiments) == 2
