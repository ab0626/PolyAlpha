"""Family E1 evaluator — energy threshold calibration (Kalshi ERCOT peak load).

Implements the frozen observation rule in ``docs/ENERGY_E1_SPEC.md`` (v1). The
unit of analysis is a (price, outcome) pair observed at a fixed ex-ante horizon;
one operating day is one dependence cluster, so inference is day-clustered and
strikes within a day are never treated as independent.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from .kalshi import KalshiPublicClient

SERIES = "KXTXERCOTPEAKD"
HORIZONS_HOURS: tuple[int, ...] = (24, 12, 6, 1)
SPLIT_DATE = datetime(2026, 10, 1, tzinfo=UTC)
TOLERANCE_MINUTES = 15
MIN_BIN_PAIRS = 20
MIN_BIN_DAYS = 5
MIN_HORIZON_PAIRS = 50
MISSING_RATE_FLAG = 0.20
SEED = 42

BINS = [(round(i * 0.1, 1), round((i + 1) * 0.1, 1)) for i in range(10)]


def _f(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _operating_day(market: dict) -> str:
    return (market.get("occurrence_datetime") or market.get("close_time") or "")[:10]


def fetch_settled_markets(client: KalshiPublicClient) -> list[dict]:
    """All settled binary markets in the series (result yes/no)."""
    body = client.markets(series_ticker=SERIES, limit=1000)
    out = []
    for m in body.get("markets", []):
        if m.get("result") in ("yes", "no") and m.get("close_time"):
            out.append(m)
    return out


def fetch_candles(client: KalshiPublicClient, ticker: str,
                  start_ts: int, end_ts: int) -> list[dict]:
    """1-minute candlesticks over [start_ts, end_ts], following pagination."""
    candles: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {
            "start_ts": str(start_ts),
            "end_ts": str(end_ts),
            "period_interval": "1",
        }
        if cursor:
            params["cursor"] = cursor
        body = client._get(f"/series/{SERIES}/markets/{ticker}/candlesticks", params)
        candles.extend(body.get("candlesticks", []))
        cursor = body.get("cursor")
        if not cursor:
            break
    return candles


def _close_prices(candles: list[dict], t_h: int) -> tuple[float, float] | None:
    """Bid/ask close of the last 1-min candle ending at or before t_h, within tolerance."""
    best: dict | None = None
    for c in candles:
        end = c.get("end_period_ts")
        if not isinstance(end, int):
            continue
        if end <= t_h and t_h - end <= TOLERANCE_MINUTES * 60:
            if best is None or end > best.get("end_period_ts", 0):
                best = c
    if best is None:
        return None
    bid = _f((best.get("yes_bid") or {}).get("close_dollars"))
    ask = _f((best.get("yes_ask") or {}).get("close_dollars"))
    if bid is None or ask is None:
        return None
    return bid, ask


def observation_prices(client: KalshiPublicClient, market: dict) -> dict[int, tuple[float, float] | None]:
    """Map horizon (hours) -> (yes_bid, yes_ask) at t_h = close - h."""
    close = _parse_iso(market["close_time"])
    end_ts = int(close.timestamp())
    start_ts = end_ts - (max(HORIZONS_HOURS) + 1) * 3600
    try:
        candles = fetch_candles(client, market["ticker"], start_ts, end_ts)
    except Exception:  # noqa: BLE001
        return {h: None for h in HORIZONS_HOURS}
    out: dict[int, tuple[float, float] | None] = {}
    for h in HORIZONS_HOURS:
        out[h] = _close_prices(candles, end_ts - h * 3600)
    return out


def _bin_of(p: float) -> int:
    return min(int(p * 10), 9)


def evaluate(client: KalshiPublicClient) -> dict[str, Any]:
    markets = fetch_settled_markets(client)
    rows: list[dict] = []
    for m in markets:
        y = 1 if m["result"] == "yes" else 0
        day = _operating_day(m)
        prices = observation_prices(client, m)
        for h, pq in prices.items():
            if pq is None:
                rows.append({"h": h, "day": day, "y": y, "bid": None, "ask": None, "mid": None,
                             "close": m["close_time"], "missing": True})
            else:
                bid, ask = pq
                mid = (bid + ask) / 2.0
                rows.append({"h": h, "day": day, "y": y, "bid": bid, "ask": ask, "mid": mid,
                             "close": m["close_time"], "missing": False})

    report: dict[str, Any] = {
        "series": SERIES,
        "n_settled_markets": len(markets),
        "split": {"exploratory_before": SPLIT_DATE.isoformat()},
        "horizons": {},
    }
    for h in HORIZONS_HOURS:
        hr = [r for r in rows if r["h"] == h]
        report["horizons"][h] = _summarize(hr)
    return report


def _summarize(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    missing = sum(1 for r in rows if r["missing"])
    present = [r for r in rows if not r["missing"]]
    days = {r["day"] for r in present}
    out: dict[str, Any] = {
        "n_pairs": n,
        "n_present": len(present),
        "n_missing": missing,
        "missing_rate": round(missing / n, 4) if n else None,
        "n_operating_days": len(days),
        "flagged": "",
    }
    if len(present) < MIN_HORIZON_PAIRS:
        out["flagged"] = "INSUFFICIENT_DATA"
        out["brier_mid"] = None
        out["brier_ask"] = None
        out["brier_bid"] = None
        out["bins"] = []
        return out
    if missing / n > MISSING_RATE_FLAG:
        out["flagged"] = "LOW_QUALITY"

    out["brier_mid"] = round(_brier(present, "mid"), 5)
    out["brier_ask"] = round(_brier(present, "ask"), 5)
    out["brier_bid"] = round(_brier(present, "bid"), 5)
    out["mid_ci95"] = _day_bootstrap_ci(present, "mid")

    bins = []
    for lo, hi in BINS:
        bucket = [r for r in present if lo <= r["mid"] < hi or (hi == 1.0 and r["mid"] == 1.0)]
        bdays = {r["day"] for r in bucket}
        if len(bucket) >= MIN_BIN_PAIRS and len(bdays) >= MIN_BIN_DAYS:
            mean_p = sum(r["mid"] for r in bucket) / len(bucket)
            mean_y = sum(r["y"] for r in bucket) / len(bucket)
            bins.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(bucket),
                         "n_days": len(bdays), "mean_p": round(mean_p, 4),
                         "mean_y": round(mean_y, 4)})
        else:
            bins.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(bucket),
                         "n_days": len(bdays), "mean_p": None, "mean_y": None,
                         "note": "INSUFFICIENT_DATA"})
    out["bins"] = bins
    return out


def _brier(rows: list[dict], field: str) -> float:
    return sum((r[field] - r["y"]) ** 2 for r in rows) / len(rows)


def _day_bootstrap_ci(rows: list[dict], field: str, n_resample: int = 2000) -> tuple[float, float]:
    import random
    rng = random.Random(SEED)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    days = list(by_day.keys())
    stats: list[float] = []
    for _ in range(n_resample):
        sample: list[dict] = []
        for _d in range(len(days)):
            sample.extend(by_day[rng.choice(days)])
        if sample:
            stats.append(_brier(sample, field))
    stats.sort()
    lo = stats[int(0.025 * len(stats))]
    hi = stats[int(0.975 * len(stats))]
    return round(lo, 5), round(hi, 5)


def main() -> int:
    client = KalshiPublicClient()
    report = evaluate(client)
    print(json.dumps(report, indent=2, default=str))
    return 0