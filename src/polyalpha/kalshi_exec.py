"""Kalshi Trade API — demo venue adapter / execution-mechanics layer.

This is a venue adapter that proves ORDER MECHANICS are correct, NOT a
production execution system. It is intentionally NOT wired into the pre-trade
gate / OrderIntent / approval-ledger / exposure architecture; strategy code must
never call it directly. Production is hard-blocked, so this is safe by
construction today.

Class-B execution safety (see ``docs/ENERGY_RESEARCH_AGENDA_V0_1.md`` change
classification): kill-switch, idempotency, reconciliation, fail-closed gating.
A mutation is inert until **all** of the following hold:

1. the client was constructed with ``KALSHI_ENV=demo`` (production is hard-blocked
   at construction time and never reachable),
2. the file-backed kill switch has been explicitly cleared by an operator
   (deactivation is persistent and never automatic), and
3. the mutation passes client-side validation (fail-closed: bad input raises
   before any network call).

It does **not** validate alpha. Demo prices and order books do not reflect
production markets, so a successful demo order proves mechanics only, never edge.

Prices and counts are ``Decimal`` and are serialized as fixed-point strings
(never floats, never scientific notation). Counts are 0.01-contract granular;
prices carry at most 6 decimal places. Kalshi's V2 single-book order model quotes
the YES leg only: ``side="bid"`` means buy YES, ``side="ask"`` means sell YES
(economically equivalent to buying NO at ``1 - price``).

Endpoints (V2, event-market order family):
    POST   /portfolio/events/orders               create
    POST   /portfolio/events/orders/{id}/amend    amend price / max fillable count
    DELETE /portfolio/events/orders/{id}          cancel (auto-route: exchange_index=-1)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .kalshi_auth import (
    API_ROOT,
    BASE_URLS,
    ENV_ENV,
    ENV_KEY_ID,
    ENV_PRIVATE_KEY_PATH,
    load_private_key,
    sign_request,
)
from .kill_switch import TradingKillSwitch

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KILL_SWITCH_PATH = _REPO_ROOT / "data" / "energy" / "kalshi" / "exec_kill_switch.json"

# Fixed-point string that Kalshi accepts; format(Decimal, "f") preserves the
# value's scale and never emits scientific notation.
_fmt = lambda d: format(d, "f")  # noqa: E731


def new_client_order_id() -> str:
    """A fresh idempotency key. Reuse the SAME value on retry after an unknown
    outcome — never mint a new one for the same logical order."""
    return str(uuid.uuid4())


def parse_decimal(value: Any) -> Decimal | None:
    """Parse a fixed-point string/number to Decimal, or None if unusable."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _is_whole_multiple(value: Decimal, unit: Decimal) -> bool:
    """True iff ``value`` is an exact whole multiple of ``unit``."""
    scaled = value / unit
    return scaled == scaled.to_integral_value()


class OrderSide(str, Enum):
    BID = "bid"  # buy YES
    ASK = "ask"  # sell YES


class TimeInForce(str, Enum):
    GOOD_TILL_CANCELED = "good_till_canceled"
    IMMEDIATE_OR_CANCEL = "immediate_or_cancel"
    FILL_OR_KILL = "fill_or_kill"


# Kalshi self-trade prevention. Kept as a plain string because the venue may add
# values; the client validates only that it is non-empty.
SELF_TRADE_TAKER_AT_CROSS = "taker_at_cross"
SELF_TRADE_MAKER = "maker"


class KalshiExecutionError(Exception):
    """Base for execution-layer failures."""


class ExecutionBlocked(KalshiExecutionError):
    """A local guard refused the mutation (env / kill-switch / validation)."""


class OrderRejected(KalshiExecutionError):
    """The venue definitively rejected the order (it was NOT placed)."""


class OrderOutcomeUnknown(KalshiExecutionError):
    """The submission outcome is ambiguous (network / 429 / 5xx). Reconcile."""


class KalshiConflict(KalshiExecutionError):
    """HTTP 409 — the client_order_id was already submitted (idempotent repeat)."""


@dataclass(frozen=True)
class NewOrder:
    """A single validated order intent.

    ``count`` and ``price`` are ``Decimal``; the venue serializes them as
    fixed-point strings. ``client_order_id`` is the idempotency key and is
    REQUIRED: call ``new_client_order_id()`` before constructing the order and
    keep the value, so an ambiguous submission can be reconciled.
    """

    ticker: str
    side: OrderSide
    count: Decimal
    price: Decimal
    client_order_id: str
    time_in_force: TimeInForce = TimeInForce.GOOD_TILL_CANCELED
    self_trade_prevention_type: str = SELF_TRADE_TAKER_AT_CROSS
    post_only: bool = False
    reduce_only: bool = False

    def __post_init__(self) -> None:
        # Fail-closed validation: nothing invalid may reach the wire.
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError("ticker must be a non-empty string")
        if not isinstance(self.side, OrderSide):
            raise ValueError("side must be OrderSide.BID or OrderSide.ASK")
        if not isinstance(self.count, Decimal) or self.count <= 0:
            raise ValueError("count must be a positive Decimal")
        if not _is_whole_multiple(self.count, Decimal("0.01")):
            raise ValueError("count must be a whole multiple of 0.01 contracts")
        if not isinstance(self.price, Decimal) or not (0 < self.price < 1):
            raise ValueError("price must be a Decimal strictly between 0 and 1")
        if not _is_whole_multiple(self.price, Decimal("0.000001")):
            raise ValueError("price must have at most 6 decimal places")
        if not isinstance(self.time_in_force, TimeInForce):
            raise ValueError("time_in_force must be a TimeInForce")
        if not self.self_trade_prevention_type:
            raise ValueError("self_trade_prevention_type must be non-empty")
        if not isinstance(self.client_order_id, str) or not self.client_order_id.strip():
            raise ValueError("client_order_id must be a non-empty string")

    def to_payload(self) -> dict[str, Any]:
        """The JSON body for POST /portfolio/events/orders."""
        return {
            "ticker": self.ticker,
            "client_order_id": self.client_order_id,
            "side": self.side.value,
            "count": _fmt(self.count),
            "price": _fmt(self.price),
            "time_in_force": self.time_in_force.value,
            "self_trade_prevention_type": self.self_trade_prevention_type,
            "post_only": self.post_only,
            "reduce_only": self.reduce_only,
        }


@dataclass(frozen=True)
class OrderAck:
    """A parsed order acknowledgment (create or amend)."""

    order_id: str | None
    client_order_id: str
    raw: dict

    @property
    def status(self) -> str | None:
        return self.raw.get("status") or self.raw.get("order_status")

    @property
    def remaining_count(self) -> Decimal | None:
        return parse_decimal(self.raw.get("remaining_count"))

    @property
    def filled_count(self) -> Decimal | None:
        return parse_decimal(self.raw.get("filled_count") or self.raw.get("fill_count"))


@dataclass(frozen=True)
class CancelAck:
    """A parsed cancellation acknowledgment (V2 returns a reduced_by shape)."""

    order_id: str | None
    client_order_id: str | None
    reduced_by: Decimal | None
    raw: dict


@dataclass(frozen=True)
class ReconcileResult:
    """Authoritative venue view of one client_order_id."""

    client_order_id: str
    found: bool
    order_id: str | None = None
    status: str | None = None
    remaining_count: Decimal | None = None
    filled_count: Decimal | None = None
    raw: dict | None = None


# Transport: (method, url, headers, body_bytes) -> (status, raw_bytes).
# Injectable so tests can drive it deterministically without a live venue.
Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]


class KalshiExecClient:
    """Authenticated Kalshi execution client (mutations; demo-only).

    Read-only account/market data remains in ``KalshiAuthClient``. This client
    only adds the mutating order endpoints and the safety gates around them.
    """

    def __init__(
        self,
        key_id: str | None = None,
        private_key_path: str | Path | None = None,
        env: str | None = None,
        kill_switch_path: str | Path | None = None,
        timeout: float = 30,
        transport: Transport | None = None,
    ) -> None:
        self.key_id = key_id or os.environ.get(ENV_KEY_ID, "")
        key_path = private_key_path or os.environ.get(ENV_PRIVATE_KEY_PATH, "")
        env = env or os.environ.get(ENV_ENV, "demo")

        # Gate 1 — environment. Execution is demo-only, unconditionally.
        if env != "demo":
            raise ValueError(
                f"Kalshi execution is demo-only (got KALSHI_ENV={env!r}); "
                "production is hard-blocked"
            )
        self.env = env
        self.base_url = BASE_URLS[env]
        if not self.key_id or not key_path:
            raise ValueError(f"{ENV_KEY_ID} and {ENV_PRIVATE_KEY_PATH} must both be set")
        self._private_key = load_private_key(key_path)
        self.timeout = timeout
        self._transport = transport or self._default_transport

        # Gate 2 — kill switch. Fail-closed: arm on first use if no state exists,
        # so a fresh deployment can never submit until an operator clears it.
        self._kill = TradingKillSwitch(
            Path(kill_switch_path) if kill_switch_path else DEFAULT_KILL_SWITCH_PATH
        )
        if not self._kill.path.exists():
            self._kill.activate("MANUAL_OPERATOR", actor="init-fail-closed")

    # ── Safety gates ──────────────────────────────────────────────────────

    def _guard(self) -> None:
        if self.env != "demo":
            raise ExecutionBlocked("execution is demo-only; production hard-blocked")
        if self._kill.active:
            state = self._kill.state()
            raise ExecutionBlocked(f"kill switch active: {state.reason}")

    def enable(self, actor: str = "operator") -> None:
        """Explicit, persistent operator enablement. Never automatic."""
        if self.env != "demo":
            raise ExecutionBlocked("execution is demo-only; production hard-blocked")
        self._kill.deactivate(actor=actor)

    def disable(self, reason: str, actor: str = "operator") -> None:
        self._kill.activate(reason, actor=actor)

    @property
    def kill_switch_state(self):
        return self._kill.state()

    # ── Transport ─────────────────────────────────────────────────────────

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        signature = sign_request(self._private_key, ts, method, API_ROOT + path)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _default_transport(
        self, method: str, url: str, headers: dict, body: bytes | None
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        # URLError / TimeoutError / OSError propagate to _request, which
        # classifies them as OrderOutcomeUnknown regardless of transport.

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> tuple[int, bytes]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = self._auth_headers(method, path)
        payload: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body, sort_keys=True).encode("utf-8")
        try:
            return self._transport(method, url, headers, payload)
        except OrderOutcomeUnknown:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OrderOutcomeUnknown(f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _decode(raw: bytes) -> dict:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # ── Mutations ─────────────────────────────────────────────────────────

    def create_order(self, order: NewOrder) -> OrderAck:
        self._guard()
        status, raw = self._request(
            "POST", "/portfolio/events/orders", body=order.to_payload()
        )
        data = self._decode(raw)

        if status == 201:
            return OrderAck(
                order_id=data.get("order_id"),
                client_order_id=data.get("client_order_id") or order.client_order_id,
                raw=data,
            )
        if status == 409:
            # Idempotency collision: this client_order_id already exists at the
            # venue. Treat as "already submitted", not an error — reconcile.
            raise KalshiConflict(
                f"client_order_id {order.client_order_id!r} already exists at venue: {data}"
            )
        if status == 429:
            raise OrderOutcomeUnknown(
                f"rate limited (429); order status unknown, reconcile: {data}"
            )
        if 400 <= status < 500:
            raise OrderRejected(
                f"venue rejected order (HTTP {status}): {data}"
            )
        if status >= 500:
            raise OrderOutcomeUnknown(
                f"venue error (HTTP {status}); order status unknown, reconcile: {data}"
            )
        raise OrderOutcomeUnknown(
            f"unexpected status {status}; order status unknown, reconcile: {data}"
        )

    def cancel_order(self, order_id: str, market_ticker: str) -> CancelAck:
        """Cancel a resting order (explicit auto-route: exchange_index=-1)."""
        self._guard()
        if not order_id or not market_ticker:
            raise ValueError("order_id and market_ticker are both required")
        status, raw = self._request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            query={"exchange_index": -1, "market_ticker": market_ticker},
        )
        data = self._decode(raw)
        if status in (200, 204):
            return CancelAck(
                order_id=data.get("order_id") or order_id,
                client_order_id=data.get("client_order_id"),
                reduced_by=parse_decimal(data.get("reduced_by")),
                raw=data,
            )
        if status == 429:
            raise OrderOutcomeUnknown(f"cancel outcome unknown (HTTP 429): {data}")
        if 400 <= status < 500:
            raise OrderRejected(f"venue rejected cancel (HTTP {status}): {data}")
        if status >= 500:
            raise OrderOutcomeUnknown(f"cancel outcome unknown (HTTP {status}): {data}")
        raise OrderOutcomeUnknown(f"unexpected status {status}: {data}")

    def amend_order(
        self,
        order_id: str,
        *,
        ticker: str,
        side: OrderSide,
        price: Decimal,
        count: Decimal,
        original_client_order_id: str,
        updated_client_order_id: str,
    ) -> OrderAck:
        """Amend price and/or max fillable count.

        ``count`` is the updated TOTAL fillable count (already-filled plus the
        desired resting remainder). Both client IDs are required up front so an
        ambiguous amend can be reconciled: ``original_client_order_id`` is the
        order being amended; ``updated_client_order_id`` is the new id the venue
        will record (call ``new_client_order_id()`` before transmitting).
        """
        self._guard()
        if not (0 < price < 1):
            raise ValueError("price must be strictly between 0 and 1")
        if count <= 0:
            raise ValueError("count must be positive")
        if not original_client_order_id.strip() or not updated_client_order_id.strip():
            raise ValueError("original and updated client_order_id are required")
        body = {
            "ticker": ticker,
            "side": side.value,
            "price": _fmt(price),
            "count": _fmt(count),
            "client_order_id": original_client_order_id,
            "updated_client_order_id": updated_client_order_id,
        }
        status, raw = self._request(
            "POST", f"/portfolio/events/orders/{order_id}/amend", body=body
        )
        data = self._decode(raw)
        if status == 200:
            return OrderAck(
                order_id=data.get("order_id") or order_id,
                client_order_id=data.get("updated_client_order_id")
                or updated_client_order_id,
                raw=data,
            )
        if status == 429:
            raise OrderOutcomeUnknown(f"amend outcome unknown (HTTP 429): {data}")
        if status == 404:
            raise OrderRejected(f"order not found to amend (HTTP 404): {data}")
        if 400 <= status < 500:
            raise OrderRejected(f"venue rejected amend (HTTP {status}): {data}")
        if status >= 500:
            raise OrderOutcomeUnknown(f"amend outcome unknown (HTTP {status}): {data}")
        raise OrderOutcomeUnknown(f"unexpected status {status}: {data}")

    # ── Read-only (reconciliation support) ────────────────────────────────

    def list_orders(self, **params: Any) -> list[dict]:
        """Authoritative open-order view (GET /portfolio/orders)."""
        status, raw = self._request("GET", "/portfolio/orders", query=params or None)
        data = self._decode(raw)
        if status != 200:
            raise OrderOutcomeUnknown(f"order list failed (HTTP {status}): {data}")
        orders = data.get("orders", data if isinstance(data, list) else [])
        return [o for o in orders if isinstance(o, dict)]

    def reconcile(self, client_order_id: str) -> ReconcileResult:
        """Resolve a client_order_id against the authoritative venue view.

        Used after an ``OrderOutcomeUnknown``: the caller must NOT blindly retry
        with a fresh id; it reconciles and, if absent, retries with the SAME id.
        """
        if not client_order_id:
            raise ValueError("client_order_id is required")
        for o in self.list_orders():
            if o.get("client_order_id") == client_order_id:
                return ReconcileResult(
                    client_order_id=client_order_id,
                    found=True,
                    order_id=o.get("order_id"),
                    status=o.get("status"),
                    remaining_count=parse_decimal(o.get("remaining_count")),
                    filled_count=parse_decimal(o.get("filled_count")),
                    raw=o,
                )
        return ReconcileResult(client_order_id=client_order_id, found=False)


def load_exec_client(
    env_file: str | Path | None = None,
    *,
    kill_switch_path: str | Path | None = None,
    transport: Transport | None = None,
) -> KalshiExecClient:
    """Build a client from environment (optionally loading a .env file).

    Construction never clears persisted kill-switch state: an armed switch stays
    armed across restarts. The only enabling path is an explicit
    ``client.enable(actor=...)`` after the operator has inspected why it is armed.
    """
    if env_file:
        from .kalshi_auth import load_env_file

        load_env_file(env_file)
    return KalshiExecClient(kill_switch_path=kill_switch_path, transport=transport)