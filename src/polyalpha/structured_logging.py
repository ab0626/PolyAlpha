"""Structured JSONL logging for decisions and system events.

Decision log: append-only JSONL, one JSON object per line, written on every
engine decision (queued, filled, rejected, exit_queued, exit_filled, etc.).

System log: append-only JSONL for system-level events (risk halts, drift
alerts, collection stats, calibration updates). Rotated by size.
"""

import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class _JsonlWriter:
    """Append-only JSONL writer with optional file rotation."""

    def __init__(self, path: str | Path, max_bytes: int = 50 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._file = None

    def _open(self):
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, "a", encoding="utf-8")

    def write(self, record: dict):
        self._open()
        line = json.dumps(record, default=str, allow_nan=False)
        self._file.write(line + "\n")
        self._file.flush()
        self._rotate_if_needed()

    def _rotate_if_needed(self):
        try:
            if self.path.stat().st_size > self.max_bytes:
                self._file.close()
                rotated = self.path.with_suffix(f".{int(datetime.utcnow().timestamp())}.jsonl")
                self.path.rename(rotated)
                self._file = None
        except OSError:
            pass

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None


@dataclass(frozen=True)
class TradeRecord:
    """Single trade decision record."""

    timestamp: str
    token: str
    reason: str
    market_id: str | None = None
    event_id: str | None = None
    cluster: str | None = None
    category: str | None = None
    side: str | None = None
    shares: str | None = None
    vwap: str | None = None
    fees: str | None = None
    net_edge: str | None = None
    model: str | None = None
    fair_probability: str | None = None
    conservative_probability: str | None = None
    best_bid: str | None = None
    best_ask: str | None = None
    spread: str | None = None
    staleness_penalty: str | None = None
    rejections: list[str] | None = None


class TradeLogger:
    """Append-only trade decision log in JSONL format.

    Usage:
        logger = TradeLogger("data/decisions.jsonl")
        logger.log({"timestamp": "...", "token": "...", "reason": "filled", ...})
        logger.close()
    """

    def __init__(self, path: str | Path, max_bytes: int = 50 * 1024 * 1024):
        self._writer = _JsonlWriter(path, max_bytes)

    def log(self, record: dict):
        self._writer.write(record)

    def log_decision(self, decision: dict, context: dict | None = None):
        """Log a decision with optional enriched context."""
        enriched = dict(decision)
        if context:
            enriched.update(context)
        self._writer.write(enriched)

    def flush(self):
        if self._writer._file:
            self._writer._file.flush()

    def close(self):
        self._writer.close()


class SystemLogger:
    """System-level structured log (JSONL) for monitoring and diagnostics."""

    def __init__(self, path: str | Path, max_bytes: int = 50 * 1024 * 1024):
        self._writer = _JsonlWriter(path, max_bytes)

    def log_event(self, event_type: str, **kwargs):
        record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "event_type": event_type,
            **kwargs,
        }
        self._writer.write(record)

    def log_risk_halt(self, reason: str, equity: str, peak: str):
        self.log_event("risk_halt", reason=reason, equity=equity, peak=peak)

    def log_drift_alert(self, drift_type: str, metrics: dict):
        self.log_event("drift_alert", drift_type=drift_type, metrics=metrics)

    def log_calibration_update(self, method: str, metrics: dict):
        self.log_event("calibration_update", method=method, metrics=metrics)

    def log_collection_stats(self, stats: dict):
        self.log_event("collection_stats", **stats)

    def flush(self):
        if self._writer._file:
            self._writer._file.flush()

    def close(self):
        self._writer.close()


class _StdoutJsonlHandler(logging.Handler):
    """Python logging handler that emits JSONL to stdout."""

    def emit(self, record):
        try:
            msg = self.format(record)
            payload = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "level": record.levelname,
                "logger": record.name,
                "message": msg,
            }
            print(json.dumps(payload, default=str), flush=True)
        except Exception:
            self.handleError(record)


def setup_logging(
    level: str = "INFO",
    json_output: bool = True,
) -> logging.Logger:
    """Configure root logger with JSONL output.

    Returns the root 'polyalpha' logger.
    """
    root = logging.getLogger("polyalpha")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    if json_output:
        handler = _StdoutJsonlHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)

    return root
