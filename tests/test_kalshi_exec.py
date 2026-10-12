"""Tests for the Kalshi execution layer (demo-only, kill-switched).

These prove the ORDER MECHANICS: request construction, fixed-point serialization,
error taxonomy (rejected vs unknown), idempotency, reconciliation, and the
safety gates. They use an injected fake transport — no live venue, no funds.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from polyalpha.kalshi_exec import (
    CancelAck,
    ExecutionBlocked,
    KalshiConflict,
    KalshiExecClient,
    NewOrder,
    OrderAck,
    OrderOutcomeUnknown,
    OrderRejected,
    OrderSide,
    new_client_order_id,
)


@pytest.fixture(scope="module")
def key_id() -> str:
    return "test-key-id"


@pytest.fixture(scope="module")
def key_path(tmp_path_factory):
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    p = tmp_path_factory.mktemp("keys") / "key.pem"
    p.write_bytes(pem)
    return p


def make_client(key_id, key_path, tmp_path, transport, env="demo"):
    return KalshiExecClient(
        key_id=key_id,
        private_key_path=key_path,
        env=env,
        kill_switch_path=tmp_path / "kill_switch.json",
        transport=transport,
    )


def canned(status, payload=None):
    """A fake transport that records its single call and returns ``status``."""
    calls: list[dict] = []

    def transport(method, url, headers, body):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        raw = json.dumps(payload).encode() if payload is not None else b""
        return status, raw

    return transport, calls


def make_order(**kwargs) -> NewOrder:
    base = dict(
        ticker="KX-1",
        side=OrderSide.BID,
        count=Decimal("1"),
        price=Decimal("0.01"),
        client_order_id="cid-1",
    )
    base.update(kwargs)
    return NewOrder(**base)


# ── Safety gates ─────────────────────────────────────────────────────────


def test_production_env_hard_blocked(key_id, key_path, tmp_path):
    transport, _ = canned(201, {"order_id": "x"})
    with pytest.raises(ValueError, match="demo-only"):
        make_client(key_id, key_path, tmp_path, transport, env="production")


def test_kill_switch_armed_on_first_use(key_id, key_path, tmp_path):
    transport, _ = canned(201, {"order_id": "x"})
    client = make_client(key_id, key_path, tmp_path, transport)
    assert client.kill_switch_state.active is True


def test_construction_never_clears_persisted_arm(key_id, key_path, tmp_path):
    transport, _ = canned(201, {"order_id": "x"})
    client_a = make_client(key_id, key_path, tmp_path, transport)
    client_a.disable("DAILY_LOSS_BREACH")
    assert client_a.kill_switch_state.active is True

    # A fresh client over the same kill-switch file must NOT auto-clear it.
    client_b = make_client(key_id, key_path, tmp_path, transport)
    assert client_b.kill_switch_state.active is True
    assert client_b.kill_switch_state.reason == "DAILY_LOSS_BREACH"


def test_mutation_blocked_until_enabled(key_id, key_path, tmp_path):
    transport, calls = canned(201, {"order_id": "o1", "client_order_id": "c1"})
    client = make_client(key_id, key_path, tmp_path, transport)

    with pytest.raises(ExecutionBlocked):
        client.create_order(make_order())
    assert calls == []  # nothing reached the wire

    client.enable()
    ack = client.create_order(make_order())
    assert ack.order_id == "o1"
    assert calls  # now it went through


# ── Order validation ─────────────────────────────────────────────────────


def test_client_order_id_required():
    with pytest.raises(TypeError):
        NewOrder(
            ticker="KX-1", side=OrderSide.BID, count=Decimal("1"), price=Decimal("0.01")
        )
    with pytest.raises(ValueError, match="client_order_id"):
        make_order(client_order_id="  ")


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"ticker": "  "}, "ticker"),
        ({"count": Decimal("0")}, "count"),
        ({"count": Decimal("-1")}, "count"),
        ({"price": Decimal("0")}, "price"),
        ({"price": Decimal("1")}, "price"),
        ({"price": Decimal("1.1")}, "price"),
        ({"client_order_id": "  "}, "client_order_id"),
    ],
)
def test_new_order_validation_rejects_bad_input(kwargs, match):
    with pytest.raises(ValueError, match=match):
        make_order(**kwargs)


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"count": Decimal("0.001")}, "count"),
        ({"count": Decimal("0.005")}, "count"),
        ({"price": Decimal("0.1234567")}, "price"),
        ({"price": Decimal("0.0000001")}, "price"),
    ],
)
def test_new_order_rejects_invalid_granularity(kwargs, match):
    with pytest.raises(ValueError, match=match):
        make_order(**kwargs)


def test_new_order_payload_serialization():
    order = NewOrder(
        ticker="KXWTI-26OCT1314-T90.49",
        side=OrderSide.ASK,
        count=Decimal("10.00"),
        price=Decimal("0.5600"),
        client_order_id="abc-123",
    )
    payload = order.to_payload()
    assert payload == {
        "ticker": "KXWTI-26OCT1314-T90.49",
        "client_order_id": "abc-123",
        "side": "ask",
        "count": "10.00",
        "price": "0.5600",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "reduce_only": False,
    }


def test_decimal_serialization_never_scientific():
    order = make_order(count=Decimal("1E+0"), price=Decimal("1E-2"))
    payload = order.to_payload()
    assert payload["count"] == "1"
    assert payload["price"] == "0.01"


# ── Request construction (mechanics) ─────────────────────────────────────


def test_create_order_request_shape(key_id, key_path, tmp_path):
    transport, calls = canned(201, {"order_id": "o1", "client_order_id": "c1"})
    client = make_client(key_id, key_path, tmp_path, transport)
    client.enable()

    client.create_order(
        NewOrder(
            ticker="KXWTI-26OCT1314-T90.49",
            side=OrderSide.BID,
            count=Decimal("1"),
            price=Decimal("0.01"),
            client_order_id="c1",
        )
    )
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/trade-api/v2/portfolio/events/orders")
    assert call["headers"]["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert call["headers"]["KALSHI-ACCESS-SIGNATURE"]
    assert call["headers"]["KALSHI-ACCESS-TIMESTAMP"]
    assert call["headers"]["Content-Type"] == "application/json"
    assert json.loads(call["body"])["side"] == "bid"


# ── Error taxonomy ───────────────────────────────────────────────────────


def test_create_order_rejected_400(key_id, key_path, tmp_path):
    transport, _ = canned(400, {"error": {"message": "bad price"}})
    client = make_client(key_id, key_path, tmp_path, transport)
    client.enable()
    with pytest.raises(OrderRejected):
        client.create_order(make_order())


def test_create_order_conflict_409(key_id, key_path, tmp_path):
    transport, _ = canned(409, {"error": {"message": "dup"}})
    client = make_client(key_id, key_path, tmp_path, transport)
    client.enable()
    with pytest.raises(KalshiConflict):
        client.create_order(make_order())


@pytest.mark.parametrize("status", [429, 500, 503])
def test_create_order_unknown_status(key_id, key_path, tmp_path, status):
    transport, _ = canned(status, {"error": "ambiguous"})
    client = make_client(key_id, key_path, tmp_path, transport)
    client.enable()
    with pytest.raises(OrderOutcomeUnknown):
        client.create_order(make_order())


def test_network_error_is_unknown(key_id, key_path, tmp_path):
    def boom(method, url, headers, body):
        raise TimeoutError("socket timed out")

    client = make_client(key_id, key_path, tmp_path, boom)
    client.enable()
    with pytest.raises(OrderOutcomeUnknown, match="TimeoutError"):
        client.create_order(make_order())


# ── Cancel / amend ───────────────────────────────────────────────────────


def test_cancel_order(key_id, key_path, tmp_path):
    transport, calls = canned(200, {"order_id": "o1", "reduced_by": "10.00"})
    client = make_client(key_id, key_path, tmp_path, transport)
    client.enable()

    ack = client.cancel_order("o1", market_ticker="KXWTI-26OCT1314-T90.49")
    assert isinstance(ack, CancelAck)
    assert ack.reduced_by == Decimal("10.00")
    call = calls[0]
    assert call["method"] == "DELETE"
    assert "/portfolio/events/orders/o1" in call["url"]
    assert "exchange_index=-1" in call["url"]
    assert "market_ticker=KXWTI-26OCT1314-T90.49" in call["url"]


def test_cancel_order_429_is_unknown(key_id, key_path, tmp_path):
    transport, _ = canned(429, {"error": "slow down"})
    client = make_client(key_id, key_path, tmp_path, transport)
    client.enable()
    with pytest.raises(OrderOutcomeUnknown):
        client.cancel_order("o1", market_ticker="KX-1")


def test_amend_order(key_id, key_path, tmp_path):
    transport, calls = canned(200, {"order_id": "o1", "status": "resting"})
    client = make_client(key_id, key_path, tmp_path, transport)
    client.enable()

    ack = client.amend_order(
        "o1",
        ticker="KX-1",
        side=OrderSide.BID,
        price=Decimal("0.0200"),
        count=Decimal("5"),
        original_client_order_id="orig-cid",
        updated_client_order_id="updated-cid",
    )
    assert isinstance(ack, OrderAck)
    body = json.loads(calls[0]["body"])
    assert body["price"] == "0.0200"
    assert body["count"] == "5"
    assert body["client_order_id"] == "orig-cid"
    assert body["updated_client_order_id"] == "updated-cid"
    assert "/portfolio/events/orders/o1/amend" in calls[0]["url"]


def test_amend_order_429_is_unknown(key_id, key_path, tmp_path):
    transport, _ = canned(429, {"error": "slow down"})
    client = make_client(key_id, key_path, tmp_path, transport)
    client.enable()
    with pytest.raises(OrderOutcomeUnknown):
        client.amend_order(
            "o1",
            ticker="KX-1",
            side=OrderSide.BID,
            price=Decimal("0.02"),
            count=Decimal("5"),
            original_client_order_id="orig",
            updated_client_order_id="updated",
        )


# ── Reconciliation ───────────────────────────────────────────────────────


def test_reconcile_found(key_id, key_path, tmp_path):
    transport, _ = canned(
        200,
        {
            "orders": [
                {
                    "order_id": "o1",
                    "client_order_id": "cid-1",
                    "status": "resting",
                    "remaining_count": "7.00",
                    "filled_count": "3.00",
                }
            ]
        },
    )
    client = make_client(key_id, key_path, tmp_path, transport)
    client.enable()

    result = client.reconcile("cid-1")
    assert result.found is True
    assert result.order_id == "o1"
    assert result.status == "resting"
    assert result.remaining_count == Decimal("7.00")


def test_reconcile_not_found(key_id, key_path, tmp_path):
    transport, _ = canned(200, {"orders": []})
    client = make_client(key_id, key_path, tmp_path, transport)
    client.enable()

    result = client.reconcile("does-not-exist")
    assert result.found is False


def test_idempotency_key_is_uuid4():
    a, b = new_client_order_id(), new_client_order_id()
    assert a != b
    assert len(a) == 36