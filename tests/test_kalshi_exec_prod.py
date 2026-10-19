"""Tests for the Kalshi PRODUCTION execution adapter (LIVE_CALIBRATION_V0).

Prove the production-only gate, fail-closed arming, operator-armed gate, frozen
version gate, per-order cap, and the reused error taxonomy / idempotency /
reconciliation semantics. Fake transport only — no live venue, no funds.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from polyalpha.kalshi_exec import (
    ExecutionBlocked,
    KalshiConflict,
    NewOrder,
    OrderOutcomeUnknown,
    OrderRejected,
    OrderSide,
)
from polyalpha.kalshi_exec_prod import (
    CALIBRATION_PROTOCOL_VERSION,
    KalshiExecProdClient,
    arm_operator,
)
from polyalpha.kill_switch import TradingKillSwitch


def make_key_path(tmp_path):
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    p = tmp_path / "key.pem"
    p.write_bytes(pem)
    return p


def make_client(tmp_path, transport, expected_version=CALIBRATION_PROTOCOL_VERSION, env="production"):
    return KalshiExecProdClient(
        key_id="test-key-id",
        private_key_path=make_key_path(tmp_path),
        env=env,
        kill_switch_path=tmp_path / "kill_switch_prod.json",
        armed_path=tmp_path / "armed_prod.json",
        expected_version=expected_version,
        transport=transport,
    )


def make_order(**kwargs) -> NewOrder:
    base = dict(
        ticker="KX-1",
        side=OrderSide.BID,
        count=Decimal("1"),
        price=Decimal("0.10"),
        client_order_id="cid-1",
    )
    base.update(kwargs)
    return NewOrder(**base)


def canned(status, payload=None):
    calls: list[dict] = []

    def transport(method, url, headers, body):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        raw = json.dumps(payload).encode() if payload is not None else b""
        return status, raw

    return transport, calls


def enable_mutation(tmp_path, armed_path=None):
    """Arm operator + clear the fail-closed kill switch for a client."""
    armed = armed_path or (tmp_path / "armed_prod.json")
    kill = tmp_path / "kill_switch_prod.json"
    arm_operator(armed, actor="test")
    TradingKillSwitch(kill).deactivate(actor="test")


def test_rejects_demo_environment(tmp_path):
    transport, _ = canned(200)
    with pytest.raises(ValueError, match="production-only"):
        make_client(tmp_path, transport, env="demo")


def test_fail_closed_kill_switch_armed_on_first_use(tmp_path):
    transport, _ = canned(200)
    client = make_client(tmp_path, transport)
    assert client.kill_switch_state.active is True
    assert client.kill_switch_state.reason == "MANUAL_OPERATOR"


def test_mutation_blocked_until_armed_and_cleared(tmp_path):
    transport, calls = canned(201, {"order_id": "o1", "client_order_id": "cid-1"})
    client = make_client(tmp_path, transport)
    order = make_order()
    # Version ok but kill switch armed + operator not armed -> blocked before wire.
    with pytest.raises(ExecutionBlocked):
        client.create_order(order)
    assert calls == []

    # Clear kill switch only: still blocked by operator-arming gate.
    TradingKillSwitch(tmp_path / "kill_switch_prod.json").deactivate(actor="test")
    with pytest.raises(ExecutionBlocked):
        client.create_order(order)
    assert calls == []

    # Arm operator too: mutation now reaches the (fake) venue.
    arm_operator(tmp_path / "armed_prod.json", actor="test")
    ack = client.create_order(order)
    assert ack.order_id == "o1"
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"


def test_version_gate_blocks_when_unverified_or_mismatched(tmp_path):
    transport, calls = canned(201, {"order_id": "o1", "client_order_id": "cid-1"})
    # expected_version=None -> unverified -> blocked.
    client = make_client(tmp_path, transport, expected_version=None)
    enable_mutation(tmp_path)
    with pytest.raises(ExecutionBlocked, match="version"):
        client.create_order(make_order())
    assert calls == []

    # expected_version mismatch -> blocked.
    client2 = make_client(tmp_path, transport, expected_version="wrong-version")
    enable_mutation(tmp_path)
    with pytest.raises(ExecutionBlocked, match="version"):
        client2.create_order(make_order())
    assert calls == []


def test_per_order_notional_cap(tmp_path):
    transport, calls = canned(201, {"order_id": "o1", "client_order_id": "cid-1"})
    client = make_client(tmp_path, transport)
    enable_mutation(tmp_path)
    with pytest.raises(ExecutionBlocked, match="cap"):
        client.create_order(make_order(count=Decimal("2.01")))
    assert calls == []


def test_create_order_error_taxonomy(tmp_path):
    cases = [
        (201, {"order_id": "o1"}, "ok"),
        (409, {"detail": "exists"}, KalshiConflict),
        (429, {}, OrderOutcomeUnknown),
        (400, {"detail": "bad"}, OrderRejected),
        (500, {}, OrderOutcomeUnknown),
    ]
    for status, payload, expected in cases:
        transport, calls = canned(status, payload)
        client = make_client(tmp_path, transport)
        enable_mutation(tmp_path)
        if expected == "ok":
            ack = client.create_order(make_order(client_order_id=f"cid-{status}"))
            assert ack.order_id == "o1"
        else:
            with pytest.raises(expected):
                client.create_order(make_order(client_order_id=f"cid-{status}"))


def test_cancel_order(tmp_path):
    transport, calls = canned(200, {"order_id": "o1", "reduced_by": "1"})
    client = make_client(tmp_path, transport)
    enable_mutation(tmp_path)
    ack = client.cancel_order("o1", market_ticker="KX-1")
    assert ack.order_id == "o1"
    assert calls[0]["method"] == "DELETE"


def test_reconcile_paginates_and_finds(tmp_path):
    def transport(method, url, headers, body):
        page1 = {"orders": [{"order_id": "x", "client_order_id": "other"}], "cursor": "c2"}
        page2 = {
            "orders": [{"order_id": "o9", "client_order_id": "cid-1", "status": "resting"}],
            "cursor": None,
        }
        body = page1 if "cursor=c2" not in url else page2
        return 200, json.dumps(body).encode()

    client = make_client(tmp_path, transport)
    enable_mutation(tmp_path)
    rec = client.reconcile("cid-1")
    assert rec.found is True
    assert rec.order_id == "o9"
    assert rec.status == "resting"


def test_reconcile_absent(tmp_path):
    transport, _ = canned(200, {"orders": [], "cursor": None})
    client = make_client(tmp_path, transport)
    enable_mutation(tmp_path)
    rec = client.reconcile("cid-missing")
    assert rec.found is False