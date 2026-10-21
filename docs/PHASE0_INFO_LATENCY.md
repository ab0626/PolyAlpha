# Phase 0 — Causal-Source Information Latency (frozen)

Freezes the **read-only/offline** measurement of "does the causal information
source lead the prediction-market book, and is the lag economically executable?"
**before** any result is read. This track is independent of #6C (which continues
accumulating untouched) and of the terminal-outcome gate.

Append-only. Any change to a frozen definition = a new version section.

## Objective

Quantify, for ERCOT (and later crypto/equity), the timestamp chain from the
causal source to the prediction-market book, and answer whether the residual
lag survives spread + fees as executable edge.

## Venue & lineage (frozen)

- **Source side**: ERCOT `data/energy/ercot/forecast_vintages.jsonl`
  (`publish_timestamp`, `received_at`, `system_total_mw`, `forecast_hour`,
  `delivery_day`, `operating_day`).
- **Market side**: Kalshi 30s REST ladder `data/energy/kalshi/ladders/*.jsonl`
  (BBO `yes_bid_dollars`/`yes_ask_dollars` per ticker, `captured_at`,
  `threshold`, `raw.close_time`), series `KXTXERCOTPEAKD`.
- Fair value uses the frozen E3 model: `_load_actuals` + `_load_forecast` +
  `ForecastErrorModel` (`ENERGY_E3_BASELINE_V0`), exactly as the frozen runner.

The WS orderbook (sub-30s) reaction is **not** part of Phase 0; it is the
extractor's job later. Phase 0 measures reaction at 30s ladder resolution.

## Timestamp chain (frozen)

```
t_source_publish          ERCOT vintage publish_timestamp (source time, CT)
t_source_received_local   ERCOT received_at (our local ingest, UTC)
t_first_reaction          first ladder snapshot (captured_at) >= t_source_publish
                          where the ATM-strike YES mid moved >= 0.01 in the
                          direction implied by the revision
t_material_repricing      first ladder snapshot >= t_source_publish where
                          |mid_ATM - P_new| <= 0.02
```

## Source surprise (frozen)

- Per-vintage daily-peak forecast = max `system_total_mw` over `forecast_hour`
  for one `(delivery_day, doc_id)`.
- A vintage is an **event** iff `|delta_peak| >= 100 MW` vs the previous vintage
  for the same `delivery_day`.
- Direction: `delta_peak > 0` ⇒ greater-strike YES mid should **rise**.

## Fair value (frozen)

`P_new = ForecastErrorModel.p_peak_gt(day, as_of=t_source_publish, strike)`
using only vintages with `publish_timestamp <= t_source_publish`.
`ATM strike` = the strike nearest the current daily-peak forecast.
(Residuals are the frozen model's; this is a latency measurement, not an alpha
claim.)

## Metrics (frozen)

```
source_ingest_latency      t_source_received_local - t_source_publish
market_reaction_latency    t_first_reaction        - t_source_publish
total_source_to_market     t_material_repricing    - t_source_publish
stale_quote_window         t_material_repricing    - t_source_publish  (economic)
```

Report p50 / p95 / p99 for each, stratified by:
- strike proximity (ATM vs ±1 strike),
- time-to-expiry (hours to close),
- surprise magnitude (|delta_peak| in {100–250, 250–500, >500} MW).

## Executable edge (frozen)

At `t_source_publish`, for the ATM strike:

```
edge_YES = P_new - yes_ask - 0.07          (buy YES if > 0)
edge_NO  = (1 - P_new) - no_ask - 0.07     (buy NO if > 0)
```

Report the maximum `edge_*` observed within `[t_source_publish,
t_material_repricing]` together with the available size at that quote.

## Live-only filter (frozen)

Include only events whose `received_at - publish_timestamp <= 30 min` (exclude
backfilled vintages where `received_at` is a replay artifact, not live ingest).

## Research protection (frozen)

- Thresholds (`100 MW`, `0.01`, `0.02`, `0.07`, `30 min`) are pre-registered;
  no post-hoc re-tuning.
- Read-only: consumes existing captures, writes a derived report only. Never
  trades, never mutates a ledger, never touches #6C collectors.

## PolyBolt leg (frozen finding)

The 92 GB `data/us/market_data/raw` lineage is CLOB books/trades only; it does
**not** contain PolyBolt `price.crypto` / `price.equity` reference channels.
Therefore the crypto/equity leg is **prospective-only**: it requires a separate
non-authoritative PolyBolt recorder, not a claim on the existing archive.

## Verdict rubric (frozen)

- If executable edge disappears after spread + fees ⇒ **do not buy feed
  infrastructure**; the information race is not economically real here.
- If median stale window is sub-minute with meaningful depth and net edge
  survives costs ⇒ feed placement / region / FIX-vs-WS become economically
  justified, not just technically impressive.