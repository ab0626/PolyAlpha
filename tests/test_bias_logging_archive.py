"""Tests for bias.py, logging_config.py, structured_logging.py, archive.py."""

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

D = Decimal

NOW = datetime(2026, 1, 15, tzinfo=UTC)


# ── bias.py ─────────────────────────────────────────────────────────────────


class TestBiasChecks:
    def _clean_report(self):
        return {
            "decisions": [],
            "fill_journal": [],
            "equity": [],
            "positions": [],
            "cluster_exposure": {},
        }

    def test_lookahead_bias_clean(self):
        from polyalpha.bias import check_lookahead_bias

        report = self._clean_report()
        report["decisions"] = [{"reason": "filled", "vwap": "0.55", "timestamp": NOW.isoformat()}]
        check = check_lookahead_bias(report)
        assert check.passed
        assert check.name == "lookahead_bias"

    def test_lookahead_bias_bad_vwap(self):
        from polyalpha.bias import check_lookahead_bias

        report = self._clean_report()
        report["decisions"] = [{"reason": "filled", "vwap": "1.5"}]
        check = check_lookahead_bias(report)
        assert not check.passed

    def test_lookahead_bias_negative_vwap(self):
        from polyalpha.bias import check_lookahead_bias

        report = self._clean_report()
        report["decisions"] = [{"reason": "filled", "vwap": "-0.1"}]
        check = check_lookahead_bias(report)
        assert not check.passed

    def test_midpoint_fill_bias_single_level(self):
        from polyalpha.bias import check_midpoint_fill_bias

        report = self._clean_report()
        report["fill_journal"] = [{"order_id": "o1", "levels": [("0.50", 100)], "shares": 100}]
        check = check_midpoint_fill_bias(report)
        assert not check.passed

    def test_midpoint_fill_bias_multi_level(self):
        from polyalpha.bias import check_midpoint_fill_bias

        report = self._clean_report()
        report["fill_journal"] = [
            {"order_id": "o1", "levels": [("0.50", 100), ("0.51", 50)], "shares": 100}
        ]
        check = check_midpoint_fill_bias(report)
        assert check.passed

    def test_midpoint_fill_bias_small_fill(self):
        from polyalpha.bias import check_midpoint_fill_bias

        report = self._clean_report()
        report["fill_journal"] = [{"order_id": "o1", "levels": [("0.50", 100)], "shares": 30}]
        check = check_midpoint_fill_bias(report)
        assert check.passed  # small fills at single level are ok

    def test_fee_omission_no_fees(self):
        from polyalpha.bias import check_fee_omission

        report = self._clean_report()
        report["fill_journal"] = [{"fees": 0, "notional": 100}]
        check = check_fee_omission(report)
        # fee_omission passes when total_fees == 0 (informational)
        assert check.passed
        assert check.details["warning"] is not None

    def test_fee_omission_with_fees(self):
        from polyalpha.bias import check_fee_omission

        report = self._clean_report()
        report["fill_journal"] = [{"fees": 1.0, "notional": 100}]
        check = check_fee_omission(report)
        assert check.passed

    def test_fee_omission_empty(self):
        from polyalpha.bias import check_fee_omission

        report = self._clean_report()
        check = check_fee_omission(report)
        assert check.passed  # no fills = ok

    def test_slippage_omission(self):
        from polyalpha.bias import check_slippage_omission

        report = self._clean_report()
        report["fill_journal"] = [{"depth_slippage": 0.01}]
        check = check_slippage_omission(report)
        assert check.passed  # always passes (diagnostic)

    def test_survivorship_bias(self):
        from polyalpha.bias import check_survivorship_bias

        report = self._clean_report()
        report["decisions"] = [{"reason": "rejected", "rejections": ["low_liquidity"]}]
        check = check_survivorship_bias(report)
        assert check.passed  # informational

    def test_trade_independence_concentrated(self):
        from polyalpha.bias import check_trade_independence

        report = self._clean_report()
        report["cluster_exposure"] = {"c1": 900, "c2": 100}
        check = check_trade_independence(report)
        assert not check.passed  # > 50% in one cluster

    def test_trade_independence_diversified(self):
        from polyalpha.bias import check_trade_independence

        report = self._clean_report()
        report["cluster_exposure"] = {"c1": 400, "c2": 300, "c3": 300}
        check = check_trade_independence(report)
        assert check.passed

    def test_trade_independence_empty(self):
        from polyalpha.bias import check_trade_independence

        report = self._clean_report()
        check = check_trade_independence(report)
        assert check.passed

    def test_overfitting_low_dd_many_fills(self):
        from polyalpha.bias import check_overfitting

        report = self._clean_report()
        report["fills"] = 20
        report["max_drawdown"] = "0.005"
        check = check_overfitting(report)
        assert not check.passed

    def test_overfitting_normal(self):
        from polyalpha.bias import check_overfitting

        report = self._clean_report()
        report["fills"] = 20
        report["max_drawdown"] = "0.10"
        check = check_overfitting(report)
        assert check.passed

    def test_timestamp_alignment_out_of_order(self):
        from polyalpha.bias import check_timestamp_alignment

        report = self._clean_report()
        report["equity"] = [
            {"timestamp": "2026-01-02"},
            {"timestamp": "2026-01-01"},
        ]
        check = check_timestamp_alignment(report)
        assert not check.passed

    def test_timestamp_alignment_in_order(self):
        from polyalpha.bias import check_timestamp_alignment

        report = self._clean_report()
        report["equity"] = [
            {"timestamp": "2026-01-01"},
            {"timestamp": "2026-01-02"},
        ]
        check = check_timestamp_alignment(report)
        assert check.passed

    def test_timestamp_alignment_insufficient(self):
        from polyalpha.bias import check_timestamp_alignment

        report = self._clean_report()
        report["equity"] = [{"timestamp": "2026-01-01"}]
        check = check_timestamp_alignment(report)
        assert check.passed

    def test_concentration_high(self):
        from polyalpha.bias import check_concentration

        report = self._clean_report()
        report["positions"] = [{"shares": 100, "basis": 500}, {"shares": 50, "basis": 100}]
        check = check_concentration(report)
        assert not check.passed  # 500/600 > 0.5

    def test_concentration_low(self):
        from polyalpha.bias import check_concentration

        report = self._clean_report()
        report["positions"] = [
            {"shares": 100, "basis": 200},
            {"shares": 100, "basis": 200},
            {"shares": 100, "basis": 200},
        ]
        check = check_concentration(report)
        assert check.passed  # 200/600 = 0.33 < 0.5

    def test_concentration_no_positions(self):
        from polyalpha.bias import check_concentration

        report = self._clean_report()
        check = check_concentration(report)
        assert check.passed

    def test_run_all_checks(self):
        from polyalpha.bias import run_all_checks

        checks = run_all_checks(self._clean_report())
        assert len(checks) == 9

    def test_bias_summary_all_pass(self):
        from polyalpha.bias import bias_summary, run_all_checks

        checks = run_all_checks(self._clean_report())
        summary = bias_summary(checks)
        assert summary["total_checks"] == 9
        assert summary["overall_pass"] is True
        assert (
            summary["recommendation"]
            == "Results may be trustworthy pending investigation of warnings"
        )

    def test_bias_summary_critical_failures(self):
        from polyalpha.bias import BiasCheck, bias_summary

        checks = [
            BiasCheck("test", False, "critical", "desc", {}),
            BiasCheck("test2", True, "info", "desc", {}),
        ]
        summary = bias_summary(checks)
        assert not summary["overall_pass"]
        assert "test" in summary["critical_failures"]


# ── logging_config.py ──────────────────────────────────────────────────────


class TestLoggingConfig:
    def test_structured_formatter(self):
        from polyalpha.logging_config import StructuredFormatter

        fmt = StructuredFormatter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
        result = fmt.format(record)
        parsed = json.loads(result)
        assert "timestamp" in parsed
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "hello"

    def test_get_logger(self):
        from polyalpha.logging_config import get_logger

        logger = get_logger("test_module")
        assert logger.name == "polyalpha.test_module"
        assert logger.level == logging.INFO
        assert len(logger.handlers) > 0

    def test_trade_logger_methods(self):
        from polyalpha.logging_config import TradeLogger

        tl = TradeLogger()
        # All methods should not raise
        tl.signal_generated({"market": "m1"})
        tl.signal_rejected("low_edge", market="m1")
        tl.order_queued(order_id="o1")
        tl.fill_executed(order_id="o1")
        tl.exit_triggered(order_id="o1")
        tl.risk_halt("max_drawdown")
        tl.settlement(market="m1")

    def test_system_logger_methods(self):
        from polyalpha.logging_config import SystemLogger

        sl = SystemLogger()
        sl.data_collected(markets=10)
        sl.drift_detected(feature="spread")
        sl.anomaly_detected(type="spread_widening")
        sl.calibration_update(method="isotonic")


# ── structured_logging.py ──────────────────────────────────────────────────


class TestStructuredLogging:
    def test_trade_record_frozen(self):
        from polyalpha.structured_logging import TradeRecord

        r = TradeRecord(timestamp="2026-01-01", token="y1", reason="filled")
        assert r.token == "y1"
        with pytest.raises(AttributeError):
            r.token = "x1"

    def test_jsonl_writer_write_and_read(self):
        from polyalpha.structured_logging import _JsonlWriter

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            writer = _JsonlWriter(path)
            writer.write({"key": "value1", "num": 42})
            writer.write({"key": "value2"})
            writer.close()

            lines = path.read_text().strip().split("\n")
            assert len(lines) == 2
            assert json.loads(lines[0])["key"] == "value1"

    def test_jsonl_writer_rotation(self):
        from polyalpha.structured_logging import _JsonlWriter

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            writer = _JsonlWriter(path, max_bytes=100)
            writer.write({"data": "x" * 60})
            writer.write({"data": "y" * 60})
            writer.close()
            # Rotation may rename the file; check the directory for any .jsonl
            files = list(Path(tmpdir).glob("*.jsonl"))
            assert len(files) >= 1

    def test_trade_logger(self):
        from polyalpha.structured_logging import TradeLogger

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "trades.jsonl"
            logger = TradeLogger(path)
            logger.log({"token": "y1", "reason": "filled"})
            logger.log_decision({"token": "y1", "reason": "queued"}, {"extra": "context"})
            logger.flush()
            logger.close()

            lines = path.read_text().strip().split("\n")
            assert len(lines) == 2
            record = json.loads(lines[1])
            assert record["extra"] == "context"

    def test_system_logger(self):
        from polyalpha.structured_logging import SystemLogger

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "system.jsonl"
            logger = SystemLogger(path)
            logger.log_event("test_event", value=42)
            logger.log_risk_halt("max_drawdown", equity="9500", peak="10000")
            logger.log_drift_alert("calibration", {"brier": 0.3})
            logger.log_calibration_update("isotonic", {"ece": 0.02})
            logger.log_collection_stats({"markets": 10})
            logger.flush()
            logger.close()

            lines = path.read_text().strip().split("\n")
            assert len(lines) == 5

    def test_setup_logging_json(self):
        from polyalpha.structured_logging import setup_logging

        logger = setup_logging("INFO", json_output=True)
        assert logger.level == logging.INFO

    def test_setup_logging_plain(self):
        from polyalpha.structured_logging import setup_logging

        logger = setup_logging("DEBUG", json_output=False)
        assert logger.level == logging.DEBUG

    def test_stdout_jsonl_handler(self):
        from polyalpha.structured_logging import _StdoutJsonlHandler

        handler = _StdoutJsonlHandler()
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
        # Should not raise
        handler.emit(record)


# ── archive.py ──────────────────────────────────────────────────────────────


class TestArchive:
    def test_export_jsonl(self):
        from polyalpha.archive import export_jsonl, read_jsonl
        from polyalpha.storage import Record

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "export.jsonl"

            # Create an in-memory store mock
            class MockStore:
                def replay(self, as_of, kind=None):
                    return [
                        Record(
                            id=1,
                            kind="market",
                            entity_id="m1",
                            received_at=NOW,
                            source_at=NOW,
                            payload={"question": "test"},
                        ),
                        Record(
                            id=2,
                            kind="book",
                            entity_id="y1",
                            received_at=NOW,
                            source_at=NOW,
                            payload={"bids": []},
                        ),
                    ]

            count = export_jsonl(MockStore(), path, NOW)
            assert count == 2
            assert path.exists()

            records = list(read_jsonl(path))
            assert len(records) == 2
            assert records[0].entity_id == "m1"

    def test_read_jsonl_unordered(self):
        from polyalpha.archive import read_jsonl
        from polyalpha.storage import Record

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.jsonl"
            # Write out-of-order records
            Record(id=1, kind="m", entity_id="m1", received_at=NOW, source_at=None, payload={})
            Record(
                id=2,
                kind="m",
                entity_id="m2",
                received_at=NOW - __import__("datetime").timedelta(hours=1),
                source_at=None,
                payload={},
            )
            with open(path, "w") as f:
                f.write(
                    json.dumps(
                        {
                            "id": 1,
                            "kind": "m",
                            "entity_id": "m1",
                            "received_at": NOW.isoformat(),
                            "source_at": None,
                            "payload": {},
                        }
                    )
                    + "\n"
                )
                f.write(
                    json.dumps(
                        {
                            "id": 2,
                            "kind": "m",
                            "entity_id": "m2",
                            "received_at": (
                                NOW - __import__("datetime").timedelta(hours=1)
                            ).isoformat(),
                            "source_at": None,
                            "payload": {},
                        }
                    )
                    + "\n"
                )
            with pytest.raises(ValueError, match="unordered"):
                list(read_jsonl(path))
