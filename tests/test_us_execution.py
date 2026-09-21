"""Tests for the US-venue live-readiness additions: the compliance/eligibility
model and the Retail execution scaffold (auth + fail-closed gating). All
offline. The signing test skips cleanly when PyNaCl is absent.
"""

import base64
import sys

import pytest

sys.path.insert(0, "src")

from polyalpha.us.compliance import can_trade_us_venue, check_us_eligibility
from polyalpha.us.execution import (
    UsExecutionDisabled,
    UsExecutionNotConfigured,
    UsRetailAuth,
    UsRetailExecutionClient,
)

# ── Compliance / eligibility ────────────────────────────────────────────────


def test_us_jurisdiction_eligible_but_unverified_cannot_trade():
    result = check_us_eligibility("US")
    assert result.eligible is True
    assert result.can_trade is False  # operator verification is required


def test_us_jurisdiction_verified_can_trade():
    assert check_us_eligibility("US", verified=True).can_trade is True


def test_unknown_jurisdiction_is_blocked():
    result = check_us_eligibility("XX", verified=True)
    assert result.eligible is False
    assert result.can_trade is False


def test_sanctioned_blocked_even_if_verified():
    assert check_us_eligibility("SANCTIONED", verified=True).can_trade is False


def test_can_trade_us_venue_helper():
    assert can_trade_us_venue("US", verified=True) is True
    assert can_trade_us_venue("US", verified=False) is False


# ── Retail auth / Ed25519 signing ───────────────────────────────────────────


def test_auth_signature_verifies_and_is_stable():
    pytest.importorskip("nacl")
    from nacl.signing import SigningKey

    seed = bytes(range(32))
    auth = UsRetailAuth("access-key-123", base64.b64encode(seed).decode())
    ts = "1700000000000"

    sig = auth.sign("POST", "/v1/orders", ts)
    raw_sig = base64.b64decode(sig)

    # The signature must verify against the documented canonical message.
    SigningKey(seed).verify_key.verify(f"{ts}POST/v1/orders".encode(), raw_sig)
    # Deterministic for the same inputs.
    assert auth.sign("POST", "/v1/orders", ts) == sig


def test_auth_accepts_64_byte_seed_and_public_key():
    pytest.importorskip("nacl")
    from nacl.signing import SigningKey

    seed = bytes(range(32))
    sk = SigningKey(seed)
    # The venue's secret is base64(seed || public_key), 64 bytes.
    secret_64 = base64.b64encode(seed + sk.verify_key.encode()).decode()
    auth = UsRetailAuth("ak", secret_64)
    sig = auth.sign("POST", "/v1/orders", "1700000000000")
    raw_sig = base64.b64decode(sig)
    sk.verify_key.verify(b"1700000000000POST/v1/orders", raw_sig)


def test_auth_headers_contain_required_fields():
    pytest.importorskip("nacl")
    auth = UsRetailAuth("ak", base64.b64encode(b"1" * 32).decode())
    headers = auth.headers("GET", "/v1/orders/open", "1700000000000")
    assert headers["X-PM-Access-Key"] == "ak"
    assert headers["X-PM-Timestamp"] == "1700000000000"
    assert headers["X-PM-Signature"]


# ── Fail-closed gating ──────────────────────────────────────────────────────


def test_from_env_raises_without_credentials(monkeypatch):
    monkeypatch.delenv("POLYMARKET_US_ACCESS_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_US_SECRET", raising=False)
    with pytest.raises(UsExecutionNotConfigured):
        UsRetailExecutionClient.from_env()


def test_gated_client_refuses_to_transmit_without_live_enabled():
    auth = UsRetailAuth("ak", base64.b64encode(b"2" * 32).decode())
    client = UsRetailExecutionClient(auth=auth, live_enabled=False)
    with pytest.raises(UsExecutionDisabled):
        client.submit_order({"side": "BUY", "size": 1, "price": 0.5})


def test_client_requires_auth_when_live_enabled():
    client = UsRetailExecutionClient(auth=None, live_enabled=True)
    with pytest.raises(UsExecutionNotConfigured):
        client.submit_order({})


def test_secret_never_exposed_as_attribute():
    pytest.importorskip("nacl")
    secret = base64.b64encode(b"3" * 32).decode()
    auth = UsRetailAuth("ak", secret)
    assert not hasattr(auth, "secret")
    assert "_secret" in auth.__dict__  # in-memory only
