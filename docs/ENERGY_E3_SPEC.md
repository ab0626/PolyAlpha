# Energy E3 Spec — Physical-Model Residual (frozen)

Freezes the physical-model residual family (E3) before any model-vs-market
comparison is read. Append-only; change = new version section.

## Objective

Does our physical model know the settlement variable better than the market?

## Residual (frozen)

For strike `K` at time `t` (the ERCOT daily peak load `M_d`):

```
R_{K,t} = p_physical(M_d > K | X_t) - p_market(M_d > K)
```

- `p_physical` is a point-in-time distribution estimate using only information
  available at `t` (ERCOT load forecast vintage, weather forecast vintage,
  seasonality, calendar). No lookahead, no revised-actual leak.
- `p_market` reference:
  - **diagnostic:** Kalshi midpoint `(yes_bid + yes_ask) / 2`;
  - **primary:** executable quotes (see edge below).

## Executable edge — BOTH directions (frozen)

For every strike `K`, compute separately:

```
Edge_YES = p_model - YES_ask - C
Edge_NO  = (1 - p_model) - NO_ask - C
```

- `YES_ask` / `NO_ask` are Kalshi's executable ask dollars (probability units).
- `C` = fees + adverse-selection/slippage buffer. Fee schedule is read from the
  venue; `C` is frozen here as a conservative placeholder (`C = 0.02` probability
  points) and replaced by the measured schedule before any capital decision.
- A large model-vs-**mid** disagreement does **not** count as edge; only the
  executable both-direction inequalities above do.

## Clustering (frozen)

- One underlying **operating day** = one dependence cluster.
- Strikes within a day are correlated and never treated as independent.

## Split (frozen)

- **Exploratory:** any historical reconstruction (pre-prospective-capture).
- **Locked OOS:** operating days observed **prospectively** (after capture start,
  2026-10-07), evaluated only after settlement.

## Metrics (frozen)

1. `E[ Y - p_market | R_{K,t} ]` — settlement residual conditional on the model
   residual (does the model add information beyond price?).
2. `E[ net executable PnL | R_{K,t} ]` — the economically meaningful quantity,
   computed with both-direction edges, fees, and realized fill.
Both reported with day-clustered confidence intervals (block bootstrap, seed 42).

## Minimum support (frozen)

- A residual bucket is reported with `≥ 20` (R, outcome) pairs and `≥ 5`
  operating days; otherwise `INSUFFICIENT_DATA`.

## No-lookahead invariant (frozen)

`X_t` = information actually available at `t`. Revised ERCOT actuals are never
used to compute `p_physical` at an earlier `t`.

## Observation rule (frozen — v2, appended 2026-10-11)

Appended before any prospective E3 evaluation; does not alter the v1 residual,
edge, clustering, or metric definitions.

- **Prospective cutover:** `2026-10-11T03:15:00Z`. Only Kalshi snapshots with
  `captured_at >= cutover` may enter the shadow observation ledger. Historical /
  backfilled ERCOT forecast vintages and actuals may train the residual
  distribution, but never enter the ledger as observations.
- **Sampling (per lead, once):** for each operating day `d`, each strike `K` in
  the day's ladder, and each lead bucket `h ∈ {0..6}` whose training residual
  count `N_h(d) >= 5` (using actual days strictly before `d`):
    - take the latest forecast vintage for `(publish_day = d−h, delivery_day = d)`;
    - `t*` = the first Kalshi snapshot for `(d, K)` with
      `captured_at >= max(forecast.received_at, cutover)`;
    - record exactly one observation at `t*` (deterministic id, idempotent).
- **Denominator:** every strike at every qualifying `(d, K, h)` is recorded, not
  only positive-edge ones. `is_candidate` is derived from the frozen edge rule
  (`edge_yes > 0` or `edge_no > 0`).
- **Resolution:** when operating day `d` completes, record BOTH the physical
  outcome (`M_d = max_h ActualSystemLoad_{d,h}` and `physical_outcome_yes`) and the
  venue settlement (`kalshi_result`, `kalshi_expiration_value`) separately, using
  the first complete NP6-346 settlement-source publication (never a later
  corrected revision). Disagreement between physical and venue outcomes is a
  settlement/integration alarm, never silently collapsed.

## Prohibited

- Post-hoc horizon/bin/split/cost selection.
- Mid-only edge claims.
- Mapping futures/options to a binary probability without a mathematically
  justified, point-in-time bridge.

## Version history

- **v1** — initial freeze (2026-10-07), before any model-vs-market comparison.
- **v2** — freeze the observation rule + prospective cutover (2026-10-11), before
  any prospective E3 evaluation.