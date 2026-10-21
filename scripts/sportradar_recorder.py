#!/usr/bin/env python3
"""Sportradar Push recorder (SPORTS_CAUSAL_US_V1 — source capture, no alpha).

Long-lived HTTP chunked streaming (NOT WebSocket). Follows redirect, keeps the
connection open, parses streaming JSON, detects ~5s heartbeats, and writes
raw-before-parse + normalized SportsEvents. On disconnect it records an explicit
recovery boundary and reconnects; REST catch-up is a follow-up once the summary/
play-by-play endpoints are pinned.

Auth: x-api-key header. Env: SPORTRADAR_API_KEY, SPORTRADAR_ACCESS_LEVEL.

Usage:
    .venv/Scripts/python.exe scripts/sportradar_recorder.py --league nfl --kind events
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.sports.sportradar import normalize_push, push_url  # noqa: E402

OUT = ROOT / "data" / "sports" / "sportradar"


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class DayWriter:
    def __init__(self, root: Path):
        self.root = root
        self.day = None
        self.fh = None

    def write(self, record: dict) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        if day != self.day:
            if self.fh:
                self.fh.close()
            self.day = day
            p = self.root / f"{day}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            self.fh = p.open("a", encoding="utf-8")
        self.fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self.fh.flush()

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


def _iter_stream(resp, buf: bytearray):
    """Yield decoded text lines from a chunked HTTP stream, with buffering."""
    for chunk in iter(lambda: resp.read(4096), b""):
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            if line.strip():
                yield line.decode("utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="nfl", choices=["nfl", "nba", "nhl", "ncaamb"])
    ap.add_argument("--kind", default="events", choices=["events", "pulse"])
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--max-seconds", type=float, default=0.0)
    args = ap.parse_args()

    _load_env(Path(args.env))
    key = os.environ.get("SPORTRADAR_API_KEY", "")
    level = os.environ.get("SPORTRADAR_ACCESS_LEVEL", "production")
    if not key:
        print("SPORTRADAR_API_KEY not set (blocked on Sportradar Push-enabled key)", file=sys.stderr)
        return 1

    url = push_url(args.league, level, args.kind)
    writer = DayWriter(OUT)
    session_seq = 0
    session_id = f"sportradar-{int(time.time() * 1000)}"
    started = time.monotonic()

    try:
        while True:
            if args.max_seconds and (time.monotonic() - started) >= args.max_seconds:
                break
            try:
                req = urllib.request.Request(url, headers={"x-api-key": key})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    print(f"[{datetime.now(UTC).isoformat()}] connected ({args.league}/{args.kind}) status={resp.status}", flush=True)
                    buf = bytearray()
                    for line in _iter_stream(resp, buf):
                        if args.max_seconds and (time.monotonic() - started) >= args.max_seconds:
                            break
                        wall = time.time_ns()
                        perf = time.perf_counter_ns()
                        session_seq += 1
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            raw = {"_unparseable": line}
                        # heartbeat / keepalive: raw-only, not a sports event
                        is_heartbeat = isinstance(raw, dict) and raw.get("type") in ("heartbeat", "keepalive", "ping")
                        writer.write({
                            "record_type": "raw",
                            "league": args.league,
                            "session_id": session_id,
                            "session_seq": session_seq,
                            "received_wall_ns": wall,
                            "received_perf_ns": perf,
                            "heartbeat": is_heartbeat,
                            "raw": raw,
                        })
                        if not is_heartbeat:
                            ev = normalize_push(args.league, raw, wall, perf, session_id, session_seq)
                            writer.write({"record_type": "sports_event", **ev.to_record()})
            except Exception as exc:  # noqa: BLE001 — reconnect with recovery record
                writer.write({
                    "record_type": "recovery_boundary",
                    "league": args.league,
                    "session_id": session_id,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "at": datetime.now(UTC).isoformat(),
                    "note": "REST play-by-play/summary catch-up to be pinned and applied here",
                })
                session_id = f"sportradar-{int(time.time() * 1000)}"
                session_seq = 0
                print(f"[{datetime.now(UTC).isoformat()}] disconnect; reconnect in 5s ({exc})", file=sys.stderr, flush=True)
                time.sleep(5)
    finally:
        writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())