# v0.5 hypothesis backlog (observation-only, not preregistered)

Status: BACKLOG — candidate hypotheses for a future v0.5, collected from
literature and external research. None is wired into code, and none is evidence.
Each becomes actionable only after a v0.5 preregistration with H0, test, min
effect, min effective-N, FDR, and cluster rules.

## H-v5-1: Liquidation cascade → BTC 5-minute Up/Down market

- Source: Polymarket microstructure (observed in the Moon Dev bot ecosystem:
  `liq_cascade_chaser`); liquidation flow as an external pressure source.
- Hypothesis: BTC liquidation volume (perps/funding) predicts the subsequent
  BTC 5-minute Up/Down market beyond the market's own price.
- H0: liquidation flow is uninformative of the 5-min outcome conditional on spot.
- Family: A6/A8 (microstructure) + A10 (external information incorporation).
- Venue caveat: BTC 5-min markets are International (research-reference only for
  us; US venue may not list them). Reproduce-on-US discipline applies.
- Status: NOT_RUN. Blocked on liquidation/tick feed (on-chain or a clean source).

## H-v5-2: In-play sports overreaction

- Source: naive in-play retail flow ("30% discounted bid on the leader" pattern
  observed in the same ecosystem); implies a possible systematic overreaction.
- Hypothesis: in-play prices overreact to the current score/leader, then
  partially revert; the discounted-bid-on-leader flow is the symptom.
- H0: in-play price is a martingale w.r.t. the current game state (no revert).
- Family: A1 (calibration) + A6/A8 (microstructure/execution).
- Venue: US sports contracts (already collected; stratified under "sports").
- Status: NOT_RUN. Requires enough in-play book/fill history for a cluster-aware
  test; sports is where we measure execution, not where we assume calibration
  edge.

## Rule

Backlog entries are hypotheses to reproduce, not conclusions. They do not change
the frozen v0.4 methodology, the collection universe, or the eligibility gate.
