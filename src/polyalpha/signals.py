"""Signal generation and edge ranking.

Evaluates every candidate market against the ensemble forecast,
computes net edge after all costs, and ranks by attractiveness.

Section 21 of the spec: entry conditions and rejection reasons.
Section 17: net edge decomposition.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .domain import Book, Market
from .execution import FeeSchedule, walk
from .forecasting import Forecast, net_edge

D = Decimal


@dataclass(frozen=True)
class MarketSnapshot:
    """Snapshot of market state for a single market."""

    market_id: str
    condition_id: str
    timestamp: datetime
    question: str
    category: str
    yes_bid: Decimal | None = None
    yes_ask: Decimal | None = None
    no_bid: Decimal | None = None
    no_ask: Decimal | None = None
    yes_spread: Decimal | None = None
    no_spread: Decimal | None = None
    yes_mid: Decimal | None = None
    no_mid: Decimal | None = None
    liquidity: Decimal | None = None
    volume: Decimal | None = None
    active: bool = False
    accepting_orders: bool = False

    @property
    def is_tradeable(self) -> bool:
        return (
            self.active
            and self.accepting_orders
            and self.yes_ask is not None
            and self.no_ask is not None
        )

    @property
    def mid(self) -> Decimal | None:
        if self.yes_mid is not None:
            return self.yes_mid
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        return None

    @property
    def executable_price(self) -> Decimal | None:
        return self.yes_ask


@dataclass(frozen=True)
class Signal:
    """Evaluated trading signal for a single token."""

    market_id: str
    token_id: str
    side: str  # BUY or SELL
    timestamp: datetime
    fair_probability: Decimal
    conservative_probability: Decimal
    execution_price: Decimal
    fee_per_share: Decimal
    slippage_penalty: Decimal
    uncertainty_penalty: Decimal
    resolution_penalty: Decimal
    stale_data_penalty: Decimal
    liquidity_penalty: Decimal
    gross_edge: Decimal
    net_edge: Decimal
    confidence: Decimal
    model_version: str
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    spread: Decimal | None = None
    rejection_reason: str | None = None
    shares: Decimal = D(0)
    budget: Decimal = D(0)

    @property
    def is_positive_edge(self) -> bool:
        return self.net_edge > 0

    @property
    def is_executable(self) -> bool:
        return self.is_positive_edge and self.rejection_reason is None

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "token_id": self.token_id,
            "side": self.side,
            "timestamp": self.timestamp.isoformat(),
            "fair_probability": str(self.fair_probability),
            "conservative_probability": str(self.conservative_probability),
            "execution_price": str(self.execution_price),
            "fee_per_share": str(self.fee_per_share),
            "slippage_penalty": str(self.slippage_penalty),
            "uncertainty_penalty": str(self.uncertainty_penalty),
            "resolution_penalty": str(self.resolution_penalty),
            "stale_data_penalty": str(self.stale_data_penalty),
            "liquidity_penalty": str(self.liquidity_penalty),
            "gross_edge": str(self.gross_edge),
            "net_edge": str(self.net_edge),
            "confidence": str(self.confidence),
            "model_version": self.model_version,
            "best_bid": str(self.best_bid) if self.best_bid is not None else None,
            "best_ask": str(self.best_ask) if self.best_ask is not None else None,
            "spread": str(self.spread) if self.spread is not None else None,
            "rejection_reason": self.rejection_reason,
        }


@dataclass
class SignalEvaluation:
    """Result of evaluating a market for potential entry."""

    signal: Signal | None
    rejections: list[str] = field(default_factory=list)
    candidate_edge: Decimal = D(0)


def evaluate_entry(
    market: Market,
    book: Book,
    yes_book: Book,
    forecast: Forecast,
    fees: FeeSchedule,
    equity: Decimal,
    budget: Decimal,
    at: datetime,
    min_edge: Decimal = D("0.025"),
    spread_multiplier: Decimal = D("1.25"),
    max_prediction_age_seconds: int = 30,
    uncertainty_penalty: Decimal = D("0.025"),
    resolution_penalty: Decimal = D("0.01"),
    staleness_penalty: Decimal = D("0"),
    liquidity_penalty: Decimal = D("0"),
    *,
    yes_token: bool = True,
) -> SignalEvaluation:
    """Evaluate a market for potential entry.

    Returns a SignalEvaluation with either an actionable Signal or rejection reasons.
    """
    rejections = []
    token_id = market.yes_token_id if yes_token else market.no_token_id

    # Section 21: Entry rejection conditions
    if not market.active:
        rejections.append("market_inactive")
    if not market.accepting_orders:
        rejections.append("not_accepting_orders")
    if not market.enable_order_book:
        rejections.append("order_book_disabled")
    if book.spread is None or book.spread <= 0:
        rejections.append("unusable_spread")
    if book.mid is None or book.mid <= 0:
        rejections.append("no_midprice")

    # Check data freshness
    book_age = (at - book.source_at).total_seconds()
    if book_age > max_prediction_age_seconds:
        rejections.append(f"stale_book_{int(book_age)}s")

    # Check forecast freshness
    forecast_age = (at - forecast.timestamp).total_seconds()
    if forecast_age > max_prediction_age_seconds:
        rejections.append(f"stale_forecast_{int(forecast_age)}s")

    # Check minimum depth
    if book.asks and book.asks[0].size < D(10):
        rejections.append("insufficient_top_ask_depth")
    if book.bids and book.bids[0].size < D(10):
        rejections.append("insufficient_top_bid_depth")

    if rejections:
        return SignalEvaluation(
            signal=None,
            rejections=rejections,
        )

    # Calculate fair probability for this side
    fair_p = forecast.probability if yes_token else (D(1) - forecast.probability)
    conservative_p = forecast.conservative(yes_token)

    # Simulate execution to get VWAP
    shares = (budget / (D(1) + fees.rate / D(4))).quantize(D(".01"), rounding="ROUND_DOWN")
    if shares <= 0:
        rejections.append("budget_too_small")
        return SignalEvaluation(signal=None, rejections=rejections)

    try:
        order_token = market.yes_token_id if yes_token else market.no_token_id
        from .execution import Order

        order = Order(f"eval-{order_token[:8]}", order_token, "BUY", shares, at)
        fill = walk(book, order, fees, at)
    except ValueError as e:
        rejections.append(f"execution_failed: {e}")
        return SignalEvaluation(signal=None, rejections=rejections)

    if fill.shares <= 0:
        rejections.append("no_fill_possible")
        return SignalEvaluation(signal=None, rejections=rejections)

    # Compute net edge (Section 17)
    raw_edge = net_edge(forecast, fill, yes_token, resolution_penalty=resolution_penalty)
    raw_edge -= staleness_penalty

    fee_per_share = fill.fees / fill.shares if fill.shares else D(0)
    slippage = fill.depth_slippage / fill.shares if fill.shares else D(0)

    # Apply uncertainty penalty
    edge_after_uncertainty = raw_edge - uncertainty_penalty

    # Apply liquidity penalty
    edge_final = edge_after_uncertainty - liquidity_penalty

    # Section 17: Entry threshold
    required_edge = max(min_edge, spread_multiplier * book.spread)
    if edge_final < required_edge:
        rejections.append(f"insufficient_net_edge: {edge_final:.4f} < {required_edge:.4f}")
        return SignalEvaluation(
            signal=None,
            rejections=rejections,
            candidate_edge=edge_final,
        )

    # Classify confidence
    if edge_final >= D("0.05"):
        confidence = "high"
    elif edge_final >= D("0.03"):
        confidence = "medium"
    else:
        confidence = "low"

    signal = Signal(
        market_id=market.market_id,
        token_id=token_id,
        side="BUY",
        timestamp=at,
        fair_probability=fair_p,
        conservative_probability=conservative_p,
        execution_price=fill.vwap,
        fee_per_share=fee_per_share,
        slippage_penalty=slippage,
        uncertainty_penalty=uncertainty_penalty,
        resolution_penalty=resolution_penalty,
        stale_data_penalty=staleness_penalty,
        liquidity_penalty=liquidity_penalty,
        gross_edge=raw_edge,
        net_edge=edge_final,
        confidence=D("0.95")
        if confidence == "high"
        else D("0.80")
        if confidence == "medium"
        else D("0.60"),
        model_version=forecast.version,
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        spread=book.spread,
        shares=fill.shares,
        budget=budget,
    )

    return SignalEvaluation(signal=signal, candidate_edge=edge_final)


def rank_signals(evaluations: list[SignalEvaluation], top_n: int = 10) -> list[Signal]:
    """Rank actionable signals by net edge, return top N."""
    actionable = [e.signal for e in evaluations if e.signal and e.signal.is_executable]
    actionable.sort(key=lambda s: s.net_edge, reverse=True)
    return actionable[:top_n]


def format_signal_log(signal: Signal) -> dict:
    """Format a signal for structured logging (Section 35)."""
    return {
        "event": "signal_generated",
        "market_id": signal.market_id,
        "token_id": signal.token_id,
        "side": signal.side,
        "fair_probability": str(signal.fair_probability),
        "conservative_probability": str(signal.conservative_probability),
        "execution_price": str(signal.execution_price),
        "fee_per_share": str(signal.fee_per_share),
        "slippage_penalty": str(signal.slippage_penalty),
        "uncertainty_penalty": str(signal.uncertainty_penalty),
        "resolution_penalty": str(signal.resolution_penalty),
        "net_edge": str(signal.net_edge),
        "confidence": signal.confidence,
        "spread": str(signal.spread),
        "model_version": signal.model_version,
    }
