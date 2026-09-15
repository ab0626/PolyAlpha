"""Expected value calculation with comprehensive cost modeling.

Central formula:
    net_edge = fair_probability
               - execution_price
               - estimated_fees_per_share
               - expected_slippage
               - model_uncertainty_buffer
               - liquidity_penalty
               - stale_data_penalty
               - resolution_risk_penalty
"""

from dataclasses import dataclass
from decimal import Decimal

from .execution import Fill
from .forecasting import Forecast
from .uncertainty import UncertaintyEstimate

D = Decimal


@dataclass(frozen=True)
class ExpectedValueResult:
    """Complete expected-value decomposition for a potential trade."""

    fair_probability: Decimal
    execution_price: Decimal
    fee_per_share: Decimal
    slippage_penalty: Decimal
    uncertainty_penalty: Decimal
    resolution_penalty: Decimal
    stale_data_penalty: Decimal
    liquidity_penalty: Decimal
    gross_edge: Decimal
    net_edge: Decimal
    side: str  # "YES" or "NO"

    @property
    def is_positive(self) -> bool:
        return self.net_edge > 0


def calculate_gross_edge(
    fair_probability: Decimal,
    execution_vwap: Decimal,
    side: str = "YES",
) -> Decimal:
    """Gross edge = fair_probability - execution_price (for YES).

    For NO: gross_edge = (1 - fair_probability) - execution_price.
    """
    if side == "YES":
        return fair_probability - execution_vwap
    elif side == "NO":
        return (D(1) - fair_probability) - execution_vwap
    else:
        raise ValueError("side must be YES or NO")


def calculate_fee_per_share(fill: Fill) -> Decimal:
    """Fee per share = fill.fees / fill.shares."""
    if fill.shares <= 0:
        return D(0)
    return fill.fees / fill.shares


def calculate_uncertainty_penalty(
    uncertainty: UncertaintyEstimate,
    confidence: Decimal = D("1.0"),
) -> Decimal:
    """Uncertainty penalty = uncertainty_score * confidence.

    A conservative approach uses full uncertainty.
    """
    return uncertainty.uncertainty_score * confidence


def calculate_liquidity_penalty(
    book_depth: Decimal,
    requested_shares: Decimal,
    base_penalty: Decimal = D("0.005"),
) -> Decimal:
    """Penalty for trading in thin markets.

    penalty = base_penalty * (requested_shares / book_depth) if depth > 0
    """
    if book_depth <= 0:
        return base_penalty * D(2)  # double penalty for zero depth
    ratio = requested_shares / book_depth
    return base_penalty * min(ratio, D(2))


def calculate_stale_data_penalty(
    data_age_seconds: float,
    max_age: float = 30,
    base_penalty: Decimal = D("0.003"),
) -> Decimal:
    """Penalty for stale data. Increases linearly with age."""
    if data_age_seconds <= 0:
        return D(0)
    age_ratio = min(data_age_seconds / max_age, D(2))
    return base_penalty * D(str(age_ratio))


def expected_value(
    forecast: Forecast,
    fill: Fill,
    uncertainty: UncertaintyEstimate,
    side: str = "YES",
    resolution_penalty: Decimal = D("0.01"),
    stale_data_penalty: Decimal = D("0"),
    liquidity_penalty: Decimal = D("0"),
    confidence: Decimal = D("1.0"),
) -> ExpectedValueResult:
    """Calculate full expected-value decomposition.

    Args:
        forecast: Model forecast with probability estimate
        fill: Simulated execution fill with VWAP and fees
        uncertainty: Uncertainty estimate
        side: "YES" or "NO"
        resolution_penalty: Penalty for resolution risk
        stale_data_penalty: Penalty for stale data
        liquidity_penalty: Penalty for low liquidity
        confidence: Confidence level for uncertainty (0-1)

    Returns:
        ExpectedValueResult with full decomposition
    """
    if fill.shares <= 0 or fill.side != "BUY":
        raise ValueError("expected_value requires a buy fill")

    fair_p = forecast.conservative(yes=(side == "YES"))
    vwap = fill.vwap
    fee_per_share = calculate_fee_per_share(fill)
    unc_penalty = calculate_uncertainty_penalty(uncertainty, confidence)

    gross = calculate_gross_edge(fair_p, vwap, side)
    net = (
        gross
        - fee_per_share
        - unc_penalty
        - resolution_penalty
        - stale_data_penalty
        - liquidity_penalty
    )

    return ExpectedValueResult(
        fair_probability=fair_p,
        execution_price=vwap,
        fee_per_share=fee_per_share,
        slippage_penalty=fill.depth_slippage / fill.shares if fill.shares else D(0),
        uncertainty_penalty=unc_penalty,
        resolution_penalty=resolution_penalty,
        stale_data_penalty=stale_data_penalty,
        liquidity_penalty=liquidity_penalty,
        gross_edge=gross,
        net_edge=net,
        side=side,
    )


def kelly_fraction(
    probability: Decimal,
    price: Decimal,
    payoff_ratio: Decimal = D(1),
) -> Decimal:
    """Kelly criterion for binary contracts.

    For a binary contract bought at price c with probability p:
    - Win: payoff_ratio (default 1-c, i.e., $1 payout minus cost)
    - Lose: c (the cost)

    Kelly fraction = (p * payoff_ratio - (1-p) * c) / payoff_ratio

    With payoff_ratio = 1 (binary payout of $1):
    Kelly fraction = (p * (1 - price) - (1-p) * price) / (1 - price)
                   = (p - price) / (1 - price)
    """
    if not (0 < price < 1):
        return D(0)
    if not (0 <= probability <= 1):
        return D(0)

    # Simplified for binary contract paying $1 on win
    win_profit = D(1) - price  # profit if win
    lose_cost = price  # loss if lose

    edge = probability * win_profit - (D(1) - probability) * lose_cost
    if edge <= 0:
        return D(0)

    # Kelly fraction = edge / payoff_ratio
    fraction = edge / win_profit
    return max(D(0), min(fraction, D(1)))


def fractional_kelly(
    probability: Decimal,
    price: Decimal,
    kelly_multiplier: Decimal = D("0.25"),
) -> Decimal:
    """Fractional Kelly: kelly_fraction * multiplier.

    Uses a conservative multiplier (default 0.25 = quarter Kelly).
    """
    full = kelly_fraction(probability, price)
    return full * kelly_multiplier
