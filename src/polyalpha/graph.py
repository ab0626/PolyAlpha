"""Reviewed graph and depth-priced partition baskets; quotes never imply atomic fills."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .domain import Book, Market, utc
from .execution import FeeSchedule, Order, walk
from .quality import Filter
from .relative_value import Constraint, violations
from .resolution import ResolutionReview, definition_hash

D = Decimal


@dataclass(frozen=True)
class ReviewedNode:
    market: Market
    review: ResolutionReview


class ConstraintGraph:
    def __init__(self, nodes: list[ReviewedNode], rules: list[Constraint]):
        self.nodes = {node.market.market_id: node for node in nodes}
        if len(self.nodes) != len(nodes):
            raise ValueError("duplicate graph node")
        tokens = [
            token for node in nodes for token in (node.market.yes_token_id, node.market.no_token_id)
        ]
        if len(set(tokens)) != len(tokens):
            raise ValueError("duplicate graph token")
        self.rules = tuple(rules)
        for rule in rules:
            if any(member not in self.nodes for member in rule.markets):
                raise ValueError("constraint references missing node")

    def validate(self, rule: Constraint, at: datetime) -> None:
        utc(at)
        if rule not in self.rules or rule.reviewed_at > at:
            raise ValueError("unavailable constraint review")
        for member in rule.markets:
            node = self.nodes[member]
            if node.market.received_at > at:
                raise ValueError("future market metadata")
            if node.review.reviewed_at > rule.reviewed_at:
                raise ValueError("constraint predates definition review")
            node.review.penalty(node.market, at)

    def inconsistencies(self, forecasts, at: datetime, max_age_seconds=30):
        probabilities = {}
        for member, forecast in forecasts.items():
            if forecast.market_id != member or forecast.timestamp > at:
                raise ValueError("mismatched or future forecast")
            if (at - forecast.timestamp).total_seconds() <= max_age_seconds:
                probabilities[member] = forecast.probability
        available = []
        for rule in self.rules:
            self.validate(rule, at)
            available.append(rule)
        return violations(available, probabilities, at)

    def quote_partition(
        self,
        rule: Constraint,
        books: dict[str, Book],
        shares: Decimal,
        at: datetime,
        quality: Filter | None = None,
        extra_slippage: Decimal = D(".002"),
        max_book_skew_seconds: float = 2,
    ) -> dict:
        """Equal-share YES basket. Hypothetical payout requires reviewed exhaustive partition."""
        self.validate(rule, at)
        if rule.kind != "partition":
            raise ValueError("basket payout requires exhaustive partition")
        if not shares.is_finite() or shares <= 0:
            raise ValueError("invalid basket size")
        if not extra_slippage.is_finite() or extra_slippage < 0 or max_book_skew_seconds < 0:
            raise ValueError("invalid basket controls")
        quality = quality or Filter()
        legs, reasons, times = [], [], []
        for member in rule.markets:
            node = self.nodes[member]
            market = node.market
            book = books.get(market.yes_token_id)
            if book is None:
                reasons.append(f"{member}:missing_book")
                continue
            failures = quality.market_reasons(market, at) + quality.book_reasons(book, at)
            if book.condition_id != market.condition_id or book.token_id != market.yes_token_id:
                failures.append("contract_mismatch")
            if (at - market.received_at).total_seconds() > 300:
                failures.append("stale_metadata")
            if failures:
                reasons.extend(f"{member}:{failure}" for failure in failures)
                continue
            try:
                schedule = FeeSchedule.from_market(market)
                fill = walk(
                    book, Order(f"basket:{member}", book.token_id, "BUY", shares, at), schedule, at
                )
            except ValueError as error:
                reasons.append(f"{member}:{error}")
                continue
            times.append(book.source_at)
            legs.append(
                dict(
                    market_id=member,
                    token_id=book.token_id,
                    vwap=str(fill.vwap),
                    notional=str(fill.notional),
                    fees=str(fill.fees),
                    depth_slippage=str(fill.depth_slippage),
                    resolution_penalty=str(node.review.penalty(market, at) * shares),
                    definition_sha256=definition_hash(market),
                    source_at=book.source_at.isoformat(),
                    received_at=book.received_at.isoformat(),
                )
            )
        if times and (max(times) - min(times)).total_seconds() > max_book_skew_seconds:
            reasons.append("asynchronous_books")
        report = dict(
            status="rejected" if reasons else "research_quote",
            reasons=reasons,
            at=at.isoformat(),
            shares=str(shares),
            legs=legs,
            review=rule.review_reference,
            atomic_execution=False,
            net_surplus=None,
        )
        if not reasons:
            cost = sum((D(leg["notional"]) + D(leg["fees"]) for leg in legs), D(0))
            penalties = sum((D(leg["resolution_penalty"]) for leg in legs), D(0))
            penalties += shares * extra_slippage * len(legs)
            report.update(
                hypothetical_payout=str(shares),
                execution_cost=str(cost),
                risk_buffers=str(penalties),
                net_surplus=str(shares - cost - penalties),
            )
        return report
