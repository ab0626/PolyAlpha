"""Sportradar source-adapter tests (parsing + normalization; no live access)."""

from __future__ import annotations

import json

from polyalpha.sports.sportradar import (
    LEAGUE_ENDPOINTS,
    normalize_push,
    push_url,
)


def _nfl_event():
    return {
        "event": {
            "match_id": "sr-match-123",
            "event_id": "ev-1",
            "event_type": "touchdown",
            "event_category": "scoring_play",
            "home_team": "TEN",
            "away_team": "LAC",
            "home_score": "14",
            "away_score": "7",
            "quarter": "2",
            "clock": "05:32",
            "wall_clock": "2025-11-02T18:45:00Z",
            "created_at": "2025-11-02T18:45:01Z",
            "updated_at": "2025-11-02T18:45:01Z",
        }
    }


def test_push_url_construction():
    assert push_url("nfl", "production", "events").startswith(
        "https://api.sportradar.com/nfl/official/production/stream/en/events/subscribe"
    )
    assert push_url("nba", "trial", "events").startswith(
        "https://api.sportradar.com/nba/trial/stream/en/events/subscribe"
    )
    assert "nfl" in LEAGUE_ENDPOINTS and "pulse" in LEAGUE_ENDPOINTS["nfl"]


def test_normalize_retains_provider_timestamps_and_raw():
    raw = _nfl_event()
    ev = normalize_push("nfl", raw, received_wall_ns=111, received_perf_ns=222, session_id="s1", session_seq=1)
    assert ev.league == "nfl"
    assert ev.match_id == "sr-match-123"
    assert ev.event_type == "touchdown"
    assert ev.home_score == __import__("decimal").Decimal("14")
    assert ev.away_score == __import__("decimal").Decimal("7")
    assert ev.period == "2"
    assert ev.game_clock == "05:32"
    assert ev.provider_event_ts is not None      # wall_clock -> epoch ms
    assert ev.provider_created_at is not None
    assert ev.provider_updated_at is not None
    assert ev.received_wall_ns == 111 and ev.received_perf_ns == 222
    assert ev.raw_payload == raw                  # raw preserved exactly


def test_normalize_defensive_on_unknown_schema():
    ev = normalize_push("nfl", {"unexpected": 1}, 1, 2, "s", 1)
    assert ev.provider == "sportradar"
    assert ev.match_id is None and ev.event_id is None
    assert ev.recovery_status == "live"


def test_deterministic_serialization():
    raw = _nfl_event()
    a = normalize_push("nfl", raw, 1, 2, "s", 1).to_record()
    b = normalize_push("nfl", raw, 1, 2, "s", 1).to_record()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)