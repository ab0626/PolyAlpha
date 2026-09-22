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

## Aggregate Flow Toxicity & Information Mediation

- Source: HFT order-flow toxicity (VPIN/PIN/Kyle) reframed for prediction markets.
- Core problem: **infer the information content and adverse-selection risk of
  anonymous aggregate flow** — NOT "identify informed traders" (per-user skill
  is blocked on US: no public account/wallet identity).
- Primary outcome (observable, not a latent state): realized adverse-selection
  markout in log-odds, net of spread:

      M_j(h) = s_j [ l(p_{t+h}) - l(p_t) ],  l = logit

  Positive M_j = aggressor traded into subsequent repricing = passive
  counterparty adversely selected. Toxicity T_h = E[M(h) | X_t].
- Honest flow metric (not "VPIN", not "probability of informed trading"):

      Q_k = sum_j s_j q_j,  I_k = |Q_k| / sum_j q_j,  VI_t = (1/N) sum I_k

  I_k = 0.90 means "flow is one-sided", not "90% informed". Interpretation is
  earned empirically.
- Three toxicity types: (1) trade toxicity = realized markout; (2) book
  toxicity = OFI / queue / cancel imbalance / depth shock (note: event-level
  OFI needs add/cancel events — PARTIALLY_GATED; snapshot queue imbalance is
  available); (3) information-conditioned toxicity = T_h | EventShock vs
  T_h | NoShock (needs no new data — EventShock + flow + price already frozen).
- Conditioning state X_t: { OFI, signed trade imbalance, queue imbalance, λ,
  spread, depth, trade intensity, recent markout, TTR τ, probability p (in
  log-odds), event state }.
- Two separate hypotheses, never conflated:
      H_cost  : X_t -> future adverse selection
      H_alpha : X_t -> future price direction (net of cost, OOS)
- Benchmark hierarchy (no ML first), add only if ΔOOS(M_i, M_{i-1}) > 0:
      M0 spread/depth, M1 +OFI, M2 +signed imbalance, M3 +λ, M4 +VI, M5 +event/TTR/price-state.
- Priority order for construction: markout > OFI > queue/depth/cancel >
  trade-flow imbalance > Kyle λ > VPIN > PIN. PIN last: its latent/Poisson
  assumptions are uncomfortable when the information event time is often known.
- Status: NOT_RUN (v0.5 candidate). Does NOT modify frozen A6/A8.

## Rule

Backlog entries are hypotheses to reproduce, not conclusions. They do not change
the frozen v0.4 methodology, the collection universe, or the eligibility gate.
