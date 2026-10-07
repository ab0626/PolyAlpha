# E3 Baseline v0 — Official ERCOT Forecast + Lead-Matched Error (frozen)

This freezes the **first** E3 physical baseline, before any model-vs-market edge
is scored. Append-only; change = new version.

## Model (frozen)

Let `M_d` = daily peak system load (max hourly `TOTAL`) for operating day `d`.

```
M̂_{d,t} = max_h L̂_{d,h|t}          # ERCOT 7-day load forecast peak at time t
ε_{d,t}  = M_d − M̂_{d,t}           # additive-MW forecast error at lead (d − t)
```

For strike `K` at forecast time `t`:

```
p_YES(K, t) = P( ε > K − M̂_{d,t} )
```

This yields a coherent monotone survival curve across all strikes by
construction.

## Data-product alignment (frozen)

- **Forecast:** ERCOT `NP3-560-CD` (Seven-Day Load Forecast by Forecast Zone),
  `SystemTotal` column. This is the apples-to-apples forecast against…
- **Settlement/actual:** ERCOT `NP6-346-CD` (Actual System Load by Forecast Zone),
  `TOTAL` column — the exact Kalshi settlement source.
- `NP3-565-CD` (forecast by model and weather zone) is a **different product** and
  is not silently substituted for the baseline.

## Lead discipline (frozen)

Residuals are matched by **lead bucket** (forecast age in days: 0..6+). A
6-day-ahead residual is never pooled with a 6-hour-ahead residual; forecast
uncertainty collapses as the operating day approaches.

## Point-in-time residual training (frozen)

Historical evaluation day `d` uses residuals from days **strictly before `d`**
only (expanding window). Never estimate the residual distribution on the whole
sample and then score those same days.

## Finite-sample smoothing (frozen)

The empirical error CDF uses a rank correction so a modest sample never yields
literal 0/1:

```
F̂(x) = r / (N + 1),   r = #{ ε ≤ x }
```

## Settlement vintage (frozen)

Store the **first complete** NP6-346 file for each operating day: file bytes
hash, publish timestamp, and extracted peak. Later corrected ERCOT history never
silently replaces the settlement-source value.

## Executable edge — both directions (frozen)

```
Edge_YES = p_YES − Ask_YES − Fees
Edge_NO  = (1 − p_YES) − Ask_NO − Fees
```

Sizes are recorded; zero resting size ⇒ zero capacity. The NO ask is the
complement of the resting YES bid (verified against the Kalshi orderbook).

## Shadow ledger (frozen)

Record **every** snapshot (not only attractive ones): `p_model`, `yes_bid`,
`yes_ask`, `no_ask`, sizes, fees, forecast age (lead), residual-training `N`,
model version, and eventual settlement. This is what separates real alpha from
cherry-picked examples.

## Explicitly excluded (frozen)

No weather ML, no temperature/month/weekday/renewable conditioning yet. Start
with `ε | lead` only; any visible heteroskedasticity/regime bias becomes a
preregistered E3-v1, not something tuned until the market looks mispriced.

## Version history

- **v0** — initial freeze (2026-10-07), before any model-vs-market scoring.