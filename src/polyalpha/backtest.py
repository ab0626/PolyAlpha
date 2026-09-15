"""Receipt-time event replay with delayed paper orders and deterministic accounting."""

import json
from datetime import timedelta
from decimal import Decimal

from .anomaly import AnomalyDetector
from .execution import FeeSchedule, Order, Simulator
from .expected_value import calculate_stale_data_penalty
from .forecasting import Baseline, net_edge
from .parsing import parse_book, parse_market
from .portfolio import Portfolio
from .quality import Filter
from .risk import Risk
from .uncertainty import EnsembleUncertainty, HistoricalErrorUncertainty, SpreadBasedUncertainty

D = Decimal


class Engine:
    def __init__(
        self,
        clusters,
        model=None,
        initial_cash=D(10000),
        latency_seconds=1,
        min_edge=D(".025"),
        quality=None,
        exit_policy="hold",
        require_resolution_review=False,
        decision_log=None,
        # Section 22: Exit conditions
        stop_loss_pct=D("0.15"),
        take_profit_pct=D("0.25"),
        trailing_stop_pct=D("0.10"),
        time_exit_hours=24,
        edge_exit_threshold=D("0"),
        stale_metadata_age=300,
        resolution_penalty=D("0.01"),
        risk_limits=None,
    ):
        if latency_seconds < 0 or min_edge < 0:
            raise ValueError("invalid simulation parameters")
        # market ID -> explicit event, semantic cluster, category; never infer independence.
        if exit_policy not in ("hold", "edge", "time", "stop_loss", "trailing", "all"):
            raise ValueError("unsupported exit policy")
        self.exit_policy = exit_policy
        self.require_resolution_review = require_resolution_review
        self.exit_pending = {}
        self.exit_trail_high = {}  # token -> highest observed fair value
        self.exit_entry_prices = {}  # token -> entry execution price
        self.clusters = clusters
        self.model = model or Baseline()
        self.portfolio = Portfolio(initial_cash)
        from .risk import Limits

        self.risk = Risk(initial_cash, risk_limits or Limits())
        self.simulator = Simulator()
        self.latency = timedelta(seconds=latency_seconds)
        self.min_edge = min_edge
        self.quality = quality or Filter()
        self.anomaly_detector = AnomalyDetector()
        self.uncertainty_estimator = EnsembleUncertainty(
            [HistoricalErrorUncertainty(), SpreadBasedUncertainty()]
        )
        self.decision_log = decision_log
        # Section 22: Exit parameters
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.time_exit_hours = time_exit_hours
        self.edge_exit_threshold = edge_exit_threshold
        self.stale_metadata_age = stale_metadata_age
        self.resolution_penalty = resolution_penalty
        self.time_exit_hours = time_exit_hours
        self.edge_exit_threshold = edge_exit_threshold
        self.markets = {}
        self.tokens = {}
        self.books = {}
        self.fees = {}
        self.pending = {}
        self.decisions = []
        self.equity = []
        self.forecasts = []
        self.last_at = None
        self.labels = {}
        self.label_times = {}
        self.resolved = set()
        self.resolved_tokens = set()
        self.counter = 0

    def log(self, at, token, reason, **values):
        record = dict(timestamp=at.isoformat(), token=token, reason=reason, **values)
        self.decisions.append(record)
        if self.decision_log is not None:
            self.decision_log.log(record)

    def on_record(self, record):
        at = record.received_at
        if self.last_at is not None and at < self.last_at:
            raise ValueError("replay must be chronological")
        self.last_at = at
        if record.kind == "stream_gap":
            self.books.clear()
            self.pending.clear()
            self.exit_pending.clear()
            return
        if record.kind == "market":
            market = parse_market(record.payload, at)
            self.markets[market.market_id] = market
            self.tokens[market.yes_token_id] = (market.market_id, True)
            self.tokens[market.no_token_id] = (market.market_id, False)
            for token in (market.yes_token_id, market.no_token_id):
                self.fees.pop(token, None)
                try:
                    self.fees[token] = FeeSchedule.from_market(market)
                except ValueError:
                    pass  # Unknown fees are explicit entry rejection below.
            return
        if record.kind == "settlement":
            p = record.payload
            # Settlements are explicit curated input, not inferred from a last price.
            from datetime import datetime

            known = datetime.fromisoformat(p["known_at"])
            if not p.get("source_url") or p.get("verified") is not True:
                raise ValueError("settlement provenance required")
            if known > at:
                raise ValueError("future settlement")
            token = p["token_id"]
            payout = D(str(p["payout"]))
            if not payout.is_finite() or not 0 <= payout <= 1:
                raise ValueError("invalid settlement payout")
            self.resolved_tokens.add(token)
            if token in self.tokens:
                market_id, yes = self.tokens[token]
                if payout in (0, 1):
                    outcome = int(payout) if yes else 1 - int(payout)
                    if market_id in self.labels and self.labels[market_id] != outcome:
                        raise ValueError("conflicting settlements")
                    self.labels[market_id] = outcome
                    self.label_times.setdefault(market_id, at)
                self.resolved.add(market_id)
            if p["token_id"] in self.portfolio.positions:
                self.portfolio.settle(str(record.id), p["token_id"], D(str(p["payout"])), known, at)
            self.pending.pop(p["token_id"], None)
            self.exit_pending.pop(p["token_id"], None)
            self.snapshot(at)
            return
        if record.kind != "book":
            return
        book = parse_book(record.payload, at, record.entity_id)
        previous = self.books.get(book.token_id)
        if previous is not None and book.source_at < previous.source_at:
            self.log(at, book.token_id, "out_of_order_book")
            return
        self.books[book.token_id] = book
        mark = self.portfolio.mark(self.books, self.fees, at, self.simulator.consumed)
        equity = mark["liquidation_equity"]
        if not mark["unliquidated"]:
            self.risk.observe(equity, at)
        self.equity.append(
            dict(
                timestamp=at.isoformat(),
                equity=str(equity),
                unliquidated=mark["unliquidated"],
            )
        )
        token = book.token_id
        if token not in self.tokens:
            self.log(at, token, "missing_metadata")
            return
        market_id, yes = self.tokens[token]
        market = self.markets[market_id]
        reasons = self.quality.market_reasons(market, at) + self.quality.book_reasons(book, at)
        if market_id in self.resolved or token in self.resolved_tokens:
            reasons.append("resolved_market")
        if (at - market.received_at).total_seconds() > self.stale_metadata_age:
            reasons.append("stale_metadata")
        if book.condition_id != market.condition_id:
            reasons.append("condition_mismatch")
        if token not in self.fees:
            reasons.append("unknown_fees")
        exit_data_valid = not reasons
        assignment = self.clusters.get(market_id)
        if not assignment or not all(assignment.get(k) for k in ("event", "cluster", "category")):
            reasons.append("unassigned_exposure")
        resolution_penalty = self.resolution_penalty
        review_invalid = False
        if self.require_resolution_review:
            from .resolution import ResolutionReview

            try:
                review = ResolutionReview.from_dict((assignment or {})["resolution_review"])
                resolution_penalty = review.penalty(market, at)
            except (KeyError, ValueError) as error:
                review_invalid = True
                reasons.append("resolution_review_invalid: " + str(error))
        if exit_data_valid and self.process_exit(book, market, yes, at, force=review_invalid):
            return
        if mark["unliquidated"]:
            reasons.append("incomplete_liquidation_valuation")
        if self.risk.halted:
            reasons.append("risk_halted")
        if reasons:
            self.pending.pop(token, None)
            self.log(at, token, "rejected", rejections=reasons)
            return
        budget = self.risk.budget(
            self.portfolio,
            equity,
            market_id,
            assignment["event"],
            assignment["cluster"],
            assignment["category"],
        )
        if self.risk.correlation_registry is not None:
            budget = self.risk.correlation_adjusted_budget(
                self.portfolio,
                equity,
                market_id,
                assignment["event"],
                assignment["cluster"],
                assignment["category"],
            )
        if token in self.pending:
            order, forecast = self.pending[token]
            if at < order.submitted_at + self.latency:
                return
            del self.pending[token]
            if (at - forecast.timestamp).total_seconds() > 30:
                self.log(at, token, "expired_forecast")
                return
            try:
                fill = self.simulator.quote(book, order, self.fees[token], at)
                edge = net_edge(forecast, fill, yes, resolution_penalty=resolution_penalty)
                if fill.notional + fill.fees > budget:
                    raise ValueError("arrival_risk_budget_exceeded")
                if edge < max(self.min_edge, D("1.25") * book.spread):
                    raise ValueError("arrival_edge_insufficient")
                self.portfolio.apply(
                    fill,
                    market_id,
                    assignment["event"],
                    assignment["cluster"],
                    assignment["category"],
                )
                self.simulator.commit(fill)
                self.snapshot(at)
                self.log(
                    at,
                    token,
                    "filled",
                    shares=str(fill.shares),
                    vwap=str(fill.vwap),
                    fees=str(fill.fees),
                    net_edge=str(edge),
                )
            except ValueError as error:
                self.log(at, token, "rejected", rejections=[str(error)])
            return
        yes_book = self.books.get(market.yes_token_id)
        if yes_book is None or self.quality.book_reasons(yes_book, at):
            self.log(at, token, "missing_fresh_yes_book")
            return
        # Detect anomalies and compute uncertainty multiplier
        anomalies = self.anomaly_detector.detect(book, {"microprice": None, "midpoint": None})
        uncertainty_multiplier = self.anomaly_detector.uncertainty_multiplier(anomalies)
        try:
            forecast = self.model.predict(market_id, yes_book, at)
            if forecast.market_id != market_id or forecast.timestamp > at:
                raise ValueError("invalid model identity/time")
            # Estimate uncertainty for this forecast
            uncertainty = self.uncertainty_estimator.estimate(
                forecast.probability, {"spread": book.spread}, market_id=market_id
            )
            self.forecasts.append(
                dict(
                    market_id=market_id,
                    cluster=assignment["cluster"],
                    at=at.isoformat(),
                    p=str(forecast.probability),
                    uncertainty=str(uncertainty.uncertainty_score),
                    uncertainty_lower=str(uncertainty.lower_bound),
                    uncertainty_upper=str(uncertainty.upper_bound),
                    anomalies=len(anomalies),
                )
            )
            # Bound cost by a one-dollar payoff plus the maximum fee per share.
            shares = (budget / (1 + self.fees[token].rate / D(4))).quantize(
                D(".01"), rounding="ROUND_DOWN"
            )
            self.counter += 1
            order = Order(f"paper-{self.counter}", token, "BUY", shares, at)
            quote = self.simulator.quote(book, order, self.fees[token], at)
            edge = net_edge(forecast, quote, yes, resolution_penalty=resolution_penalty)
            # Apply staleness penalty based on book age
            book_age_seconds = (at - book.source_at).total_seconds()
            staleness_penalty = calculate_stale_data_penalty(book_age_seconds)
            edge -= staleness_penalty
            # Anomalies increase required edge threshold
            adjusted_min_edge = self.min_edge * uncertainty_multiplier
            if edge < max(adjusted_min_edge, D("1.25") * book.spread):
                raise ValueError("insufficient_net_edge")
            self.pending[token] = (order, forecast)
            self.log(
                at,
                token,
                "queued",
                net_edge=str(edge),
                model=forecast.version,
                fair_probability=str(forecast.probability),
                conservative_probability=str(forecast.conservative(yes)),
                best_bid=str(book.best_bid),
                best_ask=str(book.best_ask),
                vwap=str(quote.vwap),
                fee=str(quote.fees),
                depth_slippage=str(quote.depth_slippage),
                spread=str(book.spread),
                staleness_penalty=str(staleness_penalty),
            )
        except ValueError as error:
            self.log(at, token, "rejected", rejections=[str(error)])

    def process_exit(self, book, market, yes, at, force=False):
        """Evaluate exit conditions (Section 22).

        Supports multiple exit policies:
        - hold: never exit early (settlement only)
        - edge: exit when fair probability <= executable exit threshold
        - time: exit N hours before resolution deadline
        - stop_loss: exit if position drops below stop-loss threshold
        - trailing: trailing stop that follows the highest observed fair value
        - all: check all conditions
        """
        token = book.token_id
        position = self.portfolio.positions.get(token)
        if position is None or not position.shares:
            return False

        # If an exit is already pending, wait for fill
        if token in self.exit_pending:
            order = self.exit_pending[token]
            if at < order.submitted_at + self.latency:
                return True
            del self.exit_pending[token]
            try:
                fill = self.simulator.quote(book, order, self.fees[token], at)
                if not fill.shares:
                    raise ValueError("no exit depth")
                self.portfolio.apply(
                    fill,
                    position.market_id,
                    position.event_id,
                    position.cluster,
                    position.category,
                )
                self.simulator.commit(fill)
                self.snapshot(at)
                self.log(
                    at,
                    token,
                    "exit_filled",
                    shares=str(fill.shares),
                    vwap=str(fill.vwap),
                    fees=str(fill.fees),
                )
            except ValueError as error:
                self.log(at, token, "exit_rejected", rejections=[str(error)])
            return True

        # Record entry price for stop-loss/take-profit
        if token not in self.exit_entry_prices:
            self.exit_entry_prices[token] = position.average_cost

        should_exit = False
        exit_reason = ""

        # ── Risk halt: always exit ──────────────────────────────────────────
        if self.risk.halted or force:
            should_exit = True
            exit_reason = "risk" if self.risk.halted else "forced"

        # ── Section 22: Edge disappearance ──────────────────────────────────
        if not should_exit and self.exit_policy in ("edge", "all"):
            yes_book = self.books.get(market.yes_token_id)
            if yes_book is not None:
                try:
                    forecast = self.model.predict(market.market_id, yes_book, at)
                    fair = forecast.probability if yes else 1 - forecast.probability
                    # Exit if fair value is below the bid minus fees (no edge to hold)
                    exit_price = fair - self.fees[token].fee(D(1), book.best_bid)
                    if exit_price <= self.edge_exit_threshold:
                        should_exit = True
                        exit_reason = "edge_disappeared"
                except ValueError:
                    pass

        # ── Section 22: Stop-loss ───────────────────────────────────────────
        if not should_exit and self.exit_policy in ("stop_loss", "all"):
            entry_price = self.exit_entry_prices.get(token, position.average_cost)
            if entry_price > 0:
                current_price = book.mid or book.best_bid or entry_price
                loss_pct = (entry_price - current_price) / entry_price
                if loss_pct >= self.stop_loss_pct:
                    should_exit = True
                    exit_reason = f"stop_loss_{loss_pct:.1%}"

        # ── Section 22: Take-profit ─────────────────────────────────────────
        if not should_exit and self.exit_policy in ("stop_loss", "all"):
            entry_price = self.exit_entry_prices.get(token, position.average_cost)
            if entry_price > 0:
                current_price = book.mid or book.best_ask or entry_price
                gain_pct = (current_price - entry_price) / entry_price
                if gain_pct >= self.take_profit_pct:
                    should_exit = True
                    exit_reason = f"take_profit_{gain_pct:.1%}"

        # ── Section 22: Trailing stop ───────────────────────────────────────
        if not should_exit and self.exit_policy in ("trailing", "all"):
            fair = None
            yes_book = self.books.get(market.yes_token_id)
            if yes_book is not None:
                try:
                    forecast = self.model.predict(market.market_id, yes_book, at)
                    fair = float(forecast.probability if yes else 1 - forecast.probability)
                except ValueError:
                    pass
            if fair is not None:
                high = self.exit_trail_high.get(token, fair)
                if fair > high:
                    self.exit_trail_high[token] = fair
                    high = fair
                drop_from_high = high - fair
                if drop_from_high >= float(self.trailing_stop_pct) * high:
                    should_exit = True
                    exit_reason = f"trailing_stop_drop={drop_from_high:.3f}"

        # ── Section 22: Time-based exit ─────────────────────────────────────
        if not should_exit and self.exit_policy in ("time", "all"):
            if market.deadline is not None:
                hours_to_resolution = (market.deadline - at).total_seconds() / 3600
                if hours_to_resolution <= self.time_exit_hours:
                    should_exit = True
                    exit_reason = f"time_exit_{hours_to_resolution:.1f}h"

        # ── Section 22: Probability convergence ─────────────────────────────
        if not should_exit and self.exit_policy in ("edge", "all"):
            yes_book = self.books.get(market.yes_token_id)
            if yes_book is not None:
                try:
                    forecast = self.model.predict(market.market_id, yes_book, at)
                    fair = forecast.probability if yes else 1 - forecast.probability
                    # Exit if market price has converged close to our estimate
                    bid = book.best_bid or D(0)
                    if bid > 0 and abs(fair - bid) < D("0.015"):
                        should_exit = True
                        exit_reason = "probability_converged"
                except ValueError:
                    pass

        if should_exit:
            self.counter += 1
            self.exit_pending[token] = Order(
                f"paper-exit-{self.counter}",
                token,
                "SELL",
                position.shares,
                at,
                allow_partial=True,
            )
            self.pending.pop(token, None)
            self.log(
                at,
                token,
                "exit_queued",
                policy=exit_reason,
                entry_price=str(self.exit_entry_prices.get(token, "")),
                current_bid=str(book.best_bid),
            )
            return True
        return False

    def snapshot(self, at):
        mark = self.portfolio.mark(self.books, self.fees, at, self.simulator.consumed)
        if not mark["unliquidated"]:
            self.risk.observe(mark["liquidation_equity"], at)
        self.equity.append(
            dict(
                timestamp=at.isoformat(),
                equity=str(mark["liquidation_equity"]),
                unliquidated=mark["unliquidated"],
            )
        )

    def _category_performance(self):
        """Aggregate fill-level performance by category."""
        from collections import defaultdict

        cat_stats = defaultdict(
            lambda: {
                "fills": 0,
                "total_notional": D(0),
                "total_fees": D(0),
                "total_slippage": D(0),
                "tokens": set(),
            }
        )
        for fill in self.simulator.orders.values():
            token = fill.token_id
            info = self.tokens.get(token)
            if info is None:
                continue
            market_id = info[0]
            market = self.markets.get(market_id)
            if market is None:
                continue
            cat = market.category
            cat_stats[cat]["fills"] += 1
            cat_stats[cat]["total_notional"] += fill.notional
            cat_stats[cat]["total_fees"] += fill.fees
            cat_stats[cat]["total_slippage"] += fill.depth_slippage
            cat_stats[cat]["tokens"].add(token)
        return {
            cat: dict(
                fills=s["fills"],
                total_notional=str(s["total_notional"]),
                total_fees=str(s["total_fees"]),
                total_slippage=str(s["total_slippage"]),
                unique_tokens=len(s["tokens"]),
            )
            for cat, s in cat_stats.items()
        }

    def _risk_adjusted_ratios(self):
        """Compute Sharpe-like and Sortino-like ratios from equity curve.

        Uses daily-equivalent returns computed from consecutive liquidated equity observations.
        Risk-free rate assumed zero (paper trading, no capital cost).
        """

        values = []
        for row in self.equity:
            if not row["unliquidated"]:
                values.append(D(row["equity"]))
        if len(values) < 2:
            return {"sharpe_ratio": None, "sortino_ratio": None, "return_volatility": None}
        returns = [(values[i] - values[i - 1]) / values[i - 1] for i in range(1, len(values))]
        n = len(returns)
        mean_r = sum(returns) / n
        variance = sum((r - mean_r) ** 2 for r in returns) / n
        std = float(variance.sqrt()) if variance > 0 else 0.0
        downside = [r for r in returns if r < 0]
        downside_var = sum(r**2 for r in downside) / n if downside else D(0)
        downside_std = float(downside_var.sqrt()) if downside_var > 0 else 0.0
        sharpe = float(mean_r) / std if std > 0 else None
        sortino = float(mean_r) / downside_std if downside_std > 0 else None
        return {
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "return_volatility": std,
        }

    def run(self, records):
        import hashlib
        from dataclasses import asdict

        digest = hashlib.sha256()
        record_count = 0
        for record in records:
            digest.update(
                json.dumps(asdict(record), sort_keys=True, default=str, allow_nan=False).encode()
            )
            digest.update(b"\n")
            record_count += 1
            self.on_record(record)
        final_mark = (
            self.portfolio.mark(self.books, self.fees, self.last_at, self.simulator.consumed)
            if self.last_at
            else dict(liquidation_equity=self.portfolio.cash, unliquidated={})
        )
        equity = final_mark["liquidation_equity"]
        complete = not final_mark["unliquidated"]
        exit_fees = final_mark.get("estimated_exit_fees", D(0))
        total_costs = self.portfolio.fees + exit_fees
        peak = self.portfolio.initial_cash
        max_dd = D(0)
        dd_values = []
        for row in self.equity:
            if row["unliquidated"]:
                continue
            value = D(row["equity"])
            peak = max(peak, value)
            dd = (peak - value) / peak if peak > 0 else D(0)
            max_dd = max(max_dd, dd)
            dd_values.append(dd)
        scored = {}
        for row in self.forecasts:
            if row["market_id"] in self.labels:
                scored.setdefault(row["market_id"], row)
        from .calibration import metrics

        calibration = (
            metrics(
                [float(r["p"]) for r in scored.values()],
                [self.labels[m] for m in scored],
            )
            if scored
            else None
        )
        if calibration:
            calibration["independent_clusters"] = len({r["cluster"] for r in scored.values()})
        depth_cost = sum((f.depth_slippage for f in self.simulator.orders.values()), D(0))
        risk_ratios = self._risk_adjusted_ratios()

        # Section 23: mark-to-mid equity (use midpoint of best bid/ask for each position)
        mark_to_mid = self.portfolio.cash
        for token, pos in self.portfolio.positions.items():
            book = self.books.get(token)
            if book is not None and book.mid is not None and pos.shares > 0:
                mark_to_mid += pos.shares * book.mid
        # Also include bid-side liquidation value for YES positions
        mark_to_bid = self.portfolio.cash
        for token, pos in self.portfolio.positions.items():
            book = self.books.get(token)
            if book is not None and pos.shares > 0:
                bid = book.best_bid or D(0)
                mark_to_bid += pos.shares * bid

        # Section 28: trade-level metrics from fill journal
        fills_list = list(self.simulator.orders.values())
        winners = []
        losers = []
        for fill in fills_list:
            info = self.tokens.get(fill.token_id)
            if info is None:
                continue
            mid_market_id, yes = info
            book = self.books.get(fill.token_id)
            if book is None or book.mid is None:
                continue
            if fill.side == "BUY":
                pnl_per_share = book.mid - fill.vwap
            else:
                pnl_per_share = fill.vwap - book.mid
            total_pnl = pnl_per_share * fill.shares
            if total_pnl > 0:
                winners.append(float(total_pnl))
            elif total_pnl < 0:
                losers.append(float(abs(total_pnl)))
        gross_wins = sum(winners) if winners else 0.0
        gross_losses = sum(losers) if losers else 0.0
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else None

        # Aggregate uncertainty and anomaly statistics from forecasts
        uncertainty_scores = [float(r.get("uncertainty", 0)) for r in self.forecasts]
        anomaly_counts = [int(r.get("anomalies", 0)) for r in self.forecasts]
        return dict(
            status="paper_simulation_only",
            period_start=self.equity[0]["timestamp"] if self.equity else None,
            input_sha256=digest.hexdigest(),
            input_records=record_count,
            fill_journal=[asdict(f) for f in self.simulator.orders.values()],
            positions=[asdict(p) for p in self.portfolio.positions.values()],
            risk_state=dict(
                halted=self.risk.halted,
                reason=self.risk.reason,
                peak=str(self.risk.peak),
            ),
            period_end=self.last_at.isoformat() if self.last_at else None,
            fills=len(self.simulator.orders),
            pending_unfilled=len(self.pending) + len(self.exit_pending),
            cash=str(self.portfolio.cash),
            liquidation_equity=str(equity) if complete else None,
            valuation_complete=complete,
            unliquidated=final_mark["unliquidated"],
            liquidation_equity_lower_bound=str(equity),
            net_pnl_lower_bound=str(equity - self.portfolio.initial_cash),
            net_pnl=str(equity - self.portfolio.initial_cash) if complete else None,
            fees=str(self.portfolio.fees),
            estimated_exit_fees=str(exit_fees),
            transaction_costs=str(total_costs),
            gross_pnl=str(equity - self.portfolio.initial_cash + total_costs) if complete else None,
            depth_slippage_diagnostic=str(depth_cost),
            realized_pnl=str(self.portfolio.realized),
            # Section 23: both mark-to-mid and executable-liquidation equity
            mark_to_mid_equity=str(mark_to_mid),
            mark_to_bid_equity=str(mark_to_bid),
            # Section 28: trade metrics
            total_trades=len(fills_list),
            winners=len(winners),
            losers=len(losers),
            win_rate=str(len(winners) / len(fills_list)) if fills_list else None,
            average_winner=str(gross_wins / len(winners)) if winners else None,
            average_loser=str(gross_losses / len(losers)) if losers else None,
            payoff_ratio=(
                str((gross_wins / len(winners)) / (gross_losses / len(losers)))
                if winners and losers
                else None
            ),
            profit_factor=profit_factor,
            # Section 28: drawdown statistics
            average_drawdown=(str(sum(dd_values) / len(dd_values)) if dd_values else None),
            max_drawdown=str(max_dd)
            if all(not row["unliquidated"] for row in self.equity)
            else None,
            max_drawdown_observed=str(max_dd),
            valuation_gap_count=sum(bool(row["unliquidated"]) for row in self.equity),
            cluster_exposure={k: str(v) for k, v in self.portfolio.exposures("cluster").items()},
            category_exposure={k: str(v) for k, v in self.portfolio.exposures("category").items()},
            category_performance=self._category_performance(),
            forecast_count=len(self.forecasts),
            forecasts=self.forecasts,
            outcome_labels={
                m: dict(outcome=y, known_at=self.label_times[m].isoformat())
                for m, y in self.labels.items()
            },
            calibration=calibration,
            resolved_markets=len(scored),
            out_of_sample="not established",
            decisions=self.decisions,
            equity=self.equity,
            uncertainty_statistics=dict(
                mean_uncertainty=sum(uncertainty_scores) / len(uncertainty_scores)
                if uncertainty_scores
                else 0,
                max_uncertainty=max(uncertainty_scores) if uncertainty_scores else 0,
                total_anomalies=sum(anomaly_counts),
                forecasts_with_anomalies=sum(1 for c in anomaly_counts if c > 0),
            ),
            sharpe_ratio=risk_ratios["sharpe_ratio"],
            sortino_ratio=risk_ratios["sortino_ratio"],
            return_volatility=risk_ratios["return_volatility"],
        )
