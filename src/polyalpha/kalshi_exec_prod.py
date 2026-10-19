"""Kalshi Trade API — PRODUCTION execution adapter (LIVE_CALIBRATION_V0 scaffold).

Production-only, fail-closed, hard-capped. This is the additive production path
to the frozen demo adapter (``kalshi_exec.py``): it REUSES the proven order
mechanics — ``NewOrder`` / ``OrderSide`` / error taxonomy / idempotency /
reconciliation semantics — but inverts the environment gate and layers three new
safety controls:

1. environment: production ONLY (demo is rejected at construction and on every
   mutation);
2. operator arming: a separate, file-backed armed state that only an explicit
   operator action may set. Strategy code may OBSERVE it, never RELAX it;
3. frozen caps: module-level constants enforced on every mutation — never
   derived from caller-supplied values.

A mutation is inert until ALL of the following hold:
  - env == "production",
  - calibration protocol version == the frozen runtime version,
  - the operator-armed state file is armed,
  - the file-backed kill switch is clear (persistent, operator-only deactivation),
  - the order notional is within the frozen per-order cap.

This module does NOT validate alpha. LIVE_CALIBRATION PnL != validated alpha.

Credential environment (never hard-coded, never logged):
    KALSHI_API_KEY_ID         production API key id
    KALSHI_PRIVATE_KEY_PATH   production private key (.pem)
    KALSHI_ENV                must be "production"
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
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
from .kalshi_exec import (
    CancelAck,
    ExecutionBlocked,
    KalshiConflict,
    NewOrder,
    OrderAck,
    OrderOutcomeUnknown,
    OrderRejected,
    OrderSide,
    ReconcileResult,
    parse_decimal,
)
from .kill_switch import TradingKillSwitch

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ── Frozen calibration protocol + caps (LIVE_CALIBRATION_V0.1 successor) ───
# v0.1 is a SAFETY successor to v0: it repairs persistent risk/recovery semantics
# (durable journal, singleton lease, file-backed daily-loss/bankroll) and truthful
# fee/signal provenance. It does NOT change E3 probabilities, the 5c screen, or
# the sizing caps. The historical v0 anchor is preserved, not rewritten.
# These are the single source of truth; the OMS imports them. They must never be
# derived from caller input, and changing any of them is a re-freeze event.
CALIBRATION_PROTOCOL_VERSION = "live-calibration-v0.1"

# Contracts == dollars of notional (1 contract pays $1). Capping notional at 2
# contracts bounds worst-case loss strictly below $2 because price in (0,1).
MAX_NOTIONAL_PER_ORDER = Decimal("2.00")
MIN_SCREEN_EDGE = Decimal("0.05")  # per contract (operational screen, not a confidence bound)
DAILY_LOSS_STOP = Decimal("10.00")
BANKROLL_CAP = Decimal("50.00")

DEFAULT_KILL_SWITCH_PATH = _REPO_ROOT / "data" / "energy" / "kalshi" / "exec_kill_switch_prod.json"
DEFAULT_ARMED_PATH = _REPO_ROOT / "data" / "energy" / "kalshi" / "operator_armed_prod.json"

_fmt = lambda d: format(d, "f")  # noqa: E731
Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]


# ── Operator-armed state (separate from the kill switch) ───────────────────

@dataclass(frozen=True)
class ArmedState:
    armed: bool
    armed_at: str | None = None
    armed_by: str | None = None

    def as_dict(self) -> dict:
        return {"armed": self.armed, "armed_at": self.armed_at, "armed_by": self.armed_by}


def _write_armed(path: Path, armed: bool, actor: str) -> ArmedState:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "armed": armed,
        "armed_at": datetime.now(UTC).isoformat(),
        "armed_by": actor,
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return ArmedState(**data)


def arm_operator(path: str | Path, actor: str) -> ArmedState:
    """Explicit operator arming. Called ONLY by the operator CLI, never strategy."""
    return _write_armed(Path(path), True, actor)


def disarm_operator(path: str | Path, actor: str) -> ArmedState:
    """Explicit operator disarm. Called ONLY by the operator CLI, never strategy."""
    return _write_armed(Path(path), False, actor)


class OperatorArmed:
    """Read-only view of the operator-armed state file.

    ``KalshiExecProdClient`` holds this read-only handle. The write functions
    (``arm_operator`` / ``disarm_operator``) live at module scope and are only
    reachable from the operator CLI, so strategy code cannot relax this state.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def state(self) -> ArmedState:
        if not self.path.exists():
            return ArmedState(armed=False)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Unreadable armed-state file fails closed (not armed).
            return ArmedState(armed=False)
        return ArmedState(
            armed=bool(data.get("armed")),
            armed_at=data.get("armed_at"),
            armed_by=data.get("armed_by"),
        )

    def is_armed(self) -> bool:
        return self.state().armed


# ── Production execution client ────────────────────────────────────────────

class KalshiExecProdClient:
    """Authenticated Kalshi PRODUCTION execution client (fail-closed, capped).

    Strategy code uses this client to observe safety state and submit orders; it
    can never relax safety state (there is no ``enable`` here). Clearing the
    kill switch or toggling operator arming is done by the operator CLI only.
    """

    def __init__(
        self,
        key_id: str | None = None,
        private_key_path: str | Path | None = None,
        env: str | None = None,
        kill_switch_path: str | Path | None = None,
        armed_path: str | Path | None = None,
        expected_version: str | None = None,
        timeout: float = 30,
        transport: Transport | None = None,
    ) -> None:
        self.key_id = key_id or os.environ.get(ENV_KEY_ID, "")
        key_path = private_key_path or os.environ.get(ENV_PRIVATE_KEY_PATH, "")
        env = env or os.environ.get(ENV_ENV, "production")

        # Gate 1 — environment: production ONLY.
        if env != "production":
            raise ValueError(
                f"KalshiExecProdClient is production-only (got KALSHI_ENV={env!r}); "
                "demo is rejected"
            )
        self.env = env
        self.base_url = BASE_URLS["production"]
        if not self.key_id or not key_path:
            raise ValueError(f"{ENV_KEY_ID} and {ENV_PRIVATE_KEY_PATH} must both be set")
        self._private_key = load_private_key(key_path)
        self.timeout = timeout
        self._transport = transport or self._default_transport

        # Gate 2 — kill switch. Fail-closed: arm on first use if no state exists.
        self._kill = TradingKillSwitch(
            Path(kill_switch_path) if kill_switch_path else DEFAULT_KILL_SWITCH_PATH
        )
        if not self._kill.path.exists():
            self._kill.activate("MANUAL_OPERATOR", actor="init-fail-closed")

        # Gate 3 — operator arming (read-only handle; default NOT armed).
        self._armed = OperatorArmed(
            Path(armed_path) if armed_path else DEFAULT_ARMED_PATH
        )

        # Gate 4 — version. Fail-closed: unverified/mismatched version blocks.
        self._version_ok = expected_version == CALIBRATION_PROTOCOL_VERSION

    # ── Safety observation (read-only) ─────────────────────────────────────

    @property
    def kill_switch_state(self):
        return self._kill.state()

    @property
    def armed_state(self) -> ArmedState:
        return self._armed.state()

    def disable(self, reason: str, actor: str = "operator") -> None:
        """ARM the kill switch (safe direction — permitted from strategy)."""
        self._kill.activate(reason, actor=actor)

    def _guard(self, order: NewOrder | None = None) -> None:
        if self.env != "production":
            raise ExecutionBlocked("execution is production-only; demo rejected")
        if not self._version_ok:
            raise ExecutionBlocked(
                "calibration protocol version unverified or mismatched"
            )
        if not self._armed.is_armed():
            raise ExecutionBlocked("operator arming not set")
        if self._kill.active:
            raise ExecutionBlocked(f"kill switch active: {self._kill.state().reason}")
        if order is not None and order.count > MAX_NOTIONAL_PER_ORDER:
            raise ExecutionBlocked(
                f"per-order notional cap exceeded: {order.count} > {MAX_NOTIONAL_PER_ORDER}"
            )

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
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OrderOutcomeUnknown(f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _decode(raw: bytes) -> dict:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # ── Mutations (each passes _guard) ─────────────────────────────────────

    def create_order(self, order: NewOrder) -> OrderAck:
        self._guard(order)
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
            raise KalshiConflict(
                f"client_order_id {order.client_order_id!r} already exists at venue: {data}"
            )
        if status == 429:
            raise OrderOutcomeUnknown(
                f"rate limited (429); order status unknown, reconcile: {data}"
            )
        if 400 <= status < 500:
            raise OrderRejected(f"venue rejected order (HTTP {status}): {data}")
        if status >= 500:
            raise OrderOutcomeUnknown(
                f"venue error (HTTP {status}); order status unknown, reconcile: {data}"
            )
        raise OrderOutcomeUnknown(
            f"unexpected status {status}; order status unknown, reconcile: {data}"
        )

    def cancel_order(self, order_id: str, market_ticker: str) -> CancelAck:
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
        self._guard()
        if not (0 < price < 1):
            raise ValueError("price must be strictly between 0 and 1")
        if count <= 0:
            raise ValueError("count must be positive")
        if count > MAX_NOTIONAL_PER_ORDER:
            raise ExecutionBlocked(
                f"per-order notional cap exceeded: {count} > {MAX_NOTIONAL_PER_ORDER}"
            )
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
        status, raw = self._request("GET", "/portfolio/orders", query=params or None)
        data = self._decode(raw)
        if status != 200:
            raise OrderOutcomeUnknown(f"order list failed (HTTP {status}): {data}")
        orders = data.get("orders", data if isinstance(data, list) else [])
        return [o for o in orders if isinstance(o, dict)]

    def _orders_page(self, cursor: str | None = None) -> tuple[list[dict], str | None]:
        params: dict[str, Any] = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        status, raw = self._request("GET", "/portfolio/orders", query=params)
        data = self._decode(raw)
        if status != 200:
            raise OrderOutcomeUnknown(f"order list failed (HTTP {status}): {data}")
        orders = data.get("orders", data if isinstance(data, list) else [])
        orders = [o for o in orders if isinstance(o, dict)]
        next_cursor = data.get("cursor") if isinstance(data, dict) else None
        return orders, (next_cursor or None)

    def reconcile(self, client_order_id: str) -> ReconcileResult:
        """Resolve a client_order_id against the authoritative venue view.

        Paginates the open-order list until the id is found or the cursor is
        exhausted, so a CID outside the first page is never reported absent.
        """
        if not client_order_id:
            raise ValueError("client_order_id is required")
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            orders, next_cursor = self._orders_page(cursor)
            for o in orders:
                if o.get("client_order_id") == client_order_id:
                    return ReconcileResult(
                        client_order_id=client_order_id,
                        found=True,
                        order_id=o.get("order_id"),
                        status=o.get("status") or o.get("order_status"),
                        remaining_count=parse_decimal(o.get("remaining_count")),
                        filled_count=parse_decimal(
                            o.get("filled_count") or o.get("fill_count")
                        ),
                        raw=o,
                    )
            if next_cursor is None or next_cursor in seen:
                break
            seen.add(next_cursor)
            cursor = next_cursor
        return ReconcileResult(client_order_id=client_order_id, found=False)