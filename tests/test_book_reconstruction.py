"""Invariant tests for deterministic Kalshi WS book reconstruction.

These protect the microstructure research from invisible book corruption. The
contract is docs/MICROSTRUCTURE_EVIDENCE_V1.md.
"""

from __future__ import annotations

from decimal import Decimal

from polyalpha.energy.book_reconstruction import Reconstructor

D = Decimal


def _snap(session="s1", seq=1, frame="f1", ts=1000, yes=None, no=None):
    return {
        "frame_id": frame,
        "session_id": session,
        "session_seq": seq,
        "received_at_ns": ts * 1_000_000,
        "type": "orderbook_snapshot",
        "message": {
            "msg": {
                "market_ticker": "T",
                "market_id": "m1",
                "yes_dollars_fp": yes,
                "no_dollars_fp": no,
            },
            "sending_ts_ms": ts,
            "seq": seq,
            "sid": 1,
            "type": "orderbook_snapshot",
        },
    }


def _delta(session="s1", seq=2, frame="f2", ts=2000, side="yes", price="0.5", delta="50"):
    return {
        "frame_id": frame,
        "session_id": session,
        "session_seq": seq,
        "received_at_ns": ts * 1_000_000,
        "type": "orderbook_delta",
        "message": {
            "msg": {
                "market_ticker": "T",
                "market_id": "m1",
                "side": side,
                "price_dollars": price,
                "delta_fp": delta,
                "ts_ms": ts,
            },
            "sending_ts_ms": ts,
            "seq": seq,
            "sid": 1,
            "type": "orderbook_delta",
        },
    }


def _reconstruct(rows):
    rec = Reconstructor()
    return [rec.apply(r) for r in rows]


def test_snapshot_seeds_and_derives_mid():
    rows = [
        _snap(seq=1, frame="f1", yes=[["0.40", "100"], ["0.50", "200"]], no=[["0.40", "300"]]),
    ]
    out = _reconstruct(rows)
    assert len(out) == 1
    bs = out[0]
    assert bs.gap_clean is True
    assert bs.best_bid == D("0.50")       # best YES bid
    assert bs.best_ask == D("0.60")       # 1 - best NO bid (0.40)
    assert bs.mid == D("0.55")
    assert bs.spread == D("0.10")
    # bid_depth_k = 200 + 100 = 300; ask_depth_k (NO bids) = 300
    assert bs.bid_depth_k == D("300")
    assert bs.ask_depth_k == D("300")
    assert bs.imbalance == D("0")


def test_delta_applies_and_updates_depth():
    rows = [
        _snap(seq=1, frame="f1", yes=[["0.50", "200"]], no=[["0.40", "300"]]),
        _delta(seq=2, frame="f2", side="yes", price="0.50", delta="100"),
    ]
    out = _reconstruct(rows)
    assert len(out) == 2
    assert out[1].bid_depth_k == D("300")
    assert out[1].gap_clean is True


def test_duplicate_frame_does_not_mutate_twice():
    rows = [
        _snap(seq=1, frame="f1", yes=[["0.50", "200"]], no=[["0.40", "300"]]),
        _delta(seq=2, frame="dup", side="yes", price="0.50", delta="100"),
        _delta(seq=2, frame="dup", side="yes", price="0.50", delta="100"),  # duplicate frame
    ]
    rec = Reconstructor()
    out = [rec.apply(r) for r in rows]
    assert out[2] is None
    assert rec.stats.duplicate_frames == 1
    # depth applied only once: 200 + 100 = 300, not 400
    assert out[1].bid_depth_k == D("300")


def test_sequence_gap_invalidates_state():
    rows = [
        _snap(seq=1, frame="f1", yes=[["0.50", "200"]], no=[["0.40", "300"]]),
        _delta(seq=3, frame="f2", side="yes", price="0.50", delta="100"),  # seq 2 missing
    ]
    out = _reconstruct(rows)
    assert out[1].gap_clean is False


def test_session_change_without_snapshot_invalidates():
    rows = [
        _snap(seq=1, frame="f1", yes=[["0.50", "200"]], no=[["0.40", "300"]]),
        _delta(session="s2", seq=1, frame="f2", side="yes", price="0.50", delta="100"),
        _snap(session="s2", seq=2, frame="f3", yes=[["0.60", "100"]], no=[["0.30", "300"]]),
    ]
    rec = Reconstructor()
    out = [rec.apply(r) for r in rows]
    # cross-session delta applies to a stale book but is marked invalid
    assert out[1].gap_clean is False
    # new session snapshot re-seeds and does NOT leak the old session's book
    assert out[2].gap_clean is True
    assert out[2].best_bid == D("0.60")


def test_negative_depth_is_hard_error():
    rows = [
        _snap(seq=1, frame="f1", yes=[["0.50", "100"]], no=[["0.40", "300"]]),
        _delta(seq=2, frame="f2", side="yes", price="0.50", delta="-200"),
    ]
    rec = Reconstructor()
    out = [rec.apply(r) for r in rows]
    assert rec.stats.negative_depth_errors == 1
    assert out[1].gap_clean is False


def test_recovery_snapshot_restores_gap_clean():
    rows = [
        _snap(seq=1, frame="f1", yes=[["0.50", "200"]], no=[["0.40", "300"]]),
        _delta(seq=3, frame="f2", side="yes", price="0.50", delta="100"),  # gap
        _snap(seq=4, frame="f3", yes=[["0.50", "250"]], no=[["0.40", "300"]]),  # recovery
    ]
    rec = Reconstructor()
    out = [rec.apply(r) for r in rows]
    assert out[1].gap_clean is False
    assert out[2].gap_clean is True
    assert rec.stats.recovery_snapshots == 1


def test_determinism_same_outputs():
    rows = [
        _snap(seq=1, frame="f1", yes=[["0.40", "100"], ["0.50", "200"]], no=[["0.40", "300"]]),
        _delta(seq=2, frame="f2", side="yes", price="0.50", delta="50"),
        _delta(seq=3, frame="f3", side="no", price="0.40", delta="-100"),
    ]
    a = _reconstruct(rows)
    b = _reconstruct(rows)
    assert [x.to_record() for x in a] == [x.to_record() for x in b]
    assert len(a) == 3