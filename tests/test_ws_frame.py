"""Tests for the ws-frame-v2 transport frame + acquisition invariants."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from polyalpha.us.market_data_ws import MarketDataStream
from polyalpha.us.trade_stream import UsTradeStream
from polyalpha.ws_frame import (
    WsFrameStore,
    WsSession,
    capture_frame,
    frame_bytes,
    frame_text,
)


def test_session_id_changes_on_reconnect():
    s = WsSession()
    a = s.on_connect()
    b = s.on_connect()
    assert a != b


def test_session_seq_monotonic_and_resets():
    s = WsSession()
    s.on_connect()
    assert [s.take_seq() for _ in range(3)] == [1, 2, 3]
    s.on_connect()
    assert s.take_seq() == 1


def test_frame_preserves_original_wall_and_mono_stamps():
    s = WsSession()
    s.on_connect()
    f = capture_frame("src", s, "x", wall_ns=1000, mono_ns=2000)
    assert f.received_at_ns == 1000
    assert f.received_monotonic_ns == 2000


def test_malformed_json_wire_survives_exactly():
    s = WsSession()
    s.on_connect()
    bad = "{not json"
    f = capture_frame("src", s, bad)
    assert f.wire == bad
    assert frame_text(f) == bad
    with pytest.raises(json.JSONDecodeError):
        json.loads(frame_text(f))


def test_bytes_wire_survives_exactly():
    s = WsSession()
    s.on_connect()
    raw = b"\x00\x01\x02"
    f = capture_frame("src", s, raw)
    assert f.wire_encoding == "base64"
    assert frame_bytes(f) == raw


def test_frame_store_cutover_and_append(tmp_path):
    store = WsFrameStore(tmp_path, "src")
    store.mark_cutover(datetime(2026, 10, 19, tzinfo=UTC))
    s = WsSession()
    s.on_connect()
    wall = int(datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC).timestamp() * 1e9)  # frozen wall_ns
    f = capture_frame("src", s, "hello", wall_ns=wall, mono_ns=0)
    store.append(f)
    assert (tmp_path / "CUTOVER.json").exists()
    rows = [json.loads(line) for line in (tmp_path / "2026-10-19.jsonl").read_text().splitlines()]
    assert rows[0]["wire"] == "hello"
    assert rows[0]["session_id"] == f.session_id


def test_duplicate_frames_survive_but_dedupe_yields_one_fill():
    s = WsSession()
    s.on_connect()
    f1 = capture_frame("src", s, "dup")
    f2 = capture_frame("src", s, "dup")
    assert f1.frame_id != f2.frame_id  # two frames survive acquisition
    assert f1.session_seq == 1 and f2.session_seq == 2

    trade = {"trade": {"marketSlug": "slug", "price": "0.5", "quantity": "10",
                       "maker": {"side": "ORDER_SIDE_BUY"},
                       "taker": {"side": "ORDER_SIDE_SELL"}}}
    stream = UsTradeStream()
    assert stream.ingest(trade, datetime(2026, 10, 19, tzinfo=UTC)) is not None
    assert stream.ingest(trade, datetime(2026, 10, 19, tzinfo=UTC)) is None  # deduped


def test_parser_failure_does_not_mutate_book_state():
    stream = MarketDataStream()
    assert stream.ingest({"foo": "bar"}, datetime(2026, 10, 19, tzinfo=UTC)) is None
    assert stream.books == {}


def test_non_energy_lifecycle_cannot_enter_energy_scoped_lineage():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from energy_ws_collector import _is_energy_scoped  # noqa: E402

    subscribed = {"KXTXERCOTPEAKD-26OCT20-T53500"}
    non_energy = {"msg": {"market_ticker": "BTC-26OCT20-T100"}}
    energy = {"msg": {"market_ticker": "KXTXERCOTPEAKD-26OCT20-T53500"}}
    no_ticker = {"msg": {}}
    assert _is_energy_scoped(non_energy, subscribed) is False
    assert _is_energy_scoped(energy, subscribed) is True
    assert _is_energy_scoped(no_ticker, subscribed) is True