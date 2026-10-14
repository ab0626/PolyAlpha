"""Tests for the US collection gap watchdog (state machine, journal, reconciler)."""

import json
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, "src")

from polyalpha.us.forward_evidence import REAL_DATA_START_US
from polyalpha.us.gap_watchdog import (
    HEALTHY,
    OUTAGE_OPEN,
    RECOVERING,
    SUSPECT,
    US_RETAIL_FEEDS,
    Reading,
    StateMachine,
    build_candidates,
    closed_intervals_from_events,
    coverage_snapshot,
    subtract_intervals,
    union_intervals,
)


def _kinds(actions):
    return [a.kind for a in actions]


def _by_kind(actions, kind):
    return [a for a in actions if a.kind == kind]


def test_healthy_stays_healthy_and_updates_heartbeat():
    m = StateMachine()
    m.last_healthy_heartbeat = 1000.0
    assert m.step(Reading(True, 1000.0, 1000.0)) == []
    assert m.state == HEALTHY
    m.step(Reading(True, 1010.0, 1010.0))
    assert m.last_healthy_heartbeat == 1010.0


def test_process_dead_opens_outage_immediately_and_launches():
    m = StateMachine()
    m.last_healthy_heartbeat = 1000.0
    m.step(Reading(True, 1000.0, 1000.0))
    actions = m.step(Reading(False, 1000.0, 1100.0))
    assert m.state == OUTAGE_OPEN
    assert "outage_open" in _kinds(actions)
    assert "launch_process" in _kinds(actions)
    assert "kill_process" not in _kinds(actions)
    # gap start = last known healthy heartbeat, not detection time (1100)
    assert _by_kind(actions, "outage_open")[0].payload["gap_start"] == 1000.0


def test_alive_but_stale_requires_confirmation():
    m = StateMachine(stale_seconds=900.0)
    m.last_healthy_heartbeat = 1000.0
    m.step(Reading(True, 1000.0, 1000.0))
    # alive but heartbeat frozen -> only suspect on first check
    a1 = m.step(Reading(True, 1000.0, 2000.0))
    assert m.state == SUSPECT
    assert a1 == []
    # still stale -> confirmed outage, kill + launch
    a2 = m.step(Reading(True, 1000.0, 2030.0))
    assert m.state == OUTAGE_OPEN
    assert "outage_open" in _kinds(a2)
    assert "kill_process" in _kinds(a2)
    assert "launch_process" in _kinds(a2)


def test_suspect_transient_blip_does_not_open_gap():
    m = StateMachine(stale_seconds=900.0)
    m.last_healthy_heartbeat = 1000.0
    m.step(Reading(True, 1000.0, 1000.0))
    m.step(Reading(True, 1000.0, 2000.0))  # -> SUSPECT
    a = m.step(Reading(True, 2090.0, 2100.0))  # recovered
    assert m.state == HEALTHY
    assert a == []


def test_recovery_records_first_fresh_heartbeat_as_gap_end():
    m = StateMachine(stale_seconds=900.0)
    m.last_healthy_heartbeat = 1000.0
    m.step(Reading(True, 1000.0, 1000.0))
    m.step(Reading(False, 1000.0, 2000.0))  # outage_open, gap_start=1000
    a1 = m.step(Reading(True, 2990.0, 3000.0))  # first fresh heartbeat
    assert m.state == RECOVERING
    assert a1 == []
    a2 = m.step(Reading(True, 3030.0, 3030.0))  # confirm recovery
    assert m.state == HEALTHY
    close = _by_kind(a2, "outage_close")
    assert close and close[0].payload["gap_start"] == 1000.0
    # gap end = first fresh heartbeat (2990), not confirm time or restart time
    assert close[0].payload["gap_end"] == 2990.0


def test_warmup_grace_prevents_thrash_kill():
    m = StateMachine(stale_seconds=900.0, warmup_grace_seconds=600.0)
    m.last_healthy_heartbeat = 1000.0
    m.step(Reading(True, 1000.0, 1000.0))
    m.step(Reading(True, 1000.0, 2000.0))  # SUSPECT
    m.step(Reading(True, 1000.0, 2030.0))  # OUTAGE_OPEN: kill+launch (attempt 1)
    # 30s later, still alive but stale (relaunched collector warming up)
    a = m.step(Reading(True, 1000.0, 2060.0))
    assert "kill_process" not in _kinds(a)
    assert "launch_process" not in _kinds(a)


def test_bounded_retries_give_up():
    m = StateMachine(
        stale_seconds=900.0,
        warmup_grace_seconds=0.0,
        backoff_initial=10.0,
        backoff_factor=2.0,
        backoff_max=100.0,
        max_restart_attempts=3,
    )
    m.last_healthy_heartbeat = 1000.0
    m.step(Reading(True, 1000.0, 1000.0))
    m.step(Reading(False, 1000.0, 2000.0))  # attempt 1
    assert m.restart_attempts == 1
    m.step(Reading(False, 1000.0, 2010.0))  # attempt 2
    assert m.restart_attempts == 2
    m.step(Reading(False, 1000.0, 2030.0))  # attempt 3
    assert m.restart_attempts == 3
    a = m.step(Reading(False, 1000.0, 2070.0))  # max reached -> no more restarts
    assert m.restart_attempts == 3
    assert "launch_process" not in _kinds(a)
    assert any(
        x.kind == "recovery_failed" and x.payload.get("reason") == "max_restart_attempts_reached"
        for x in a
    )


def test_starting_stale_baseline_opens_gap_without_kill():
    m = StateMachine(stale_seconds=900.0, warmup_grace_seconds=600.0)
    m.last_healthy_heartbeat = 1000.0  # stale on-disk baseline (e.g. after reboot)
    a = m.step(Reading(True, 1000.0, 5000.0))
    assert m.state == OUTAGE_OPEN
    oo = _by_kind(a, "outage_open")[0]
    assert oo.payload["gap_start"] == 1000.0
    assert oo.payload["reason"] == "stale_at_startup"
    # process alive: do not kill or launch on the first check (may be warming up)
    assert "kill_process" not in _kinds(a)
    assert "launch_process" not in _kinds(a)


def test_starting_fresh_goes_healthy():
    m = StateMachine(stale_seconds=900.0)
    m.last_healthy_heartbeat = 1000.0
    m.step(Reading(True, 1050.0, 1060.0))
    assert m.state == HEALTHY
    assert m.last_healthy_heartbeat == 1050.0


def test_union_intervals_merges_overlap():
    assert union_intervals([(0, 10), (5, 15), (20, 25)]) == [(0, 15), (20, 25)]
    # adjacent (touching) intervals merge, matching the coverage gate's union
    assert union_intervals([(0, 10), (10, 20)]) == [(0, 20)]


def test_subtract_intervals():
    assert subtract_intervals((0, 100), [(20, 40), (60, 80)]) == [(0, 20), (40, 60), (80, 100)]
    assert subtract_intervals((0, 100), [(0, 100)]) == []


def test_closed_intervals_pairing():
    events = [
        {"event": "outage_open", "gap_start": "a", "reason": "process_dead"},
        {"event": "outage_close", "gap_start": "a", "gap_end": "b"},
        {"event": "outage_open", "gap_start": "c", "reason": "process_dead"},
    ]
    closed, still_open = closed_intervals_from_events(events)
    assert len(closed) == 1
    assert closed[0]["gap_start"] == "a"
    assert closed[0]["gap_end"] == "b"
    assert len(still_open) == 1
    assert still_open[0]["gap_start"] == "c"


def test_coverage_snapshot_official_vs_provisional(tmp_path):
    start = REAL_DATA_START_US
    gaps = {
        "gaps": [
            {
                "id": "g1",
                "start": (start + timedelta(days=1)).isoformat(),
                "end": (start + timedelta(days=2)).isoformat(),
                "reason": "test",
            }
        ]
    }
    cfg = tmp_path / "gaps.json"
    cfg.write_text(json.dumps(gaps), encoding="utf-8")
    now = start + timedelta(days=10)
    snap = coverage_snapshot(now, gaps_config=cfg)
    assert snap["official_coverage"] == pytest.approx(0.9)
    # an open gap of one more day drops provisional to 0.8
    open_gap = (start + timedelta(days=3), start + timedelta(days=4))
    snap2 = coverage_snapshot(now, open_gap=open_gap, gaps_config=cfg)
    assert snap2["provisional_coverage"] == pytest.approx(0.8)
    assert snap2["open_gap_seconds"] == pytest.approx(86400.0)


def test_build_candidates_pairs_and_subtracts():
    events = [
        {"event": "outage_open", "gap_start": "2026-10-14T10:00:00+00:00", "reason": "process_dead"},
        {"event": "outage_close", "gap_start": "2026-10-14T10:00:00+00:00", "gap_end": "2026-10-14T12:00:00+00:00"},
        {"event": "outage_open", "gap_start": "2026-10-14T13:00:00+00:00", "reason": "process_dead"},
    ]
    existing = [
        {"id": "existing", "start": "2026-10-14T10:00:00+00:00", "end": "2026-10-14T11:00:00+00:00"}
    ]
    candidates, still_open = build_candidates(events, existing)
    assert len(candidates) == 1
    assert candidates[0]["start"] == "2026-10-14T11:00:00+00:00"
    assert candidates[0]["end"] == "2026-10-14T12:00:00+00:00"
    assert candidates[0]["price_path"] == "unobserved"
    assert candidates[0]["outcomes"] == "recovered"
    assert set(candidates[0]["feeds_affected"]) == set(US_RETAIL_FEEDS)
    assert len(still_open) == 1


def test_process_manager_launch_does_not_adopt_stale_pid(monkeypatch, tmp_path):
    """Regression: launch() must not keep returning a dead pid after the process died."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "us_gap_watchdog_script", "scripts/us_gap_watchdog.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "_find_supervisor_pid", lambda: 99999)  # stale pid
    monkeypatch.setattr(mod, "_tasklist_has_pid", lambda pid: False)  # but it is dead
    monkeypatch.setattr(mod, "DEFAULT_SUPERVISOR_LOG", tmp_path)

    class _FakeProc:
        pid = 55555

    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: _FakeProc())

    pm = mod.ProcessManager(["python", "x.py"])
    assert pm.launch() == 55555  # a real launch, not the stale 99999