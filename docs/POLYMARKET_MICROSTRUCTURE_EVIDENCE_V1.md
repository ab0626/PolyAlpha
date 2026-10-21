# Polymarket Microstructure Evidence V1 — Research Contract (frozen)

Freezes the Polymarket CLOB fast-horizon research track **before** any hypothesis
result is read. This is a SEPARATE venue contract from
`MICROSTRUCTURE-EVIDENCE-V1` (Kalshi energy); the architecture is shared, the
venue assumptions are not.

Append-only. Any change to a frozen definition = a new version section + a new
freeze (`scripts/freeze_polymarket_microstructure.py`).

## Objective

Does repeatable, cost-adjusted short-horizon structure exist in the Polymarket
CLOB book/trade flow, and can we distinguish **signal absent** from **signal
present but overwhelmed by cost** from **signal present only in high-liquidity
regimes**? Liquidity/regime stratification is a first-class, pre-registered
control — not something added after seeing results.

## Venue & lineage (frozen)

- **Books**: `data/us/market_data/raw/YYYY/MM/DD/polymarket_us_retail_market_data-*.jsonl`
  — outer envelope `{canonical_json, sha256}` (hash-chained, raw-before-parse).
- **Trades**: `data/us/trade/*.jsonl` — same envelope, `SUBSCRIPTION_TYPE_TRADE`.
- Both carry `exchange_timestamp_ms` (venue), `received_at_ns`,
  `processed_at_ns`, `received_monotonic_ns`, `message_sequence_local`.

## Canonical state (frozen)

```
BookState:
  venue_ts_ms      int     exchange_timestamp_ms (venue)
  receive_ts_ns    int     received_at_ns (local)
  market_id        str     marketSlug
  best_bid         Dec     max(bids[].px.value)      (bids ascending, best = last)
  best_ask         Dec     min(offers[].px.value)    (offers ascending, best = first)
  bid_depth_k      Dec     sum of top-k bid qty
  ask_depth_k      Dec     sum of top-k ask qty
  mid              Dec     (best_bid + best_ask) / 2
  spread           Dec     best_ask - best_bid
  microprice       Dec     (best_ask*bid_depth_k + best_bid*ask_depth_k) / (bid_depth_k+ask_depth_k)
  imbalance        Dec     (bid_depth_k - ask_depth_k) / (bid_depth_k + ask_depth_k)
  seq              int     message_sequence_local
  gap_clean        bool    hash-chain + sequence integrity

TradeEvent:
  venue_ts_ms      int     tradeTime
  market_id        str     marketSlug
  price            Dec     trade.price.value
  size             Dec     trade.quantity.value
  taker_side       str     taker.outcomeSide (YES/NO)
  taker_intent     str     taker.intent (BUY_LONG/SELL_SHORT)
  maker_side       str     maker.side
  trade_id         str     trade.id
```

`k = 5` (frozen). Unlike Kalshi, Polymarket has **no complement logic** — the
book is directly bid/ask, and best bid/ask are read straight from the arrays.

## Book semantics & sequence (frozen)

- Books are **full snapshots** (bids + offers arrays), not deltas. Each
  `marketData` message is a complete, self-contained book for one `marketSlug`.
- Min tick = `0.001` (audited: min positive price difference across 550 distinct
  price levels).
- `gap_clean = true` iff the per-record integrity hash verifies
  (`sha256 == sha256(canonical_json)`; 0 mismatches audited). Books are snapshots,
  so reconstruction does **not** depend on sequence continuity — a dropped record
  means a missing snapshot, not a corrupted book. `message_sequence_local` is
  present but NOT contiguous (per-connection discontinuities audited); it is
  recorded for provenance only, not used as a gap gate.

## Audit findings (2026-09-21 sample, 265,646 book records / 3,063 trades)

- Snapshot semantics: full book per message; 98.7% two-sided (bids+offers both non-empty).
- Hash integrity: `sha256 == sha256(canonical_json)` for 265,646/265,646 records.
- Timestamps: venue `exchange_timestamp_ms` + local `received_at_ns` on every record.
- Trades: `taker.intent` (BUY_LONG/SELL_SHORT) + `taker.outcomeSide` (YES/NO) present on 92% (2,824/3,063).
- Outcome mapping: book = YES/long token; NO/short = complement (stats `longPx`/`shortPx`).
- Cluster: `marketSlug` (114 unique in the day sample).

## Research universe (frozen)

A `marketSlug` contributes eligible observations when, over the window:
1. book is `MARKET_STATE_OPEN`,
2. both bid and ask are non-empty (mid defined),
3. spread ≤ `0.02` (2 cents) and top-5 depth ≥ `500` (shares) and the window has
   ≥ `10` trades (liquidity gate),
4. `gap_clean = true` for all book/trade records in the window.

## Labels (frozen)

Signed mid markout, same structure as the Kalshi contract:

```
markout_h(t) = s * (mid(t+h) - mid(t))
net_h(t)     = markout_h(t) - 0.5*spread(t+h) - taker_fee * mid(t+h)
```

Horizons: `100ms, 1s, 5s, 30s, 300s, 1800s` (primary); tolerances
`{100ms±50ms, 1s±200ms, 5s±500ms, 30s±2s, 300s±10s, 1800s±30s}`.
`taker_fee` is pinned from the venue fee schedule **before** evaluation (not
inferred here).

## Independence (frozen)

- Cluster = `(marketSlug, UTC day)`. Never tick count.
- Day-clustered block bootstrap, `seed = 42`, `n_boot = 2000`.

## Hypotheses (frozen, first batch)

Each has one pre-registered primary horizon.

**P1 — imbalance → markout.** `primary = 1s`. `s = sign(imbalance)`.
Test `E[markout_1s] > 0` and `E[net_1s] > 0`.

**P2 — microprice displacement → markout.** `primary = 5s`.
`d = microprice - mid`; `s = sign(d)`. Test `E[markout_5s] > 0`, `E[net_5s] > 0`.

**P3 — aggressive flow → continuation/reversion.** `primary = 5s`.
YES-direction sign `+1` if `taker.outcomeSide=YES` and `taker.intent=BUY_LONG`,
else `−1`. Trailing `1s` signed flow `flow = Σ sign_i * size_i`; `s = sign(flow)`.
Test `E[markout_5s] > 0` (continuation; negative = reversion).

**P4 — maker/taker adverse selection.** `primary = 1s`.
`maker_markout = -s * (mid(t+h) - mid(t))`; adverse selection ⇒
`E[maker_markout] < 0`, monotone in taker size. Test `E[maker_markout] < 0`.

**P5 — spread/depth withdrawal → repricing.** `primary = 30s`.
`Δask = ask_depth_k(t) - ask_depth_k(t-5s)`, `Δbid = bid_depth_k(t) - bid_depth_k(t-5s)`;
`d = Δask - Δbid`; `s = sign(d)`. Test `E[markout_30s] > 0`, `E[net_30s] > 0`.

## Controls (frozen — liquidity/regime stratification is mandatory)

Report every primary test conditioned on, with PASS = directional consistency:

- spread bucket (below / above median),
- depth bucket (below / above median top-5 depth),
- **liquidity regime** (top-5 depth quartiles — the key Polymarket stratification),
- price bucket (mid in 5 quintiles),
- time-to-expiry (before / after the operating-day midpoint),
- volatility regime (below / above median trailing 5s mid vol).

Promotion requires ≥4/5 price buckets and ≥2 liquidity quartiles directionally
consistent.

## Multiple testing & stopping (frozen)

- Family = 5 primary endpoints (P1–P5). Holm-Bonferroni, `α = 0.05`.
- Milestones `N ∈ {40, 80, 160, 320}` clusters.
- PROMOTE iff `N ≥ 160`, Holm-adjusted net CI lower bound > 0, control stability.
- KILL iff net CI upper bound < 0 **at a milestone** (`N ≥ 40`).
- Otherwise CONTINUE.

## Holdout (frozen)

- **Development**: `venue_ts < 2026-09-22T00:00:00Z` (machinery validation only).
- **Forward**: `venue_ts >= 2026-09-22T00:00:00Z` (the only data counted toward
  promote/kill). The forward window is untouched until the first milestone.

## Research protection (frozen)

- Fixed batch (P1–P5); fixed transformations; fixed stopping; untouched holdout;
  no execution feedback into labels.
- No porting of Kalshi H1–H5 results as prior; this is an independent
  pre-registration.

## Status

- Frozen: `polymarket-microstructure-evidence-v1`
- Spec sha256: pinned by `scripts/freeze_polymarket_microstructure.py`
- Runtime (parser / evaluator): to be added to `RUNTIME_FILES` and re-frozen once built.