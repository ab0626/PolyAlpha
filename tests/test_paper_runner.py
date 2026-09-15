"""Tests for paper_runner.py (compute_section54_metrics, build_clusters, run_backtest)."""

from datetime import UTC, datetime
from decimal import Decimal

D = Decimal

NOW = datetime(2026, 1, 15, tzinfo=UTC)


class TestPaperRunner:
    def test_compute_section54_metrics_minimal(self):
        from polyalpha.paper_runner import compute_section54_metrics

        report = {
            "input_records": 100,
            "period_start": "2026-01-01",
            "period_end": "2026-06-01",
            "fills": 10,
            "resolved_markets": 25,
            "cash": "10500",
            "net_pnl": "500",
            "gross_pnl": "600",
            "fees": "80",
            "estimated_exit_fees": "20",
            "transaction_costs": "100",
            "depth_slippage_diagnostic": "0.02",
            "max_drawdown": "0.03",
            "max_drawdown_observed": "0.035",
            "calibration": {
                "brier": 0.22,
                "log_loss": 0.45,
                "ece": 0.015,
                "buckets": [],
                "independent_clusters": 8,
            },
            "risk_state": {"halted": False, "reason": None},
            "cluster_exposure": {"c1": 200},
            "category_exposure": {"politics": 300},
            "category_performance": {"politics": {"pnl": 100}},
            "decisions": [
                {"reason": "queued"},
                {"reason": "filled"},
                {"reason": "filled"},
                {"reason": "rejected"},
            ],
            "uncertainty_statistics": {"mean": 0.04},
            "valuation_complete": True,
            "valuation_gap_count": 0,
        }
        metrics = compute_section54_metrics(report, D(10000))
        assert metrics["sample_size"] == 100
        assert metrics["fills"] == 10
        assert metrics["brier_score"] == 0.22
        assert metrics["risk_halted"] is False
        assert metrics["decision_summary"]["queued"] == 1
        assert metrics["decision_summary"]["filled"] == 2
        assert metrics["valuation_complete"] is True

    def test_compute_section54_metrics_empty(self):
        from polyalpha.paper_runner import compute_section54_metrics

        metrics = compute_section54_metrics({}, D(10000))
        assert metrics["sample_size"] == 0
        # brier_score only set when calibration is present
        assert "brier_score" not in metrics or metrics["brier_score"] is None
        assert metrics["risk_halted"] is None

    def test_build_clusters_from_market_records(self):
        from polyalpha.paper_runner import build_clusters
        from polyalpha.storage import Record

        records = [
            Record(
                id=1,
                kind="market",
                entity_id="m1",
                received_at=NOW,
                source_at=None,
                payload={
                    "id": "m1",
                    "conditionId": "c1",
                    "question": "Test?",
                    "description": "Test",
                    "events": [{"id": "e1"}],
                    "outcomes": ["Yes", "No"],
                    "clobTokenIds": '["y1","n1"]',
                    "active": True,
                    "acceptingOrders": True,
                    "enableOrderBook": True,
                    "minimum_order_size": "1",
                    "minimum_tick_size": "0.01",
                    "yes_token_id": "y1",
                    "no_token_id": "n1",
                    "outcome_prices": "[0.5,0.5]",
                    "liquidity": "5000",
                    "volume": "10000",
                    "end_date_iso": "2026-03-01T00:00:00Z",
                    "endDate": "2026-03-01T00:00:00Z",
                    "category": "politics",
                    "feesEnabled": False,
                    "feeSchedule": None,
                    "resolutionSource": "official",
                },
            ),
        ]
        clusters = build_clusters(records)
        assert "m1" in clusters
        assert clusters["m1"]["event"] == "e1"
        assert clusters["m1"]["category"] == "politics"

    def test_build_clusters_skips_non_market(self):
        from polyalpha.paper_runner import build_clusters
        from polyalpha.storage import Record

        records = [
            Record(id=1, kind="book", entity_id="y1", received_at=NOW, source_at=None, payload={}),
        ]
        clusters = build_clusters(records)
        assert len(clusters) == 0

    def test_run_backtest_returns_report(self):
        from polyalpha.paper_runner import run_backtest

        # Empty records should still produce a valid report
        report = run_backtest([], {}, initial_cash=D(10000))
        assert "fills" in report
        assert "cash" in report
        assert report["fills"] == 0
