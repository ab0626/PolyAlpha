"""TradingKillSwitch — single authoritative stop for all new execution intents.

Part of the live-ready execution architecture. Activation must be:
  * logged, timestamped, reason-coded, audit-visible
  * persistent (restarting software must NOT automatically clear it)
  * able to prevent all new intents from progressing

Deactivation requires an explicit operator action, never an automatic reset.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

KILL_REASONS = {
    "MANUAL_OPERATOR",
    "DAILY_LOSS_BREACH",
    "DRAWDOWN_BREACH",
    "MARKET_DATA_FAILURE",
    "ABNORMAL_RECONCILIATION",
    "ACCOUNT_STATE_INCONSISTENCY",
    "ABNORMAL_LATENCY",
    "REPEATED_EXECUTION_FAILURE",
    "UNEXPECTED_MODEL_BEHAVIOR",
    "ABNORMAL_SIGNAL_RATE",
    "CONFIG_MISMATCH",
    "RESEARCH_HASH_MISMATCH",
    "PLATFORM_ELIGIBILITY_FAILURE",
    "CRITICAL_DEPENDENCY_OUTAGE",
}


@dataclass(frozen=True)
class KillSwitchState:
    active: bool
    reason: str | None = None
    activated_at: str | None = None
    activated_by: str | None = None

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "reason": self.reason,
            "activated_at": self.activated_at,
            "activated_by": self.activated_by,
        }


class TradingKillSwitch:
    """File-backed kill switch that survives process restarts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"active": False}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # If the state file is unreadable, fail closed.
            return {"active": True, "reason": "STATE_FILE_UNREADABLE"}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    @property
    def active(self) -> bool:
        return bool(self._read().get("active"))

    def state(self) -> KillSwitchState:
        data = self._read()
        return KillSwitchState(
            active=bool(data.get("active")),
            reason=data.get("reason"),
            activated_at=data.get("activated_at"),
            activated_by=data.get("activated_by"),
        )

    def activate(self, reason: str, actor: str = "system") -> KillSwitchState:
        if reason not in KILL_REASONS:
            raise ValueError(f"unknown kill reason: {reason}")
        data = {
            "active": True,
            "reason": reason,
            "activated_at": datetime.now(UTC).isoformat(),
            "activated_by": actor,
        }
        self._write(data)
        return self.state()

    def deactivate(self, actor: str = "operator") -> KillSwitchState:
        """Explicit operator deactivation. Never automatic."""
        data = self._read()
        data["active"] = False
        data["deactivated_at"] = datetime.now(UTC).isoformat()
        data["deactivated_by"] = actor
        self._write(data)
        return self.state()

    def require_clear(self) -> None:
        """Raise if the kill switch is active (fail-closed on the hot path)."""
        if self.active:
            s = self.state()
            raise RuntimeError(f"kill switch active: {s.reason}")