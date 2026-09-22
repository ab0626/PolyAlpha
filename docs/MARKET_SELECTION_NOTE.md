# Market selection — research posture (frozen, not methodology)

Status: NOTE — research-universe posture, not a change to frozen v0.4 methodology.
Date: 2026-09-22.

## Principle

Do not choose markets to trade yet. Alpha is UNKNOWN. Market selection for
execution is an **output** of forward research, never an input.

The eventual gate, unchanged:

    E[PnL_OOS - fees - spread - slippage - impact] > 0

at executable depth, cluster-independent, FDR-controlled, on the forward holdout.

## Venue discipline

Polymarket US is a distinct CFTC-designated contract market (QCX LLC,
designated 2025-07-09). Offshore/historical Polymarket findings — including the
Cardozo favorite-longshot result (588M trades, USDC, pseudonymous wallets) — are
**preregisterable hypotheses to reproduce on the U.S. venue**, not evidence that
the U.S. venue has the same anomaly. The participant population is structurally
different (KYC vs pseudonymous), and FLB is a behavioral bias that lives in a
population.

## No category ranking

Do NOT rank categories (e.g. "macro > politics > crypto"). Instead use a
coverage matrix: collect enough of each class to test its mechanism, without
declaring where alpha exists.

| Market class | Reason to collect | Candidate families |
|---|---|---|
| Scheduled macro | authoritative t0 | A10, A2 |
| Political/election | related-contract structure; offshore FLB hypothesis | A1, A4 |
| Crypto-linked | external continuous price anchor | A4, A9 |
| Sports | frequent events, rich in-play state | A6, A8 |
| Energy/weather/economic | primary sources now in shadow bus | A10 (external) |
| Other liquid contracts | control population (prevents category-selection bias) | baseline |

"Sports shows no FLB in the offshore dataset" implies a **weaker prior for that
one family**, not general sports efficiency.

## Universe rule (frozen before outcomes)

    U_research = U_required_releases  U  U_activity  U  U_category-stratified

The strata, the liquidity thresholds, and the taxonomy are pinned in
`config/liquidity_strata.json` BEFORE inspecting category outcomes. Activity
ranking must never displace required or stratified contracts, and paper-priors
must never displace activity (the two symmetric selection biases).

## Liquidity is measured, not asserted

"Polymarket US is thinner than International" is an empirical claim. Measure it
(`scripts/liquidity_by_category.py`): per stratum, median spread, depth +/-1c
and +/-5c, VWAP impact(q), trades/hour, quote updates/hour, time-since-last-
trade, plus sample support N_markets / N_market-hours / N_fills. A category
with great median depth over 3 markets is not equivalent to one over hundreds
of market-hours.

## Eligible markets (computed after, never before)

    Eligible = MeasuredLiquid  ∩  FamilySupportSurvives  ∩  ExecutableNetEdge > 0

`MeasuredLiquid` is defined by the frozen thresholds in `liquidity_strata.json`,
applied only after the liquidity report exists — the report itself is
descriptive, never a ranking of what to trade.

## Kalshi

Kalshi is a separate CFTC-designated contract market (designated 2020-11). It is
a potential external benchmark or a separately preregistered second-venue
replication — NOT silently mixed into the Polymarket US experiment.

## Flow

    broaden collection → stratify by class → test preregistered families
    → forward validation → execution validation → then determine eligible markets
