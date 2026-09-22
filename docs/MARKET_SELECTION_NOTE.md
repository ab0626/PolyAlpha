# Market selection — research note (not methodology)

Status: NOTE (research direction, not a change to frozen methodology).
Date: 2026-09-22.

## Principle

Do not pick markets to trade yet. Alpha is UNKNOWN; forward evidence is
~5/30 days, 0/200 settled. Market selection is an **output** of the research,
not an input. Choosing markets now would be retrospective selection.

## Criteria

1. Executable depth (US Polymarket is newer/thinner than International → execution/adverse-selection is first-order).
2. Information structure (clean t0 vs unscheduled vs in-play).
3. Literature: favorite-longshot is documented in **Crypto and Politics**, absent in **Sports** (Cardozo).

## Research priority (what to measure, in order)

1. **Macro (CPI/FOMC/NFP)** — clean t0, authoritative, already collected → A10, A2.
2. **Politics / elections** — FLB documented, rich cross-market → A1, A4.
3. **Crypto-linked (BTC/ETH)** — FLB documented, external spot anchor → A1, A4/A9.
4. **Sports** — most efficient; use for **microstructure/execution**, not calibration → A6, A8.

## Gap

Current collection is sports-heavy (NBA/MLB) + macro. **Politics/election and
crypto contracts are not collected**, yet those are where the calibration
anomaly is documented. Extend collection coverage (not methodology) to
politics + crypto before the research can answer market selection.

## Decision rule for "what to trade"

Trade only what survives the preregistered gates:

    E[PnL_OOS - fees - spread - slippage - impact] > 0

at executable depth, cluster-independent, FDR-controlled, on the forward
holdout.

## Venue

US Polymarket (executable target). Kalshi is a sibling US-accessible venue in
`venue.py` — a decision for after evidence exists, not now.
