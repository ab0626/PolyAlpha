"""LivePreTradeGate — pre-trade revalidation of the world at execution time.

Part of the live-ready execution architecture. A signal valid 500ms ago does
NOT imply it remains valid. Before any OrderIntent proceeds toward execution,
the gate re-checks every condition that could have changed. If ANY check
fails, the intent is REJECTED — requirements are never weakened to obtain a
fill.

Also implements TOCTOU protection: the state used to generate a signal
(signal_book_hash) is compared against the state immediately before execution
(execution_book_hash), with configurable drift tolerances.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .order_intent import OrderIntent

D = Decimal


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    details: str = ""

    def summary(self) -> dict:
        return {"name": self.name, "passed": self.passed, "details": self.details}


@dataclass(frozen=True)
class Drift:
    price_drift: Decimal
    spread_drift: Decimal
    depth_drift: Decimal
    probability_edge_drift: Decimal

    def to_dict(self) -> dict:
        return {
            "price_drift": str(self.price_drift),
            "spread_drift": str(self.spread_drift),
            "depth_drift": str(self.depth_drift),
            "probability_edge_drift": str(self.probability_edge_drift),
        }


@dataclass(frozen=True)
class PreTradeResult:
    intent_id: str
    approved: bool
    checks: list[GateCheck]
    drift: Drift | None = None

    def summary(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "approved": self.approved,
            "failed_count": sum(1 for c in self.checks if not c.passed),
            "checks": [c.summary() for c in self.checks],
            "drift": self.drift.to_dict() if self.drift else None,
        }


@dataclass
class LivePreTradeGate:
    """Revalidates the execution-time world before an intent proceeds.

    `world` is a dict supplied by the caller with the execution-time state:
      market: Market-like object with active/closed/accepting_orders/...
      book: reconstructed book dict {bids, asks, timestamp, hash}
      forecast_freshness_seconds: int
      metadata_freshness_seconds: int
      resolution_definition_hash: str
      fee_schedule_version: str
      eligibility_ok: bool
      data_health: str  # "GREEN" | ...
      reconciliation_healthy: bool
      risk_limits_ok: bool
      daily_loss_breaker: bool
      drawdown_breaker: bool
      kill_switch_active: bool
    """

    price_tolerance: Decimal = D("0.05")
    spread_tolerance: Decimal = D("0.02")
    depth_tolerance: Decimal = D("0.50")
    edge_tolerance: Decimal = D("0.01")
    max_book_age_seconds: int = 30
    max_forecast_age_seconds: int = 60
    max_metadata_age_seconds: int = 300

    def evaluate(self, intent: OrderIntent, world: dict, current_book: dict | None = None) -> PreTradeResult:
        checks: list[GateCheck] = []
        # Resolve the effective execution book once: explicit arg wins, else the
        # world's book. Used by both TOCTOU drift and the hash check.
        execution_book = current_book or world.get("book")

        # 1. Market still active.
        market = world.get("market")
        checks.append(GateCheck(
            "market_active",
            bool(market and getattr(market, "active", False)),
            "market no longer active",
        ))
        # 2. Market still accepting orders.
        checks.append(GateCheck(
            "accepting_orders",
            bool(market and getattr(market, "accepting_orders", False)),
            "market no longer accepting orders",
        ))
        # 3. Order book still enabled.
        checks.append(GateCheck(
            "order_book_enabled",
            bool(market and getattr(market, "enable_order_book", False)),
            "order book disabled",
        ))
        # 4. Market has not entered resolution/closure.
        checks.append(GateCheck(
            "not_closed",
            bool(market and not getattr(market, "closed", True)),
            "market closed or resolving",
        ))
        # 5. Geographic/platform eligibility.
        checks.append(GateCheck(
            "eligibility_ok",
            bool(world.get("eligibility_ok")),
            "platform eligibility not established",
        ))

        # 6. Book freshness.
        book_age = world.get("book_age_seconds")
        checks.append(GateCheck(
            "book_fresh",
            book_age is not None and book_age <= self.max_book_age_seconds,
            f"book age {book_age}s exceeds {self.max_book_age_seconds}s",
        ))
        # 7. Forecast freshness.
        forecast_age = world.get("forecast_age_seconds")
        checks.append(GateCheck(
            "forecast_fresh",
            forecast_age is not None and forecast_age <= self.max_forecast_age_seconds,
            f"forecast age {forecast_age}s exceeds {self.max_forecast_age_seconds}s",
        ))
        # 8. Metadata freshness.
        metadata_age = world.get("metadata_age_seconds")
        checks.append(GateCheck(
            "metadata_fresh",
            metadata_age is not None and metadata_age <= self.max_metadata_age_seconds,
            f"metadata age {metadata_age}s exceeds {self.max_metadata_age_seconds}s",
        ))
        # 9. Resolution definition unchanged (fail-closed: empty/mismatch rejects).
        checks.append(GateCheck(
            "resolution_unchanged",
            bool(world.get("resolution_definition_hash"))
            and world.get("resolution_definition_hash") == intent.resolution_definition_hash,
            "resolution definition unknown, empty, or changed",
        ))

        # TOCTOU drift between signal book and execution book.
        drift = self._compute_drift(intent, execution_book, world)
        if drift is not None:
            checks.append(GateCheck(
                "book_hash_matches",
                bool(intent.signal_book_hash)
                and execution_book is not None
                and execution_book.get("hash", "") == intent.signal_book_hash,
                "signal book hash empty or execution book hash differs",
            ))
            checks.append(GateCheck(
                "price_drift_within_tolerance",
                drift.price_drift <= self.price_tolerance,
                f"price drift {drift.price_drift} exceeds {self.price_tolerance}",
            ))
            checks.append(GateCheck(
                "spread_drift_within_tolerance",
                drift.spread_drift <= self.spread_tolerance,
                f"spread drift {drift.spread_drift} exceeds {self.spread_tolerance}",
            ))
            checks.append(GateCheck(
                "depth_drift_within_tolerance",
                drift.depth_drift <= self.depth_tolerance,
                f"depth drift {drift.depth_drift} exceeds {self.depth_tolerance}",
            ))
            checks.append(GateCheck(
                "edge_drift_within_tolerance",
                drift.probability_edge_drift <= self.edge_tolerance,
                f"edge drift {drift.probability_edge_drift} exceeds {self.edge_tolerance}",
            ))
        else:
            checks.append(GateCheck("book_hash_matches", False, "execution book not provided"))

        # 10. Best bid/ask within tolerance (drift above covers this).
        # 11. Expected VWAP remains acceptable (price drift covers).
        # 12. Net edge remains above threshold.
        net_edge_ok = world.get("current_net_edge")
        checks.append(GateCheck(
            "net_edge_above_threshold",
            net_edge_ok is not None and net_edge_ok >= world.get("min_net_edge", D("0")),
            f"net edge {net_edge_ok} missing or below threshold",
        ))
        # 13. Sufficient depth remains (fail-closed: missing -> reject).
        checks.append(GateCheck(
            "sufficient_depth",
            world.get("depth_ok") is True,
            "depth unknown or insufficient",
        ))
        # 14. Fee schedule unchanged (fail-closed: empty/mismatch rejects).
        checks.append(GateCheck(
            "fee_schedule_unchanged",
            bool(world.get("fee_schedule_version"))
            and world.get("fee_schedule_version") == intent.fee_schedule_version,
            "fee schedule unknown, empty, or changed",
        ))
        # 15. Size satisfies all exposure caps (pre-computed by risk engine).
        checks.append(GateCheck(
            "risk_limits_ok",
            bool(world.get("risk_limits_ok", False)),
            "intended size violates exposure cap",
        ))
        # 16. Daily loss breaker.
        checks.append(GateCheck(
            "daily_loss_breaker_clear",
            not bool(world.get("daily_loss_breaker")),
            "daily loss breaker active",
        ))
        # 17. Drawdown breaker.
        checks.append(GateCheck(
            "drawdown_breaker_clear",
            not bool(world.get("drawdown_breaker")),
            "drawdown breaker active",
        ))
        # 18. Global kill switch.
        checks.append(GateCheck(
            "kill_switch_clear",
            not bool(world.get("kill_switch_active")),
            "global kill switch active",
        ))
        # 19. Data-health state GREEN.
        checks.append(GateCheck(
            "data_health_green",
            world.get("data_health") == "GREEN",
            f"data health is {world.get('data_health')}",
        ))
        # 20. Reconciliation state healthy.
        checks.append(GateCheck(
            "reconciliation_healthy",
            bool(world.get("reconciliation_healthy")),
            "account/order reconciliation unhealthy",
        ))
        # 21. No unresolved exchange/account ambiguity (fail-closed).
        checks.append(GateCheck(
            "no_account_ambiguity",
            world.get("no_account_ambiguity") is True,
            "account-state ambiguity unknown or unresolved",
        ))

        approved = all(c.passed for c in checks)
        return PreTradeResult(intent_id=intent.intent_id, approved=approved, checks=checks, drift=drift)

    @staticmethod
    def _best_price(book: dict, side: str) -> Decimal | None:
        levels = book.get("bids" if side == "BUY" else "asks", [])
        if not levels:
            return None
        return D(str(levels[0].get("price"))) if levels else None

    def _compute_drift(self, intent: OrderIntent, current_book: dict | None, world: dict) -> Drift | None:
        """Compare the signal-time state against the execution-time state."""
        if current_book is None:
            return None
        # Price drift: best executable price moved relative to the intent's
        # expected vwap / limit. For a BUY you execute against the best ASK;
        # for a SELL against the best BID. current_bid reads "bids",
        # current_ask reads "asks" (spread = ask - bid > 0).
        current_bid = self._best_price(current_book, "BUY")
        current_ask = self._best_price(current_book, "SELL")
        exec_price = current_ask if intent.side == "BUY" else current_bid
        if exec_price is None:
            price_drift = D("0")
        else:
            price_drift = abs(exec_price - intent.limit_price)

        spread = D("0")
        if current_ask is not None and current_bid is not None:
            spread = current_ask - current_bid
        spread_drift = spread

        # Depth drift: depth at the limit price vs requested shares.
        total_depth = D("0")
        levels = current_book.get("bids" if intent.side == "BUY" else "asks", [])
        for level in levels:
            p = D(str(level.get("price")))
            if intent.side == "BUY" and p <= intent.limit_price:
                total_depth += D(str(level.get("size", 0)))
            elif intent.side == "SELL" and p >= intent.limit_price:
                total_depth += D(str(level.get("size", 0)))
        depth_drift = D(0) if intent.requested_shares == 0 else max(
            D(0), (intent.requested_shares - total_depth) / intent.requested_shares
        )

        # Edge drift: the execution-time net edge differs from the signal-time
        # predicted net edge. This is a genuine TOCTOU measure.
        current_edge = world.get("current_net_edge")
        if current_edge is None:
            probability_edge_drift = D("0")
        else:
            probability_edge_drift = abs(D(str(current_edge)) - intent.predicted_net_edge)

        return Drift(
            price_drift=price_drift,
            spread_drift=spread_drift,
            depth_drift=depth_drift,
            probability_edge_drift=probability_edge_drift,
        )