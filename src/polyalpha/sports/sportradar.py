"""Sportradar Push source adapter (SPORTS_CAUSAL_US_V1 — source layer only).

Normalizes Sportradar Push event payloads (NFL/NBA/NHL/NCAAMB) into a canonical
SportsEvent, retaining ALL provider timestamps and the raw payload. No alpha,
no markouts, no lead/lag. Auth: x-api-key header.

Transport note: Push is long-lived HTTP chunked streaming (NOT WebSocket); the
client must follow the redirect, keep the connection open, parse streaming JSON,
and detect ~5s heartbeats. Recovery is REST play-by-play/summary, not stateful
resume.

Frozen scope: none yet — SPORTS_CAUSAL_LEAD_LAG_V1 is not frozen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

PROVIDER = "sportradar"

# access_level is substituted into {level} (e.g. "production" or "trial").
LEAGUE_ENDPOINTS: dict[str, dict[str, str]] = {
    "nfl": {
        "events": "https://api.sportradar.com/nfl/official/{level}/stream/en/events/subscribe",
        "pulse": "https://api.sportradar.com/nfl/official/{level}/stream/en/pulse/subscribe",
    },
    "nba": {
        "events": "https://api.sportradar.com/nba/{level}/stream/en/events/subscribe",
    },
    "nhl": {
        "events": "https://api.sportradar.com/nhl/{level}/stream/en/events/subscribe",
    },
    "ncaamb": {
        "events": "https://api.sportradar.com/ncaamb/{level}/stream/en/events/subscribe",
    },
}


def _dec(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _str(v):
    if v is None:
        return None
    return str(v)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ts(v):
    """Parse a provider timestamp (ISO string or epoch) -> epoch ms, else None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class SportsEvent:
    provider: str
    league: str
    match_id: str | None
    event_id: str | None
    event_type: str | None
    event_category: str | None
    home_team: str | None
    away_team: str | None
    home_score: Decimal | None
    away_score: Decimal | None
    period: str | None
    game_clock: str | None
    provider_event_ts: int | None
    provider_created_at: int | None
    provider_updated_at: int | None
    received_wall_ns: int
    received_perf_ns: int
    session_id: str
    session_seq: int
    recovery_status: str
    raw_payload: dict = field(compare=False)

    def to_record(self) -> dict:
        return {
            "provider": self.provider,
            "league": self.league,
            "match_id": self.match_id,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_category": self.event_category,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_score": str(self.home_score) if self.home_score is not None else None,
            "away_score": str(self.away_score) if self.away_score is not None else None,
            "period": self.period,
            "game_clock": self.game_clock,
            "provider_event_ts": self.provider_event_ts,
            "provider_created_at": self.provider_created_at,
            "provider_updated_at": self.provider_updated_at,
            "received_wall_ns": self.received_wall_ns,
            "received_perf_ns": self.received_perf_ns,
            "session_id": self.session_id,
            "session_seq": self.session_seq,
            "recovery_status": self.recovery_status,
            "raw_payload": self.raw_payload,
        }


def _pick(d: dict, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _find_event(d: dict) -> dict:
    """Locate the event object inside a Push envelope (defensive)."""
    for k in ("event", "payload", "message", "data"):
        v = d.get(k)
        if isinstance(v, dict):
            return v
    return d


def normalize_push(league: str, raw: dict, received_wall_ns: int, received_perf_ns: int,
                   session_id: str, session_seq: int, recovery_status: str = "live") -> SportsEvent:
    """Normalize a single Push message into a SportsEvent (defensive, retains raw)."""
    e = _find_event(raw)
    return SportsEvent(
        provider=PROVIDER,
        league=league,
        match_id=_str(_pick(e, "match_id", "matchId", "game_id", "gameId")),
        event_id=_str(_pick(e, "event_id", "eventId", "id", "sequence")),
        event_type=_str(_pick(e, "event_type", "eventType", "type", "event")),
        event_category=_str(_pick(e, "event_category", "eventCategory", "category")),
        home_team=_str(_pick(e, "home_team", "homeTeam", "home", "home_name", "homeName")),
        away_team=_str(_pick(e, "away_team", "awayTeam", "away", "away_name", "awayName")),
        home_score=_dec(_pick(e, "home_score", "homeScore", "home_points", "homePoints", "score_home")),
        away_score=_dec(_pick(e, "away_score", "awayScore", "away_points", "awayPoints", "score_away")),
        period=_str(_pick(e, "period", "quarter", "period_number", "periodNumber")),
        game_clock=_str(_pick(e, "game_clock", "gameClock", "clock", "time")),
        provider_event_ts=_ts(_pick(e, "wall_clock", "wallClock", "event_time", "eventTime", "timestamp")),
        provider_created_at=_ts(_pick(e, "created_at", "createdAt", "created")),
        provider_updated_at=_ts(_pick(e, "updated_at", "updatedAt", "updated")),
        received_wall_ns=received_wall_ns,
        received_perf_ns=received_perf_ns,
        session_id=session_id,
        session_seq=session_seq,
        recovery_status=recovery_status,
        raw_payload=raw,
    )


def push_url(league: str, access_level: str, kind: str = "events") -> str:
    cfg = LEAGUE_ENDPOINTS.get(league)
    if cfg is None or kind not in cfg:
        raise ValueError(f"unsupported league/kind: {league}/{kind}")
    return cfg[kind].format(level=access_level)


__all__ = [
    "SportsEvent", "normalize_push", "push_url", "LEAGUE_ENDPOINTS", "PROVIDER",
]