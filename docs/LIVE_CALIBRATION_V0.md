# LIVE_CALIBRATION_V0 — pre-registered production execution-calibration experiment

## Status and purpose

This document is a **pre-registration**. It freezes, before the first real-money
mutation, the rules of a micro-sized live execution-calibration experiment.

**This is an execution-calibration experiment, not alpha validation.** Its only
question is whether a quoted conservative edge survives real execution. It makes
**no** claim that E3 (or any family) predicts outcomes, and it does **not** unlock
or alter the mainline forward-evidence gate.

Three evidence streams are now explicitly independent:

| Stream | Verdict |
|---|---|
| Mainline US forward evidence | `PENDING_EVIDENCE`, alpha `UNKNOWN` (80% observed-coverage gate unchanged) |
| E3 research | prospective only, verdict `UNKNOWN` |
| **LIVE_CALIBRATION_V0** | **execution quality / fillability / friction measurement only** |

```
LIVE_CALIBRATION PnL  !=  validated alpha
```

LIVE_CALIBRATION PnL must never alter E3 parameters, the 80% coverage rule,
family thresholds, or sizing during the frozen experiment.

## Frozen rules (fixed before the first live order)

- **Venue:** Kalshi production only (`external-api.kalshi.com`). Demo is rejected.
- **Signal family:** E3 only.
- **Signal source:** `ForecastErrorModel.p_peak_gt()` under the existing frozen
  E3 methodology (`ENERGY_E3_SPEC`). E3 research logic is **not** modified.
- **Denominator:** every eligible E3 opportunity is logged whether traded or not.
- **No parameter tuning** from interim live results.
- **No scaling** based on early PnL.
- **Fixed tiny risk sizing** (below).
- **Hard per-order risk cap** (below).
- **Hard daily loss stop** (below).
- **Hard total experimental bankroll cap** (below).
- **Minimum conservative executable edge** after actual fees + explicit
  adverse-selection/slippage buffer (below).

## Frozen sizing and caps

| Quantity | Frozen value |
|---|---|
| Max risk per order | **$2.00** (max 2 contracts notional; worst-case loss < $2 because price ∈ (0,1)) |
| Daily realized + marked loss stop | **$10.00** |
| Total bankroll allocated | **$50.00** |
| Minimum conservative net edge | **$0.05 / contract** |

```
Edge_conservative = p_model_conservative - p_executable - Fee_actual - Buffer_adverse_selection/slippage
```

An order is eligible only if `Edge_conservative >= $0.05/contract`. These values
are intentionally small and conservative; they are frozen before the first trade.

## Fee model (mandatory before any mutation is enabled)

The E3 `COST = 0.02` placeholder is **NOT** a basis for a capital decision. Before
any mutation is enabled, the authoritative production fee rule must be obtained
from Kalshi (API/docs), encoded explicitly, tested, and its provenance recorded
here (see the readiness checklist). Until then, mutation remains blocked.

## Safety model

```
strategy may OBSERVE safety state, never RELAX safety state
```

- **Operator arming** and **kill-switch clearing** are separate, file-backed,
  persistent states. The trading process may **read** both; it may **arm** (safe
  direction) but never **clear/relax** either. Clearing requires an explicit
  operator action via a dedicated CLI, never a call reachable from strategy code.
- **Fail-closed on first use:** a fresh deployment arms the kill switch.
- **UNKNOWN ⇒ no new intents.** Any ambiguous create/cancel/reconcile state halts
  the harness and blocks a fresh `client_order_id` until the persisted episode is
  reconciled.
- **Restart with a nonterminal journal** reconciles before any new CID is minted.
- **Version gate:** production mutation is impossible unless the calibration
  protocol version matches the frozen runtime version.
- **Caps are code invariants**, enforced on every mutation inside the adapter;
  they are never derived from caller-provided values.

## Realized execution metrics (collected, never used for tuning)

quoted edge · submitted edge · fill probability · fill latency · partial fills ·
actual fees · slippage · post-fill markout · realized PnL.

## Production-readiness checklist (all required before mutation is enabled)

- [ ] production auth succeeds
- [ ] production balance readable
- [ ] positions readable
- [ ] open orders readable
- [ ] fills readable
- [ ] exact production fee model verified
- [ ] calibration spec frozen
- [ ] prod adapter freeze verifies
- [ ] prod OMS freeze verifies
- [ ] kill switch starts armed
- [ ] hard caps tested
- [ ] duplicate-process test passes
- [ ] crash/restart reconciliation tests pass
- [ ] unknown outcome halts new intents
- [ ] full test suite + ruff + CI green

No order is placed as part of the implementation pass. The first production
test is a separate, explicit step:

1. read-only production smoke (balance / positions / orders / fills),
2. fee calculation verified against an actual eligible contract,
3. explicit operator approval for the first **$2-risk** calibration order.

## Version history

- **v0** — initial pre-registration (this document). Frozen caps and safety model
  as above. Written before any real-money mutation, while mainline is
  `PENDING_EVIDENCE` and E3 verdict is `UNKNOWN`.