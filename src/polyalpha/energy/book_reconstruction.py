"""Deterministic Kalshi WS order-book reconstruction -> canonical BookState.

Implements the frozen reconstruction contract in docs/MICROSTRUCTURE_EVIDENCE_V1.md.

Frozen invariants (the load-bearing rules for microstructure research):
  * A session cannot produce gap_clean=true until an orderbook_snapshot seeded it.
  * Deltas apply strictly in (session_id, session_seq) order.
  * Duplicate frames never mutate book state twice.
  * A session_seq discontinuity, or a session change without a fresh snapshot,
    invalidates state (gap_clean=false) until the next snapshot.
  * Cross-session state never leaks (state is per-ticker and re-seeded on a
    session change).
  * Negative depth after a delta is a HARD reconstruction error (state invalid),
    never silently clamped.
  * Deterministic: replaying the same lineage twice yields identical BookStates.

Semantics (Kalshi WS):
  * orderbook_snapshot.msg.yes_dollars_fp / no_dollars_fp are BID arrays of
    [price_dollars, count_fp], sorted ascending (best bid = last element).
    A missing key means that side's bid book is empty at that snapshot.
  * orderbook_delta.msg = {side: yes|no, price_dollars, delta_fp (signed)}.
    Applied to the corresponding BID book.
  * best_yes_ask = 1 - best_no_bid (the NO bid is the complement of the YES ask).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

D = Decimal

# Frozen constants (docs/MICROSTRUCTURE_EVIDENCE_V1.md).
K_LEVELS = 5
MARKOUT_HORIZONS = (0.1, 1.0, 5.0, 30.0, 300.0, 1800.0)  # seconds
MARKOUT_TOLERANCE = {0.1: 0.05, 1.0: 0.2, 5.0: 0.5, 30.0: 2.0, 300.0: 10.0, 1800.0: 30.0}  # seconds
EVIDENCE_PARTITION_BOUNDARY = "2026-10-21T00:00:00+00:00"


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return D(str(v))
    except (InvalidOperation, ValueError):
        return None


def _parse_iso(v):
    from datetime import datetime
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class BookState:
    """Canonical book state per the frozen contract."""
    venue_ts_ms: int
    receive_ts_ns: int
    ticker: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    bid_depth_k: Decimal
    ask_depth_k: Decimal
    mid: Decimal | None
    microprice: Decimal | None
    imbalance: Decimal | None
    spread: Decimal | None
    session_id: str
    seq: int
    gap_clean: bool
    source: str
    frame_id: str

    def to_record(self) -> dict:
        return {
            "venue_ts_ms": self.venue_ts_ms,
            "receive_ts_ns": self.receive_ts_ns,
            "ticker": self.ticker,
            "best_bid": str(self.best_bid) if self.best_bid is not None else None,
            "best_ask": str(self.best_ask) if self.best_ask is not None else None,
            "bid_depth_k": str(self.bid_depth_k),
            "ask_depth_k": str(self.ask_depth_k),
            "mid": str(self.mid) if self.mid is not None else None,
            "microprice": str(self.microprice) if self.microprice is not None else None,
            "imbalance": str(self.imbalance) if self.imbalance is not None else None,
            "spread": str(self.spread) if self.spread is not None else None,
            "session_id": self.session_id,
            "seq": self.seq,
            "gap_clean": self.gap_clean,
            "source": self.source,
            "frame_id": self.frame_id,
        }


@dataclass
class ReconstructionStats:
    snapshots_seen: int = 0
    sessions_seen: int = 0
    deltas_seen: int = 0
    deltas_applied: int = 0
    duplicate_frames: int = 0
    sequence_gaps: int = 0
    negative_depth_errors: int = 0
    recovery_snapshots: int = 0
    gap_clean_events: int = 0
    total_book_events: int = 0

    def report(self) -> dict:
        total = self.total_book_events
        return {
            "snapshots_seen": self.snapshots_seen,
            "sessions_seen": self.sessions_seen,
            "deltas_seen": self.deltas_seen,
            "deltas_applied": self.deltas_applied,
            "duplicate_frames": self.duplicate_frames,
            "sequence_gaps": self.sequence_gaps,
            "negative_depth_errors": self.negative_depth_errors,
            "recovery_snapshots": self.recovery_snapshots,
            "gap_clean_fraction": round(self.gap_clean_events / total, 6) if total else None,
        }


@dataclass
class _TickerState:
    yes_bids: dict = field(default_factory=dict)   # Decimal price -> Decimal size
    no_bids: dict = field(default_factory=dict)
    gap_clean: bool = False
    has_snapshot: bool = False
    snapshot_epoch: int = -1
    local_dirty: bool = False


def _best_and_depth(bids: dict) -> tuple[Decimal | None, Decimal]:
    if not bids:
        return None, D(0)
    prices = sorted(bids, reverse=True)[:K_LEVELS]
    best = prices[0]
    depth = sum(bids[p] for p in prices)
    return best, depth


def _derive(state: _TickerState, ticker: str, venue_ts_ms: int, receive_ts_ns: int,
            session_id: str, seq: int, source: str, frame_id: str) -> BookState:
    best_yes_bid, bid_depth = _best_and_depth(state.yes_bids)
    best_no_bid, no_depth = _best_and_depth(state.no_bids)
    best_yes_ask = D(1) - best_no_bid if best_no_bid is not None else None
    mid = (best_yes_bid + best_yes_ask) / D(2) if (best_yes_bid is not None and best_yes_ask is not None) else None
    spread = best_yes_ask - best_yes_bid if (best_yes_bid is not None and best_yes_ask is not None) else None
    total_depth = bid_depth + no_depth
    imbalance = (bid_depth - no_depth) / total_depth if total_depth > 0 else D(0)
    microprice = None
    if best_yes_bid is not None and best_yes_ask is not None and total_depth > 0:
        microprice = (best_yes_ask * bid_depth + best_yes_bid * no_depth) / total_depth
    return BookState(
        venue_ts_ms=venue_ts_ms,
        receive_ts_ns=receive_ts_ns,
        ticker=ticker,
        best_bid=best_yes_bid,
        best_ask=best_yes_ask,
        bid_depth_k=bid_depth,
        ask_depth_k=no_depth,
        mid=mid,
        microprice=microprice,
        imbalance=imbalance,
        spread=spread,
        session_id=session_id,
        seq=seq,
        gap_clean=state.gap_clean,
        source=source,
        frame_id=frame_id,
    )


def _apply_snapshot(state: _TickerState, msg: dict) -> None:
    state.yes_bids = {_dec(p): _dec(s) for p, s in msg.get("yes_dollars_fp", []) or []}
    state.no_bids = {_dec(p): _dec(s) for p, s in msg.get("no_dollars_fp", []) or []}
    # Drop any unparsable level defensively (should not happen).
    state.yes_bids = {p: s for p, s in state.yes_bids.items() if p is not None and s is not None}
    state.no_bids = {p: s for p, s in state.no_bids.items() if p is not None and s is not None}
    state.has_snapshot = True


class Reconstructor:
    """Streaming deterministic book reconstructor over the WS lineage."""

    def __init__(self, k: int = K_LEVELS):
        self.stats = ReconstructionStats()
        self._tickers: dict[str, _TickerState] = {}
        self._sessions: set[str] = set()
        self._seen_frames: set[str] = set()
        # Global orderbook-channel gap epoch: bumps on any session boundary or
        # venue sequence discontinuity. Books are gap-clean only if their seed
        # snapshot happened at the current epoch.
        self._epoch = 0
        self._last_session = None
        self._last_seq = None

    def _venue_ts(self, message: dict, msg: dict) -> int | None:
        ts = msg.get("ts_ms")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            return int(ts)
        st = message.get("sending_ts_ms")
        if isinstance(st, (int, float)) and not isinstance(st, bool):
            return int(st)
        return None

    def _advance_epoch(self, session_id: str, seq: int) -> None:
        if self._last_session is None:
            pass
        elif session_id != self._last_session:
            # Session boundary: cross-session isolation -> invalidate all books.
            self._epoch += 1
        elif seq != self._last_seq + 1:
            # Missed orderbook message(s): cannot attribute to one ticker ->
            # invalidate all books (conservative).
            self.stats.sequence_gaps += 1
            self._epoch += 1
        self._last_session = session_id
        self._last_seq = seq

    def apply(self, row: dict) -> BookState | None:
        mtype = row.get("type")
        if mtype not in ("orderbook_snapshot", "orderbook_delta"):
            return None
        message = row.get("message") or {}
        msg = message.get("msg") or {}
        ticker = msg.get("market_ticker")
        frame_id = row.get("frame_id") or ""
        session_id = row.get("session_id") or ""
        venue_seq = message.get("seq")
        receive_ns = row.get("received_at_ns")
        venue_ts = self._venue_ts(message, msg)
        if not ticker or session_id == "" or venue_seq is None or venue_ts is None or receive_ns is None:
            return None

        if session_id not in self._sessions:
            self._sessions.add(session_id)
            self.stats.sessions_seen += 1

        if frame_id and frame_id in self._seen_frames:
            self.stats.duplicate_frames += 1
            return None
        if frame_id:
            self._seen_frames.add(frame_id)

        st = self._tickers.get(ticker)
        if st is None:
            st = _TickerState()
            self._tickers[ticker] = st

        self.stats.total_book_events += 1
        self._advance_epoch(session_id, venue_seq)

        if mtype == "orderbook_snapshot":
            self.stats.snapshots_seen += 1
            was_dirty = st.has_snapshot and not st.gap_clean
            _apply_snapshot(st, msg)
            st.snapshot_epoch = self._epoch
            st.local_dirty = False
            st.gap_clean = True
            if was_dirty:
                self.stats.recovery_snapshots += 1
            self.stats.gap_clean_events += 1
            return _derive(st, ticker, venue_ts, receive_ns, session_id, venue_seq, "snapshot", frame_id)

        # orderbook_delta
        self.stats.deltas_seen += 1
        if not st.has_snapshot:
            return None

        side = msg.get("side")
        price = _dec(msg.get("price_dollars"))
        delta = _dec(msg.get("delta_fp"))
        if side not in ("yes", "no") or price is None or delta is None:
            return None

        book = st.yes_bids if side == "yes" else st.no_bids
        cur = book.get(price, D(0))
        new_size = cur + delta
        if new_size < 0:
            # HARD reconstruction error: removed more than existed.
            self.stats.negative_depth_errors += 1
            st.local_dirty = True
        else:
            if new_size == 0:
                book.pop(price, None)
            else:
                book[price] = new_size
            self.stats.deltas_applied += 1

        st.gap_clean = st.has_snapshot and st.snapshot_epoch == self._epoch and not st.local_dirty
        if st.gap_clean:
            self.stats.gap_clean_events += 1
        return _derive(st, ticker, venue_ts, receive_ns, session_id, venue_seq, "delta", frame_id)


def iter_ws_rows(paths: list[Path]):
    for p in paths:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if '"orderbook_' not in line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


__all__ = [
    "BookState", "ReconstructionStats", "Reconstructor", "iter_ws_rows",
    "K_LEVELS", "MARKOUT_HORIZONS", "MARKOUT_TOLERANCE",
    "EVIDENCE_PARTITION_BOUNDARY",
]