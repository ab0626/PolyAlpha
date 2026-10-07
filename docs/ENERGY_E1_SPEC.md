# Energy E1 Spec — Historical Calibration Observation Rule (frozen)

This freezes, **before the first read of any calibration number**, the exact
historical probability-observation rule for Family E1 ("energy threshold
calibration") on the Kalshi `KXTXERCOTPEAKD` ERCOT peak-load ladder.

It is append-only. Change = new version section, never an edit.

## Verified data availability (2026-10-07)

- Kalshi exposes point-in-time historical candlesticks at
  `GET /series/{series}/markets/{ticker}/candlesticks` (`period_interval` ∈ {1, 60,
  1440} minutes), each with `yes_bid` / `yes_ask` / `price` OHLC in dollars.
- Markets settled **before** `market_settled_ts` (cutoff `2026-08-08` at time of
  writing) live under `GET /historical/markets/{ticker}/candlesticks`; markets
  settled after the cutoff remain on the live candlestick endpoint.
- `KXTXERCOTPEAKD` settled markets from 2026-08-29 onward are **live** (post-cutoff).

## Universe

All `KXTXERCOTPEAKD` markets with a binary outcome:
`result ∈ {"yes", "no"}` and a known `close_time`. Void/canceled/`""` results are
excluded and **reported** (not silently dropped).

## Outcome

`Y = 1` if `result == "yes"` (peak load exceeded strike `K`), else `0`.

## Observation horizons (frozen)

`H = {24h, 12h, 6h, 1h}` before `close_time`. For each market and horizon `h`:

```
t_h = close_time - h
```

## Price reconstruction (frozen)

- Source: 1-minute candlesticks (`period_interval=1`).
- Selection: the candlestick with the largest `end_period_ts ≤ t_h`.
- **Tolerance:** if that candle's `end_period_ts < t_h − 15min`, the observation
  is `MISSING` (no imputation, no backfill).
- **Executable prices (primary):** `yes_bid.close_dollars`, `yes_ask.close_dollars`.
- **Midpoint (diagnostic, clearly labeled):** `(bid + ask) / 2`.

## Clustering (frozen)

- One underlying **operating day** = one dependence cluster.
- The strike ladder within a day is **correlated, not independent**: all strikes
  of one operating day share one physical outcome, so they are never counted as
  independent observations for inference (point estimates are over all pairs;
  confidence/significance use day-level clustering).

## Train / exploratory vs locked OOS split (frozen)

- **Exploratory:** `close_time < 2026-10-01`.
- **Locked OOS:** `close_time ≥ 2026-10-01`.
- The calibration conclusion is drawn from **locked OOS**; exploratory is
  reported for completeness but carries no confirmatory weight.

## Calibration metric (frozen)

- **Primary:** mean calibration error and Brier score at the **midpoint**
  (labeled "market-implied probability — diagnostic").
- **Executable view:** the same tables computed with `yes_ask` (cost to buy YES)
  and `yes_bid` (price received selling YES), labeled executable.
- **Bins:** 10 probability bins `[0,0.1), …, [0.9,1.0]`.
- Reported per horizon, with day-clustered confidence intervals (block bootstrap,
  2000 resamples, seed 42).

## Minimum support (frozen)

- A bin is reported only with `≥ 20` (price, outcome) pairs **and** `≥ 5`
  operating days; otherwise the bin is `INSUFFICIENT_DATA`.
- A horizon is reported only with `≥ 50` pairs; otherwise `INSUFFICIENT_DATA`.

## Missing-quote treatment (frozen)

- Per-horizon missing rate is reported. Missing observations are excluded, never
  imputed. A horizon with `> 20%` missing is flagged `LOW_QUALITY`.

## Prohibited

- Post-hoc choice of the horizon, bins, split, or metric that makes calibration
  look best or worst.
- Using the final/near-final settlement price as the "ex-ante" observation.
- Treating strikes within a day as independent samples.

## Version history

- **v1** — initial freeze (2026-10-07), before any calibration read.