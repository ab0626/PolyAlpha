# Microstructure Evidence V1 — Fast-Horizon Research Contract (frozen)

Freezes the fast-horizon (microstructure) research track **before** the book
reconstruction or label extractor is built. This track is **separate from and
does not wait for** the frozen terminal-outcome gate (30 days / 200 settled
markets / 80% coverage). The terminal gate is unchanged for
calibration / favorite-longshot / terminal-EV families.

Append-only. Any change to a frozen definition = a new version section + a new
freeze (`scripts/freeze_microstructure.py`). This contract pins the *research
semantics*; the runtime (book reconstruction, extractor, evaluator) is pinned
separately once it is built.

## Objective

Is there repeatable, **cost-adjusted** short-horizon structure in the Kalshi WS
order book and trade flow that survives day/session-level clustering and a
pre-registered multiple-testing + stopping discipline?

`PENDING_EVIDENCE` on the terminal gate means "not promoted to capital yet"; it
must not stop this track. This track is the fast-feedback loop.

## Venue & lineage (frozen)

- Kalshi **production** WebSocket, scoped energy lineage:
  `data/energy/kalshi/ws/YYYY-MM-DD.jsonl`.
- Message types consumed: `orderbook_snapshot` (initial/full book),
  `orderbook_delta` (price-level size deltas), `trade` (taker-sided fills),
  `ticker` (BBO fallback only).
- Every derived state carries `venue_ts` (exchange `ts_ms`), `receive_ts`
  (`received_at_ns`), `session_id`, and `session_seq`.

## Canonical state (frozen)

The extractor emits one canonical record per book/trade event:

```
BookState:
  venue_ts      int     exchange ts_ms of the triggering message
  receive_ts    int     received_at_ns (local receive, latency attribution)
  ticker        str     market_ticker
  best_bid      Dec     best YES bid            (None if empty)
  best_ask      Dec     best YES ask            (None if empty)
  bid_depth_k   Dec     sum of top-k YES-bid sizes
  ask_depth_k   Dec     sum of top-k YES-ask sizes
  mid           Dec     (best_bid + best_ask) / 2   (None if either side empty)
  microprice    Dec     volume-weighted YES mid (see H2)
  imbalance     Dec     (bid_depth_k - ask_depth_k) / (bid_depth_k + ask_depth_k)
  spread        Dec     best_ask - best_bid
  session_id    str
  seq           int     session_seq
  gap_clean     bool
```

`k = 5` (frozen). Prices are YES-token probability dollars in `(0,1)`.

**Book reconstruction contract (frozen):**
- Reconstruct per `market_ticker`, ordered by `(session_id, session_seq)`.
- Seed from the most recent `orderbook_snapshot`; then apply each
  `orderbook_delta` in sequence order: `size(price, side) += delta_fp`; drop the
  level when `size <= 0`.
- `gap_clean = true` iff there has been no `session_seq` discontinuity (>1) since
  the last snapshot and no `session_id` change without a fresh snapshot.
- Fixed-point scaling of `delta_fp`/`count_fp` is pinned by the runtime freeze.

## Research universe (frozen)

A `market_ticker` contributes eligible windows only when, over the window:

1. **Continuously observed**: at least one `orderbook_snapshot` and no
   `gap_clean = false` state within the window.
2. **Liquid**: median top-of-book spread ≤ `0.02` and median combined top-5
   depth (`bid_depth_k + ask_depth_k`) ≥ `100` (dollars), and ≥ `20` trades in
   the window.
3. **No gap contamination**: any window crossing a reconnect (`session_id`
   change) or a `session_seq` gap is excluded (never silently spliced).

These are v1 a-priori constants, chosen **before** any label was mined. They may
only be changed by a new version section.

## Labels (frozen)

Signed YES-mid markout. For an observation at time `t` with directional sign
`s ∈ {+1, −1}` (`+1` = predicts mid **rise**):

```
markout_h(t) = s * ( mid(t+h) - mid(t) )          # raw, probability units
net_h(t)     = markout_h(t) - 0.5*spread(t+h) - 0.07 * mid(t+h)   # cost-adjusted
```

- `mid` is the reconstructed-book YES mid (ticker BBO fallback only when the
  book is not reconstructable; fallback flagged by `source`).
- `t+h` quote = the nearest `BookState` by `venue_ts` within the horizon
  tolerance. Missing quote ⇒ label is `unavailable` (never zero).
- `0.07` = Kalshi standard taker rate; `0.5*spread` = half-spread exit cost
  (both frozen).

Horizons (frozen):

```
primary:   100ms, 1s, 5s, 30s, 300s
secondary: 1800s, terminal (terminal recorded opportunistically only;
           terminal promotion is the terminal gate's domain, not this track)
tolerance: 100ms±50ms, 1s±200ms, 5s±500ms, 30s±2s, 300s±10s, 1800s±30s
```

## Independence (frozen)

- The **inference cluster is a (market_ticker, operating_day)** — a "market-day".
- Observations within a market-day are **never** treated as independent.
- All CIs are day-clustered block bootstrap (`seed = 42`, `n_boot = 2000`).
- Effective sample size = number of clusters, **never** event count.

## Hypotheses (frozen, first batch)

Five hypotheses. Each has exactly one pre-registered **primary horizon**; the
full horizon set is computed and reported, but only the primary horizon enters
promotion/rejection.

**H1 — order-book imbalance → markout.** `primary = 1s`.
`imbalance` (canonical field) predicts mid direction: `s = sign(imbalance)`.
Test: `E[ markout_1s ] > 0` (raw) and `E[ net_1s ] > 0`.

**H2 — microprice displacement → markout.** `primary = 5s`.
`microprice = (best_ask*bid_depth_k + best_bid*ask_depth_k) / (bid_depth_k+ask_depth_k)`.
Displacement `d = microprice - mid`; `s = sign(d)`. Test: `E[ markout_5s ] > 0`
and `E[ net_5s ] > 0`.

**H3 — aggressive trade flow → continuation.** `primary = 5s`.
YES-direction sign of a trade: `+1` if (`taker_side=yes` ∧ `taker_book_side=ask`)
or (`taker_side=no` ∧ `taker_book_side=bid`), else `−1`. Aggregate signed flow
over the trailing `1s`: `flow = Σ sign_i * count_fp_i`. `s = sign(flow)`.
Test: `E[ markout_5s ] > 0` (continuation; a reliably negative effect = reversion).

**H4 — maker-taker adverse selection (size-scaled).** `primary = 1s`.
For each trade, `maker_markout = -s * (mid(t+h) - mid(t))` (the passive side is
opposite the aggressor). Adverse selection ⇒ `E[maker_markout] < 0`, monotone in
aggressor size. Primary test: `E[ maker_markout ] < 0` **and**
`E[ maker_markout | |count_fp| top tercile ] < E[ maker_markout | bottom tercile ]`.

**H5 — liquidity withdrawal → movement.** `primary = 30s`.
`Δask = ask_depth_k(t) - ask_depth_k(t - 5s)`, `Δbid = bid_depth_k(t) - bid_depth_k(t - 5s)`.
`d = Δask - Δbid`; `s = sign(d)` (ask-side withdrawal ⇒ upward pressure).
Test: `E[ markout_30s ] > 0` and `E[ net_30s ] > 0`.

## Controls (frozen)

Report every primary test **conditioned on** (stability requires the sign of the
point estimate to be consistent across):

- spread bucket (below / above median),
- depth bucket (below / above median combined top-5 depth),
- price bucket (mid in `[0,0.2) [0.2,0.4) [0.4,0.6) [0.6,0.8) [0.8,1]`),
- time-to-expiry (before / after the operating-day midpoint),
- realized volatility (below / above median of trailing 5s mid vol),
- market family (series ticker prefix).

Promotion requires the effect to be directionally consistent in ≥ 4 of 5 price
buckets and ≥ 2 of the observed market families.

## Multiple testing & stopping (frozen)

- Family size = 5 primary tests (one per hypothesis). **Holm-Bonferroni** at
  `α = 0.05` family-wise across the 5 primary endpoints.
- Secondary horizons and control-conditioned slices are **descriptive only** —
  they never trigger promotion or rejection by themselves.
- Fixed evaluation milestones at `N_clusters ∈ {40, 80, 160, 320}`.
- **PROMOTE** (candidate) iff `N_clusters ≥ 160` and the Holm-adjusted net CI
  lower bound > 0 and the control-stability rule holds.
- **KILL** iff the net CI upper bound < 0 at any milestone (pre-committed
  futility).
- Otherwise **CONTINUE**. No early stopping other than the futility rule.

## Holdout (frozen)

- **Development window**: `venue_ts < 2026-10-21T00:00:00Z`. Used **only** to
  validate book reconstruction, label correctness, and machinery. Never for
  promotion/rejection.
- **Forward window**: `venue_ts >= 2026-10-21T00:00:00Z`. The **only** data that
  counts toward `N_clusters` and any PROMOTE/KILL decision.
- The forward window is untouched until the first milestone evaluation.

## Research protection (frozen)

- Fixed hypothesis batch (H1–H5). No new hypotheses in v1 without a new version.
- Fixed feature transformations (formulas above).
- Fixed stopping rule and fixed α-spending (Holm-Bonferroni, futility rule).
- Untouched holdout (forward window).
- No execution feedback into labels: our own venue fills (live-calibration)
  are excluded from trade-flow features and never affect label construction.

## Promotion / rejection (frozen)

```
PROMOTE candidate  ->  forward-shadow confirmation ->  manual live-canary ->  scaling
KILL               ->  rejected (recorded, append-only)
CONTINUE           ->  keep accumulating to the next milestone
```

A PROMOTE in this track is a **research claim**, not a capital allocation. Any
capital deployment still requires the existing manual arm + kill-switch + frozen
execution path, and (for terminal-outcome families) the terminal gate.

## Status

- Frozen: `microstructure-evidence-v1`
- Spec sha256: pinned by `scripts/freeze_microstructure.py`
- Runtime (book reconstruction / extractor / evaluator): **not yet frozen** —
  to be added to `RUNTIME_FILES` and re-frozen under a new version once built.