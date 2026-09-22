# v0.5 hypothesis backlog (observation-only, not preregistered)

Status: BACKLOG — candidate hypotheses for a future v0.5, collected from
literature and external research. None is wired into code, and none is evidence.
Each becomes actionable only after a v0.5 preregistration with H0, test, min
effect, min effective-N, FDR, and cluster rules.

## v0.5 External Research Principles (frozen before outcomes)

- Family-level error budget: let F = {F1,...,FK}. Preregister a hierarchical
  error budget alpha_family -> alpha_within-family (or hierarchical FDR), and
  declare the hierarchy before inspecting outcomes. Family selection itself is
  a multiple-testing step — protecting only the within-family level turns a
  broad shadow archive into a multiple-hypothesis generator.
- Interval-censored clocks: every source carries an uncertainty interval
  [t_i-, t_i+] (source timestamp precision, polling interval, publication
  delay, API latency, bucket size, revision behavior). Precedence "A before B"
  holds only when t_A+ < t_B-; overlapping intervals mean ordering UNRESOLVED,
  never "A led B".
- Add-data rule: add a source only when provenance + legality + clock semantics
  are good enough to support the hypothesis — NOT because an API exists.
- Attention data defaults to state (X_t = attention/regime), not a predictive
  trigger (Signal_t); a predictive use needs its own preregistered test.
- Page-diff ingestion is FALLBACK_PAGE_MONITOR: raw HTML hash changes are too
  noisy; a substantive-change classifier must gate anything downstream.
- Stop condition: keep the current six-source shadow bus. The rest of the
  public internet is a catalog of possible future instruments, not a backlog of
  collectors to build now.

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
- Canonical questions (not "who is informed"):
      (1) Which observable flow states consistently precede correct repricing?
      (2) When is incoming flow likely to adversely select us?
- Vocabulary: "informed flow" is an unobservable causal claim; use
  "adversely selecting flow" / "high-information-content flow" — the observable
  is trade -> subsequent favorable repricing. Luck vs information vs speed vs
  modeling are not distinguishable; we don't need to distinguish them.
- Identity: NOT necessary for toxicity modeling (the equity tape is mostly
  anonymous / partially attributed — MPID labels the member, not the beneficial
  owner). A flow-source label can carry info where legitimately available, but
  is not required. On US, no public account/wallet identity -> aggregate only.
- Primary outcome (observable): realized adverse-selection markout, net of
  spread:

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
  T_h | NoShock (needs no new data). Prediction-market advantage: we have both
  halves of ExternalInformation -> Flow -> Price, so E[M_h | Shock, OFI] vs
  E[M_h | NoShock, OFI] is estimable.
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

## Politician Disclosure (delayed public-disclosure source)

- Source: STOCK Act political-disclosure filings (House Clerk / Senate eFD as
  authoritative primaries; Capitol Trades / Quiver Quantitative / Quantgress /
  Congress Trading Monitor as enrichment/normalization only).
- Framing: a DELAYED public-disclosure research source, NOT an "insider signal".
  Qualifying transactions > $1,000 are reported within 30 days of notice, no
  later than 45 days after the transaction — fundamentally too lagged for
  same-minute execution. Disclosures are legal, public, lagged filings.
- Record fields: filer, owner (SELF/SPOUSE/DEPENDENT/JOINT/UNKNOWN), asset,
  transaction type, transaction date, filing date, disclosure lag days, amount
  range, source document, primary-source provenance.
- Freshness class: DELAYED (45-day lag) / HISTORICAL.
- Status: BLOCKED_PENDING_LEGAL_REVIEW (spec only; no ingestion until resolved).
- Three explicit blockers:
  1. LEGAL gate: House Clerk and Senate Ethics both state it is unlawful to
     obtain/use disclosure reports for "any commercial purpose" (except
     news/communications media); Senate adds civil-penalty exposure. Whether a
     trading-adjacent research pipeline is prohibited commercial use is a legal
     interpretation we must NOT make ourselves. Do not ingest until resolved.
  2. CLOCK gate: t0 is interval-censored, not point-observed:
         t_last-poll-without-filing < t_filing-public <= t_first-poll-with-filing
     Store [t_last-not-seen, t_first-seen]; never pretend t0 = t_first-seen.
  3. HYPOTHESIS gate: the economically meaningful public event is the DISCLOSURE,
     not the weeks-old underlying transaction. Future test:
         H0: E[dp | new public disclosure] = 0
     vs. a preregistered alternative of systematic post-disclosure
     reaction/underreaction. Not "old transaction -> future prediction".
     If prices moved BEFORE the disclosure became public, that is separate
     evidence about already-available information, not the disclosure signal.

## Pre-trade state vector (v0.5 spec, not code)

- These are NOT trade controls; they are state variables that change
  P(Y | I_t), the stale-quote probability, and the cost/risk of executing.
- X_t = {
    Market: p, spread, depth, OFI, volatility, markout;
    Cross-market: related contracts, external anchors;
    Information: latest events, source disagreement, freshness;
    Calendar: TTR, time-to-next-scheduled-event;
    Resolution: rule clarity, authoritative source, settlement risk;
    Portfolio: market/event/cluster/category exposure, drawdown, capital lockup;
    Infrastructure: feed health, clock health, API latency;
  }
- Decision rule:
    Trade  iff  Edge_conservative > Cost_execution + Cost_adverse_selection
                  + Risk_resolution + Risk_portfolio
  subject to all data being sufficiently fresh. Edge_conservative is the
  forward-validated edge, never the in-sample estimate.
- Resolution risk: Edge_economic != Edge_settlement-adjusted (ambiguity, source
  reliability, settlement delay, dispute risk). The resolution-definition hash
  is a binary "did the rule change" check; the risk quantity is a separate gap.
- Anchor residual (Residual_t = dlogit(P_PM) - f(dX_external)) must use
  interval-censored clock semantics, or it measures our latency as market lag.

## Future external-source catalog (not built)

Add only when a preregistered v0.5 family or an available US contract needs it:

- Real-time anchors: Alpaca IEX (real-time, one exchange — PARTIAL_EXCHANGE),
  crypto spot/books, stock/ETF proxies.
- Contract-specific primaries: Congress.gov (bills/actions/amendments),
  OpenFEC (campaign finance, usage-restricted), openFDA (drug/device/
  enforcement), USDA NASS Quick Stats (crops/livestock/prices).
- Physical-world: USGS earthquake GeoJSON (per-minute), hurricanes/drought/fire,
  transportation, power-grid.
- Fiscal: Treasury Fiscal Data (debt, auctions).
- Event calendars: earnings, votes, game starts, scheduled releases.

Rule: the six-source shadow bus stays untouched; these are candidates, not a
build queue. Preserve the "add data only when provenance + legality + clock
semantics support the hypothesis" principle.

## Rule

Backlog entries are hypotheses to reproduce, not conclusions. They do not change
the frozen v0.4 methodology, the collection universe, or the eligibility gate.
