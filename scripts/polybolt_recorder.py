#!/usr/bin/env python3
"""PolyBolt prospective recorder v0 — non-authoritative, read-only.

Captures Polymarket PolyBolt reference-price + market envelopes over one
WebSocket. This is a DATA-CAPTURE stage only: it never computes alpha, fair
value, or modifies any #6C / frozen collector.

Channels (CLOB-gated; see docs.polymarket.com PolyBolt):
  price.crypto, price.equity, price.crypto.twap, price.polymarket

Capture schema (one row per envelope, raw-before-parse):
  record_type, channel, instrument_id, provider_ts_ms, seq, dropped,
  received_wall_ns, received_perf_ns, session_id, session_seq, reconnect_count,
  raw (the full JSON envelope text)

Persistence: append-only, UTC day partition, bounded off-loop writer,
reconnect/gap records, atomic status heartbeat.

Usage:
    .venv/Scripts/python.exe scripts/polybolt_recorder.py \
        --env-file .env [--poly-markets 0xABC...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]

WS_URL = "wss://ws-live-v2.polymarket.com/ws"
DEFAULT_ENV_FILE = ROOT / ".env"
ROOT_OUT = ROOT / "data" / "polymarket" / "polybolt"
STATUS_PATH = ROOT_OUT / "status.json"

ENV_API_KEY = "POLYMARKET_CLOB_API_KEY"
ENV_SECRET = "POLYMARKET_CLOB_API_SECRET"
ENV_PASSPHRASE = "POLYMARKET_CLOB_API_PASSPHRASE"

MAX_BACKOFF = 60.0
STATUS_INTERVAL = 30.0


def _default_subscriptions(poly_markets: list[str]) -> list[dict]:
    subs = [
        {"channel": "price.crypto", "filter": {"symbol": "btcusd"}},
        {"channel": "price.crypto", "filter": {"symbol": "ethusd"}},
        {"channel": "price.equity", "filter": {"symbol": "aapl"}},
        {"channel": "price.crypto.twap", "filter": {"symbol": "btcusd", "window_seconds": 60}},
    ]
    for m in poly_markets:
        subs.append({"channel": "price.polymarket", "filter": {"market": m}})
    return subs


def _instrument_id(envelope: dict) -> str | None:
    payload = envelope.get("payload") or {}
    return payload.get("symbol") or payload.get("market") or payload.get("asset_id")


def _provider_ts(envelope: dict) -> int | None:
    payload = envelope.get("payload") or {}
    ts = payload.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return int(ts)
    t = envelope.get("ts")
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return int(t)
    return None


class DayWriter:
    """Append-only buffered writer, one open handle per UTC day."""

    def __init__(self, root: Path):
        self.root = root
        self.day: str | None = None
        self.fh = None

    def write(self, record: dict) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        if day != self.day:
            if self.fh is not None:
                self.fh.close()
            self.day = day
            path = self.root / f"{day}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = path.open("a", encoding="utf-8")
        self.fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self.fh.flush()

    def close(self) -> None:
        if self.fh is not None:
            self.fh.flush()
            self.fh.close()
            self.fh = None


def _writer_loop(q: queue.Queue, writer: DayWriter) -> None:
    while True:
        item = q.get()
        if item is None:
            break
        try:
            writer.write(item)
        finally:
            q.task_done()


def _write_status(status: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATUS_PATH)


async def run(args: argparse.Namespace) -> int:
    api_key = os.environ.get(ENV_API_KEY, "")
    secret = os.environ.get(ENV_SECRET, "")
    passphrase = os.environ.get(ENV_PASSPHRASE, "")
    have_creds = bool(api_key and secret and passphrase)

    ROOT_OUT.mkdir(parents=True, exist_ok=True)
    writer = DayWriter(ROOT_OUT)
    q: queue.Queue = queue.Queue(maxsize=100_000)
    wthread = threading.Thread(target=_writer_loop, args=(q, writer), daemon=True)
    wthread.start()

    subs = _default_subscriptions(args.poly_markets)
    status = {
        "started_at": datetime.now(UTC).isoformat(),
        "connected_at": None,
        "heartbeat_at": None,
        "last_message_at": None,
        "authed": False,
        "credentials_present": have_creds,
        "messages_received": 0,
        "reconnects": 0,
        "last_error": None,
        "last_error_at": None,
    }
    _write_status(status)

    backoff = 1.0
    reconnect_count = 0
    session_seq = 0
    session_id = f"polybolt-{int(time.time() * 1000)}"
    last_seq: dict[str, int] = {}
    started = time.monotonic()

    try:
        while True:
            if args.max_seconds and (time.monotonic() - started) >= args.max_seconds:
                break
            try:
                async with websockets.connect(WS_URL, max_size=2**21) as ws:
                    backoff = 1.0
                    status["connected_at"] = datetime.now(UTC).isoformat()
                    status["last_error"] = None
                    status["last_error_at"] = None
                    _write_status(status)

                    if have_creds:
                        await ws.send(json.dumps({
                            "op": "auth", "rid": "a1",
                            "auth": {"apiKey": api_key, "secret": secret, "passphrase": passphrase},
                        }))
                    await ws.send(json.dumps({"op": "subscribe", "rid": "s1", "subscriptions": subs}))

                    while True:
                        if args.max_seconds and (time.monotonic() - started) >= args.max_seconds:
                            break
                        try:
                            text = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            if time.monotonic() - status.get("_last_write", 0) >= STATUS_INTERVAL:
                                status["heartbeat_at"] = datetime.now(UTC).isoformat()
                                status["_last_write"] = time.monotonic()
                                _write_status(status)
                            continue

                        wall_ns = time.time_ns()
                        perf_ns = time.perf_counter_ns()
                        session_seq += 1
                        status["messages_received"] += 1
                        status["last_message_at"] = datetime.now(UTC).isoformat()
                        status["heartbeat_at"] = status["last_message_at"]

                        rec = {
                            "record_type": "data",
                            "channel": None,
                            "instrument_id": None,
                            "provider_ts_ms": None,
                            "seq": None,
                            "dropped": None,
                            "received_wall_ns": wall_ns,
                            "received_perf_ns": perf_ns,
                            "session_id": session_id,
                            "session_seq": session_seq,
                            "reconnect_count": reconnect_count,
                            "raw": text,
                        }
                        try:
                            env = json.loads(text)
                        except json.JSONDecodeError:
                            rec["record_type"] = "unparseable"
                            q.put(rec)
                            continue

                        op = env.get("op")
                        if op:
                            # ack / error / ping — record control frames too
                            rec["record_type"] = "ack" if op in ("authed", "subscribed", "unsubscribed", "ping", "pong") else "error"
                            rec["op"] = op
                            rec["channel"] = env.get("channel")
                            rec["code"] = env.get("code")
                            if op == "authed":
                                status["authed"] = True
                            if op == "error" and env.get("code") == "auth_invalid":
                                status["last_error"] = "auth_invalid"
                                status["last_error_at"] = datetime.now(UTC).isoformat()
                            q.put(rec)
                            continue

                        channel = env.get("channel")
                        seq = env.get("seq")
                        dropped = env.get("dropped")
                        rec["channel"] = channel
                        rec["instrument_id"] = _instrument_id(env)
                        rec["provider_ts_ms"] = _provider_ts(env)
                        rec["seq"] = seq
                        rec["dropped"] = dropped

                        # Per-channel sequence gap detection (seq resets on reconnect).
                        if channel and isinstance(seq, int):
                            prev = last_seq.get(channel)
                            if prev is not None and seq != prev + 1 and not env.get("snapshot"):
                                q.put({
                                    "record_type": "gap",
                                    "channel": channel,
                                    "prev_seq": prev,
                                    "seq": seq,
                                    "session_id": session_id,
                                    "session_seq": session_seq,
                                    "reconnect_count": reconnect_count,
                                    "received_wall_ns": wall_ns,
                                    "received_perf_ns": perf_ns,
                                })
                            last_seq[channel] = seq
                        q.put(rec)
            except Exception as exc:  # noqa: BLE001 — reconnect with backoff
                reconnect_count += 1
                status["reconnects"] = reconnect_count
                status["last_error"] = f"{type(exc).__name__}: {exc}"
                status["last_error_at"] = datetime.now(UTC).isoformat()
                status["last_disconnect_at"] = datetime.now(UTC).isoformat()
                _write_status(status)
                q.put({
                    "record_type": "reconnect",
                    "reconnect_count": reconnect_count,
                    "reason": status["last_error"],
                    "at": status["last_disconnect_at"],
                    "session_id": session_id,
                })
                session_id = f"polybolt-{int(time.time() * 1000)}"
                session_seq = 0
                last_seq = {}
                print(f"[{datetime.now(UTC).isoformat()}] reconnect {reconnect_count}: {status['last_error']}", file=sys.stderr, flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
    finally:
        q.put(None)
        wthread.join(timeout=5)
        writer.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="PolyBolt prospective recorder v0 (read-only)")
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    ap.add_argument("--poly-markets", nargs="*", default=[], help="market ids for price.polymarket channel")
    ap.add_argument("--max-seconds", type=float, default=0.0, help="stop after N seconds (0 = forever)")
    args = ap.parse_args()

    # Load simple KEY=VALUE env (never logs values).
    env_path = Path(args.env_file)
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v

    if not any(os.environ.get(k) for k in (ENV_API_KEY, ENV_SECRET, ENV_PASSPHRASE)):
        print("NOTE: no POLYMARKET_CLOB_API_KEY/SECRET/PASSPHRASE set — "
              "reference-price channels are CLOB-gated and will reject with auth_required.", file=sys.stderr)

    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()