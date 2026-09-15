"""Enhanced paper monitor — comprehensive paper trading decision tracking.

Records enough data to replay every paper decision, including signals,
decisions, fills, positions, model versions, calibration state, and
both paper and shadow PnL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

D = Decimal


@dataclass
class SignalRecord:
    """Record of a signal seen by the paper system."""

    signal_id: str
    timestamp: datetime
    market_id: str
    signal_side: str
    signal_probability: Decimal
    midpoint: Decimal
    spread: Decimal
    book_depth: Decimal
    model_version: str
    features: dict = field(default_factory=dict)
    raw_forecast: Decimal = D(0)
    accepted: bool = False
    rejection_reason: str | None = None


@dataclass
class DecisionRecord:
    """Record of a trading decision."""

    decision_id: str
    signal_id: str
    timestamp: datetime
    market_id: str
    side: str
    size: Decimal
    entry_price: Decimal
    forecast: Decimal
    edge: Decimal
    model_version: str
    calibration_version: str
    risk_budget_used: Decimal
    fill_simulated: bool = False
    fill_price: Decimal | None = None
    fill_size: Decimal | None = None
    slippage: Decimal = D(0)
    rejection_reason: str | None = None


@dataclass
class ExitRecord:
    """Record of a position exit."""

    exit_id: str
    decision_id: str
    timestamp: datetime
    market_id: str
    side: str
    exit_price: Decimal
    exit_size: Decimal
    realized_pnl: Decimal
    exit_reason: str
    time_held_seconds: int = 0


@dataclass
class ModelVersionState:
    """Snapshot of model version and calibration at decision time."""

    model_version: str
    calibration_version: str
    model_trained_at: datetime | None = None
    calibration_updated_at: datetime | None = None
    brier_score: Decimal = D(0)
    edge_distribution_mean: Decimal = D(0)


@dataclass
class ExposureSnapshot:
    """Point-in-time exposure breakdown."""

    timestamp: datetime
    total_exposure: Decimal
    by_market: dict[str, Decimal] = field(default_factory=dict)
    by_event: dict[str, Decimal] = field(default_factory=dict)
    by_cluster: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class PaperMonitorState:
    """Full state of the paper monitor."""

    signals_seen: int = 0
    signals_accepted: int = 0
    signals_rejected: int = 0
    fill_simulations: int = 0
    active_positions: int = 0
    event_exposure: dict[str, Decimal] = field(default_factory=dict)
    model_versions: list[ModelVersionState] = field(default_factory=list)
    current_calibration: ModelVersionState | None = None
    trailing_realized_edge: Decimal = D(0)
    paper_pnl: Decimal = D(0)
    shadow_pnl: Decimal = D(0)


class PaperMonitor:
    """Enhanced paper trading monitor with full decision audit trail.

    Records every signal, decision, fill, and exit with enough context
    to replay any paper trading decision. Maintains running statistics
    for monitoring and comparison with backtest results.
    """

    def __init__(self, initial_cash: Decimal = D(10000)):
        self._signals: list[SignalRecord] = []
        self._decisions: list[DecisionRecord] = []
        self._exits: list[ExitRecord] = []
        self._exposure_history: list[ExposureSnapshot] = []
        self._model_versions: list[ModelVersionState] = []
        self._state = PaperMonitorState()
        self._initial_cash = initial_cash
        self._active_positions: dict[str, DecisionRecord] = {}
        self._realized_edge_trail: list[Decimal] = []
        self._start_time = datetime.now(timezone.utc)

    @property
    def state(self) -> PaperMonitorState:
        return self._state

    def record_signal(
        self,
        market_id: str,
        signal_side: str,
        signal_probability: Decimal,
        midpoint: Decimal,
        spread: Decimal,
        book_depth: Decimal,
        model_version: str,
        features: dict | None = None,
        raw_forecast: Decimal = D(0),
    ) -> str:
        """Record an incoming signal. Returns signal_id."""
        signal_id = str(uuid4())
        record = SignalRecord(
            signal_id=signal_id,
            timestamp=datetime.now(timezone.utc),
            market_id=market_id,
            signal_side=signal_side,
            signal_probability=signal_probability,
            midpoint=midpoint,
            spread=spread,
            book_depth=book_depth,
            model_version=model_version,
            features=features or {},
            raw_forecast=raw_forecast,
        )
        self._signals.append(record)
        self._state.signals_seen += 1
        return signal_id

    def record_decision(
        self,
        signal_id: str,
        market_id: str,
        side: str,
        size: Decimal,
        entry_price: Decimal,
        forecast: Decimal,
        edge: Decimal,
        model_version: str,
        calibration_version: str,
        risk_budget_used: Decimal,
        fill_simulated: bool = False,
        fill_price: Decimal | None = None,
        fill_size: Decimal | None = None,
        slippage: Decimal = D(0),
        rejection_reason: str | None = None,
    ) -> str:
        """Record a trading decision. Returns decision_id."""
        decision_id = str(uuid4())
        record = DecisionRecord(
            decision_id=decision_id,
            signal_id=signal_id,
            timestamp=datetime.now(timezone.utc),
            market_id=market_id,
            side=side,
            size=size,
            entry_price=entry_price,
            forecast=forecast,
            edge=edge,
            model_version=model_version,
            calibration_version=calibration_version,
            risk_budget_used=risk_budget_used,
            fill_simulated=fill_simulated,
            fill_price=fill_price,
            fill_size=fill_size,
            slippage=slippage,
            rejection_reason=rejection_reason,
        )
        self._decisions.append(record)

        # Update signal acceptance/rejection
        for sig in self._signals:
            if sig.signal_id == signal_id:
                if rejection_reason:
                    sig.rejection_reason = rejection_reason
                else:
                    sig.accepted = True
                break

        if rejection_reason:
            self._state.signals_rejected += 1
        else:
            self._state.signals_accepted += 1
            if fill_simulated:
                self._state.fill_simulations += 1
                self._active_positions[decision_id] = record
                self._state.active_positions = len(self._active_positions)

        return decision_id

    def record_exit(
        self,
        decision_id: str,
        exit_price: Decimal,
        exit_size: Decimal,
        realized_pnl: Decimal,
        exit_reason: str,
    ) -> str:
        """Record a position exit. Returns exit_id."""
        exit_id = str(uuid4())
        decision = self._active_positions.get(decision_id)
        time_held = 0
        market_id = ""
        side = ""
        if decision:
            time_held = int(
                (datetime.now(timezone.utc) - decision.timestamp).total_seconds()
            )
            market_id = decision.market_id
            side = decision.side

        record = ExitRecord(
            exit_id=exit_id,
            decision_id=decision_id,
            timestamp=datetime.now(timezone.utc),
            market_id=market_id,
            side=side,
            exit_price=exit_price,
            exit_size=exit_size,
            realized_pnl=realized_pnl,
            exit_reason=exit_reason,
            time_held_seconds=time_held,
        )
        self._exits.append(record)

        if decision_id in self._active_positions:
            del self._active_positions[decision_id]
            self._state.active_positions = len(self._active_positions)

        self._state.paper_pnl += realized_pnl
        self._realized_edge_trail.append(realized_pnl)
        self._state.trailing_realized_edge = self._compute_trailing_edge()

        return exit_id

    def _compute_trailing_edge(self) -> Decimal:
        """Compute trailing realized edge (rolling 20-trade average)."""
        if not self._realized_edge_trail:
            return D(0)
        window = self._realized_edge_trail[-20:]
        return (sum(window) / D(len(window))).quantize(D("0.0001"))

    def update_model_version(
        self,
        model_version: str,
        calibration_version: str,
        trained_at: datetime | None = None,
        calibration_updated_at: datetime | None = None,
        brier_score: Decimal = D(0),
        edge_distribution_mean: Decimal = D(0),
    ) -> None:
        """Record a model version change."""
        state = ModelVersionState(
            model_version=model_version,
            calibration_version=calibration_version,
            model_trained_at=trained_at,
            calibration_updated_at=calibration_updated_at,
            brier_score=brier_score,
            edge_distribution_mean=edge_distribution_mean,
        )
        self._model_versions.append(state)
        self._state.current_calibration = state

    def update_shadow_pnl(self, pnl: Decimal) -> None:
        """Update shadow (backtest-equivalent) PnL."""
        self._state.shadow_pnl = pnl

    def update_exposure(self, exposure: ExposureSnapshot) -> None:
        """Record current exposure state."""
        self._exposure_history.append(exposure)
        self._state.event_exposure = exposure.by_event

    def get_summary(self) -> dict:
        """Get comprehensive summary of paper monitor state."""
        return {
            "signals_seen": self._state.signals_seen,
            "signals_accepted": self._state.signals_accepted,
            "signals_rejected": self._state.signals_rejected,
            "acceptance_rate": (
                str(
                    D(self._state.signals_accepted)
                    / D(self._state.signals_seen)
                    * D(100)
                )
                + "%"
                if self._state.signals_seen > 0
                else "N/A"
            ),
            "fill_simulations": self._state.fill_simulations,
            "active_positions": self._state.active_positions,
            "paper_pnl": str(self._state.paper_pnl),
            "shadow_pnl": str(self._state.shadow_pnl),
            "trailing_realized_edge": str(self._state.trailing_realized_edge),
            "model_versions_used": len(self._model_versions),
            "current_calibration": (
                {
                    "model": self._state.current_calibration.model_version,
                    "calibration": self._state.current_calibration.calibration_version,
                }
                if self._state.current_calibration
                else None
            ),
            "total_exits": len(self._exits),
            "running_time_seconds": int(
                (datetime.now(timezone.utc) - self._start_time).total_seconds()
            ),
        }

    def export_log(self) -> dict:
        """Export full decision log for replay and audit."""
        return {
            "summary": self.get_summary(),
            "signals": [
                {
                    "signal_id": s.signal_id,
                    "timestamp": s.timestamp.isoformat(),
                    "market_id": s.market_id,
                    "side": s.signal_side,
                    "probability": str(s.signal_probability),
                    "midpoint": str(s.midpoint),
                    "spread": str(s.spread),
                    "model_version": s.model_version,
                    "accepted": s.accepted,
                    "rejection_reason": s.rejection_reason,
                }
                for s in self._signals
            ],
            "decisions": [
                {
                    "decision_id": d.decision_id,
                    "signal_id": d.signal_id,
                    "timestamp": d.timestamp.isoformat(),
                    "market_id": d.market_id,
                    "side": d.side,
                    "size": str(d.size),
                    "entry_price": str(d.entry_price),
                    "forecast": str(d.forecast),
                    "edge": str(d.edge),
                    "model_version": d.model_version,
                    "fill_simulated": d.fill_simulated,
                    "fill_price": str(d.fill_price) if d.fill_price else None,
                    "slippage": str(d.slippage),
                    "rejection_reason": d.rejection_reason,
                }
                for d in self._decisions
            ],
            "exits": [
                {
                    "exit_id": e.exit_id,
                    "decision_id": e.decision_id,
                    "timestamp": e.timestamp.isoformat(),
                    "exit_price": str(e.exit_price),
                    "exit_size": str(e.exit_size),
                    "realized_pnl": str(e.realized_pnl),
                    "exit_reason": e.exit_reason,
                    "time_held_seconds": e.time_held_seconds,
                }
                for e in self._exits
            ],
            "model_versions": [
                {
                    "version": m.model_version,
                    "calibration": m.calibration_version,
                    "trained_at": m.model_trained_at.isoformat() if m.model_trained_at else None,
                    "brier_score": str(m.brier_score),
                }
                for m in self._model_versions
            ],
        }
