# PolyAlpha — Live-Readiness Architecture

**Status:** ARCHITECTURE READY — live transmission NOT implemented
**Baseline:** `v0.3.0-research-baseline-334b911` (immutable)
**Current priority:** BURNIN_RUNNING (data collection, NOT live execution)

---

## 1. Project objective (corrected framing)

PolyAlpha is NOT ultimately a paper-trading-only platform. The final
engineering objective is:

> A production-grade prediction-market trading system, validated through
> research, replay, backtesting, shadow execution, and paper execution before
> deployment.

Replace the old framing "no real-money execution — paper trading only" with:

> Current execution mode is research/paper/shadow. Target architecture is
> live-trading-ready after empirical validation, compliance checks, and
> explicit operator approval.

Do NOT weaken any research-integrity constraints because live trading is the
eventual goal. Alpha remains **UNKNOWN** until real prospective evidence says
otherwise.

---

## 2. System lifecycle

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Research infrastructure | COMPLETE |
| 2 | Data-collection hardening | COMPLETE |
| 3 | Live collector burn-in | NEXT |
| 4 | Real forward data collection | pending |
| 5 | Frozen baseline empirical evaluation | pending |
| 6 | True locked holdout evaluation | pending |
| 7 | Forward shadow/paper validation | pending |
| 8 | Execution-model calibration | pending |
| 9 | Live-trading readiness | pending |
| 10 | Operator-approved live deployment | pending |

Phase progression is enforced by the collection phase state machine
(`phases.py`) — do not bypass `BURNIN_RUNNING → BURNIN_PASSED →
REAL_DATA_START → COLLECTION_RUNNING`.

---

## 3. Execution abstraction

The strategy/risk pipeline produces the same **OrderIntent** regardless of
execution mode. No strategy contains exchange-specific order-submission logic.

```
market data → features → models → forecast → EV engine → signal engine
  → risk engine → OrderIntent → execution gateway
```

Modes preserve identical strategy semantics across backtest, replay, paper,
shadow, and live-ready operation.

### OrderIntent (`order_intent.py`)

Carries the full decision context: identity (`intent_id`, `execution_attempt_id`,
`decision_id`), market/outcome/side, sizing/pricing, fair value, expected
execution costs, edge, exposure before/after, freshness timestamps, and hash
provenance (`research_logic_sha256`, `config_sha256`, `signal_book_hash`).

### ExecutionGateway (`execution_gateway.py`)

| Gateway | Mode | Behavior |
|---------|------|----------|
| `PaperExecutionGateway` | PAPER | depth-walk simulation (fees, partial fills, latency) |
| `ShadowExecutionGateway` | SHADOW | records exact intended order + observed book; never executes |
| `LiveReadyExecutionGateway` | LIVE_READY | GATED SCAFFOLD — runs kill switch, pre-trade gate, idempotency, then stops at APPROVAL_PENDING. Transmits nothing without an operator-approved venue adapter. |

The LiveReady gateway contains **no venue client and no credentials**. Wiring a
real adapter is a separate, operator-approved step that is out of scope for the
current phase.

---

## 4. Pre-trade safety gate (`pre_trade_gate.py`)

Every live-ready OrderIntent must pass `LivePreTradeGate` immediately before
execution. A signal valid 500 ms ago does NOT imply it remains valid. The gate
re-checks (21 conditions):

1. Market still active
2. Market still accepting orders
3. Order book still enabled
4. Market not in resolution/closure
5. Geographic/platform eligibility OK
6. Book fresh
7. Forecast fresh
8. Metadata fresh
9. Resolution definition unchanged
10–11. Best bid/ask + expected VWAP within tolerance
12. Net edge above threshold
13. Sufficient depth
14. Fee schedule unchanged
15. Intended size satisfies market/event/cluster/category/total caps
16. Daily-loss breaker clear
17. Drawdown breaker clear
18. Global kill switch clear
19. Data-health state GREEN
20. Reconciliation healthy
21. No unresolved account/exchange ambiguity

If any check fails → **REJECT INTENT**. Requirements are never weakened to
obtain a fill.

### TOCTOU protection

The state used to generate a signal may differ from the state at execution.
Store `signal_book_hash` and compare against `execution_book_hash`. Compute:

```
price_drift, spread_drift, depth_drift, probability_edge_drift
```

with configurable tolerances. A four-cent move that destroys expected value
must cause REJECT or RECOMPUTE, never blind forwarding.

---

## 5. Idempotency (`order_intent.py`)

Every intent has `intent_id`; every attempt has `execution_attempt_id`.
Repeated processing of the same event must not create a second logical order.
`IntentLedger` deduplicates by intent_id and attempt id. States:

```
CREATED → VALIDATING → APPROVAL_PENDING → APPROVED → SUBMISSION_PENDING
  → ACKNOWLEDGED → PARTIALLY_FILLED / FILLED / CANCELLED / FAILED
  → RECONCILIATION_REQUIRED
```

---

## 6. Ambiguous order state + reconciliation (`order_reconciliation.py`)

The most dangerous state: request sent → connection fails → outcome unknown.
Never blindly retry. Transition to **RECONCILIATION_REQUIRED** and reconcile
intended vs acknowledged/open/fills/cancellations/positions/balances. The
system must distinguish *definitely rejected* from *submission outcome unknown*.

### Permanent rule: RECONCILIATION_REQUIRED is sticky

Once an intent reaches `RECONCILIATION_REQUIRED`, normal workflow must NOT
move it back toward submission simply because time passed. It requires:

```
venue/account state reconciliation
        ↓
definitive state established
        ↓
new transition
```

If state cannot be established, the intent **remains blocked**. This is
enforced by `assert_transition_allowed`: transitioning out of
`RECONCILIATION_REQUIRED` toward any forward/submission state raises, and only
definitive outcomes (REJECTED, CANCELLED, FILLED, ...) are legal. This is the
right behavior even when it is operationally annoying.

---

## 7. Cancel-on-stale (`cancel_policy.py`)

Resting orders must have explicit invalidation rules. Triggers: model
probability change, market price move, resolution metadata change, external
shock, stale feed, WS disconnect, reconciliation failure, risk cap reached,
portfolio uncertainty, kill switch. On uncertainty → ESCALATE (do not
blindly cancel either).

---

## 8. Global kill switch (`kill_switch.py`)

Single authoritative `TradingKillSwitch`. Persistent (survives restart),
reason-coded, timestamped, audit-visible. Activation reasons include manual,
daily-loss, drawdown, data failure, abnormal reconciliation, account-state
inconsistency, abnormal latency, repeated execution failure, unexpected model
behavior, abnormal signal rate, config mismatch, research hash mismatch,
eligibility failure, dependency outage. Restart never clears it.

---

## 9. Live observability

The operational dashboard must always show the operating mode unambiguously:

```
RESEARCH | PAPER | SHADOW | LIVE_READY
```

Sections: COLLECTOR HEALTH, MODEL HEALTH, SIGNAL HEALTH, RISK, EXECUTION
(intents, approval pending, acks, partial/full fills, cancels, failures,
reconciliation-required), SYSTEM (kill switch, mode, research/collector/
interface/config hashes).

---

## 10. Shadow mode

Consumes real forward market data, frozen real model predictions, and real
portfolio-state assumptions; produces exact intended `OrderIntent` objects at
the time they would have been generated — but they remain non-executing.
Records: intended order, intended price, observed book, subsequently
available fills, actual market evolution. Answers: "What exactly would the
live system have attempted?"

---

## 11. Paper mode

Remains a validation layer. Continues simulating depth, fees, partial fills,
latency, market movement, and maker-queue assumptions. Not removed because
live deployment is the goal.

---

## 12. Live-readiness promotion gate (`promotion_gate.py`)

Do not promote a strategy merely because it is profitable. Require evidence:

- FORECASTING: positive ΔBrier, acceptable calibration, stable disagreement
- EXECUTION: realistic edge realization, acceptable paper-vs-backtest
  discrepancy, calibrated execution model
- STATISTICS: adequate effective N, clustered bootstrap, multiple-testing aware
- ROBUSTNESS: cost-ladder survival, latency survival, parameter stability,
  Monte-Carlo stress survival
- RISK: acceptable drawdown, no excessive concentration, caps functioning
- FORWARD: prospective shadow + paper results
- HOLDOUT: untouched final-holdout result
- DATA: collector fidelity healthy, no unresolved integrity failures

---

## 13. Deployment progression

```
shadow → paper → operator-approved minimal live exposure
  → monitored limited exposure → expand only after empirical evidence
```

Initial live position/risk limits must be substantially smaller than research
capacity. Never infer safe deployment size solely from expected return.

---

## 14. Capacity analysis (`capacity.py`)

Evaluate identical signals at notional $100 → $100k. Per level: executable
quantity, VWAP, slippage, fees, remaining edge, fill fraction. Produce
capital → net edge / expected return / fill rate. Alpha does not have
unlimited capacity.

---

## 15. Execution-model calibration (`execution_calibration.py`)

Compare predicted vs observed VWAP/slippage/fill-rate/latency error,
conditioned on spread, liquidity, depth, volatility, category, trade size,
time-to-resolution. Determines whether historical execution assumptions are
systematically optimistic.

---

## 16. Security and privilege separation

Never place private keys, credentials, or authentication secrets in source
code, committed configs, logs, manifests, or exception traces. Use a secrets
boundary. Privilege separation:

```
data collector → research engine → risk / intent generator → isolated execution boundary
```

Only the final execution boundary requires execution credentials. The
collector and research processes must not have access to them.

---

## 17. Compliance

Never implement VPN/proxy jurisdiction bypass, falsified location, account
sharing, or restriction circumvention. The live pre-trade layer fails closed
if platform eligibility cannot be established. Research/data collection and
live execution remain separate concerns.

---

## 18. Human approval boundary

Live-ready intents stop at **APPROVAL_PENDING** and require a deliberate
human approval step. The approval must receive a complete pre-trade summary:

market, outcome, intended size, limit price, fair probability, conservative
probability, predicted net edge, current spread, expected VWAP, exposure
after trade, drawdown state, model version, data freshness, resolution-risk
status.

No autonomous real-money wagering and no silent order transmission.

---

## 19. Current priority (unchanged)

Do NOT start building live execution now. The next milestone is still:

```
BURNIN_RUNNING → fault injection → dual replay → burn-in report
  → BURNIN_PASSED → REAL_DATA_START → COLLECTION_RUNNING
```

The next meaningful artifact is a **REAL burn-in integrity report**, not a
strategy module and not an exchange execution module.

After enough independent real observations: run frozen baseline → lock/evaluate
holdout → forward shadow/paper validation → calibrate execution assumptions →
evaluate live-readiness gate.