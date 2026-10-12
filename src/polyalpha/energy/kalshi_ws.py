"""Authenticated Kalshi WebSocket collector helpers (parallel, isolated lineage).

Collects real-time order-book deltas, trades, and market lifecycle over the
authenticated Kalshi Trade API WebSocket. This is a SEPARATE raw lineage from
the frozen 30-second REST ladder snapshots (E3-v3) — the two never mix.

Storage (append-only; nothing overwritten):
    data/energy/kalshi/ws/YYYY-MM-DD.jsonl   one row per message
    data/energy/kalshi/ws/status.json        atomic freshness heartbeat

Signing for the WS handshake is identical to REST except the signed path is
``/trade-api/ws/v2`` (see docs.kalshi.com — Quick Start: WebSockets).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..kalshi_auth import sign_request

WS_SIGN_PATH = "/trade-api/ws/v2"
WS_URLS = {
    "production": "wss://external-api-ws.kalshi.com" + WS_SIGN_PATH,
    "demo": "wss://external-api-ws.demo.kalshi.co" + WS_SIGN_PATH,
}


def ws_auth_headers(key_id: str, private_key, now_ms: int | None = None) -> dict[str, str]:
    """KALSHI-ACCESS-* headers for the authenticated WS handshake."""
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
    signature = sign_request(private_key, ts, "GET", WS_SIGN_PATH)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


def subscribe_message(msg_id: int, channels: list[str], market_tickers: list[str] | None = None) -> dict:
    params: dict = {"channels": list(channels)}
    if market_tickers:
        params["market_tickers"] = list(market_tickers)
    return {"id": msg_id, "cmd": "subscribe", "params": params}


def ws_raw_dir(root: str | Path = "data/energy/kalshi/ws") -> Path:
    return Path(root)


def append_jsonl(path: str | Path, rows: list[dict]) -> int:
    """Append rows to a JSONL file; returns bytes written. No-op if empty."""
    path = Path(path)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, sort_keys=True, default=str) for r in rows) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
    return len(text.encode("utf-8"))


def write_status(path: str | Path, status: dict) -> None:
    """Atomically write the freshness heartbeat (tmp + rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(p)