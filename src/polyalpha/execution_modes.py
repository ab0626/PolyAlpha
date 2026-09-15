"""Execution mode — the operating mode must always be visually obvious."""

from __future__ import annotations

from enum import Enum


class ExecutionMode(str, Enum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE_READY = "LIVE_READY"

    def __str__(self) -> str:
        return self.value


def banner(mode: ExecutionMode) -> str:
    """Render an unambiguous mode banner for logs/dashboards."""
    return f"MODE:{mode.value}" + "=" * max(0, 12 - len(mode.value))


def is_executing(mode: ExecutionMode) -> bool:
    """True only for modes that may submit orders to a venue."""
    return mode == ExecutionMode.LIVE_READY