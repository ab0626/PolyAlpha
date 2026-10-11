#!/usr/bin/env python3
"""Authenticated Kalshi WebSocket collector — energy markets.

Streams real-time order-book deltas, trades, ticker updates, and market
lifecycle over the authenticated Kalshi Trade API WebSocket, into a raw lineage
that is SEPARATE from the frozen 30-second REST ladder snapshots (E3-v3).

    data/energy/kalshi/ws/YYYY-MM-DD.jsonl   one row per message (receipt + seq + type + msg)
    data/energy/kalshi/ws/status.json        atomic freshness heartbeat

Reconnects with exponential backoff (capped), re-subscribes on reconnect, and
periodically re-discovers active energy markets so newly opened operating days
are picked up. Append-only; idempotent across restarts.

Usage:
    .venv/Scripts/python.exe scripts/energy_ws_collector.py \
        --env-file C:\\Users\\adith\\powermarkets.env
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.kalshi import ENERGY_SERIES, KalshiPublicClient  # noqa: E402
from polyalpha.energy.kalshi_ws import (  # noqa: E402
    WS_URLS,
    subscribe_message,
    ws_auth_headers,
    ws_raw_dir,
    write_status,
)
from polyalpha.kalshi_auth import load_env_file, load_private_key  # noqa: E402

DEFAULT_ENV_FILE = r"C:\Users\adith\powermarkets.env"
STATUS_INTERVAL_SECONDS = 30.0
FLUSH_EVERY_MESSAGES = 50
MAX_BACKOFF_SECONDS = 60.0


class DayWriter:
    """Buffered append-only writer, one open handle per UTC day."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.day: str | None = None
        self.fh = None

    def write(self, row: dict) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        if day != self.day:
            if self.fh is not None:
                self.fh.close()
            self.day = day
            path = self.root / f"{day}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = path.open("a", encoding="utf-8")
        self.fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    def flush(self) -> None:
        if self.fh is not None:
            self.fh.flush()

    def close(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None


def discover_active_markets(series_tickers: list[str]) -> list[str]:
    """Active market tickers for the given series (public REST)."""
    client = KalshiPublicClient()
    tickers: list[str] = []
    for series in series_tickers:
        try:
            body = client.markets(series_ticker=series, limit=1000)
        except Exception:  # noqa: BLE001 — best-effort per series
            continue
        for m in body.get("markets", []):
            if m.get("status") == "active" and m.get("ticker"):
                tickers.append(m["ticker"])
    return tickers


async def run(args: argparse.Namespace) -> int:
    key_id = os.environ["KALSHI_API_KEY_ID"]
    private_key = load_private_key(os.environ["KALSHI_PRIVATE_KEY_PATH"])
    env = os.environ.get("KALSHI_ENV", "production")
    ws_url = WS_URLS.get(env)
    if ws_url is None:
        print(f"unknown KALSHI_ENV {env!r}", file=sys.stderr)
        return 1

    root = Path(args.root)
    status_path = Path(args.status_file)
    writer = DayWriter(root)

    status = {
        "started_at": datetime.now(UTC).isoformat(),
        "env": env,
        "connected_at": None,
        "heartbeat_at": None,
        "last_message_at": None,
        "last_message_type": None,
        "messages_received": 0,
        "reconnects": 0,
        "market_tickers": 0,
        "last_error": None,
        "last_error_at": None,
        "last_disconnect_at": None,
    }
    write_status(status_path, status)

    backoff = 1.0
    msg_id = 0
    seq = 0
    last_status_write = time.monotonic()
    started = time.monotonic()

    try:
        while True:
            if args.max_seconds and (time.monotonic() - started) >= args.max_seconds:
                print("max-seconds reached; stopping", flush=True)
                break

            market_tickers = await asyncio.to_thread(discover_active_markets, args.series)
            status["market_tickers"] = len(market_tickers)
            write_status(status_path, status)

            try:
                headers = ws_auth_headers(key_id, private_key)
                async with websockets.connect(ws_url, additional_headers=headers) as ws:
                    backoff = 1.0
                    status["connected_at"] = datetime.now(UTC).isoformat()
                    status["last_error"] = None
                    status["last_error_at"] = None
                    status["last_disconnect_at"] = None
                    write_status(status_path, status)

                    msg_id += 1
                    await ws.send(json.dumps(subscribe_message(
                        msg_id, ["orderbook_delta", "trade", "ticker"], market_tickers
                    )))
                    msg_id += 1
                    await ws.send(json.dumps(subscribe_message(
                        msg_id, ["market_lifecycle_v2"], market_tickers
                    )))
                    print(
                        f"[{datetime.now(UTC).isoformat()}] connected ({env}); "
                        f"subscribed to {len(market_tickers)} energy markets",
                        flush=True,
                    )

                    last_resubscribe = time.monotonic()
                    while True:
                        if args.max_seconds and (time.monotonic() - started) >= args.max_seconds:
                            break
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            # Heartbeat: confirm alive + connected even when no
                            # messages are flowing (quiet market).
                            if time.monotonic() - last_status_write >= STATUS_INTERVAL_SECONDS:
                                last_status_write = time.monotonic()
                                status["heartbeat_at"] = datetime.now(UTC).isoformat()
                                write_status(status_path, status)
                            # Wake-up: re-discover markets on the refresh cadence to
                            # pick up newly opened operating days.
                            if time.monotonic() - last_resubscribe >= args.refresh_seconds:
                                last_resubscribe = time.monotonic()
                                new_tickers = await asyncio.to_thread(
                                    discover_active_markets, args.series
                                )
                                if new_tickers != market_tickers:
                                    market_tickers = new_tickers
                                    status["market_tickers"] = len(market_tickers)
                                    msg_id += 1
                                    await ws.send(json.dumps(subscribe_message(
                                        msg_id, ["orderbook_delta", "trade", "ticker"], market_tickers
                                    )))
                                    print(
                                        f"[{datetime.now(UTC).isoformat()}] resubscribed: "
                                        f"{len(market_tickers)} energy markets",
                                        flush=True,
                                    )
                            continue

                        seq += 1
                        received = datetime.now(UTC)
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            data = {"raw": message}
                        writer.write({
                            "received_at": received.isoformat(),
                            "seq": seq,
                            "type": data.get("type"),
                            "message": data,
                        })

                        status["last_message_at"] = received.isoformat()
                        status["last_message_type"] = data.get("type")
                        status["messages_received"] += 1
                        status["last_error"] = None
                        status["last_error_at"] = None

                        if seq % FLUSH_EVERY_MESSAGES == 0:
                            writer.flush()
                        now = time.monotonic()
                        if now - last_status_write >= STATUS_INTERVAL_SECONDS:
                            last_status_write = now
                            write_status(status_path, status)

            except Exception as exc:  # noqa: BLE001 — reconnect with backoff
                status["last_error"] = f"{type(exc).__name__}: {exc}"
                status["last_error_at"] = datetime.now(UTC).isoformat()
                status["last_disconnect_at"] = datetime.now(UTC).isoformat()
                status["reconnects"] += 1
                writer.flush()
                write_status(status_path, status)
                print(
                    f"[{datetime.now(UTC).isoformat()}] DISCONNECT {status['last_error']} "
                    f"— retry in {backoff:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    finally:
        writer.flush()
        writer.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Authenticated Kalshi WS collector (energy)")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                    help="path to the .env file with Kalshi credentials")
    ap.add_argument("--series", nargs="*", default=list(ENERGY_SERIES.keys()),
                    help="series tickers to subscribe to (default: all energy series)")
    ap.add_argument("--root", default="data/energy/kalshi/ws")
    ap.add_argument("--status-file", default="data/energy/kalshi/ws/status.json")
    ap.add_argument("--refresh-seconds", type=float, default=300.0,
                    help="seconds between market re-discovery (default 300)")
    ap.add_argument("--max-seconds", type=float, default=0.0,
                    help="stop after this many seconds (0 = run forever; testing)")
    args = ap.parse_args()

    load_env_file(args.env_file)
    if not os.environ.get("KALSHI_API_KEY_ID") or not os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        print("KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set", file=sys.stderr)
        return 1

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())