"""Operational gap watchdog for US retail collection — core (platform-free) logic.

The watchdog is a runtime recovery daemon. It has **no** authority to edit the
canonical research gap ledger (``config/us_known_gaps.json``): that file is the
reviewed, preregistered input to the coverage gate, and a runtime daemon must
not mutate it because a heartbeat hiccuped. The watchdog instead writes its own
append-only operational journal (``data/us/gap_watchdog/events.jsonl``) and
``scripts/reconcile_us_gaps.py`` turns that journal into candidate ledger
additions for human review.

This module holds the pure state machine, the append-only journal, the state
store, and the interval/coverage helpers so they can be unit-tested without
Windows process/restart/alert side effects. The CLI daemon
(``scripts/us_gap_watchdog.py``) supplies the platform adapters.

Timestamp rules (the whole point of honest gap measurement):
    * gap start  = the last known healthy heartbeat (the last observed data
      write), NOT "when the watchdog noticed". Using detection time would
      undercount the missing interval.
    * gap end    = the first fresh heartbeat observed after recovery, NOT "when
      the restart was launched". Using launch time would overcount the missing
      interval.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .forward_evidence import (
    DEFAULT_KNOWN_GAPS,
    _load_known_gaps,
    _observed_coverage,
)

STARTING = "STARTING"
HEALTHY = "HEALTHY"
SUSPECT = "SUSPECT"
OUTAGE_OPEN = "OUTAGE_OPEN"
RECOVERING = "RECOVERING"

ALL_STATES = (STARTING, HEALTHY, SUSPECT, OUTAGE_OPEN, RECOVERING)

# All four US retail feeds go dark together in a collector outage.
US_RETAIL_FEEDS = (
    "polymarket_us_retail_book",
    "polymarket_us_retail_markets",
    "polymarket_us_retail_events",
    "polymarket_us_retail_settlement",
)


@dataclass
class Reading:
    """One watchdog observation of the world."""

    process_alive: bool
    heartbeat_ts: float | None  # epoch seconds of the newest data write (None = no data)
    now: float


@dataclass
class Action:
    """An imperative the state machine asks the CLI to perform."""

    kind: str
    payload: dict = field(default_factory=dict)


class StateMachine:
    """Gap state machine with honest gap timestamps and bounded restart backoff.

    States: STARTING -> HEALTHY -> SUSPECT -> OUTAGE_OPEN -> RECOVERING -> HEALTHY.

    * STARTING establishes the baseline from the on-disk data. If that baseline
      is already stale (e.g. a reboot), the gap opens immediately — gap start is
      the last data write — but an alive collector is not killed on the very
      first check (it may simply be warming up).
    * A *dead* process restarts immediately (there is nothing to confirm).
    * An *alive but stale* process (collector up, no data arriving — DNS down,
      wedged child) is killed only after a second confirmation.
    """

    def __init__(
        self,
        stale_seconds: float = 900.0,
        warmup_grace_seconds: float = 600.0,
        backoff_initial: float = 30.0,
        backoff_factor: float = 2.0,
        backoff_max: float = 600.0,
        max_restart_attempts: int = 6,
    ) -> None:
        self.stale_seconds = stale_seconds
        self.warmup_grace_seconds = warmup_grace_seconds
        self.backoff_initial = backoff_initial
        self.backoff_factor = backoff_factor
        self.backoff_max = backoff_max
        self.max_restart_attempts = max_restart_attempts

        self.state = STARTING
        self.started_at: float | None = None
        self.last_healthy_heartbeat: float | None = None
        self.suspect_since: float | None = None
        self.outage_start: float | None = None
        self.outage_reason: str | None = None
        self.recovery_seen_at: float | None = None
        self.restart_attempts = 0
        self.last_restart_at: float | None = None
        self._gave_up_reported = False

    # -- helpers -------------------------------------------------------------

    def _heartbeat_fresh(self, r: Reading) -> bool:
        return r.heartbeat_ts is not None and (r.now - r.heartbeat_ts) <= self.stale_seconds

    def _restart_due(self, r: Reading) -> bool:
        if self.restart_attempts >= self.max_restart_attempts:
            return False
        if self.last_restart_at is None:
            return True
        backoff = min(
            self.backoff_max,
            self.backoff_initial * (self.backoff_factor ** (self.restart_attempts - 1)),
        )
        elapsed = r.now - self.last_restart_at
        # If the process is alive but still not producing data, give the freshly
        # relaunched collector a warmup grace before killing it again, so we do
        # not thrash a collector that is merely slow to start writing.
        required = backoff
        if r.process_alive:
            required = max(required, self.warmup_grace_seconds)
        return elapsed >= required

    def _restart(self, r: Reading, actions: list[Action]) -> None:
        self.restart_attempts += 1
        self.last_restart_at = r.now
        if r.process_alive:
            actions.append(Action("kill_process"))
        actions.append(Action("launch_process", {"attempt": self.restart_attempts}))

    def _begin_outage(self, r: Reading, actions: list[Action], reason: str, restart: bool) -> None:
        self.state = OUTAGE_OPEN
        self.outage_reason = reason
        # Gap starts at the last known healthy heartbeat, not at detection time.
        self.outage_start = self.last_healthy_heartbeat
        self.recovery_seen_at = None
        self.restart_attempts = 0
        # When restart=False we start the warmup/backoff clock now without acting,
        # so an alive-but-warming-up process is not killed on the very first check.
        self.last_restart_at = None if restart else r.now
        self._gave_up_reported = False
        actions.append(
            Action("outage_open", {"gap_start": self.outage_start, "reason": reason})
        )
        if restart:
            self._restart(r, actions)

    def _open_outage(self, r: Reading, actions: list[Action], reason: str) -> None:
        self._begin_outage(r, actions, reason, restart=True)

    def _close_outage(self, r: Reading, actions: list[Action]) -> None:
        actions.append(
            Action(
                "outage_close",
                {"gap_start": self.outage_start, "gap_end": self.recovery_seen_at},
            )
        )
        self.last_healthy_heartbeat = r.heartbeat_ts
        self.outage_start = None
        self.outage_reason = None
        self.recovery_seen_at = None
        self.restart_attempts = 0
        self.last_restart_at = None
        self._gave_up_reported = False
        self.state = HEALTHY

    # -- state machine -------------------------------------------------------

    def step(self, r: Reading) -> list[Action]:
        if self.started_at is None:
            self.started_at = r.now

        actions: list[Action] = []

        if self.state == STARTING:
            if self._heartbeat_fresh(r):
                self.last_healthy_heartbeat = r.heartbeat_ts
                self.state = HEALTHY
                return actions
            if self.last_healthy_heartbeat is not None:
                # The on-disk baseline (last data write) is already stale, so the
                # downtime predates this watchdog run (e.g. a reboot). Open the gap
                # now — gap start = that baseline — but do NOT kill a possibly
                # warming-up collector on the very first check.
                self._begin_outage(r, actions, reason="stale_at_startup", restart=False)
                if not r.process_alive:
                    # Process gone: launch it (no kill involved).
                    self._restart(r, actions)
                return actions
            # No baseline at all (fresh install): bring the collector up, no gap.
            if not r.process_alive:
                actions.append(Action("launch_process", {"attempt": 0}))
                self.last_restart_at = r.now
                self.restart_attempts = 0
            return actions

        if self.state == HEALTHY:
            if r.process_alive and self._heartbeat_fresh(r):
                self.last_healthy_heartbeat = r.heartbeat_ts
                return actions
            if not r.process_alive:
                # Process gone: restart immediately; there is nothing to confirm.
                self._open_outage(r, actions, reason="process_dead")
                return actions
            # Alive but stale: confirm on the next check before killing.
            self.state = SUSPECT
            self.suspect_since = r.now
            return actions

        if self.state == SUSPECT:
            if r.process_alive and self._heartbeat_fresh(r):
                # Transient blip; no gap.
                self.state = HEALTHY
                self.last_healthy_heartbeat = r.heartbeat_ts
                self.suspect_since = None
                return actions
            reason = "process_dead" if not r.process_alive else "heartbeat_stale"
            self.suspect_since = None
            self._open_outage(r, actions, reason=reason)
            return actions

        if self.state == OUTAGE_OPEN:
            if r.process_alive and self._heartbeat_fresh(r):
                # First fresh heartbeat after recovery: record it as the gap end.
                self.state = RECOVERING
                self.recovery_seen_at = r.heartbeat_ts
                return actions
            if self._restart_due(r):
                if self.restart_attempts >= 1:
                    actions.append(
                        Action(
                            "recovery_failed",
                            {
                                "attempt": self.restart_attempts,
                                "reason": self.outage_reason,
                            },
                        )
                    )
                self._restart(r, actions)
            elif self.restart_attempts >= self.max_restart_attempts and not self._gave_up_reported:
                self._gave_up_reported = True
                actions.append(
                    Action(
                        "recovery_failed",
                        {"attempt": self.restart_attempts, "reason": "max_restart_attempts_reached"},
                    )
                )
            return actions

        if self.state == RECOVERING:
            if r.process_alive and self._heartbeat_fresh(r):
                # Confirmed recovery on a second consecutive fresh check.
                self._close_outage(r, actions)
                return actions
            # Recovery flapped (or the process died again): keep the gap open.
            self.state = OUTAGE_OPEN
            self.recovery_seen_at = None
            if self._restart_due(r):
                self._restart(r, actions)
            return actions

        return actions

    # -- persistence ---------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "started_at": self.started_at,
            "last_healthy_heartbeat": self.last_healthy_heartbeat,
            "suspect_since": self.suspect_since,
            "outage_start": self.outage_start,
            "outage_reason": self.outage_reason,
            "recovery_seen_at": self.recovery_seen_at,
            "restart_attempts": self.restart_attempts,
            "last_restart_at": self.last_restart_at,
            "gave_up_reported": self._gave_up_reported,
        }

    @classmethod
    def from_dict(cls, d: dict, **cfg) -> "StateMachine":
        m = cls(**cfg)
        m.state = d.get("state", STARTING)
        m.started_at = d.get("started_at")
        m.last_healthy_heartbeat = d.get("last_healthy_heartbeat")
        m.suspect_since = d.get("suspect_since")
        m.outage_start = d.get("outage_start")
        m.outage_reason = d.get("outage_reason")
        m.recovery_seen_at = d.get("recovery_seen_at")
        m.restart_attempts = int(d.get("restart_attempts", 0))
        m.last_restart_at = d.get("last_restart_at")
        m._gave_up_reported = bool(d.get("gave_up_reported", False))
        return m


# -- journal / state persistence ---------------------------------------------


class GapJournal:
    """Append-only operational gap journal (one JSON object per line, fsync'd)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(event)
        record.setdefault("at", datetime.now(UTC).isoformat())
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


class StateStore:
    """Atomic JSON state store for the state machine."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


# -- interval helpers ---------------------------------------------------------


def union_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping/adjacent ``[start, end]`` intervals; never double-count."""
    cleaned = sorted((float(a), float(b)) for a, b in intervals if b > a)
    merged: list[list[float]] = []
    for lo, hi in cleaned:
        if not merged or lo > merged[-1][1]:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    return [(lo, hi) for lo, hi in merged]


def subtract_intervals(
    interval: tuple[float, float], covered: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Return the parts of ``interval`` not covered by the union of ``covered``."""
    lo, hi = float(interval[0]), float(interval[1])
    out: list[tuple[float, float]] = []
    for c_lo, c_hi in union_intervals(covered):
        if c_hi <= lo or c_lo >= hi:
            continue
        if c_lo > lo:
            out.append((lo, min(c_lo, hi)))
        lo = max(lo, c_hi)
        if lo >= hi:
            break
    if lo < hi:
        out.append((lo, hi))
    return out


def closed_intervals_from_events(
    events: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Pair ``outage_open``/``outage_close`` events into closed gap intervals.

    Returns ``(closed, still_open)``: closed is a list of
    ``{"gap_start", "gap_end", "reason"}`` (ISO strings as written to the
    journal), ``still_open`` is the list of ``outage_open`` events with no
    matching ``outage_close`` yet.
    """
    closed: list[dict] = []
    open_stack: list[dict] = []
    for ev in events:
        kind = ev.get("event")
        if kind == "outage_open":
            open_stack.append(ev)
        elif kind == "outage_close":
            if not open_stack:
                continue
            opener = open_stack.pop(0)
            closed.append(
                {
                    "gap_start": opener.get("gap_start"),
                    "gap_end": ev.get("gap_end"),
                    "reason": opener.get("reason"),
                }
            )
    return closed, open_stack


# -- reconciliation ----------------------------------------------------------

def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_epoch(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def existing_gap_intervals(gaps: list[dict]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for g in gaps:
        lo = parse_iso(g.get("start"))
        hi = parse_iso(g.get("end"))
        if lo is not None and hi is not None and hi > lo:
            out.append((_to_epoch(lo), _to_epoch(hi)))
    return out


def _candidate_id(start: datetime, existing_ids: set[str]) -> str:
    base = f"watchdog-{start:%Y-%m-%dT%H%M%S}"
    cid = base
    n = 2
    while cid in existing_ids:
        cid = f"{base}-{n}"
        n += 1
    return cid


def build_candidates(
    events: list[dict], existing_gaps: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Turn watchdog events into candidate ledger additions.

    Pairs ``outage_open``/``outage_close`` into closed intervals, subtracts
    anything already covered by the canonical ledger (idempotent), and returns
    ``(candidates, still_open)``. Only fully-closed intervals become candidates.
    """
    closed, still_open = closed_intervals_from_events(events)
    covered = existing_gap_intervals(existing_gaps)
    existing_ids = {g.get("id", "") for g in existing_gaps}

    candidates: list[dict] = []
    for interval in closed:
        lo = parse_iso(interval.get("gap_start"))
        hi = parse_iso(interval.get("gap_end"))
        if lo is None or hi is None or hi <= lo:
            continue
        uncovered = subtract_intervals((_to_epoch(lo), _to_epoch(hi)), covered)
        for u_lo, u_hi in uncovered:
            start = datetime.fromtimestamp(u_lo, tz=UTC)
            end = datetime.fromtimestamp(u_hi, tz=UTC)
            candidates.append(
                {
                    "id": _candidate_id(start, existing_ids),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "reason": f"watchdog-detected outage ({interval.get('reason') or 'unknown'})",
                    "feeds_affected": list(US_RETAIL_FEEDS),
                    "price_path": "unobserved",
                    "outcomes": "recovered",
                }
            )
            existing_ids.add(candidates[-1]["id"])

    return candidates, still_open


# -- coverage ----------------------------------------------------------------


def coverage_snapshot(
    now: datetime,
    open_gap: tuple[datetime, datetime] | None = None,
    gaps_config: str | Path = DEFAULT_KNOWN_GAPS,
) -> dict:
    """Official vs. provisional coverage, plus the live open-gap duration.

    ``official`` is computed from the canonical (reviewed) gap ledger only.
    ``provisional`` also folds in the still-open watchdog interval, so operators
    can see where coverage is heading without ever letting the provisional number
    declare the preregistered boundary passed.
    """
    official_gaps = _load_known_gaps(gaps_config)
    official: float | None = None
    provisional: float | None = None
    if official_gaps is not None:
        official = _observed_coverage(now, official_gaps)
        if open_gap is not None:
            start, end = open_gap
            combined = list(official_gaps)
            combined.append({"start": start.isoformat(), "end": end.isoformat()})
            provisional = _observed_coverage(now, combined)
        else:
            provisional = official
    return {
        "official_coverage": official,
        "provisional_coverage": provisional,
        "open_gap_seconds": (open_gap[1] - open_gap[0]).total_seconds()
        if open_gap is not None
        else None,
    }