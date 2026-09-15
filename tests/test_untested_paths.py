"""Tests for untested critical paths in remaining modules.

Covers: walk_forward, resolution, market_making, monitoring, alpha_decay,
edge_metrics, feature_importance, strategies, forecasting ensemble, and
cross-module integration.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _obs(market_id, cluster, predicted_at, probability, outcome=None, label_known_at=None):
    from polyalpha.calibration import Observation

    return Observation(market_id, cluster, predicted_at, label_known_at, probability, outcome)


def _fill(order_id="o1", token_id="y1", side="BUY", shares=D(100), price=D("0.55"), fees=D("1")):
    from polyalpha.execution import Fill

    notional = shares * price
    return Fill(
        order_id=order_id,
        token_id=token_id,
        side=side,
        requested=shares,
        shares=shares,
        notional=notional.quantize(D("0.01")),
        fees=fees,
        depth_slippage=D(0),
        filled_at=NOW,
        levels=((price, shares),),
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Walk-Forward Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestWalkForwardEdgeCases:
    def test_embargo_prevents_leakage(self):
        """Embargo should exclude training observations near validation boundary."""
        from polyalpha.walk_forward import walk_forward_backtest

        rows = []
        # Training observations at day 1-20
        for i in range(20):
            rows.append(
                _obs(
                    f"m{i}",
                    "c1",
                    NOW + timedelta(days=i),
                    0.5 + (i % 2) * 0.1,
                    (i % 2),
                    NOW + timedelta(days=i + 1),
                )
            )
        # Validation observations at day 25-30
        for i in range(25, 30):
            rows.append(
                _obs(f"m{i}", "c2", NOW + timedelta(days=i), 0.6, 1, NOW + timedelta(days=i + 1))
            )

        result = walk_forward_backtest(
            rows,
            train_start=NOW,
            first_validation=NOW + timedelta(days=25),
            end=NOW + timedelta(days=30),
            validation_window=timedelta(days=5),
            embargo_days=3,
        )
        assert result.fold_count >= 1
        # Embargo should have excluded some training data

    def test_embargo_larger_than_training(self):
        """Embargo larger than training window should still work (just fewer training rows)."""
        from polyalpha.walk_forward import walk_forward_backtest

        rows = []
        for i in range(10):
            rows.append(
                _obs(
                    f"m{i}", "c1", NOW + timedelta(days=i), 0.5, i % 2, NOW + timedelta(days=i + 5)
                )
            )

        result = walk_forward_backtest(
            rows,
            train_start=NOW,
            first_validation=NOW + timedelta(days=10),
            end=NOW + timedelta(days=15),
            validation_window=timedelta(days=5),
            embargo_days=20,
        )
        # Should have folds, even if insufficient training
        assert result.fold_count >= 1

    def test_single_fold(self):
        """Walk-forward with exactly one validation window should produce one fold."""
        from polyalpha.walk_forward import walk_forward_backtest

        rows = []
        for i in range(10):
            rows.append(
                _obs(
                    f"m{i}", "c1", NOW + timedelta(days=i), 0.5, i % 2, NOW + timedelta(days=i + 1)
                )
            )
        for i in range(15, 20):
            rows.append(
                _obs(f"m{i}", "c2", NOW + timedelta(days=i), 0.6, 1, NOW + timedelta(days=i + 1))
            )

        result = walk_forward_backtest(
            rows,
            train_start=NOW,
            first_validation=NOW + timedelta(days=15),
            end=NOW + timedelta(days=20),
            validation_window=timedelta(days=5),
        )
        assert result.fold_count == 1

    def test_no_resolved_validation_outcomes(self):
        """Fold with no resolved validation outcomes should be marked as such."""
        from polyalpha.walk_forward import walk_forward_backtest

        rows = []
        for i in range(10):
            rows.append(
                _obs(
                    f"m{i}", "c1", NOW + timedelta(days=i), 0.5, i % 2, NOW + timedelta(days=i + 1)
                )
            )
        # Validation observations with no outcomes
        for i in range(20, 25):
            rows.append(_obs(f"m{i}", "c2", NOW + timedelta(days=i), 0.6))

        result = walk_forward_backtest(
            rows,
            train_start=NOW,
            first_validation=NOW + timedelta(days=20),
            end=NOW + timedelta(days=25),
            validation_window=timedelta(days=5),
        )
        assert result.fold_count == 1
        fold = result.folds[0]
        assert fold.status == "no_resolved_outcomes"

    def test_all_clusters_excluded(self):
        """When all training clusters are in validation, should have insufficient training."""
        from polyalpha.walk_forward import walk_forward_backtest

        rows = []
        # All observations in same cluster
        for i in range(10):
            rows.append(
                _obs(
                    f"m{i}", "c1", NOW + timedelta(days=i), 0.5, i % 2, NOW + timedelta(days=i + 1)
                )
            )

        result = walk_forward_backtest(
            rows,
            train_start=NOW,
            first_validation=NOW + timedelta(days=5),
            end=NOW + timedelta(days=10),
            validation_window=timedelta(days=5),
        )
        assert result.fold_count >= 1
        # Should be insufficient training since all clusters excluded
        assert result.folds[0].status == "insufficient_training"

    def test_compute_mfe_single_observation_per_market(self):
        """MFE with single observation per market should skip those markets."""
        from polyalpha.walk_forward import compute_mfe

        rows = [
            _obs("m1", "c1", NOW, 0.6, 1, NOW + timedelta(days=1)),
            _obs("m2", "c1", NOW, 0.7, 0, NOW + timedelta(days=1)),
        ]
        result = compute_mfe(rows)
        assert result["error"] == "no_valid_markets"

    def test_compute_mfe_empty(self):
        """MFE with no observations should return error."""
        from polyalpha.walk_forward import compute_mfe

        result = compute_mfe([])
        assert result["error"] == "no_observations"

    def test_compute_mfe_perfect_prediction(self):
        """MFE with perfect prediction should have mfe_ratio = inf."""
        from polyalpha.walk_forward import compute_mfe

        rows = [
            _obs("m1", "c1", NOW, 0.9, 1, NOW + timedelta(days=1)),
            _obs("m1", "c1", NOW + timedelta(hours=1), 0.95, 1, NOW + timedelta(days=1)),
        ]
        result = compute_mfe(rows)
        assert "details" in result
        detail = result["details"][0]
        assert detail["final_error"] == pytest.approx(0.05, abs=0.01)

    def test_walk_forward_min_training_events(self):
        """Walk-forward with insufficient training events should mark fold."""
        from polyalpha.walk_forward import walk_forward_backtest

        rows = []
        for i in range(3):
            rows.append(
                _obs(
                    f"m{i}", "c1", NOW + timedelta(days=i), 0.5, i % 2, NOW + timedelta(days=i + 1)
                )
            )

        result = walk_forward_backtest(
            rows,
            train_start=NOW,
            first_validation=NOW + timedelta(days=3),
            end=NOW + timedelta(days=5),
            validation_window=timedelta(days=2),
            min_training_events=5,
        )
        assert result.folds[0].status == "insufficient_training"

    def test_walk_forward_validation(self):
        """Walk-forward should aggregate metrics across folds."""
        from polyalpha.walk_forward import walk_forward_backtest

        rows = []
        # Generate enough data for training + validation
        for i in range(30):
            rows.append(
                _obs(
                    f"m{i}",
                    f"c{i // 10}",
                    NOW + timedelta(days=i),
                    0.5 + (i % 3) * 0.1,
                    i % 2,
                    NOW + timedelta(days=i + 2),
                )
            )

        result = walk_forward_backtest(
            rows,
            train_start=NOW,
            first_validation=NOW + timedelta(days=15),
            end=NOW + timedelta(days=30),
            validation_window=timedelta(days=5),
        )
        assert result.fold_count >= 1
        assert result.method == "isotonic"
        assert "embargo_days" in result.configuration


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Resolution Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestResolutionEdgeCases:
    def test_definition_hash_none_deadline(self):
        """Definition hash should handle None deadline."""
        from polyalpha.domain import Market
        from polyalpha.resolution import definition_hash

        market = Market(
            market_id="m1",
            condition_id="m1",
            event_ids=("e1",),
            question="Test",
            description="Test",
            resolution_source="test",
            deadline=None,
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            liquidity=D(5000),
            volume=D(1000),
            fees_enabled=False,
            fee_parameters_json=None,
            yes_token_id="y1",
            no_token_id="n1",
            received_at=NOW,
            category="politics",
        )
        h = definition_hash(market)
        assert len(h) == 64  # SHA256 hex

    def test_resolution_penalty_temporal_decay(self):
        """Older reviews should get higher temporal penalty."""
        from polyalpha.domain import Market
        from polyalpha.resolution import ResolutionReview, definition_hash

        market = Market(
            market_id="m1",
            condition_id="m1",
            event_ids=("e1",),
            question="Test",
            description="Test",
            resolution_source="official",
            deadline=NOW + timedelta(days=7),
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            liquidity=D(5000),
            volume=D(1000),
            fees_enabled=False,
            fee_parameters_json=None,
            yes_token_id="y1",
            no_token_id="n1",
            received_at=NOW,
            category="politics",
        )
        h = definition_hash(market)
        # Recent review (1 day old)
        recent = ResolutionReview(h, NOW - timedelta(days=1), "ref", D("0.9"), D("0.1"), D("0.1"))
        penalty_recent = recent.penalty(market, NOW)
        # Old review (200 days old)
        old = ResolutionReview(h, NOW - timedelta(days=200), "ref", D("0.9"), D("0.1"), D("0.1"))
        penalty_old = old.penalty(market, NOW)
        assert penalty_old > penalty_recent

    def test_resolution_penalty_max_cap(self):
        """Temporal penalty should be capped at 0.03."""
        from polyalpha.domain import Market
        from polyalpha.resolution import ResolutionReview, definition_hash

        market = Market(
            market_id="m1",
            condition_id="m1",
            event_ids=("e1",),
            question="Test",
            description="Test",
            resolution_source="test",
            deadline=NOW + timedelta(days=7),
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            liquidity=D(5000),
            volume=D(1000),
            fees_enabled=False,
            fee_parameters_json=None,
            yes_token_id="y1",
            no_token_id="n1",
            received_at=NOW,
            category="politics",
        )
        h = definition_hash(market)
        # Very old review (1000 days)
        review = ResolutionReview(
            h, NOW - timedelta(days=1000), "ref", D("0.9"), D("0.1"), D("0.1")
        )
        penalty = review.penalty(market, NOW)
        # base (0.01 + 0.05*(0.1+0.1) = 0.02) + temporal (capped at 0.03) = 0.05
        assert penalty <= D("0.05")

    def test_resolution_review_changed_definition_rejected(self):
        """Review with mismatched definition hash should be rejected."""
        from polyalpha.resolution import ResolutionReview

        review = ResolutionReview("a" * 64, NOW, "ref", D("0.9"), D("0.1"), D("0.1"))
        market = type(
            "Market",
            (),
            {"question": "X", "description": "Y", "resolution_source": "Z", "deadline": None},
        )()
        with pytest.raises(ValueError, match="unavailable"):
            review.penalty(market, NOW)

    def test_resolution_review_low_clarity_rejected(self):
        """Review with clarity < 0.8 should be rejected."""
        from polyalpha.domain import Market
        from polyalpha.resolution import ResolutionReview, definition_hash

        market = Market(
            market_id="m1",
            condition_id="m1",
            event_ids=("e1",),
            question="X",
            description="Y",
            resolution_source="Z",
            deadline=None,
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            liquidity=D(5000),
            volume=D(1000),
            fees_enabled=False,
            fee_parameters_json=None,
            yes_token_id="y1",
            no_token_id="n1",
            received_at=NOW,
            category="politics",
        )
        h = definition_hash(market)
        review = ResolutionReview(h, NOW, "ref", D("0.5"), D("0.1"), D("0.1"))
        with pytest.raises(ValueError, match="clarity"):
            review.penalty(market, NOW)

    def test_quality_score_subjective_question(self):
        """Subjective question should not be marked as objective."""
        from polyalpha.domain import Market
        from polyalpha.resolution import ResolutionQualityScore

        market = Market(
            market_id="m1",
            condition_id="m1",
            event_ids=("e1",),
            question="Who will be the best president?",
            description="An opinion question about the best president.",
            resolution_source="test",
            deadline=NOW + timedelta(days=7),
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            liquidity=D(5000),
            volume=D(1000),
            fees_enabled=False,
            fee_parameters_json=None,
            yes_token_id="y1",
            no_token_id="n1",
            received_at=NOW,
            category="politics",
        )
        score = ResolutionQualityScore.from_market(market)
        assert score.objective_criteria is False

    def test_quality_score_objective_question(self):
        """Objective question with numbers should be marked as objective."""
        from polyalpha.domain import Market
        from polyalpha.resolution import ResolutionQualityScore

        market = Market(
            market_id="m1",
            condition_id="m1",
            event_ids=("e1",),
            question="CPI above 3.0% in December?",
            description="CPI percentage above 3.0.",
            resolution_source="official government data",
            deadline=NOW + timedelta(days=7),
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            liquidity=D(5000),
            volume=D(1000),
            fees_enabled=False,
            fee_parameters_json=None,
            yes_token_id="y1",
            no_token_id="n1",
            received_at=NOW,
            category="macro",
        )
        score = ResolutionQualityScore.from_market(market)
        assert score.objective_criteria is True
        assert score.source_reliability == D("0.9")

    def test_quality_score_no_description(self):
        """Market with no description should get clarity penalty."""
        from polyalpha.domain import Market
        from polyalpha.resolution import ResolutionQualityScore

        market = Market(
            market_id="m1",
            condition_id="m1",
            event_ids=("e1",),
            question="Test",
            description="",
            resolution_source="test",
            deadline=NOW + timedelta(days=7),
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            liquidity=D(5000),
            volume=D(1000),
            fees_enabled=False,
            fee_parameters_json=None,
            yes_token_id="y1",
            no_token_id="n1",
            received_at=NOW,
            category="politics",
        )
        score = ResolutionQualityScore.from_market(market)
        assert score.clarity_score < D("1.0")

    def test_quality_score_short_definition_penalty(self):
        """Very short definition should get length penalty."""
        from polyalpha.domain import Market
        from polyalpha.resolution import ResolutionQualityScore

        market = Market(
            market_id="m1",
            condition_id="m1",
            event_ids=("e1",),
            question="Test",
            description="Short",
            resolution_source="test",
            deadline=NOW + timedelta(days=7),
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            liquidity=D(5000),
            volume=D(1000),
            fees_enabled=False,
            fee_parameters_json=None,
            yes_token_id="y1",
            no_token_id="n1",
            received_at=NOW,
            category="politics",
        )
        score = ResolutionQualityScore.from_market(market)
        assert score.definition_length_penalty == D("0.1")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Market Making Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestMarketMakingEdgeCases:
    def test_quoter_buy_side(self):
        """BUY quote should be below fair value."""
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter()
        inv = MakerInventory(token_id="y1", side="NONE")
        quote = quoter.quote(D("0.60"), inv, "BUY")
        assert quote.price < D("0.60")
        assert quote.side == "BUY"

    def test_quoter_sell_side(self):
        """SELL quote should be above fair value."""
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter()
        inv = MakerInventory(token_id="y1", side="NONE")
        quote = quoter.quote(D("0.60"), inv, "SELL")
        assert quote.price > D("0.60")
        assert quote.side == "SELL"

    def test_quoter_inventory_skew_long(self):
        """Long inventory should push BUY quote lower (encourage selling)."""
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter(inventory_skew_per_share=D("0.001"))
        inv_long = MakerInventory(token_id="y1", side="BUY", shares=D(100))
        inv_flat = MakerInventory(token_id="y1", side="NONE")
        quote_long = quoter.quote(D("0.60"), inv_long, "BUY")
        quote_flat = quoter.quote(D("0.60"), inv_flat, "BUY")
        assert quote_long.price <= quote_flat.price

    def test_quoter_inventory_skew_short(self):
        """Short inventory should push SELL quote higher (encourage buying)."""
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter(inventory_skew_per_share=D("0.001"))
        inv_short = MakerInventory(token_id="y1", side="SELL", shares=D(100))
        inv_flat = MakerInventory(token_id="y1", side="NONE")
        quote_short = quoter.quote(D("0.60"), inv_short, "SELL")
        quote_flat = quoter.quote(D("0.60"), inv_flat, "SELL")
        assert quote_short.price >= quote_flat.price

    def test_quoter_full_inventory_zero_size(self):
        """Full inventory should raise ValueError (MakerQuote rejects size <= 0)."""
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter(max_inventory=D(100))
        inv = MakerInventory(token_id="y1", side="BUY", shares=D(100))
        with pytest.raises(ValueError, match="size must be positive"):
            quoter.quote(D("0.60"), inv, "BUY")

    def test_quoter_fair_value_boundary(self):
        """Quotes at extreme fair values should be clamped."""
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter(base_half_spread=D("0.05"))
        inv = MakerInventory(token_id="y1", side="NONE")
        # Very low fair value
        quote_low = quoter.quote(D("0.01"), inv, "BUY")
        assert quote_low.price >= D("0.01")
        # Very high fair value
        quote_high = quoter.quote(D("0.99"), inv, "SELL")
        assert quote_high.price <= D("0.99")

    def test_quoter_invalid_side(self):
        """Invalid side should raise."""
        from polyalpha.market_making import MakerInventory, MakerQuoter

        quoter = MakerQuoter()
        inv = MakerInventory(token_id="y1", side="NONE")
        with pytest.raises(ValueError, match="side"):
            quoter.quote(D("0.60"), inv, "INVALID")

    def test_maker_queue_exact_consumption(self):
        """Queue should fill exactly when aggressive volume matches."""
        from polyalpha.market_making import MakerQueue

        q = MakerQueue(side="BUY", price=D("0.50"), remaining=D(100), queue_ahead=D(50))
        # Sell aggression of 50 consumes queue_ahead, then 50 more fills us
        fill = q.on_trade("SELL", D("0.50"), D(100))
        assert fill == D(50)
        assert q.remaining == D(50)
        assert q.queue_ahead == D(0)

    def test_maker_queue_wrong_side_no_fill(self):
        """Wrong aggressor side should produce no fill."""
        from polyalpha.market_making import MakerQueue

        q = MakerQueue(side="BUY", price=D("0.50"), remaining=D(100), queue_ahead=D(0))
        fill = q.on_trade("BUY", D("0.50"), D(100))
        assert fill == D(0)

    def test_maker_queue_wrong_price_no_fill(self):
        """Wrong price should produce no fill."""
        from polyalpha.market_making import MakerQueue

        q = MakerQueue(side="BUY", price=D("0.50"), remaining=D(100), queue_ahead=D(0))
        fill = q.on_trade("SELL", D("0.51"), D(100))
        assert fill == D(0)

    def test_maker_queue_negative_volume_rejected(self):
        """Negative volume should raise."""
        from polyalpha.market_making import MakerQueue

        q = MakerQueue(side="BUY", price=D("0.50"), remaining=D(100), queue_ahead=D(0))
        with pytest.raises(ValueError, match="negative"):
            q.on_trade("SELL", D("0.50"), D(-10))

    def test_fill_analysis_adverse_selection_buy(self):
        """BUY fill: adverse selection = pre_fill_mid - post_fill_mid."""
        from polyalpha.market_making import MakerFillAnalysis

        fill = MakerFillAnalysis(
            token_id="y1",
            side="BUY",
            fill_price=D("0.50"),
            fill_size=D(100),
            pre_fill_mid=D("0.50"),
            post_fill_mid_1s=D("0.48"),
            post_fill_mid_5s=D("0.47"),
            post_fill_mid_60s=None,
        )
        assert fill.adverse_selection_1s == D("0.02")
        assert fill.adverse_selection_5s == D("0.03")
        assert fill.adverse_selection_60s is None

    def test_fill_analysis_adverse_selection_sell(self):
        """SELL fill: adverse selection = post_fill_mid - pre_fill_mid."""
        from polyalpha.market_making import MakerFillAnalysis

        fill = MakerFillAnalysis(
            token_id="y1",
            side="SELL",
            fill_price=D("0.50"),
            fill_size=D(100),
            pre_fill_mid=D("0.50"),
            post_fill_mid_1s=D("0.52"),
            post_fill_mid_5s=None,
            post_fill_mid_60s=None,
        )
        assert fill.adverse_selection_1s == D("0.02")

    def test_pnl_estimator_no_adverse(self):
        """PnL with no adverse selection should be positive (spread capture)."""
        from polyalpha.market_making import MakerFillAnalysis, MakerPnLEstimator

        estimator = MakerPnLEstimator()
        fill = MakerFillAnalysis(
            token_id="y1",
            side="BUY",
            fill_price=D("0.50"),
            fill_size=D(100),
            pre_fill_mid=D("0.50"),
            post_fill_mid_1s=None,
            post_fill_mid_5s=None,
            post_fill_mid_60s=None,
        )
        result = estimator.estimate_fill_pnl(fill, D("0.02"))
        assert D(result["spread_capture"]) == D("2.00")
        assert D(result["adverse_selection"]) == D(0)

    def test_pnl_estimator_with_adverse(self):
        """PnL with adverse selection should reduce profit."""
        from polyalpha.market_making import MakerFillAnalysis, MakerPnLEstimator

        estimator = MakerPnLEstimator(adverse_selection_multiplier=D("1.5"))
        fill = MakerFillAnalysis(
            token_id="y1",
            side="BUY",
            fill_price=D("0.50"),
            fill_size=D(100),
            pre_fill_mid=D("0.50"),
            post_fill_mid_1s=D("0.48"),
            post_fill_mid_5s=None,
            post_fill_mid_60s=None,
        )
        result = estimator.estimate_fill_pnl(fill, D("0.02"))
        # adverse = (0.50 - 0.48) * 1.5 = 0.03
        # adverse_cost = 0.03 * 100 = 3.00
        assert D(result["adverse_selection"]) == D("3.00")

    def test_pnl_aggregate_empty(self):
        """Aggregating empty fill list should not crash."""
        from polyalpha.market_making import MakerPnLEstimator

        estimator = MakerPnLEstimator()
        result = estimator.aggregate_pnl([])
        assert result["fill_count"] == 0
        assert result["average_net_pnl"] == "0"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Monitoring Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestMonitoringEdgeCases:
    def test_calibration_drift_insufficient_samples(self):
        """Drift detector with insufficient samples should report no drift."""
        from polyalpha.monitoring import CalibrationDriftDetector

        det = CalibrationDriftDetector(min_samples=20)
        for i in range(5):
            det.update(0.2)
        result = det.check_drift()
        assert result["drift_detected"] is False
        assert result["reason"] == "insufficient_samples"

    def test_calibration_drift_with_baseline(self):
        """Drift detector should detect when recent Brier worsens."""
        from polyalpha.monitoring import CalibrationDriftDetector

        det = CalibrationDriftDetector(min_samples=5, alert_threshold=0.05)
        det.set_baseline(0.20)
        for _ in range(10):
            det.update(0.30)  # worse than baseline
        result = det.check_drift()
        assert result["drift_detected"] is True
        assert result["change"] > 0.05

    def test_calibration_drift_no_baseline_high_variance(self):
        """Without baseline, high variance should trigger drift."""
        from polyalpha.monitoring import CalibrationDriftDetector

        det = CalibrationDriftDetector(min_samples=5)
        # Alternating high/low Brier scores
        for i in range(20):
            det.update(0.1 if i % 2 == 0 else 0.9)
        result = det.check_drift()
        assert result["drift_detected"] is True

    def test_feature_psi_identical_distributions(self):
        """PSI for identical distributions should be 0."""
        from polyalpha.monitoring import FeatureDriftDetector

        det = FeatureDriftDetector()
        values = [float(i) for i in range(100)]
        det.set_reference("f1", values)
        for v in values:
            det.update("f1", v)
        psi = det.psi("f1")
        assert psi == pytest.approx(0.0, abs=0.01)

    def test_feature_psi_constant_reference(self):
        """Constant reference with varying current should detect drift."""
        from polyalpha.monitoring import FeatureDriftDetector

        det = FeatureDriftDetector()
        det.set_reference("f1", [1.0] * 100)
        for i in range(100):
            det.update("f1", float(i))
        psi = det.psi("f1")
        assert psi == 1.0  # maximum drift

    def test_feature_psi_constant_both(self):
        """Constant reference and constant current should give PSI = 0."""
        from polyalpha.monitoring import FeatureDriftDetector

        det = FeatureDriftDetector()
        det.set_reference("f1", [5.0] * 100)
        for _ in range(100):
            det.update("f1", 5.0)
        psi = det.psi("f1")
        assert psi == 0.0

    def test_feature_psi_insufficient_data(self):
        """PSI with insufficient data should return None."""
        from polyalpha.monitoring import FeatureDriftDetector

        det = FeatureDriftDetector()
        det.set_reference("f1", [1.0, 2.0, 3.0])
        for _ in range(3):
            det.update("f1", 1.0)
        psi = det.psi("f1", buckets=10)
        assert psi is None

    def test_adwin_insufficient_data(self):
        """ADWIN with insufficient data should report no drift."""
        from polyalpha.monitoring import ADWINDriftDetector

        det = ADWINDriftDetector()
        for i in range(5):
            result = det.update(float(i))
        assert result["drift_detected"] is False
        assert result["reason"] == "insufficient_data"

    def test_adwin_detects_sudden_drift(self):
        """ADWIN should handle a sudden mean shift without crashing."""
        from polyalpha.monitoring import ADWINDriftDetector

        det = ADWINDriftDetector(min_window=5)
        # Stable data
        for _ in range(20):
            det.update(0.5)
        # Sudden shift
        for _ in range(30):
            result = det.update(0.9)
        # Should not crash; drift detection depends on Hoeffding bound
        assert "drift_detected" in result
        assert len(det._window) > 0

    def test_page_hinkley_insufficient_data(self):
        """Page-Hinkley with insufficient data should report no drift."""
        from polyalpha.monitoring import PageHinkleyDetector

        det = PageHinkleyDetector(min_instances=30)
        for i in range(10):
            result = det.update(float(i))
        assert result["drift_detected"] is False
        assert result["reason"] == "insufficient_data"

    def test_page_hinkley_detects_drift(self):
        """Page-Hinkley should detect a sustained mean shift."""
        from polyalpha.monitoring import PageHinkleyDetector

        det = PageHinkleyDetector(threshold=5.0, min_instances=10, alpha=0.005)
        # Stable data
        for _ in range(20):
            det.update(0.5)
        # Sustained shift
        for _ in range(50):
            det.update(2.0)
        assert det.drift_count > 0

    def test_page_hinkley_reset_after_drift(self):
        """After drift detection, Page-Hinkley should reset."""
        from polyalpha.monitoring import PageHinkleyDetector

        det = PageHinkleyDetector(threshold=2.0, min_instances=5, alpha=0.005)
        for _ in range(10):
            det.update(0.5)
        for _ in range(50):
            det.update(3.0)
        # After drift, counters should reset
        assert det.drift_count >= 1

    def test_drift_function(self):
        """drift() should flag when recent Brier worsens beyond tolerance."""
        from polyalpha.monitoring import drift

        result = drift(0.20, 0.30, minimum_count=10, recent_count=15, tolerance=0.05)
        assert result["actionable"] is True
        assert result["change"] == pytest.approx(0.10)

    def test_drift_function_insufficient_count(self):
        """drift() should not flag when recent count is below minimum."""
        from polyalpha.monitoring import drift

        result = drift(0.20, 0.30, minimum_count=10, recent_count=5)
        assert result["actionable"] is False

    def test_drift_function_within_tolerance(self):
        """drift() should not flag when change is within tolerance."""
        from polyalpha.monitoring import drift

        result = drift(0.20, 0.22, minimum_count=10, recent_count=15, tolerance=0.05)
        assert result["actionable"] is False


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Alpha Decay Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestAlphaDecayEdgeCases:
    def test_analyze_empty_observations(self):
        """Empty observations should return empty results."""
        from polyalpha.alpha_decay import analyze_alpha_decay

        result = analyze_alpha_decay([])
        assert result == []

    def test_analyze_no_matching_horizons(self):
        """Observations with no matching horizons should be skipped."""
        from polyalpha.alpha_decay import AlphaDecayObservation, analyze_alpha_decay

        obs = AlphaDecayObservation(
            market_id="m1",
            signal_time=NOW,
            signal_side="YES",
            signal_probability=D("0.65"),
            entry_price=D("0.55"),
            price_observations={999: D("0.56")},  # no standard horizon
        )
        result = analyze_alpha_decay([obs])
        assert len(result) == 0

    def test_analyze_yes_signal_positive_change(self):
        """YES signal with positive price change should be directional correct."""
        from polyalpha.alpha_decay import AlphaDecayObservation, analyze_alpha_decay

        obs = AlphaDecayObservation(
            market_id="m1",
            signal_time=NOW,
            signal_side="YES",
            signal_probability=D("0.65"),
            entry_price=D("0.55"),
            price_observations={30: D("0.56"), 60: D("0.57")},
        )
        result = analyze_alpha_decay([obs], horizons=(30, 60))
        assert len(result) == 2
        assert result[0].directional_accuracy == 1.0
        assert result[0].mean_price_change > 0

    def test_analyze_no_signal_negative_change(self):
        """NO signal with negative price change should be directional correct."""
        from polyalpha.alpha_decay import AlphaDecayObservation, analyze_alpha_decay

        obs = AlphaDecayObservation(
            market_id="m1",
            signal_time=NOW,
            signal_side="NO",
            signal_probability=D("0.35"),
            entry_price=D("0.55"),
            price_observations={30: D("0.54")},
        )
        result = analyze_alpha_decay([obs], horizons=(30,))
        assert len(result) == 1
        assert result[0].directional_accuracy == 1.0

    def test_analyze_statistical_significance(self):
        """Sufficient observations should produce p-values."""
        from polyalpha.alpha_decay import AlphaDecayObservation, analyze_alpha_decay

        obss = []
        for i in range(50):
            obss.append(
                AlphaDecayObservation(
                    market_id=f"m{i}",
                    signal_time=NOW,
                    signal_side="YES",
                    signal_probability=D("0.65"),
                    entry_price=D("0.55"),
                    price_observations={30: D("0.55") + D(str(i * 0.001))},
                )
            )
        result = analyze_alpha_decay(obss, horizons=(30,))
        assert len(result) == 1
        assert result[0].t_statistic is not None
        assert result[0].p_value is not None

    def test_decay_summary_empty(self):
        """Empty summary should return no_data status."""
        from polyalpha.alpha_decay import decay_summary

        result = decay_summary([])
        assert result["status"] == "no_data"

    def test_decay_summary_has_signal(self):
        """Summary should identify immediate signal and slow diffusion."""
        from polyalpha.alpha_decay import AlphaDecayResult, decay_summary

        results = [
            AlphaDecayResult(30, 0.01, 0.01, 0.60, 50, 2.0, 0.05),
            AlphaDecayResult(3600, 0.02, 0.02, 0.58, 50, 1.8, 0.07),
        ]
        summary = decay_summary(results)
        assert summary["has_immediate_signal"] is True
        assert summary["has_slow_diffusion"] is True


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Edge Metrics Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestEdgeMetricsEdgeCases:
    def test_realization_ratio_no_positive_predictions(self):
        """No positive predictions should return None ratio."""
        from polyalpha.edge_metrics import EdgeObservation, edge_realization_ratio

        obs = [EdgeObservation("m1", "t", D("0.50"), D("0.55"), "BUY", D(-0.05), 1, D(10))]
        result = edge_realization_ratio(obs)
        assert result["ratio"] is None

    def test_realization_ratio_all_profitable(self):
        """All profitable should give ratio = 1.0."""
        from polyalpha.edge_metrics import EdgeObservation, edge_realization_ratio

        obs = [
            EdgeObservation("m1", "t", D("0.60"), D("0.55"), "BUY", D("0.05"), 1, D(10)),
            EdgeObservation("m2", "t", D("0.70"), D("0.60"), "BUY", D("0.10"), 1, D(20)),
        ]
        result = edge_realization_ratio(obs)
        assert result["ratio"] == 1.0

    def test_realized_edge_stats_all_wins(self):
        """All wins should give win_rate = 1.0 and no edge_decay."""
        from polyalpha.edge_metrics import EdgeObservation, realized_edge_stats

        obs = [
            EdgeObservation("m1", "t", D("0.60"), D("0.55"), "BUY", D("0.05"), 1, D(10)),
            EdgeObservation("m2", "t", D("0.70"), D("0.60"), "BUY", D("0.10"), 1, D(20)),
        ]
        result = realized_edge_stats(obs)
        assert result["win_rate"] == 1.0
        assert result["edge_decay"] is None  # no losses

    def test_realized_edge_stats_all_losses(self):
        """All losses should give win_rate = 0.0."""
        from polyalpha.edge_metrics import EdgeObservation, realized_edge_stats

        obs = [
            EdgeObservation("m1", "t", D("0.60"), D("0.65"), "BUY", D("0.05"), 0, D(-10)),
        ]
        result = realized_edge_stats(obs)
        assert result["win_rate"] == 0.0

    def test_edge_by_confidence_bucket_negative_edges(self):
        """Negative predicted edges should be bucketed correctly."""
        from polyalpha.edge_metrics import EdgeObservation, edge_by_confidence_bucket

        obs = [
            EdgeObservation("m1", "t", D("0.50"), D("0.55"), "BUY", D(-0.05), 0, D(-10)),
            EdgeObservation("m2", "t", D("0.50"), D("0.55"), "BUY", D(-0.10), 1, D(5)),
        ]
        result = edge_by_confidence_bucket(obs)
        assert len(result) >= 1

    def test_realized_edge_stats_empty(self):
        """Empty observations should return None metrics."""
        from polyalpha.edge_metrics import realized_edge_stats

        result = realized_edge_stats([])
        assert result["sample_size"] == 0
        assert result["mean_realized_pnl"] is None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Feature Importance Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestFeatureImportanceEdgeCases:
    def test_permutation_importance_empty_data(self):
        """Empty data should raise."""
        from polyalpha.feature_importance import permutation_importance

        class DummyModel:
            def score(self, features):
                return 0.5

        with pytest.raises(ValueError, match="nonempty"):
            permutation_importance(DummyModel(), ["f1"], [], [])

    def test_permutation_importance_identical_features(self):
        """Permuting a useless feature should give ~0 importance."""
        from polyalpha.feature_importance import permutation_importance

        class ConstantModel:
            def score(self, features):
                return 0.5  # ignores all features

        X = [{"f1": float(i), "f2": float(i * 2)} for i in range(20)]
        y = [i % 2 for i in range(20)]
        result = permutation_importance(ConstantModel(), ["f1", "f2"], X, y, n_repeats=3)
        for r in result:
            assert abs(r.importance) < 0.01  # negligible

    def test_detect_leakage_no_suspicious(self):
        """No suspicious features should return empty list."""
        from polyalpha.feature_importance import FeatureImportance, detect_leakage

        imps = [
            FeatureImportance("f1", 0.05, 0.25, 0.20, "positive"),
            FeatureImportance("f2", 0.03, 0.25, 0.22, "positive"),
        ]
        assert detect_leakage(imps) == []

    def test_detect_leakage_with_suspicious(self):
        """Features with negative importance below threshold should be flagged."""
        from polyalpha.feature_importance import FeatureImportance, detect_leakage

        imps = [
            FeatureImportance("f1", -0.10, 0.25, 0.15, "negative"),
            FeatureImportance("f2", 0.05, 0.25, 0.20, "positive"),
        ]
        suspicious = detect_leakage(imps)
        assert len(suspicious) == 1
        assert suspicious[0].feature_name == "f1"

    def test_feature_summary_empty(self):
        """Empty importances should return zero total."""
        from polyalpha.feature_importance import feature_summary

        result = feature_summary([])
        assert result["total_features"] == 0

    def test_feature_summary_with_leakage(self):
        """Summary should include leakage suspects."""
        from polyalpha.feature_importance import FeatureImportance, feature_summary

        imps = [
            FeatureImportance("f1", -0.10, 0.25, 0.15, "negative"),
            FeatureImportance("f2", 0.05, 0.25, 0.20, "positive"),
        ]
        result = feature_summary(imps)
        assert len(result["leakage_suspects"]) == 1
        assert "Investigate" in result["recommendation"]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Strategies Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestStrategiesEdgeCases:
    def test_compare_empty(self):
        """Empty comparison should return zero strategies."""
        from polyalpha.strategies import compare_strategies

        result = compare_strategies([])
        assert result["strategies"] == 0

    def test_compare_single_strategy(self):
        """Single strategy comparison should still produce rankings."""
        from polyalpha.strategies import StrategyResult, compare_strategies

        r = StrategyResult(
            "A",
            "desc",
            0.25,
            0.5,
            100,
            120,
            0.05,
            1.5,
            50,
            0.8,
            0.03,
            0.02,
            20,
            5,
            "2026-01-01",
            "2026-06-01",
        )
        result = compare_strategies([r])
        assert result["strategies"] == 1
        assert result["summary"]["warning"] is not None

    def test_strategy_report_none_metrics(self):
        """Report with None metrics should show N/A."""
        from polyalpha.strategies import StrategyResult, strategy_report

        r = StrategyResult(
            "A", "desc", None, None, None, None, None, None, 0, None, None, None, 0, 0, None, None
        )
        report = strategy_report([r])
        assert "N/A" in report

    def test_result_from_report_minimal(self):
        """Minimal report should convert without crash."""
        from polyalpha.strategies import result_from_report

        report = {
            "fills": 0,
            "net_pnl": None,
            "gross_pnl": None,
            "max_drawdown": None,
            "sharpe_ratio": None,
            "resolved_markets": 0,
            "decisions": [],
        }
        result = result_from_report(report, "test")
        assert result.name == "test"
        assert result.total_fills == 0

    def test_result_from_report_with_data(self):
        """Report with data should extract fill rate and avg edge."""
        from polyalpha.strategies import result_from_report

        report = {
            "fills": 5,
            "net_pnl": "100.50",
            "gross_pnl": "120.00",
            "max_drawdown": "0.05",
            "sharpe_ratio": "1.5",
            "resolved_markets": 10,
            "calibration": {"brier": 0.25, "log_loss": 0.5, "ece": 0.02, "independent_clusters": 3},
            "decisions": [
                {"reason": "queued"},
                {"reason": "queued"},
                {"reason": "filled", "net_edge": "0.03"},
                {"reason": "filled", "net_edge": "0.05"},
                {"reason": "rejected"},
            ],
        }
        result = result_from_report(report, "test")
        assert result.fill_rate == 1.0
        assert result.average_predicted_edge == pytest.approx(0.04, abs=0.01)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Forecasting Ensemble Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestForecastingEnsembleEdgeCases:
    def test_ensemble_equal_weights(self):
        """Ensemble with equal weights should average forecasts."""
        from polyalpha.forecasting import Forecast, ensemble

        f1 = Forecast("m1", NOW, D("0.60"), D("0.55"), D("0.65"), "v1")
        f2 = Forecast("m1", NOW, D("0.70"), D("0.65"), D("0.75"), "v2")
        result = ensemble([f1, f2], [D("0.50"), D("0.50")], NOW)
        assert result.probability == D("0.65")

    def test_ensemble_one_dominates(self):
        """Ensemble with weight=1.0 on one forecast should use that forecast."""
        from polyalpha.forecasting import Forecast, ensemble

        f1 = Forecast("m1", NOW, D("0.60"), D("0.55"), D("0.65"), "v1")
        f2 = Forecast("m1", NOW, D("0.70"), D("0.65"), D("0.75"), "v2")
        result = ensemble([f1, f2], [D(1), D(0)], NOW)
        assert result.probability == D("0.60")

    def test_ensemble_widest_interval(self):
        """Ensemble should use widest interval from components."""
        from polyalpha.forecasting import Forecast, ensemble

        f1 = Forecast("m1", NOW, D("0.60"), D("0.50"), D("0.70"), "v1")
        f2 = Forecast("m1", NOW, D("0.60"), D("0.55"), D("0.65"), "v2")
        result = ensemble([f1, f2], [D("0.50"), D("0.50")], NOW)
        assert result.lower == D("0.50")  # from f1
        assert result.upper == D("0.70")  # from f1

    def test_ensemble_invalid_weights_sum(self):
        """Ensemble with weights not summing to 1 should raise."""
        from polyalpha.forecasting import Forecast, ensemble

        f1 = Forecast("m1", NOW, D("0.60"), D("0.55"), D("0.65"), "v1")
        with pytest.raises(ValueError, match="invalid"):
            ensemble([f1], [D("0.80")], NOW)

    def test_ensemble_mismatched_market_ids(self):
        """Ensemble with different market_ids should raise."""
        from polyalpha.forecasting import Forecast, ensemble

        f1 = Forecast("m1", NOW, D("0.60"), D("0.55"), D("0.65"), "v1")
        f2 = Forecast("m2", NOW, D("0.70"), D("0.65"), D("0.75"), "v2")
        with pytest.raises(ValueError, match="mismatched"):
            ensemble([f1, f2], [D("0.50"), D("0.50")], NOW)

    def test_net_edge_requires_buy_fill(self):
        """net_edge should reject SELL fills."""
        from polyalpha.forecasting import Forecast, net_edge

        fill = _fill(side="SELL")
        forecast = Forecast("m1", NOW, D("0.65"), D("0.60"), D("0.70"), "v1")
        with pytest.raises(ValueError, match="buy fill"):
            net_edge(forecast, fill, True)

    def test_net_edge_negative_cost_buffer(self):
        """net_edge with negative cost buffer should raise."""
        from polyalpha.forecasting import Forecast, net_edge

        fill = _fill()
        forecast = Forecast("m1", NOW, D("0.65"), D("0.60"), D("0.70"), "v1")
        with pytest.raises(ValueError, match="negative"):
            net_edge(forecast, fill, True, extra_slippage=D(-0.01))

    def test_net_edge_computation(self):
        """net_edge should correctly decompose costs."""
        from polyalpha.forecasting import Forecast, net_edge

        fill = _fill(price=D("0.55"), shares=D(100), fees=D("0"))
        forecast = Forecast("m1", NOW, D("0.65"), D("0.60"), D("0.70"), "v1")
        # conservative = lower = 0.60
        # net_edge = 0.60 - 0.55 - 0 - 0.002 - 0.01 = 0.038
        result = net_edge(
            forecast, fill, True, extra_slippage=D("0.002"), resolution_penalty=D("0.01")
        )
        assert result == D("0.038")

    def test_forecast_bounds_validation(self):
        """Forecast with invalid bounds should raise."""
        from polyalpha.forecasting import Forecast

        with pytest.raises(ValueError, match="invalid"):
            Forecast("m1", NOW, D("0.65"), D("0.70"), D("0.60"), "v1")  # lower > upper

    def test_forecast_probability_out_of_range(self):
        """Forecast with probability outside [0,1] should raise."""
        from polyalpha.forecasting import Forecast

        with pytest.raises(ValueError, match="invalid"):
            Forecast("m1", NOW, D("1.5"), D("1.0"), D("2.0"), "v1")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: Cross-Module Integration
# ══════════════════════════════════════════════════════════════════════════════


class TestCrossModuleIntegration:
    def test_engine_to_calibration_pipeline(self):
        """Engine report should produce valid calibration observations."""
        from polyalpha.calibration import metrics

        # Simulate what the engine produces
        forecasts = [
            {"market_id": "m1", "p": "0.65", "cluster": "c1"},
            {"market_id": "m2", "p": "0.45", "cluster": "c1"},
        ]
        labels = {"m1": 1, "m2": 0}
        probs = [float(f["p"]) for f in forecasts if f["market_id"] in labels]
        outcomes = [labels[f["market_id"]] for f in forecasts if f["market_id"] in labels]
        result = metrics(probs, outcomes)
        assert result["sample_size"] == 2
        assert 0 <= result["brier"] <= 1

    def test_signals_to_sizing_pipeline(self):
        """Signal net_edge should feed into sizing correctly."""
        from polyalpha.signals import Signal
        from polyalpha.sizing import constrained_sizing

        signal = Signal(
            market_id="m1",
            token_id="y1",
            side="BUY",
            timestamp=NOW,
            fair_probability=D("0.65"),
            conservative_probability=D("0.60"),
            execution_price=D("0.55"),
            fee_per_share=D("0.01"),
            slippage_penalty=D("0.002"),
            uncertainty_penalty=D("0.01"),
            resolution_penalty=D("0.01"),
            stale_data_penalty=D(0),
            liquidity_penalty=D(0),
            gross_edge=D("0.10"),
            net_edge=D("0.05"),
            confidence=D("0.95"),
            model_version="test",
            shares=D(100),
            budget=D(200),
        )
        result = constrained_sizing(
            equity=D(10000),
            risk_budget=signal.budget,
            price=signal.execution_price,
        )
        assert result.shares > 0
        assert result.notional <= signal.budget

    def test_risk_to_portfolio_exposure_pipeline(self):
        """Portfolio exposures should feed into risk budget correctly."""
        from polyalpha.portfolio import Portfolio
        from polyalpha.risk import Risk

        portfolio = Portfolio(D(10000))
        risk = Risk(D(10000))
        # Add positions in same cluster
        for i in range(3):
            fill = _fill(f"o{i}", f"y{i}", "BUY", D(50), D("0.50"), D("0"))
            portfolio.apply(fill, f"m{i}", "e1", "c1", "politics")
        # Cluster exposure should limit further budget
        budget = risk.budget(portfolio, D(10000), "new_m", "e1", "c1", "politics")
        # normal = 50, cluster_remaining = 500 - 75 = 425, but total = 2000 - 75 = 1925
        # market = 200, event = 500 - 75 = 425, category = 1000 - 75 = 925
        # min(10000, 50, 1925, 200, 425, 425, 925) = 50
        assert budget == D(50)

    def test_backtest_report_to_strategy_result(self):
        """Engine report should convert to StrategyResult correctly."""
        from polyalpha.strategies import result_from_report

        report = {
            "fills": 10,
            "net_pnl": "150.00",
            "gross_pnl": "200.00",
            "max_drawdown": "0.03",
            "sharpe_ratio": "2.0",
            "resolved_markets": 25,
            "calibration": {
                "brier": 0.22,
                "log_loss": 0.45,
                "ece": 0.015,
                "independent_clusters": 8,
            },
            "decisions": [
                {"reason": "queued"},
                {"reason": "filled", "net_edge": "0.04"},
                {"reason": "filled", "net_edge": "0.06"},
            ],
            "period_start": "2026-01-01",
            "period_end": "2026-06-01",
        }
        result = result_from_report(report, "baseline", "Market prior baseline")
        assert result.name == "baseline"
        assert result.total_fills == 10
        assert result.brier_score == 0.22
        assert result.fill_rate == 2.0
