"""Read-only realized-edge measurement layer (the "strategy sheet").

Joins the append-only measurement ledgers (E3 shadow observations + resolutions)
with venue book/fill data and reports, family by family, whether predicted edge
survives contact with the market.

Design constraints (hard):
  - ZERO imports into the signal/execution path. Nothing imports this module.
  - NO authority: reads existing ledgers, writes DERIVED reports only. It never
    trades, never scores the frozen evaluation, and never mutates a ledger row.
  - Honest at low N: metrics that need resolutions are reported as
    ``INSUFFICIENT_DATA`` / ``None``, never filled with zero.

Economic edge definitions (E3, chosen side at decision time):

    e_pred     = p_model_side - p_exec_side - fee_est - buffer
    e_terminal = Y_side        - p_exec_side - fee_real

    EdgeCaptureRatio_B = mean(e_terminal) / mean(e_pred)   over the SAME bucket B
    (ratio of means, NOT mean of per-trade ratios).

``edge_realization_ratio`` (profitable-trade frequency) from
``polyalpha.edge_metrics`` is intentionally KEPT SEPARATE and distinct from the
economic ``edge_capture_ratio`` defined here.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

D = Decimal

# ── Status / verdict vocabulary ──────────────────────────────────────────────
STATUS_INSUFFICIENT = "INSUFFICIENT_DATA"
STATUS_CONTINUE = "CONTINUE"
STATUS_PROMOTE = "PROMOTE_CANDIDATE"
STATUS_KILL = "KILL"

# Markout horizons (seconds after entry).
MARKOUT_HORIZONS = (5, 30, 300, 1800)
# Tolerance (seconds) around each horizon. 30s/5m/30m are sized for the 30s REST
# ladder snapshots; 5s is kept strict so a sub-30s source absence reads as
# unavailable rather than silently using a ~12s-later ladder quote.
MARKOUT_TOLERANCE = {5: 3.0, 30: 30.0, 300: 60.0, 1800: 60.0}  # seconds

# E3 shadow-ledger fee placeholder (frozen, ENERGY_E3_SPEC v1) — NOT an
# adverse-selection buffer. The live-calibration signal path adds a separate
# 0.02 buffer on top of this.
E3_FEES = D("0.02")

# Kalshi taker fee used for the exit leg of a markout (from the live
# calibration fee rule). Maker=0, Taker=0.07.
TAKER_RATE = D("0.07")

# ── Markout measurement stream (derived, append-only) ────────────────────────
# The measurement stream records market-response facts per (trade, horizon).
# Records are NEVER rewritten; a better later source is appended as a NEW
# measurement with a different source / derivation version.
DERIVATION_VERSION = "markout-derivation-v1"
# When multiple measurements exist for one (trade, horizon), the sheet prefers
# the earliest entry in this tuple (then smallest lag).
SOURCE_PRECEDENCE = ("ws_orderbook", "ws_ticker", "ladder_rest")
DERIVATION_DOC = (
    "economic side = NO position entered at entry_price_no; "
    "M_h_mid = NO_mid(t+h) - entry; "
    "M_h_exit = NO_bid(t+h) - entry - taker_fee(NO_bid) when the NO bid has size, "
    "else exit = NO_EXIT_LIQUIDITY; NO_bid = 1 - YES ask; NO_mid = 1 - YES mid; "
    f"taker_rate={TAKER_RATE}; horizons={MARKOUT_HORIZONS}; tolerance_s={MARKOUT_TOLERANCE}"
)


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return D(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


# ── Ledger loaders ──────────────────────────────────────────────────────────

def load_observations(path: str | Path) -> list[dict]:
    return _load_jsonl(Path(path))


def load_resolutions(path: str | Path) -> dict[str, dict]:
    """{observation_id: resolution} — the append-only resolution stream."""
    out: dict[str, dict] = {}
    for row in _load_jsonl(Path(path)):
        oid = row.get("observation_id")
        if oid:
            out[oid] = row
    return out


def load_live_calibration(path: str | Path) -> tuple[list[dict], list[dict]]:
    """(trades, settlements) from the live-calibration ledger."""
    trades: list[dict] = []
    settlements: list[dict] = []
    for row in _load_jsonl(Path(path)):
        kind = row.get("record_type")
        if kind == "live_calibration_v0_trade":
            trades.append(row)
        elif kind == "live_calibration_v0_settlement":
            settlements.append(row)
    return trades, settlements


# ── Economic edge math ──────────────────────────────────────────────────────

def predicted_edge(p_model: Decimal, p_exec: Decimal, fee_est: Decimal, buffer: Decimal = D("0")) -> Decimal:
    """e_pred for one side (YES or NO), the chosen side at decision time."""
    return p_model - p_exec - fee_est - buffer


def terminal_edge(outcome: int, p_exec: Decimal, fee_real: Decimal) -> Decimal:
    """e_terminal = Y - p_exec - fee_real for one side."""
    return D(outcome) - p_exec - fee_real


def edge_capture_ratio(
    predicted: list[float],
    terminal: list[float],
    n_boot: int = 2000,
    seed: int = 42,
) -> dict:
    """Ratio of means E[terminal]/E[predicted] over the SAME observations + CI.

    Returns ``None`` metrics when the denominator is empty or zero — never a
    fabricated zero.
    """
    n = len(predicted)
    if n == 0:
        return {"ratio": None, "mean_predicted": None, "mean_terminal": None,
                "n": 0, "ci95": None, "status": STATUS_INSUFFICIENT}
    mp = sum(predicted) / n
    mt = sum(terminal) / n
    if mp == 0:
        return {"ratio": None, "mean_predicted": mp, "mean_terminal": mt,
                "n": n, "ci95": None, "status": "undefined_denominator_zero"}
    ratio = mt / mp
    rng = random.Random(seed)
    boot: list[float] = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        bmp = sum(predicted[i] for i in idx) / n
        bmt = sum(terminal[i] for i in idx) / n
        if bmp != 0:
            boot.append(bmt / bmp)
    ci: list[float] | None = None
    if len(boot) >= 100:
        boot.sort()
        ci = [boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot))]]
    return {"ratio": ratio, "mean_predicted": mp, "mean_terminal": mt,
            "n": n, "ci95": ci, "status": "available"}


def mean_terminal_by_bin(
    predicted: list[float],
    terminal: list[float],
    edges: tuple[float, ...] = (0.0, 0.02, 0.05, 0.10, 0.20, 0.50),
) -> list[dict]:
    """E[e_terminal | e_pred in bin] — the more decision-useful number."""
    bins: list[dict] = []
    for lo, hi in zip(edges, edges[1:]):
        sel_t = [t for p, t in zip(predicted, terminal) if lo <= p < hi]
        if not sel_t:
            bins.append({"bin": f"[{lo:.2f},{hi:.2f})", "n": 0,
                         "mean_terminal": None, "status": STATUS_INSUFFICIENT})
            continue
        bins.append({"bin": f"[{lo:.2f},{hi:.2f})", "n": len(sel_t),
                     "mean_terminal": sum(sel_t) / len(sel_t), "status": "available"})
    return bins


# ── Calibration (N-gated) ───────────────────────────────────────────────────

def reliability_bins(probs: list[float], outcomes: list[int], n_bins: int = 10) -> list[dict]:
    """Binned reliability (P(outcome=1 | p̂) vs p̂). Empty when no data."""
    if not probs:
        return []
    rows = sorted(zip(probs, outcomes))
    bins: list[dict] = []
    step = 1.0 / n_bins
    for i in range(n_bins):
        lo = i * step
        hi = (i + 1) * step if i < n_bins - 1 else 1.000001
        sel = [(p, y) for p, y in rows if lo <= p < hi]
        if not sel:
            bins.append({"bin": f"[{lo:.2f},{hi:.2f})", "n": 0,
                         "mean_predicted": None, "empirical": None})
            continue
        bins.append({
            "bin": f"[{lo:.2f},{hi:.2f})",
            "n": len(sel),
            "mean_predicted": sum(p for p, _ in sel) / len(sel),
            "empirical": sum(y for _, y in sel) / len(sel),
        })
    return bins


def brier_score(probs: list[float], outcomes: list[int]) -> float | None:
    if not probs:
        return None
    return sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / len(probs)


def log_loss_score(probs: list[float], outcomes: list[int]) -> float | None:
    if not probs:
        return None
    eps = 1e-15
    return -sum(
        math.log(max(eps, min(1 - eps, p))) if y == 1 else math.log(max(eps, min(1 - eps, 1 - p)))
        for p, y in zip(probs, outcomes)
    ) / len(probs)


# ── Markouts (WS quote reconstruction) ──────────────────────────────────────

@dataclass(frozen=True)
class Quote:
    ts_ms: int
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    source: str = "ws_ticker"
    yes_ask_size: Decimal | None = None  # exit-liquidity for a long-NO position

    @property
    def yes_mid(self) -> Decimal | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / D(2)
        return self.yes_bid if self.yes_bid is not None else self.yes_ask

    @property
    def no_bid(self) -> Decimal | None:
        # Executable NO-sell price = 1 - YES ask (sell NO == buy YES).
        return D(1) - self.yes_ask if self.yes_ask is not None else None

    @property
    def no_mid(self) -> Decimal | None:
        m = self.yes_mid
        return D(1) - m if m is not None else None


def ws_quotes_for_ticker(
    ws_dir: str | Path,
    ticker: str,
    t0_ms: int,
    horizon_ms: int,
    margin_ms: int = 120_000,
) -> list[Quote]:
    """Stream WS day file(s) and return ticker-channel quotes for ``ticker``
    inside [t0 - margin, t0 + horizon + margin] (by exchange ``ts_ms``).

    Substring-filtered before JSON parse (the target ticker is a tiny fraction
    of the stream), so the cost is dominated by I/O.
    """
    ws_dir = Path(ws_dir)
    lo = t0_ms - margin_ms
    hi = t0_ms + horizon_ms + margin_ms
    quotes: list[Quote] = []
    # The entry is inside one UTC day; scan that day's file (fall back to the
    # previous day's file too, cheaply skipped if absent).
    day = datetime.fromtimestamp(t0_ms / 1000, UTC).date()
    for f in (ws_dir / f"{day.isoformat()}.jsonl", ws_dir / f"{(day - timedelta(days=1)).isoformat()}.jsonl"):
        if not f.exists():
            continue
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                if ticker not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "ticker":
                    continue
                msg = (row.get("message") or {}).get("msg") or {}
                if msg.get("market_ticker") != ticker:
                    continue
                ts = msg.get("ts_ms")
                if not isinstance(ts, (int, float)):
                    continue
                if not (lo <= ts <= hi):
                    continue
                quotes.append(Quote(
                    ts_ms=int(ts),
                    yes_bid=_dec(msg.get("yes_bid_dollars")),
                    yes_ask=_dec(msg.get("yes_ask_dollars")),
                    source="ws_ticker",
                    yes_ask_size=_dec(msg.get("yes_ask_size_fp")),
                ))
    quotes.sort(key=lambda q: q.ts_ms)
    return quotes


def ladder_quotes_for_ticker(
    ladder_dir: str | Path,
    ticker: str,
    t0_ms: int,
    horizon_ms: int,
    margin_ms: int = 120_000,
) -> list[Quote]:
    """Return 30s REST ladder BBO quotes for ``ticker`` inside the same window.

    The ladder snapshots are the reliable 30s-resolution source that spans the
    whole trade window (the WS ticker channel is sparse in this interval).
    """
    ladder_dir = Path(ladder_dir)
    lo = t0_ms - margin_ms
    hi = t0_ms + horizon_ms + margin_ms
    quotes: list[Quote] = []
    day = datetime.fromtimestamp(t0_ms / 1000, UTC).date()
    f = ladder_dir / f"{day.isoformat()}.jsonl"
    if not f.exists():
        return quotes
    with f.open("r", encoding="utf-8") as fh:
        for line in fh:
            if ticker not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("ticker") != ticker:
                continue
            cap = _parse_iso(row.get("captured_at"))
            if cap is None:
                continue
            ts_ms = int(cap.timestamp() * 1000)
            if not (lo <= ts_ms <= hi):
                continue
            raw = row.get("raw") or {}
            quotes.append(Quote(
                ts_ms=ts_ms,
                yes_bid=_dec(raw.get("yes_bid_dollars")),
                yes_ask=_dec(raw.get("yes_ask_dollars")),
                source="ladder_rest",
                yes_ask_size=_dec(raw.get("yes_ask_size_fp")),
            ))
    quotes.sort(key=lambda q: q.ts_ms)
    return quotes


def _nearest_quote(quotes: list[Quote], target_ms: int, tol_ms: float) -> Quote | None:
    best: Quote | None = None
    best_d = tol_ms + 1
    for q in quotes:
        d = abs(q.ts_ms - target_ms)
        if d < best_d:
            best_d = d
            best = q
    return best


def markout_from_quotes(
    quotes: list[Quote],
    entry_price_no: Decimal,
    t0_ms: int,
    horizons: tuple[int, ...] = MARKOUT_HORIZONS,
    exit_taker_rate: Decimal = TAKER_RATE,
) -> dict:
    """Markouts for an economic NO position entered at ``entry_price_no``.

    M_h^mid  = NO_mid(t+h)  - entry
    M_h^exit = NO_bid(t+h)  - entry - C_exit     (NO bid = 1 - YES ask)

    A missing quote at a horizon is reported as ``unavailable`` (never zero).
    """
    out: dict = {}
    for h in horizons:
        target = t0_ms + h * 1000
        tol = MARKOUT_TOLERANCE.get(h, 10.0) * 1000
        q = _nearest_quote(quotes, target, tol)
        if q is None:
            out[f"{h}s"] = {
                "status": "unavailable_no_quote",
                "markout_mid": None,
                "markout_exit": None,
                "quote_ts_ms": None,
                "quote_source": None,
            }
            continue
        no_mid = q.no_mid
        no_bid = q.no_bid
        m_mid = float(no_mid - entry_price_no) if no_mid is not None else None
        m_exit = None
        # Executable exit requires a NO bid (== 1 - YES ask) with size.
        no_exit_liq = (no_bid is None) or (no_bid <= D(0) and (q.yes_ask_size is None or q.yes_ask_size <= 0))
        if no_bid is not None and not no_exit_liq:
            c_exit = exit_taker_rate * no_bid * (D(1) - no_bid)
            m_exit = float(no_bid - entry_price_no - c_exit)
        out[f"{h}s"] = {
            "status": "available" if not no_exit_liq else "available_no_exit_liquidity",
            "markout_mid": m_mid,
            "markout_exit": m_exit,
            "no_exit_liquidity": no_exit_liq,
            "quote_ts_ms": q.ts_ms,
            "elapsed_seconds": round((q.ts_ms - t0_ms) / 1000.0, 1),
            "quote_source": q.source,
            "yes_bid": str(q.yes_bid) if q.yes_bid is not None else None,
            "yes_ask": str(q.yes_ask) if q.yes_ask is not None else None,
            "yes_mid": str(q.yes_mid) if q.yes_mid is not None else None,
            "yes_ask_size": str(q.yes_ask_size) if q.yes_ask_size is not None else None,
            "no_bid": str(no_bid) if no_bid is not None else None,
            "no_ask": str(D(1) - q.yes_bid) if q.yes_bid is not None else None,
            "no_mid": str(no_mid) if no_mid is not None else None,
        }
    return out


# ── Markout measurement persistence (append-only derived stream) ─────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _quote_content_hash(
    source: str,
    ts_ms: int | None,
    yes_bid: str | None,
    yes_ask: str | None,
    yes_ask_size: str | None,
) -> str | None:
    """Hash of ALL raw quote inputs used by the markout derivation.

    Invariant (the "source hash = all raw inputs" rule): NO prices and NO
    liquidity are reconstructed from the YES BBO — ``no_bid = 1 - yes_ask``,
    ``no_mid = 1 - (yes_bid+yes_ask)/2``, ``no_ask = 1 - yes_bid``, and the exit
    liquidity verdict depends only on ``yes_ask`` + ``yes_ask_size``. Therefore
    the COMPLETE raw input set is exactly {source, ts_ms, yes_bid, yes_ask,
    yes_ask_size}. If any no_* field is ever read directly from a source
    snapshot, it MUST be added here as well.
    """
    if ts_ms is None:
        return None
    return _sha256(json.dumps({
        "source": source,
        "ts_ms": ts_ms,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_ask_size": yes_ask_size,
    }, sort_keys=True))


def markout_measurement_id(
    trade_id: str,
    horizon: str,
    source: str,
    quote_ts_ms: int | None,
    liquidity_status: str,
    derivation_version: str = DERIVATION_VERSION,
) -> str:
    """Deterministic idempotency key for one (trade, horizon, source, quote)."""
    return _sha256("|".join([
        str(trade_id), horizon, source,
        str(quote_ts_ms or ""), liquidity_status, derivation_version,
    ]))


def build_markout_measurements(
    trade: dict,
    markouts: dict,
    entry_price_no: Decimal,
    entry_ms: int | None,
    derivation_version: str = DERIVATION_VERSION,
) -> list[dict]:
    """Build one derived measurement record per (trade, horizon).

    Records are pure derivations: they carry the exact source quote fields (and
    their content hash) so the markout value can be reconstructed, plus the
    provenance flags that keep them non-authoritative and out of the signal path.
    """
    tid = str(trade.get("trade_id") or "")
    records: list[dict] = []
    for horizon, m in sorted(markouts.items()):
        source = m.get("quote_source") or "none"
        ts = m.get("quote_ts_ms")
        status = m.get("status")
        no_exit = bool(m.get("no_exit_liquidity"))
        if status == "unavailable_no_quote":
            liquidity_status = "NO_QUOTE"
        elif no_exit:
            liquidity_status = "NO_EXIT_LIQUIDITY"
        else:
            liquidity_status = "LIQUID"

        # Source content hash over ALL raw quote inputs (never the derived markout
        # or the reconstructed NO fields), so the input snapshot is verifiable.
        content_hash = _quote_content_hash(
            source, ts, m.get("yes_bid"), m.get("yes_ask"), m.get("yes_ask_size"),
        )

        exit_price = m.get("no_bid") if liquidity_status == "LIQUID" else None
        records.append({
            "measurement_id": markout_measurement_id(tid, horizon, source, ts, liquidity_status, derivation_version),
            "record_type": "markout_measurement",
            "provenance": {
                "derived": True,
                "read_only_inputs": True,
                "non_authority": True,
                "no_signal_feedback": True,
            },
            "trade_id": tid,
            "client_order_id": trade.get("client_order_id"),
            "order_id": trade.get("order_id"),
            "ticker": trade.get("ticker"),
            "economic_side": trade.get("economic_side") or trade.get("side"),
            "entry_ts_ms": entry_ms,
            "entry_ts_iso": datetime.fromtimestamp(entry_ms / 1000, UTC).isoformat() if entry_ms else None,
            "entry_price": str(entry_price_no),
            "horizon": horizon,
            "quote": {
                "source": source,
                "ts_ms": ts,
                "ts_iso": datetime.fromtimestamp(ts / 1000, UTC).isoformat() if ts else None,
                "lag_seconds": m.get("elapsed_seconds"),
                "yes_bid": m.get("yes_bid"),
                "yes_ask": m.get("yes_ask"),
                "yes_mid": m.get("yes_mid"),
                "yes_ask_size": m.get("yes_ask_size"),
                "no_bid": m.get("no_bid"),
                "no_ask": m.get("no_ask"),
                "no_mid": m.get("no_mid"),
                "content_sha256": content_hash,
            },
            "markout": {
                "mid": m.get("markout_mid"),
                "exit": m.get("markout_exit"),
                "exit_price": exit_price,
                "liquidity_status": liquidity_status,
            },
            "derivation": {
                "version": derivation_version,
                "sha256": _sha256(DERIVATION_DOC),
            },
            "computed_at": datetime.now(UTC).isoformat(),
        })
    return records


def append_markout_measurements(path: str | Path, records: list[dict]) -> tuple[int, int]:
    """Append derived measurements idempotently (skip existing measurement_ids).

    Returns (appended, seen). Never rewrites an existing row.
    """
    p = Path(path)
    existing: set[str] = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("measurement_id"):
                existing.add(d["measurement_id"])
    fresh = [r for r in records if r["measurement_id"] not in existing]
    if fresh:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for r in fresh:
                fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")
    return len(fresh), len(records)


# ── Relative value (cross-strike CDF monotonicity) ──────────────────────────

def cross_strike_monotonicity(ladder: list[dict]) -> dict:
    """Cross-strike monotonicity of P(peak > K): mid must be non-increasing in K.

    ``ladder`` is a list of {strike_mw, yes_bid, yes_ask} for ONE operating day.
    Returns the ordered (strike, mid) pairs and any adjacent mid-increasing
    violations (diagnostic only — not a tradeable edge).
    """
    rows = sorted(
        (r for r in ladder if r.get("strike_mw") is not None and r.get("yes_bid") is not None and r.get("yes_ask") is not None),
        key=lambda r: _dec(r["strike_mw"]),
    )
    mids = [(_dec(r["strike_mw"]), (float(_dec(r["yes_bid"])) + float(_dec(r["yes_ask"]))) / 2.0) for r in rows]
    violations = []
    for (k1, m1), (k2, m2) in zip(mids, mids[1:]):
        if m2 > m1 + 1e-9:
            violations.append({"higher_strike": float(k2), "mid_jump": round(m2 - m1, 6)})
    return {
        "n_strikes": len(mids),
        "mid_points": [{"strike_mw": float(k), "mid": round(m, 6)} for k, m in mids],
        "monotone": len(violations) == 0,
        "violations": violations,
    }


# ── Displayed capacity proxy (BBO depth only) ───────────────────────────────

def displayed_depth_proxy(observations: list[dict]) -> dict:
    """Displayed BBO depth for candidate sides (single-level capacity proxy).

    This is NOT real capacity: it ignores order-book depth beyond BBO,
    replenishment, queue evolution, and post-fill adverse selection.
    """
    yes_sizes: list[float] = []
    no_sizes: list[float] = []
    for o in observations:
        if not o.get("is_candidate"):
            continue
        ey = _dec(o.get("edge_yes"))
        en = _dec(o.get("edge_no"))
        if ey is not None and ey > 0:
            s = _dec(o.get("yes_ask_size"))
            if s is not None:
                yes_sizes.append(float(s))
        if en is not None and en > 0:
            s = _dec(o.get("no_ask_size"))
            if s is not None:
                no_sizes.append(float(s))

    def _summ(vals: list[float]) -> dict:
        if not vals:
            return {"n": 0, "min": None, "median": None, "max": None, "total": None}
        s = sorted(vals)
        return {"n": len(s), "min": s[0], "median": s[len(s) // 2], "max": s[-1], "total": sum(s)}

    return {
        "label": "DisplayedCapacityProxy (BBO single-level only; NOT realized capacity)",
        "yes_candidates_depth": _summ(yes_sizes),
        "no_candidates_depth": _summ(no_sizes),
    }


# ── Report assembly ─────────────────────────────────────────────────────────

def build_strategy_sheet(
    observations: list[dict],
    resolutions: dict[str, dict],
    live_trades: list[dict],
    live_settlements: list[dict],
    *,
    family: str = "E3",
    model_version: str | None = None,
    venue: str = "kalshi",
    series: str = "KXTXERCOTPEAKD",
    ws_dir: str | Path | None = None,
    ladder_dir: str | Path | None = None,
    markout_path: str | Path | None = None,
) -> dict:
    """Assemble the full read-only strategy sheet."""
    model_version = model_version or (observations[0].get("model_version") if observations else None)

    # ── Denominator ─────────────────────────────────────────────────────────
    n_obs = len(observations)
    n_candidates = sum(1 for o in observations if o.get("is_candidate"))
    n_resolved = len(resolutions)
    days = sorted({o["operating_day"] for o in observations if o.get("operating_day")})
    strikes = sorted({_dec(o["strike_mw"]) for o in observations if o.get("strike_mw") is not None})

    # Resolved candidate observations -> the only rows that can feed ECR/calibration.
    resolved_candidates: list[dict] = []
    for o in observations:
        oid = o.get("observation_id")
        if oid not in resolutions:
            continue
        if not o.get("is_candidate"):
            continue
        resolved_candidates.append(o)

    # Build predicted/terminal edges for resolved candidates (per chosen side).
    pred: list[float] = []
    term: list[float] = []
    probs: list[float] = []
    outcomes: list[int] = []
    for o in resolved_candidates:
        r = resolutions[o["observation_id"]]
        Y = r.get("physical_outcome_yes")
        if Y is None:
            continue
        p_model = _dec(o.get("p_model"))
        ey = _dec(o.get("edge_yes"))
        en = _dec(o.get("edge_no"))
        yes_ask = _dec(o.get("yes_ask"))
        no_ask = _dec(o.get("no_ask"))
        if p_model is None:
            continue
        # Chosen side = the positive-edge side.
        if ey is not None and ey > 0 and yes_ask is not None:
            pred.append(float(ey))
            term.append(float(terminal_edge(int(Y), yes_ask, E3_FEES)))
            probs.append(float(p_model))
            outcomes.append(int(Y))
        elif en is not None and en > 0 and no_ask is not None:
            pred.append(float(en))
            term.append(float(terminal_edge(1 - int(Y), no_ask, E3_FEES)))
            probs.append(float(1 - p_model))
            outcomes.append(1 - int(Y))

    # ── Predicted edge distribution (all observations, not just resolved) ──
    edge_vals: list[float] = []
    for o in observations:
        for key in ("edge_yes", "edge_no"):
            e = _dec(o.get(key))
            if e is not None:
                edge_vals.append(float(e))

    # ── Calibration (N-gated) ───────────────────────────────────────────────
    if len(probs) > 0:
        calibration = {
            "status": "available",
            "n": len(probs),
            "reliability_bins": reliability_bins(probs, outcomes),
            "brier": brier_score(probs, outcomes),
            "log_loss": log_loss_score(probs, outcomes),
        }
    else:
        calibration = {"status": STATUS_INSUFFICIENT, "n": 0,
                       "reliability_bins": [], "brier": None, "log_loss": None}

    # ── Realized edge + capture (N-gated) ───────────────────────────────────
    if pred:
        realized = {"status": "available", "n": len(pred),
                    "mean_terminal_edge": sum(term) / len(term)}
        capture = {"status": "available",
                   "edge_capture_ratio": edge_capture_ratio(pred, term),
                   "mean_terminal_by_bin": mean_terminal_by_bin(pred, term)}
    else:
        realized = {"status": STATUS_INSUFFICIENT, "n": 0, "mean_terminal_edge": None}
        capture = {"status": STATUS_INSUFFICIENT,
                   "edge_capture_ratio": edge_capture_ratio([], []),
                   "mean_terminal_by_bin": []}

    # ── Real (live-calibration) trades: terminal edge, capture, markouts ────
    real_trades_report: list[dict] = []
    settlements_by_trade = {s.get("trade_id"): s for s in live_settlements}
    persistence = {"path": str(markout_path) if markout_path else None,
                   "appended": 0, "seen": 0, "derivation_version": DERIVATION_VERSION}
    for t in live_trades:
        tid = t.get("trade_id")
        side = t.get("side")  # economic side, canonicalized to NO by the recorder
        p_exec = _dec(t.get("executable_price_no"))
        p_pred = _dec((t.get("screen_edge_decomposition") or {}).get("screen_edge"))
        entry_ms = None
        ts = t.get("venue_ts_unix")
        if ts is not None:
            entry_ms = int(float(ts) * 1000)
        entry_price_no = p_exec

        rt = {
            "trade_id": tid,
            "ticker": t.get("ticker"),
            "operating_day": t.get("operating_day"),
            "strike_mw": t.get("strike_mw"),
            "economic_side": side,
            "entry_price_no": str(entry_price_no) if entry_price_no is not None else None,
            "predicted_edge": str(p_pred) if p_pred is not None else None,
            "n_contracts": (t.get("fill") or {}).get("count"),
            "terminal_pnl": None,
            "terminal_edge_per_contract": None,
            "settlement_result": None,
            "ercot_realized_peak_mw": None,
            "markouts": None,
        }
        st = settlements_by_trade.get(tid)
        if st:
            rt["terminal_pnl"] = st.get("net_pnl_dollars")
            rt["settlement_result"] = st.get("settlement_result")
            rt["ercot_realized_peak_mw"] = st.get("ercot_realized_peak_mw")
            # terminal edge per contract for the economic side
            if entry_price_no is not None:
                # settlement_result YES => NO lost (outcome for NO side = 0)
                y_no = 0 if st.get("settlement_result") == "YES" else (1 if st.get("settlement_result") == "NO" else None)
                if y_no is not None:
                    fee = _dec((st.get("realized_venue_fees_dollars")) or "0")
                    cnt = _dec(rt["n_contracts"]) or D(1)
                    fee_per = (fee / cnt) if cnt != 0 else D(0)
                    rt["terminal_edge_per_contract"] = float(D(y_no) - entry_price_no - fee_per)
        # Markouts from WS ticker + 30s REST ladder, merged (best-effort).
        if entry_ms is not None and entry_price_no is not None and rt["ticker"]:
            try:
                quotes: list[Quote] = []
                if ws_dir is not None:
                    quotes += ws_quotes_for_ticker(ws_dir, rt["ticker"], entry_ms, MARKOUT_HORIZONS[-1] * 1000)
                if ladder_dir is not None:
                    quotes += ladder_quotes_for_ticker(ladder_dir, rt["ticker"], entry_ms, MARKOUT_HORIZONS[-1] * 1000)
                quotes.sort(key=lambda q: q.ts_ms)
                rt["markouts"] = markout_from_quotes(quotes, entry_price_no, entry_ms)
            except Exception as exc:  # noqa: BLE001 — markouts are best-effort
                rt["markouts"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        # Persist derived markout measurements (append-only, idempotent).
        if markout_path is not None and isinstance(rt["markouts"], dict) and "5s" in rt["markouts"]:
            records = build_markout_measurements(t, rt["markouts"], entry_price_no, entry_ms)
            appended, seen = append_markout_measurements(markout_path, records)
            persistence["appended"] += appended
            persistence["seen"] += seen
        real_trades_report.append(rt)

    # ── Relative value (per operating day ladder) ───────────────────────────
    relval: dict = {}
    for day in days:
        ladder = [o for o in observations if o.get("operating_day") == day]
        relval[day] = cross_strike_monotonicity(ladder)

    # ── Concentration ───────────────────────────────────────────────────────
    by_day = defaultdict(int)
    by_strike = defaultdict(int)
    for o in observations:
        by_day[o.get("operating_day")] += 1
        by_strike[o.get("strike_mw")] += 1
    concentration = {
        "observations_by_operating_day": {k: by_day[k] for k in days},
        "observations_by_strike_mw": {str(k): v for k, v in sorted(by_strike.items())},
        "unique_operating_days": len(days),
        "unique_strikes": len(strikes),
    }

    # ── Failure analysis (real trades only; heuristic, descriptive) ─────────
    failure = []
    for rt in real_trades_report:
        pnl = _dec(rt.get("terminal_pnl"))
        failure.append({
            "trade_id": rt["trade_id"],
            "terminal_pnl": rt["terminal_pnl"],
            "classification": ("loss" if pnl is not None and pnl < 0
                               else ("win" if pnl is not None and pnl > 0 else "unsettled")),
            "note": "heuristic autopsy is descriptive only; it does not determine whether alpha exists",
        })

    # ── Overall status / verdict ────────────────────────────────────────────
    # Decision-usable only once calibration and ECR are estimable with N.
    min_n_for_verdict = 30
    if n_resolved == 0:
        verdict = STATUS_INSUFFICIENT
    elif n_resolved < min_n_for_verdict:
        verdict = STATUS_CONTINUE
    else:
        # PROMOTE/KILL is a human call; the sheet only reports CONTINUE once the
        # metrics are estimable — it never auto-promotes or auto-kills.
        verdict = STATUS_CONTINUE

    identity = {
        "family": family,
        "model_version": model_version,
        "venue": venue,
        "series": series,
        "observation_window": {"min_operating_day": days[0] if days else None,
                               "max_operating_day": days[-1] if days else None},
    }

    denominator = {
        "observations": n_obs,
        "eligible_candidates": n_candidates,
        "resolved_observations": n_resolved,
        "resolved_candidates": len(resolved_candidates),
        "unique_operating_days": len(days),
        "unique_strikes": len(strikes),
        "real_trades": len(live_trades),
        "real_settled_trades": sum(1 for t in live_trades if t.get("trade_id") in settlements_by_trade),
    }

    edge_section = {
        "status": "available" if edge_vals else STATUS_INSUFFICIENT,
        "n": len(edge_vals),
        "predicted_edge": {
            "min": min(edge_vals) if edge_vals else None,
            "median": sorted(edge_vals)[len(edge_vals) // 2] if edge_vals else None,
            "max": max(edge_vals) if edge_vals else None,
            "positive_count": sum(1 for e in edge_vals if e > 0) if edge_vals else None,
        },
    }

    confidence = {
        "calibration_decision_usable": len(probs) >= min_n_for_verdict,
        "ecr_decision_usable": len(pred) >= min_n_for_verdict,
        "min_n_for_verdict": min_n_for_verdict,
        "n_resolved": n_resolved,
    }

    return {
        "identity": identity,
        "denominator": denominator,
        "calibration": calibration,
        "edge": edge_section,
        "realized_edge": realized,
        "capture": capture,
        "execution": {
            "fillability": {
                "candidate_yes": sum(1 for o in observations if (o.get("is_candidate") and _dec(o.get("edge_yes")) is not None and _dec(o.get("edge_yes")) > 0)),
                "candidate_no": sum(1 for o in observations if (o.get("is_candidate") and _dec(o.get("edge_no")) is not None and _dec(o.get("edge_no")) > 0)),
            },
            "real_trades": real_trades_report,
        },
        "markouts": {rt["trade_id"]: rt["markouts"] for rt in real_trades_report if rt["markouts"]},
        "adverse_selection": {
            "note": "signed markout against the entry (positive = favorable); kept SEPARATE from terminal edge",
            "real_trades": [{"trade_id": rt["trade_id"], "markouts": rt["markouts"]}
                            for rt in real_trades_report if rt["markouts"]],
        },
        "capacity": displayed_depth_proxy(observations),
        "relative_value": relval,
        "concentration": concentration,
        "failure_analysis": failure,
        "confidence": confidence,
        "markout_measurements": persistence,
        "status": verdict,
        "verdict": verdict,
    }