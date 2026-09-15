"""Integration tests for end-to-end flows.

Tests complete pipelines from data ingestion through analysis,
ensuring modules work together correctly.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polyalpha.calibration import Observation
from polyalpha.calibration import metrics as cal_metrics
from polyalpha.correlation import CorrelationMatrix
from polyalpha.domain import Book, Level, Market
from polyalpha.edge_metrics import EdgeObservation, edge_realization_ratio
from polyalpha.expected_value import calculate_gross_edge, kelly_fraction
from polyalpha.forecasting import Baseline, Forecast
from polyalpha.portfolio import Portfolio
from polyalpha.quality import Filter
from polyalpha.relative_value import Constraint, violations
from polyalpha.risk import Risk

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class ChangeModel:
    """Simple model that predicts high probability to always trigger trades."""

    def predict(self, market_id, book, at):
        p = D("0.85")
        return Forecast(market_id, at, p, p - D("0.03"), p + D("0.03"), "change-model-v1")


def _market(
    market_id="m1",
    yes_token="y1",
    no_token="n1",
    event_id="e1",
    category="politics",
    fees_enabled=False,
):
    return Market(
        market_id=market_id,
        condition_id=market_id,
        event_ids=(event_id,),
        question=f"Test {market_id}",
        description="Integration test market",
        resolution_source="test",
        deadline=NOW + timedelta(days=7),
        active=True,
        closed=False,
        accepting_orders=True,
        enable_order_book=True,
        liquidity=D(5000),
        volume=D(1000),
        fees_enabled=fees_enabled,
        fee_parameters_json=None,
        yes_token_id=yes_token,
        no_token_id=no_token,
        received_at=NOW,
        category=category,
    )


def _book(
    token_id="y1", bid_price=D("0.49"), ask_price=D("0.51"), bid_size=D(1000), ask_size=D(1000)
):
    return Book(
        token_id=token_id,
        condition_id="m1",
        source_at=NOW,
        received_at=NOW,
        bids=(Level(bid_price, bid_size),),
        asks=(Level(ask_price, ask_size),),
        tick_size=D("0.01"),
        min_order_size=D(1),
        source_hash="test",
    )


# ─── End-to-End: Forecasting Pipeline ────────────────────────────────────────


class TestForecastingPipeline:
    """Test forecast generation and calibration pipeline."""

    def test_baseline_forecast(self):
        baseline = Baseline(D("0.05"))
        book = _book()
        forecast = baseline.predict("m1", book, NOW)

        assert isinstance(forecast, Forecast)
        assert forecast.market_id == "m1"
        assert D(0) <= forecast.probability <= D(1)

    def test_forecast_to_calibration(self):
        baseline = Baseline(D("0.05"))
        book = _book()

        forecasts = []
        for i in range(10):
            forecast = baseline.predict(f"m{i}", book, NOW)
            forecasts.append(forecast)

        observations = [
            Observation(
                market_id=f"m{i}",
                cluster="c1",
                predicted_at=NOW,
                label_known_at=NOW + timedelta(days=1),
                probability=float(forecast.probability),
                outcome=i % 2,
            )
            for i, forecast in enumerate(forecasts)
        ]

        result = cal_metrics(
            [o.probability for o in observations],
            [o.outcome for o in observations],
        )

        assert "brier" in result
        assert "log_loss" in result
        assert "ece" in result
        assert 0 <= result["brier"] <= 1


# ─── End-to-End: Risk Management Pipeline ────────────────────────────────────


class TestRiskManagementPipeline:
    """Test risk budget calculation and constraint checking."""

    def test_risk_budget_calculation(self):
        portfolio = Portfolio(D(10000))
        risk = Risk(D(10000))

        budget = risk.budget(
            portfolio,
            D(10000),
            market=1,
            event=1,
            cluster=1,
            category=1,
        )
        assert budget > 0
        assert budget <= D(10000)

    def test_constraint_checking(self):
        constraints = [
            Constraint(
                kind="partition",
                markets=("m1", "m2"),
                review_reference="test",
                reviewed_at=NOW,
            ),
        ]
        probs = {"m1": D("0.6"), "m2": D("0.4")}
        v = violations(constraints, probs, NOW)
        assert len(v) == 0

        probs = {"m1": D("0.7"), "m2": D("0.4")}
        v = violations(constraints, probs, NOW)
        assert len(v) == 1


# ─── End-to-End: Expected Value Pipeline ─────────────────────────────────────


class TestExpectedValuePipeline:
    """Test expected value calculation with full cost decomposition."""

    def test_edge_calculation(self):
        edge = calculate_gross_edge(D("0.60"), D("0.50"), "YES")
        assert edge == D("0.10")

    def test_kelly_sizing(self):
        kelly = kelly_fraction(D("0.60"), D("0.50"))
        assert kelly > 0
        assert kelly <= 1

    def test_edge_to_sizing_pipeline(self):
        forecast_prob = D("0.65")
        execution_price = D("0.50")
        bankroll = D(10000)

        edge = calculate_gross_edge(forecast_prob, execution_price, "YES")
        kelly_frac = kelly_fraction(forecast_prob, execution_price)
        position_size = bankroll * kelly_frac

        assert edge > 0
        assert kelly_frac > 0
        assert position_size > 0
        assert position_size <= bankroll


# ─── End-to-End: Correlation Pipeline ────────────────────────────────────────


class TestCorrelationPipeline:
    """Test correlation analysis and constraint generation."""

    def test_correlation_matrix_construction(self):
        tokens = ["y1", "y2", "y3", "n1", "n2", "n3"]
        matrix = CorrelationMatrix(tokens=tokens)

        matrix.set("y1", "y2", D("0.8"))
        matrix.set("y1", "n1", D("-0.9"))

        assert matrix.get("y1", "y2") == D("0.8")
        assert matrix.get("y1", "n1") == D("-0.9")
        assert matrix.get("y2", "y3") == D(0)

    def test_high_correlation_detection(self):
        tokens = ["y1", "y2", "y3"]
        matrix = CorrelationMatrix(tokens=tokens)
        matrix.set("y1", "y2", D("0.95"))
        matrix.set("y1", "y3", D("0.3"))
        matrix.set("y2", "y3", D("0.4"))

        high = matrix.highly_correlated(D("0.9"))
        assert len(high) == 1
        assert high[0][0] == "y1"
        assert high[0][1] == "y2"


# ─── End-to-End: Edge Metrics Pipeline ───────────────────────────────────────


class TestEdgeMetricsPipeline:
    """Test edge realization tracking."""

    def test_edge_realization(self):
        observations = [
            EdgeObservation(
                market_id=f"m{i}",
                predicted_at=NOW.isoformat(),
                fair_probability=D("0.60"),
                execution_price=D("0.50"),
                side="YES",
                predicted_edge=D("0.10"),
                realized_outcome=i % 2,
                realized_pnl=D("0.05") if i % 2 == 0 else D("-0.03"),
            )
            for i in range(20)
        ]

        result = edge_realization_ratio(observations)
        assert "ratio" in result
        assert result["total_predicted_positive"] > 0


# ─── End-to-End: Quality Filtering Pipeline ──────────────────────────────────


class TestQualityFilterPipeline:
    """Test quality filtering of markets."""

    def test_filter_pass(self):
        f = Filter(
            min_liquidity=D(100),
            min_volume=D(10),
            max_spread=D("0.10"),
            min_depth_shares=D(50),
        )
        market = _market()
        reasons = f.market_reasons(market, NOW)
        assert len(reasons) == 0

    def test_filter_reject_low_liquidity(self):
        f = Filter(min_liquidity=D(10000))
        market = _market()
        reasons = f.market_reasons(market, NOW)
        assert len(reasons) > 0


# ─── End-to-End: Experiment Tracking Pipeline ────────────────────────────────


class TestExperimentTrackingPipeline:
    """Test experiment recording and retrieval."""

    def test_experiment_record_creation(self):
        from polyalpha.experiments import ExperimentRecord

        record = ExperimentRecord(
            experiment_id="exp-001",
            git_commit="abc123",
            source_sha256="deadbeef",
            model_version="baseline-v1",
            feature_version="book-v1",
            training_window=None,
            validation_window=None,
            configuration={"min_edge": 0.025},
            data_sha256="deadbeef",
            metrics={"brier": 0.25, "log_loss": 0.5},
            created_at=NOW.isoformat(),
        )

        d = record.to_dict()
        assert d["experiment_id"] == "exp-001"
        assert d["metrics"]["brier"] == 0.25


# ─── End-to-End: Multiple Market Scenario ────────────────────────────────────


class TestMultipleMarketScenario:
    """Test a realistic multi-market scenario."""

    def test_portfolio_with_multiple_positions(self):
        portfolio = Portfolio(D(10000))
        risk = Risk(D(10000))

        markets = [
            ("m1", "e1", "c1", "politics"),
            ("m2", "e1", "c1", "politics"),
            ("m3", "e2", "c2", "crypto"),
        ]

        budgets = []
        for mid, eid, cid, cat in markets:
            budget = risk.budget(
                portfolio,
                D(10000),
                market=1,
                event=1,
                cluster=1,
                category=1,
            )
            budgets.append(budget)

        assert all(b > 0 for b in budgets)


# ─── Section 52: Example Signal ──────────────────────────────────────────────


class TestSection52ExampleSignal:
    """Test the exact example from Section 52 of the spec.

    Model probability = 0.64
    Order book: 0.56 x 50, 0.57 x 150, 0.58 x 1000
    Desired: 300 shares
    Expected VWAP = 0.571666...
    Expected raw edge = 0.64 - 0.571666... = 0.06833...
    """

    def test_vwap_calculation(self):
        book = Book(
            token_id="y1",
            condition_id="c1",
            source_at=NOW,
            received_at=NOW,
            bids=(Level(D("0.55"), D(500)),),
            asks=(
                Level(D("0.56"), D(50)),
                Level(D("0.57"), D(150)),
                Level(D("0.58"), D(1000)),
            ),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        from polyalpha.execution import FeeSchedule, Order, walk

        fees = FeeSchedule(D(0), NOW, "test-free")
        order = Order("test-order", "y1", "BUY", D(300), NOW)
        fill = walk(book, order, fees, NOW)

        # VWAP = (50*0.56 + 150*0.57 + 100*0.58) / 300
        # = (28 + 85.5 + 58) / 300 = 171.5 / 300
        expected_vwap = D("171.5") / D("300")
        assert fill.vwap == expected_vwap
        assert fill.shares == D(300)

    def test_raw_edge_with_model_probability(self):
        from polyalpha.forecasting import Forecast, net_edge

        forecast = Forecast(
            market_id="m1",
            timestamp=NOW,
            probability=D("0.64"),
            lower=D("0.61"),
            upper=D("0.67"),
            version="test",
        )

        book = Book(
            token_id="y1",
            condition_id="c1",
            source_at=NOW,
            received_at=NOW,
            bids=(Level(D("0.55"), D(500)),),
            asks=(
                Level(D("0.56"), D(50)),
                Level(D("0.57"), D(150)),
                Level(D("0.58"), D(1000)),
            ),
            tick_size=D("0.01"),
            min_order_size=D(1),
            source_hash="test",
        )
        from polyalpha.execution import FeeSchedule, Order, walk

        fees = FeeSchedule(D(0), NOW, "test-free")
        order = Order("test-order", "y1", "BUY", D(300), NOW)
        fill = walk(book, order, fees, NOW)

        edge = net_edge(forecast, fill, True, resolution_penalty=D(0))
        # conservative = lower = 0.61; VWAP = 171.5/300 = 0.571666...
        # Edge = 0.61 - 0.571666... - 0.002 (extra_slippage) = 0.03633...
        assert edge > D("0.03")
        assert edge < D("0.04")


# ─── Signal Generation (Section 21) ──────────────────────────────────────────


class TestSignalGeneration:
    """Test signal evaluation and ranking."""

    def test_signal_rejects_inactive_market(self):
        from polyalpha.signals import evaluate_entry

        book = _book()
        forecast = Forecast("m1", NOW, D("0.60"), D("0.57"), D("0.63"), "test")
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D(0), NOW, "test")
        inactive_market = Market(
            market_id="m1",
            condition_id="m1",
            event_ids=("e1",),
            question="Test",
            description="Test",
            resolution_source="test",
            deadline=NOW + timedelta(days=7),
            active=False,
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
        result = evaluate_entry(inactive_market, book, book, forecast, fees, D(10000), D(200), NOW)
        assert result.signal is None
        assert any("inactive" in r for r in result.rejections)

    def test_signal_generates_for_valid_market(self):
        from polyalpha.signals import evaluate_entry

        market = _market()
        book = _book()
        forecast = Forecast("m1", NOW, D("0.65"), D("0.62"), D("0.68"), "test")
        from polyalpha.execution import FeeSchedule

        fees = FeeSchedule(D(0), NOW, "test")
        result = evaluate_entry(
            market,
            book,
            book,
            forecast,
            fees,
            D(10000),
            D(200),
            NOW,
            min_edge=D("0.01"),
        )
        assert result.signal is not None
        assert result.signal.net_edge > 0


# ─── Rolling Correlation (Section 19) ────────────────────────────────────────


class TestRollingCorrelation:
    """Test dynamic correlation tracking."""

    def test_tracking_updates(self):
        from polyalpha.correlation import RollingCorrelationTracker

        tracker = RollingCorrelationTracker(["y1", "y2", "y3"], window=20, min_observations=5)

        # Feed correlated prices
        for i in range(10):
            p = D(str(0.5 + 0.01 * i))
            tracker.update(
                {"y1": p, "y2": p + D("0.001"), "y3": D("0.5")}, NOW + timedelta(minutes=i)
            )

        corr = tracker.get_correlation("y1", "y2")
        assert corr > D("0.9")  # should be highly correlated

    def test_uncorrelated_prices(self):
        from polyalpha.correlation import RollingCorrelationTracker

        tracker = RollingCorrelationTracker(["y1", "y2"], window=20, min_observations=5)

        import random

        rng = random.Random(42)
        for i in range(20):
            tracker.update(
                {
                    "y1": D(str(0.5 + rng.uniform(-0.1, 0.1))),
                    "y2": D(str(0.5 + rng.uniform(-0.1, 0.1))),
                },
                NOW + timedelta(minutes=i),
            )

        corr = tracker.get_correlation("y1", "y2")
        assert abs(corr) < D("0.8")  # not strongly correlated

    def test_dynamic_cluster_detection(self):
        from polyalpha.correlation import RollingCorrelationTracker

        tracker = RollingCorrelationTracker(
            ["y1", "y2", "y3"],
            window=20,
            min_observations=5,
            correlation_threshold=D("0.8"),
        )

        for i in range(15):
            p1 = D(str(0.5 + 0.005 * i))
            tracker.update(
                {"y1": p1, "y2": p1 + D("0.001"), "y3": D("0.3")},
                NOW + timedelta(minutes=i),
            )

        clusters = tracker.get_dynamic_clusters()
        # y1 and y2 should cluster together
        assert any({"y1", "y2"}.issubset(set(c)) for c in clusters)

    def test_summary(self):
        from polyalpha.correlation import RollingCorrelationTracker

        tracker = RollingCorrelationTracker(["y1", "y2"], window=10, min_observations=3)
        for i in range(5):
            tracker.update({"y1": D("0.5"), "y2": D("0.5")}, NOW + timedelta(minutes=i))

        s = tracker.summary()
        assert s["tokens"] == 2
        assert s["observations"] >= 3


# ─── Exit Conditions (Section 22) ────────────────────────────────────────────


class TestExitConditions:
    """Test enhanced exit policies."""

    def test_engine_rejects_invalid_exit_policy(self):
        from polyalpha.backtest import Engine

        try:
            Engine(clusters={}, exit_policy="invalid")
            assert False, "should have raised"
        except ValueError:
            pass

    def test_engine_accepts_all_exit_policies(self):
        from polyalpha.backtest import Engine

        for policy in ("hold", "edge", "time", "stop_loss", "trailing", "all"):
            engine = Engine(clusters={}, exit_policy=policy)
            assert engine.exit_policy == policy


# ─── Full Pipeline: Market Data → Features → Model → Signal → Backtest → Report ──


class TestFullPipelineIntegration:
    """End-to-end integration: market records → engine → report with all metrics."""

    def _records(self):
        """Build a sequence of records that exercises the full engine."""
        from polyalpha.storage import Record

        market_rec = Record(
            id=1,
            kind="market",
            entity_id="m1",
            payload={
                "id": "m1",
                "conditionId": "m1",
                "question": "Will X happen?",
                "description": "Test",
                "outcomes": ["Yes", "No"],
                "outcomePrices": '["0.50","0.50"]',
                "events": [{"id": "e1"}],
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "enableOrderBook": True,
                "volume": "10000",
                "liquidity": "5000",
                "endDate": (NOW + timedelta(days=7)).isoformat(),
                "lastUpdate": NOW.isoformat(),
                "bestBid": "0.49",
                "bestAsk": "0.51",
                "spread": "0.02",
                "yesTokenId": "y1",
                "noTokenId": "n1",
                "clobTokenIds": '["y1","n1"]',
                "minimumOrderSize": "1",
                "minimumTickSize": "0.01",
                "category": "politics",
                "resolutionSource": "test",
                "feesEnabled": False,
            },
            received_at=NOW,
            source_at=NOW,
        )

        book1 = Record(
            id=2,
            kind="book",
            entity_id="y1",
            payload={
                "market": "m1",
                "asset_id": "y1",
                "bids": [{"price": "0.49", "size": "500"}],
                "asks": [{"price": "0.51", "size": "500"}],
                "hash": "hash1",
                "timestamp": str(int(NOW.timestamp() * 1000)),
                "condition_id": "m1",
                "tick_size": "0.01",
                "min_order_size": "1",
            },
            received_at=NOW,
            source_at=NOW,
        )

        book2 = Record(
            id=3,
            kind="book",
            entity_id="y1",
            payload={
                "market": "m1",
                "asset_id": "y1",
                "bids": [{"price": "0.50", "size": "600"}],
                "asks": [{"price": "0.52", "size": "400"}],
                "hash": "hash2",
                "timestamp": str(int((NOW + timedelta(seconds=5)).timestamp() * 1000)),
                "condition_id": "m1",
                "tick_size": "0.01",
                "min_order_size": "1",
            },
            received_at=NOW + timedelta(seconds=5),
            source_at=NOW + timedelta(seconds=5),
        )

        book3 = Record(
            id=4,
            kind="book",
            entity_id="y1",
            payload={
                "market": "m1",
                "asset_id": "y1",
                "bids": [{"price": "0.52", "size": "700"}],
                "asks": [{"price": "0.54", "size": "300"}],
                "hash": "hash3",
                "timestamp": str(int((NOW + timedelta(seconds=10)).timestamp() * 1000)),
                "condition_id": "m1",
                "tick_size": "0.01",
                "min_order_size": "1",
            },
            received_at=NOW + timedelta(seconds=10),
            source_at=NOW + timedelta(seconds=10),
        )

        return [market_rec, book1, book2, book3]

    def test_full_backtest_produces_report(self):
        """Run full engine on synthetic data and verify report structure."""
        from polyalpha.backtest import Engine

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(
            clusters=clusters,
            initial_cash=D(10000),
            min_edge=D("0.01"),
        )

        records = self._records()
        report = engine.run(iter(records))

        # Report must have all Section 54 fields
        assert "status" in report
        assert report["status"] == "paper_simulation_only"
        assert "period_start" in report
        assert "period_end" in report
        assert "input_records" in report
        assert report["input_records"] == 4
        assert "cash" in report
        assert "net_pnl" in report
        assert "fees" in report
        assert "fill_journal" in report
        assert "equity" in report
        assert "decisions" in report
        assert "risk_state" in report
        assert "calibration" in report  # may be None
        assert "cluster_exposure" in report
        assert "category_exposure" in report
        assert "category_performance" in report
        assert "sharpe_ratio" in report
        assert "sortino_ratio" in report
        assert "return_volatility" in report
        assert "uncertainty_statistics" in report

    def test_full_pipeline_with_anomaly_detection(self):
        """Verify anomaly detection feeds into uncertainty multiplier in engine."""
        from polyalpha.backtest import Engine

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(clusters=clusters, initial_cash=D(10000), min_edge=D("0.01"))
        records = self._records()
        report = engine.run(iter(records))

        # Verify uncertainty statistics exist
        us = report["uncertainty_statistics"]
        assert "mean_uncertainty" in us
        assert "max_uncertainty" in us
        assert "total_anomalies" in us

    def test_full_pipeline_risk_ratios(self):
        """Verify Sharpe and Sortino ratios are computed."""
        from polyalpha.backtest import Engine

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(clusters=clusters, initial_cash=D(10000), min_edge=D("0.01"))
        records = self._records()
        report = engine.run(iter(records))

        # With only 3 equity points, ratios may be None (need >= 2 returns)
        # But the fields must exist
        assert "sharpe_ratio" in report
        assert "sortino_ratio" in report

    def test_full_pipeline_category_performance(self):
        """Verify category performance is tracked."""
        from polyalpha.backtest import Engine

        clusters = {"m1": {"event": "e1", "cluster": "c1", "category": "politics"}}
        engine = Engine(clusters=clusters, initial_cash=D(10000), min_edge=D("0.01"))
        records = self._records()
        report = engine.run(iter(records))

        cat_perf = report["category_performance"]
        assert isinstance(cat_perf, dict)
        # Even if no fills, category_performance should be empty dict


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "--tb=short"])
