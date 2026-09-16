# PolyAlpha US — Pre-registered v2 Research Agenda

## Status and provenance

This document is a **pre-registration**: it fixes, before results are analyzed,
the hypotheses, data requirements, decision rules, and stopping criteria for
the first forward-evidence analysis of the Polymarket US lineage.

**Honest timing statement.** This pre-registration was created **after**
`US_COLLECTION_RUNNING` began (raw collection was already live), but **before**
model-performance analysis, **before** any outcome-conditioned tuning, and
while the system held:

```
phase:                 US_COLLECTION_RUNNING
model performance:     LOCKED
alpha:                 UNKNOWN
research_logic_sha256: unchanged since freeze
primary feed:          polymarket_us_retail_rest
authenticated feeds:   NOT ACTIVE
```

Because collection had already started before this document was written, this
is a *partially* ex-ante registration: the decisions below were fixed without
reference to model outcomes, resolved outcomes, or edge measurements, but not
before the first raw byte. It is registered here with that limitation stated
explicitly rather than implied.

**Immutability.** This agenda is committed once. Future evaluation reports must
record this document's commit hash. New hypotheses are added by appending a new
version section (v2.1, v2.2, ...) — never by silently editing v2.0.

---

## Scope of the forward sample

The forward evidence is the US Retail REST lineage under
`data/us/retail/raw/...`, collected since `REAL_DATA_START_US` (2026-09-16).

Captured per observation:
- **Discovery** (`polymarket_us_retail_markets`): slug, category, question,
  `marketSides` (outcome-side identities — participant/team/candidate names,
  **not** trader identities), `orderPriceMinTickSize`, lifecycle timestamps
  (`createdAt`, `startDate`, `endDate`, `updatedAt`), `marketType`,
  `sportsMarketType`.
- **Book** (`polymarket_us_retail_book`): full depth `bids`/`offers` (spread,
  depth, price-impact proxy), stats (`currentPx`, `lastTradePx/Qty`,
  `sharesTraded`, `notionalTraded`, `openInterest`, `state`,
  `transactTime`).
- **Events** (`polymarket_us_retail_events`): parent-event structure for
  clustering.
- **Settlement** (`polymarket_us_retail_settlement`): final outcome (0/1).

**Explicitly NOT captured / deferred:** aggressor-signed trades, participant /
account identity, maker/taker split. These require authenticated feeds
(`polymarket_us_retail_ws`, Direct Exchange) that are currently
`NOT ACTIVE`. Families that depend on them are deferred, not partially tested.

---

## Sampling hierarchy (effective N)

Cardozo-style aggregation bias is prevented by an explicit hierarchy. Raw
market observations are never treated as independent.

```
raw market observations
    ↓  aggregate per (slug, price bucket, spread regime, ttr bucket)
market-level observations
    ↓  group child markets under their parent event
parent-event clusters
    ↓  dedupe overlapping events (e.g. same game, same outcome family)
independent-ish event clusters
    ↓  apply the family's independence rule
effective N
```

Effective N for a hypothesis = number of **independent-ish event clusters**, not
number of raw rows. A parent event with many child markets contributes one
cluster's worth of information, not one observation per child.

---

## Global decision rules

These apply to every hypothesis unless the family overrides them.

- **Train / eval split rule:** temporal split. Calibrate/tune on
  resolved events with `endDate < SPLIT_DATE`; evaluate only on
  `endDate >= SPLIT_DATE`. SPLIT_DATE is fixed now: the UTC date 45 days after
  the first `REAL_DATA_START_US` marker, or 2026-11-01, whichever is earlier.
  No random split, no repeated resampling on the eval set.
- **Multiple-testing correction:** Benjamini–Hochberg FDR at 10%, applied
  across all hypotheses and all sub-groups within a family evaluated on the
  same eval set.
- **Confidence / significance rule:** an effect must survive the FDR-adjusted
  p-value < 0.10 threshold **and** the minimum effect size of the family.
  Significance alone is insufficient.
- **Minimum effective N (global floor):** no family is evaluated until the
  eval set contains ≥ 200 independent-ish event clusters. A family may set a
  higher floor.
- **What counts as "supported":** the pre-specified directional effect is
  significant after FDR correction, meets the minimum effect size, holds on
  the held-out temporal eval split, and survives the family's dependence rule.
- **What counts as "not supported":** any of the above fails — non-significant,
  wrong direction, effect below the minimum, or not robust to the cluster rule.
  "Not supported" is a valid, informative outcome; it is not a failure of the
  experiment.

---

## Family 1 — Calibration residual (Manski / Wolfers–Zitzewitz)

**Motivation:** market price is not automatically equal to mean probability
belief. Model the mapping price → empirical outcome frequency.

- **HYPOTHESIS:** empirical outcome frequency differs from quoted probability
  conditional on price bucket, category, spread regime, liquidity proxy, and
  time-to-resolution. The residual surface is non-flat.
- **DATA REQUIRED:** discovery (category, slug), book (spread, depth,
  notionalTraded proxy), settlement (outcome).
- **UNIT OF ANALYSIS:** market-level observation = (slug, price bucket at a
  fixed TTR bucket) → final outcome.
- **CLUSTER / DEPENDENCE RULE:** independent-ish event clusters (parent-event
  grouping; one event = one cluster).
- **TRAIN / EVAL SPLIT RULE:** global temporal split.
- **PRIMARY METRIC:** expected calibration error (ECE-style) of the residual
  surface vs. flat (price = probability) baseline; per-cell mean residual.
- **NULL HYPOTHESIS:** residual surface is flat; ECE(residual model) ≥
  ECE(price-as-probability).
- **MINIMUM EFFECT SIZE:** ECE reduction ≥ 0.5 percentage points, or a
  per-cell mean residual magnitude ≥ 1 percentage point in ≥ 1 cell.
- **CONFIDENCE / SIGNIFICANCE RULE:** global FDR rule.
- **MULTIPLE-TESTING CORRECTION:** global FDR.
- **MINIMUM EFFECTIVE N:** 200 independent-ish event clusters.
- **SUPPORTED:** residual model beats flat baseline on eval, FDR-significant,
  ≥ min effect.
- **NOT SUPPORTED:** otherwise.

## Family 2 — Favorite–longshot bias (Cardozo et al.)

**Motivation:** low-probability contracts may underperform / be overbet, but
the effect varies by category and parent-event structure.

- **HYPOTHESIS:** realized outcome rates differ from quoted probabilities by
  price bucket, with the shape varying by category and parent-event structure.
- **DATA REQUIRED:** discovery (category), book (price), settlement (outcome).
- **UNIT OF ANALYSIS:** parent-event cluster, price bucket.
- **CLUSTER / DEPENDENCE RULE:** **parent-event clustering is mandatory** —
  child markets of one event are aggregated into one cluster; never counted as
  independent. Category sub-analysis uses the same rule within each category.
- **TRAIN / EVAL SPLIT RULE:** global temporal split.
- **PRIMARY METRIC:** mean residual (realized − quoted) per price bucket and
  per category; longshot underperformance specifically.
- **NULL HYPOTHESIS:** residual is zero in every bucket × category cell.
- **MINIMUM EFFECT SIZE:** ≥ 1 percentage point mean residual in at least one
  price bucket, measured at the cluster level.
- **CONFIDENCE / SIGNIFICANCE RULE:** global FDR.
- **MULTIPLE-TESTING CORRECTION:** global FDR across all bucket × category
  cells.
- **MINIMUM EFFECTIVE N:** 200 independent-ish event clusters overall; ≥ 20
  clusters per category before that category is reported.
- **SUPPORTED:** FDR-significant, ≥ min effect, at cluster level, on eval.
- **NOT SUPPORTED:** otherwise. A directionally different result by category is
  a real finding, not an excuse to subset post hoc.

## Family 3 — Microstructure / price-impact proxy (Glosten–Milgrom, Kyle)

**Motivation:** shallow depth, wide spreads, book imbalance, and large quote
moves may predict subsequent repricing or worse execution.

**Scope note:** with public REST books only, this is a **price-impact / depth
proxy**, not a true signed-order-flow Kyle λ (aggressor-signed trades are not
observed). The λ measured here is Δmid / depth-sensitivity proxy. True
signed-flow Kyle λ is deferred (Family 7).

- **HYPOTHESIS:** book states with shallow depth / wide spread / one-sided
  imbalance / large quote moves predict subsequent mid repricing (continuation
  or reversal) more than deep, tight, balanced books do.
- **DATA REQUIRED:** book depth (bids/offers), spread, imbalance, subsequent
  book snapshots.
- **UNIT OF ANALYSIS:** book-state observation (slug, timestamp) → subsequent
  Δmid over a fixed horizon (e.g. 60s, 300s).
- **CLUSTER / DEPENDENCE RULE:** independent-ish event clusters; within a
  cluster, consecutive book states are auto-correlated — use cluster-level
  aggregation, not row-level t-stats.
- **TRAIN / EVAL SPLIT RULE:** global temporal split.
- **PRIMARY METRIC:** mean Δmid after deep vs. shallow states; price-impact
  proxy magnitude per depth/spread bucket.
- **NULL HYPOTHESIS:** subsequent repricing is independent of book state.
- **MINIMUM EFFECT SIZE:** ≥ 0.5 tick mean Δmid difference between extreme
  depth/spread buckets.
- **CONFIDENCE / SIGNIFICANCE RULE:** global FDR.
- **MULTIPLE-TESTING CORRECTION:** global FDR.
- **MINIMUM EFFECTIVE N:** 200 independent-ish event clusters.
- **SUPPORTED:** significant, ≥ min effect, on eval.
- **NOT SUPPORTED:** otherwise.

## Family 4 — Cross-market consistency (Hanson / no-arbitrage)

**Motivation:** related markets must satisfy logical/probabilistic constraints
(e.g. monotonicity: P(BTC > 120k) ≤ P(BTC > 100k)).

- **HYPOTHESIS:** executable prices violate logical constraints by more than
  transaction cost on identifiable, defensible event-graph relationships.
- **DATA REQUIRED:** discovery (slug, question, category, marketSides),
  book (executable prices).
- **UNIT OF ANALYSIS:** constraint instance (pair/tuple of related slugs) →
  violation magnitude in executable price terms.
- **CLUSTER / DEPENDENCE RULE:** independent-ish event clusters; constraints
  within one event family cluster together.
- **TRAIN / EVAL SPLIT RULE:** global temporal split.
- **PRIMARY METRIC:** violation frequency and mean magnitude net of estimated
  round-trip cost.
- **NULL HYPOTHESIS:** no violation exceeds transaction cost.
- **MINIMUM EFFECT SIZE:** violation magnitude ≥ estimated round-trip cost
  (spread + fee) on ≥ 1% of constraint instances.
- **CONFIDENCE / SIGNIFICANCE RULE:** global FDR.
- **MULTIPLE-TESTING CORRECTION:** global FDR.
- **MINIMUM EFFECTIVE N:** 200 independent-ish event clusters.
- **SUPPORTED:** significant violations on eval, ≥ min effect.
- **NOT SUPPORTED:** otherwise.
- **CRITICAL RULE:** only defensible relationships are eligible. **No
  hand-labeled hindsight relationships added after outcomes are known.** If a
  relationship cannot be justified from the event graph alone, it is excluded
  from this family.

## Family 5 — Maker/taker economics (Akey et al.)

**Status: DEFERRED.**

- **Reason:** requires aggressor-signed trade flow and maker/taker attribution,
  which the current public REST feed does not provide. Marked deferred until
  authenticated trade flow exists (retail WS or Direct Exchange).
- **When it unlocks:** re-open under a new version section (v2.1+) with the
  same decision-rule template, on the then-current eval split. The deferral is
  permanent until that feed is active; it is not tested with public REST data.

## Family 6 — Informed-flow leadership (Gómez-Cram et al.)

**Status: DEFERRED.**

- **Reason:** requires participant/account identity and order-flow attribution,
  unavailable in the public REST lineage and not derivable from `marketSides`
  (which are outcome-side identities, not trader identities). Marked deferred
  until authenticated participant/order-flow data exists.
- **When it unlocks:** re-open under a new version section, same template.
  Never approximated from public REST.

---

## Evaluation boundary (forward-evidence stopping rule)

The first forward-evidence report fires only when **all** of:

1. **Resolved markets:** ≥ 200 unique settled markets in the eval set.
2. **Independent-ish event clusters:** ≥ 200 clusters meeting the sampling
   hierarchy.
3. **Calendar:** ≥ 30 days elapsed since `REAL_DATA_START_US`
   (2026-09-16).

The evaluation report must record this document's commit hash, the SPLIT_DATE,
the effective N per family, and the decision for each hypothesis.

---

## Version history

- **v2.0** — initial pre-registration (this document). Created at commit
  `f543f587ea2288b9d6d4aa7a01b68630d225c8a7`, while MODEL PERFORMANCE: LOCKED,
  ALPHA: UNKNOWN, before any outcome-conditioned analysis.

---

# v2.1 addendum

Appended after v2.0 was committed. **The v2.0 body above is unchanged.** This
addendum adds one family, one refinement, and one gated sizing layer. It was
written while MODEL PERFORMANCE: LOCKED and ALPHA: UNKNOWN, before any
outcome-conditioned analysis.

## Evidence-confidence labeling

To prevent a preprint being treated later as settled law, evidence is labeled:

- **[PEER]** peer-reviewed.
- **[WP]** working paper / preprint — hypothesis-generating only; any family
  resting solely on [WP] evidence carries a higher burden (see per-family note).

Family provenance:
- Family 1 (calibration): [PEER] Manski (2006); Wolfers–Zitzewitz;
  **Page & Clemen (2013)**.
- Family 2 (favorite–longshot): [PEER] Snowberg–Wolfers (2010); Ottaviani–
  Sørensen. [WP] Cardozo & Rivero-Wildemauwe (2026).
- Family 3 (microstructure): [PEER] Glosten–Milgrom (1985); Kyle (1985).
- Family 4 (cross-market): [PEER] Hanson (LMSR / combinatorial aggregation).
- Family 5 (maker/taker): [WP] Akey et al. (2026); Bürgi–Deng–Whelan (2026).
- Family 6 (informed flow): [WP] Gómez-Cram et al. (2026).
- Family 7 (shock continuation/reversion): [PEER] Hanson–Oprea–Porter (2006);
  [PEER] Kyle (1985).
- Sizing layer: [PEER] Kelly (1956).

## Family 1 refinement — time-to-resolution miscalibration (Page & Clemen 2013)

**Rationale:** miscalibration is not constant in time; Page & Clemen find it
strengthens farther from expiry. This makes TTR an explicit conditioning axis,
not merely a covariate.

- **HYPOTHESIS:** the calibration residual (empirical − quoted) is
  systematically larger in magnitude farther from resolution, and its sign
  structure differs across TTR buckets.
- **DATA REQUIRED:** discovery (category, `endDate`), book (price), settlement.
- **UNIT OF ANALYSIS:** market-level observation at a fixed TTR bucket
  (TTR = `endDate` − observation time).
- **CLUSTER / DEPENDENCE RULE:** independent-ish event clusters; TTR buckets
  within one cluster are not independent.
- **TRAIN / EVAL SPLIT RULE:** global temporal split.
- **PRIMARY METRIC:** |mean residual| per TTR bucket; monotonicity of
  |residual| in TTR.
- **NULL HYPOTHESIS:** |residual| is constant across TTR buckets.
- **MINIMUM EFFECT SIZE:** ≥ 1 percentage point difference in |mean residual|
  between the nearest and farthest TTR buckets.
- **CONFIDENCE / SIGNIFICANCE RULE:** global FDR.
- **MULTIPLE-TESTING CORRECTION:** global FDR across TTR buckets × category.
- **MINIMUM EFFECTIVE N:** 200 independent-ish event clusters.
- **SUPPORTED:** significant, ≥ min effect, monotone-in-TTR on eval.
- **NOT SUPPORTED:** otherwise. This is a refinement of Family 1; it does not
  create a separate tradeable alpha, it conditions Family 1.

## Family 7 — Shock continuation vs. reversion (Hanson–Oprea–Porter; Kyle)

**Rationale:** large price moves are heterogeneous. Moves confirmed by related
markets, depth response, and volume are information-like; isolated moves into
thin books that replenish are liquidity shocks. The family classifies, then
tests continuation vs. reversion.

- **HYPOTHESIS:** large Δmid events that are confirmed by related-market moves
  and depth/volume response continue; unconfirmed moves into thin books revert.
- **DATA REQUIRED:** book time series (Δmid, depth, spread), related slugs via
  parent-event/event-graph linkage, `sharesTraded` delta as a volume proxy.
- **UNIT OF ANALYSIS:** shock event (slug, timestamp, Δmid magnitude) →
  subsequent Δmid over a fixed horizon (e.g. 60s, 300s).
- **CLUSTER / DEPENDENCE RULE:** independent-ish event clusters; overlapping
  shock windows within a cluster aggregate to one observation.
- **TRAIN / EVAL SPLIT RULE:** global temporal split.
- **PRIMARY METRIC:** mean signed subsequent Δmid for confirmed vs.
  unconfirmed shocks (continuation coefficient and reversion coefficient).
- **NULL HYPOTHESIS:** subsequent Δmid is independent of confirmation status.
- **MINIMUM EFFECT SIZE:** ≥ 0.5 tick mean Δmid difference between confirmed
  and unconfirmed shocks.
- **CONFIDENCE / SIGNIFICANCE RULE:** global FDR.
- **MULTIPLE-TESTING CORRECTION:** global FDR.
- **MINIMUM EFFECTIVE N:** 200 independent-ish event clusters.
- **SUPPORTED:** significant, ≥ min effect, on eval.
- **NOT SUPPORTED:** otherwise.
- **CONFIDENCE NOTE:** "confirmed" must be defined by a pre-specified rule
  (e.g. related-market |Δmid| ≥ threshold within the same window), fixed now,
  not chosen after seeing which classification would have been profitable.

## Sizing and portfolio layer (Kelly 1956) — GATED

**Status: GATED.** Applies only after at least one signal family is
**SUPPORTED** under the decision rules above. Pre-registered now so sizing
cannot be rationalized post hoc.

- **RULE:** position size = fractional Kelly on the family's calibrated edge,
  then apply, in order: uncertainty haircut (model/parameter uncertainty),
  correlation haircut (within parent-event cluster), liquidity cap (size ≤
  fraction of displayed depth), portfolio cap. Target band 0.10–0.25 Kelly.
- **PROHIBITED:** full Kelly; sizing on an unsupported or non-significant
  edge; sizing on a [WP]-only family without the higher burden met.
- **UNIT:** per parent-event cluster exposure, not per child market.
- **This layer does not create an alpha and is never evaluated as one.** It is
  the mapping from a validated edge to capital, and it stays inactive until
  an edge exists.

## v2.1 version history entry

- **v2.1** — addendum: evidence-confidence labeling, Family 1 TTR refinement
  (Page & Clemen 2013), Family 7 shock continuation/reversion, gated Kelly
  sizing layer. v2.0 body unchanged. Written while MODEL PERFORMANCE: LOCKED,
  ALPHA: UNKNOWN.