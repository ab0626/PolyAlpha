"""Structured logging for the polyalpha platform.

Provides consistent, structured log output for signals, rejections,
fills, and system events.
"""

import json
import logging
import sys
from datetime import UTC, datetime


class StructuredFormatter(logging.Formatter):
    """JSON structured log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge extra fields if present
        if hasattr(record, "extra_data"):
            log_entry.update(record.extra_data)
        return json.dumps(log_entry, default=str)


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Get a configured structured logger."""
    logger = logging.getLogger(f"polyalpha.{name}")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


class TradeLogger:
    """Specialized logger for trade signals and execution events."""

    def __init__(self, level: int = logging.INFO):
        self.logger = get_logger("trades", level)

    def signal_generated(self, signal_data: dict):
        """Log a generated signal."""
        self.logger.info(
            "signal_generated",
            extra={"extra_data": {"event": "signal_generated", **signal_data}},
        )

    def signal_rejected(self, reason: str, **kwargs):
        """Log a rejected signal with reason."""
        self.logger.info(
            f"signal_rejected: {reason}",
            extra={"extra_data": {"event": "signal_rejected", "reason": reason, **kwargs}},
        )

    def order_queued(self, **kwargs):
        """Log an order queued for execution."""
        self.logger.info(
            "order_queued",
            extra={"extra_data": {"event": "order_queued", **kwargs}},
        )

    def fill_executed(self, **kwargs):
        """Log a completed fill."""
        self.logger.info(
            "fill_executed",
            extra={"extra_data": {"event": "fill_executed", **kwargs}},
        )

    def exit_triggered(self, **kwargs):
        """Log an exit trigger."""
        self.logger.info(
            "exit_triggered",
            extra={"extra_data": {"event": "exit_triggered", **kwargs}},
        )

    def risk_halt(self, reason: str, **kwargs):
        """Log a risk halt event."""
        self.logger.warning(
            f"risk_halt: {reason}",
            extra={"extra_data": {"event": "risk_halt", "reason": reason, **kwargs}},
        )

    def settlement(self, **kwargs):
        """Log a market settlement."""
        self.logger.info(
            "settlement",
            extra={"extra_data": {"event": "settlement", **kwargs}},
        )


class SystemLogger:
    """Logger for system-level events (data, monitoring, drift)."""

    def __init__(self, level: int = logging.INFO):
        self.logger = get_logger("system", level)

    def data_collected(self, **kwargs):
        self.logger.info(
            "data_collected",
            extra={"extra_data": {"event": "data_collected", **kwargs}},
        )

    def drift_detected(self, **kwargs):
        self.logger.warning(
            "drift_detected",
            extra={"extra_data": {"event": "drift_detected", **kwargs}},
        )

    def anomaly_detected(self, **kwargs):
        self.logger.warning(
            "anomaly_detected",
            extra={"extra_data": {"event": "anomaly_detected", **kwargs}},
        )

    def calibration_update(self, **kwargs):
        self.logger.info(
            "calibration_update",
            extra={"extra_data": {"event": "calibration_update", **kwargs}},
        )
