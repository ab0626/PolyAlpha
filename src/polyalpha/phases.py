"""Explicit collection phase state machine.

Part of the v0.3.0 data-collection architecture. Collection proceeds through a
strict phase sequence; transitions are explicit and recorded in a phase file.
The REAL_DATA_START marker may only be written from the BURNIN_PASSED phase.

    DEVELOPMENT
        ↓
    BASELINE_FROZEN
        ↓
    BURNIN_RUNNING
        ↓
    BURNIN_FAILED ──→ fix collector ──→ rerun ──→ BURNIN_RUNNING
        ↓
    BURNIN_PASSED
        ↓
    REAL_DATA_START
        ↓
    COLLECTION_RUNNING
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

VALID_PHASES = (
    "DEVELOPMENT",
    "BASELINE_FROZEN",
    "BURNIN_RUNNING",
    "BURNIN_FAILED",
    "BURNIN_PASSED",
    "REAL_DATA_START",
    "COLLECTION_RUNNING",
)

ALLOWED_TRANSITIONS = {
    "DEVELOPMENT": {"BASELINE_FROZEN"},
    "BASELINE_FROZEN": {"BURNIN_RUNNING"},
    "BURNIN_RUNNING": {"BURNIN_FAILED", "BURNIN_PASSED"},
    "BURNIN_FAILED": {"BURNIN_RUNNING"},
    "BURNIN_PASSED": {"REAL_DATA_START"},
    "REAL_DATA_START": {"COLLECTION_RUNNING"},
    "COLLECTION_RUNNING": set(),
}

TERMINAL_PHASES = ("REAL_DATA_START", "COLLECTION_RUNNING")


@dataclass(frozen=True)
class PhaseState:
    phase: str
    baseline_commit: str
    updated_at: str
    transition_log: list[dict]

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "baseline_commit": self.baseline_commit,
            "updated_at": self.updated_at,
            "transition_log": self.transition_log,
        }


class PhaseStore:
    """Read/write the phase state file. Transitions are validated."""

    def __init__(self, path: str | Path, baseline_commit: str = ""):
        self.path = Path(path)
        self.baseline_commit = baseline_commit

    def _default(self) -> PhaseState:
        now = datetime.now(UTC).isoformat()
        return PhaseState(
            phase="DEVELOPMENT",
            baseline_commit=self.baseline_commit,
            updated_at=now,
            transition_log=[],
        )

    def read(self) -> PhaseState:
        if not self.path.exists():
            return self._default()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return PhaseState(
            phase=data.get("phase", "DEVELOPMENT"),
            baseline_commit=data.get("baseline_commit", self.baseline_commit),
            updated_at=data.get("updated_at", ""),
            transition_log=data.get("transition_log", []),
        )

    def write(self, state: PhaseState) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self.path

    def transition(self, to_phase: str) -> PhaseState:
        """Move to to_phase if the transition is allowed, else raise."""
        if to_phase not in VALID_PHASES:
            raise ValueError(f"invalid phase: {to_phase}")
        current = self.read()
        allowed = ALLOWED_TRANSITIONS[current.phase]
        if to_phase not in allowed:
            raise ValueError(
                f"invalid transition {current.phase} -> {to_phase} "
                f"(allowed: {sorted(allowed)})"
            )
        now = datetime.now(UTC).isoformat()
        log = list(current.transition_log) + [
            {"from": current.phase, "to": to_phase, "at": now}
        ]
        state = PhaseState(
            phase=to_phase,
            baseline_commit=current.baseline_commit or self.baseline_commit,
            updated_at=now,
            transition_log=log,
        )
        self.write(state)
        return state

    def require(self, *phases: str) -> None:
        """Assert current phase is one of the given phases, else raise."""
        current = self.read()
        if current.phase not in phases:
            raise ValueError(
                f"phase {current.phase} not in required {phases}"
            )


def initialize_phase(path: str | Path, baseline_commit: str) -> PhaseState:
    """Write the initial BASELINE_FROZEN phase (idempotent)."""
    store = PhaseStore(path, baseline_commit)
    current = store.read()
    if current.phase != "DEVELOPMENT":
        return current
    return store.transition("BASELINE_FROZEN")