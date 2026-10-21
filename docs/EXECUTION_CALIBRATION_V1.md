# Execution Calibration V1 — Live Taker Calibration (frozen)

Freezes a **live, real-money, taker-only execution-calibration** program on
Kalshi energy. This is SEPARATE from the terminal/directional-alpha gate (which
remains `alpha = UNKNOWN`, no meaningful-size bets). Objective is instrumentation,
not profit.

Append-only. Any change to a frozen definition = a new version section + a new
freeze (`scripts/freeze_execution_calibration.py`).

## Objective (frozen)

Characterize whether the production execution stack behaves as expected with real
money. Primary metric:

```
execution_drag = (exec_px - decision_px) + taker_fee + post_fill_adverse_markout
```

- `exec_px` — actual fill price.
- `decision_px` — executable price observed at decision time.
- `taker_fee` — realized fee.
- `post_fill_adverse_markout` — signed markout against the fill direction over
  the frozen horizons (1s/5s/30s/5m).

**NOT an objective**: prove terminal alpha, optimize parameters, scale capital,
or claim maker profitability.

## Venue, universe, execution (frozen)

- Venue: Kalshi **production**.
- Universe: ERCOT energy (`KXTXERCOTPEAKD` and the ENERGY_SERIES set).
- Execution: **TAKER ONLY**. No maker/limit quoting in this contract.
- Max notional: **$2 per order**.
- One frozen sampler; one intent per run; re-arm kill switch after every fill.

## Logging schema (frozen)

Every fill records: signal age, model `p_hat`, decision-time quote (bid/ask/size),
requested vs filled size, decision→submit→ack→fill latency, quoted vs executed
price, spread paid, fees, 1s/5s/30s/5m markouts, realized spread, cancel/reject/
failure reason, and (secondary, slow) eventual settlement PnL.

## Sampling policy (frozen, pre-registered before any fill)

Eligibility (all required):
```
signal age      <= 6h
book valid      required
spread          <= 0.05
top-5 depth     >= 100 (dollars)
model available required
system healthy  required (kill switch not tripped, collectors fresh)
```

Sample strata (displayed screen edge), deliberately sampled across:
```
low     0.00 < edge < 0.05
medium  0.05 <= edge < 0.10
high    edge >= 0.10
```

The program may sample across all three strata; it does not wait only for rare
high-edge opportunities. Thresholds are frozen; sampling decisions are recorded.

## Safety invariants (frozen)

- Manual/global kill switch stays mandatory; operator arm stays manual.
- Hard daily loss cap and hard daily gross-notional cap (durable OMS state).
- `$2/order` maximum.
- Single frozen sampler; no martingale; no scaling after wins; no mid-batch
  parameter changes.
- Any ambiguous fill arms the kill switch and halts.

## Explicitly out of scope (frozen)

Maker/limit quoting, cancel/replace state machines, queue-position proxying,
time-at-best, fill-probability modeling — these belong to a **separate**
`MAKER_CALIBRATION_V1` successor, not this contract. The maker objective
`E[realized spread − adverse selection − fees]` is frozen there, later.

## Status

- Frozen: `execution-calibration-v1`
- Spec sha256: pinned by `scripts/freeze_execution_calibration.py`
- Runtime (sampler/recorder): to be added to `RUNTIME_FILES` and re-frozen once built.