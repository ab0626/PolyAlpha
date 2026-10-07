# PolyAlpha Energy — Pre-registered Research Agenda v0.1

## Status and provenance

This document pre-registers the **energy** research namespace before any energy
outcome is analyzed. It is append-only: new hypotheses are added by appending a
new version section (v0.2, v0.3, ...), never by editing this body.

**Honest timing statement.** At the time of writing there is **no energy raw
lineage and no energy model**. Nothing below was written after observing a
resolution, a backtest, or a model output on energy data. It is fully ex-ante
with respect to energy.

**Isolation invariant.** This namespace must not read or mutate the frozen
Polymarket-US forward experiment (`data/us/retail/raw`, `us/research.py`,
`us/collector.py`, `docs/RESEARCH_AGENDA.md`). `Reads_us_forward(energy)` must be
`0`, mirroring the existing `Reads_v0.4(shadow_external) == 0` rule.

**Alpha status: UNKNOWN** — not positive, not negative, until prospective
forward evidence establishes otherwise. The system must be able to conclude that
no exploitable edge exists; that is an acceptable and valid result.

---

## Scope and venue policy

We are a **U.S.-only prediction-market** platform.

- **Executable:** Polymarket US and Kalshi.
- **Prohibited execution:** Polymarket International, CME/NYMEX, Nodal, ICE,
  ISO/RTO wholesale markets. Conventional/physical markets are **information
  sources only**; `execution_allowed = false` is a code invariant, never
  operator convention.

Physical/futures markets provide information; prediction markets provide trade
expression:

```
Weather → Load → Wind/Solar → Net Load → Generation/Fuel Burn → Gas
       → Outages → Transmission → Reserves/Scarcity → Prices
       → Prediction-Market Fair Probability
```

The prediction market is the final pricing/execution layer; the physical system
is the information-generating layer.

---

## Research priority (tiering)

- **Tier 1 — ERCOT + natural gas.** First complete vertical: physical → probabilistic
  → prediction-market.
- **Tier 2 — oil / petroleum complex.** WTI, crude/Cushing/SPR stocks, refinery
  utilization, gasoline/distillate, EIA petroleum releases.
- **Tier 3 — PJM.** After ERCOT is correct.
- **Later — MISO, SPP, CAISO, NYISO, ISO-NE.** Only when evidence demands it; never
  for data-completeness alone.

---

## Change classification (freeze discipline)

Every change is classified before editing:

- **Class A — collector/infrastructure correctness** (parser, timestamp, reconnect,
  pagination, raw-store bug). Allowed with regression test + provenance.
- **Class B — execution safety** (kill-switch, idempotency, reconciliation,
  fail-closed gate). Allowed under existing change-control.
- **Class C — research methodology** (new feature, model, threshold, strategy,
  universe filter, effect-size rule, new family). Requires a new energy
  preregistration version.

Class C changes are never mixed into an existing frozen experiment.

---

## Energy taxonomy

Semantic classification, not keyword matching. A market is mapped to exactly one
leaf via an explicit, provenance-bearing mapping (see `EnergyMarketLink`).

```
POWER
  ERCOT
  PJM
  MISO
  SPP
  CAISO
  NYISO
  ISO-NE
NATURAL_GAS
CRUDE_OIL
REFINED_PRODUCTS
GRID_RELIABILITY
ENERGY_RELEASES
```

Ambiguous mappings are excluded, not guessed.

---

## Evaluation hierarchy (effective N)

```
raw observations
  ↓ valid point-in-time observations (no lookahead, revision-aware)
  ↓ market-level observations (strike/threshold bucket)
  ↓ underlying energy event/day (ERCOT operating day, EIA release, storage week)
  ↓ independent regime/event clusters
  ↓ effective N
```

`10,000` hourly rows from a handful of weather systems or storage weeks are not
`10,000` independent observations.

---

## Global decision rules

- **Point-in-time invariant:** `X_t` = information actually available at `t`, never
  the latest corrected historical value. Revisions append; earlier values are
  retained, never overwritten.
- **Temporal split:** calibrate/tune on resolved events before `SPLIT_DATE_ENERGY`;
  evaluate only after. `SPLIT_DATE_ENERGY` is fixed at the first energy raw byte,
  recorded in the first report.
- **Multiple testing:** Benjamini–Hochberg FDR at 10% across all tests within a
  family, hierarchical family-level testing where applicable.
- **Significance rule:** FDR-adjusted `p < 0.10` AND the family minimum effect size.
- **Minimum effective N:** 200 independent energy event clusters globally;
  ≥ 20 clusters per category/regime before that slice is reported.
- **Supported:** pre-specified directional effect, FDR-significant, meets minimum
  effect, holds on held-out temporal split, survives the dependence rule.
- **Not supported:** any of the above fails — a valid, informative outcome.

---

## Research families (E1–E7)

### E1 — Energy threshold calibration

- **Question:** do prediction-market probabilities accurately reflect eventual
  energy outcomes?
- **Conditioning:** commodity, threshold/strike, time-to-resolution, liquidity,
  spread, resolution source.
- **Unit:** market-level observation at a fixed TTR bucket → binary outcome.
- **Cluster rule:** underlying energy event/day cluster; threshold ladder of one
  underlying contributes one cluster's information, not one per strike.
- **Primary metric:** expected calibration error vs. flat (price = probability).
- **Min effect:** ECE reduction ≥ 0.5pp, or |mean residual| ≥ 1pp in ≥ 1 cell.
- **Min N:** 200 clusters overall; ≥ 20 per reported category.

### E2 — Ladder consistency (monotonicity)

- **Question:** for ordered strikes `K_i < K_{i+1}`, is `P(X>K_i) ≥ P(X>K_{i+1})`?
- **Test:** violation frequency and magnitude, then whether violations remain
  after executable cost (spread + fee + depth + simultaneous execution).
- **Min effect:** a violation must exceed round-trip cost on ≥ 1% of instances.
- **Critical rule:** no apparent logical arb is executable until costs and
  simultaneous-fill feasibility are modeled.

### E3 — Physical-model residual

- **Construct:** `Residual = p_physical − p_market`.
- **Examples:** ERCOT load model vs. Kalshi ERCOT peak-load ladder; oil/gas
  fundamental-implied vs. WTI/natgas threshold.
- **Question:** does the residual predict settlement beyond market price alone?
- **Baseline:** market price is the prior; physical model must add incremental
  out-of-sample value after costs.

### E4 — Forecast-revision propagation

- **Examples:** `ΔWeatherForecast`, `ΔLoadForecast`, `ΔWindForecast`,
  `ΔEIAExpectation`.
- **Test:** external update → prediction-market repricing, using EventShock-style
  clocks (source/published/first-seen/received/processed).
- **Measures:** latency, response curve, underreaction, overreaction, continuation,
  reversal.
- **Rule:** do not call latency alpha until feed precision makes it observable.

### E5 — EIA scheduled-release reaction

- **Initial events:** natural-gas storage (weekly), petroleum inventories, gasoline,
  distillates.
- **Analogous to NFP:** capture `t_0` and `Surprise = Actual − Consensus`, only with
  a point-in-time consensus source. No reconstructed hindsight consensus.

### E6 — Cross-market consistency

- **Examples:** energy-market probability vs. neighboring strikes, related
  commodities, physical model, and publicly observable futures/options-derived
  information.
- **Rule:** conventional prices are features/reference anchors, not execution
  venues. Test incremental predictive value; never assume they lead. Binary
  probability derived from futures/options must be mathematically justified and
  point-in-time.

### E7 — Weather-to-market propagation

- **Pipeline:** `WeatherRevision → Load/RenewableRevision → EnergyState →
  PredictionMarket`.
- **Question:** do forecast revisions produce predictable delays in
  prediction-market probabilities?

---

## Version history

- **v0.1** — initial energy pre-registration (E1–E7), taxonomy, change
  classification, evaluation hierarchy. Written pre-data, ALPHA: UNKNOWN.