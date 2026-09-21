# PolyAlpha — Pre-registered Research Agenda v3

**Status:** PRE-REGISTRATION (hypotheses fixed before outcome-conditioned analysis)
**Supersedes/extends:** `docs/RESEARCH_AGENDA.md` (v2.0/v2.1/v2.2). v2 bodies are unchanged.
**Alpha status:** UNKNOWN. **Model performance:** LOCKED during forward collection.

This version incorporates the 2025–2026 Polymarket empirical literature and the
marginal-risk work (`src/polyalpha/marginal_risk.py`). It fixes, before any
outcome-conditioned analysis, the hypotheses, data requirements, decision rules,
and gates for the next stage of the research program.

**v3.1 venue decision (2026-09-21):** the operator is US-based and the system is
US-only. Families **A7 (informed wallet flow)** and **A9 (external fair-value
anchors)** are therefore **dropped** (`NOT_RUN`): A7 needs public wallet identity
(absent on the CFTC venue) and A9 needs cross-venue data. They remain documented
below for completeness but are not collectable on the production venue.

---

## 0. Alpha decomposition

The naive `alpha = p_hat - p` is replaced by an execution- and cost-aware
decomposition (Della Vedova 2026; Dubach 2026):

```
alpha_net = p_hat - p_exec - fees - slippage - adverse_selection
```

and the return surface is decomposed into five families:

```
R = Information + Behavior + Microstructure + Logical-Constraints + Execution
     A1/A2/A9       A1/A2       A6               A4/A5               A8
```

Every family below must independently clear the same universal gate:

```
E[ PnL_OOS - fees - spread - slippage - impact ] > 0
```

with executable depth, point-in-time features, independent-event clustering, and
pre-registered tests.

---

## 1. Data-availability gate (the binding constraint)

The literature uses data PolyAlpha does not currently collect. Each family is
gated on data first, then on evidence.

| Family | Required data | Current status |
|---|---|---|
| A1 Calibration / FLB | books + settlements | **ACTIVE** (implemented: `us/families.py`) |
| A2 Time-to-resolution | books + settlements | **ACTIVE** (implemented) |
| A3 Shock continuation/reversion | book time series (+ related-market confirmation) | **PARTIAL** |
| A4 Cross-market consistency | both executable sides / related-market linkage | **PARTIAL** |
| A5 Combinatorial arbitrage | on-chain book + market dependency graph | **NEEDS_DATA** |
| A6 Microstructure (signed flow) | on-chain `OrderFilled` events | **NEEDS_DATA** |
| A7 Informed wallet flow | ~~wallet identities~~ | **NOT_RUN** (no public identity on US) |
| A8 Execution / adverse selection | signed fills + maker/taker attribution | **NEEDS_DATA** |
| A9 External fair-value anchors | ~~cross-venue data~~ | **NOT_RUN** (US-only) |

**Hard rule (Dubach 2026):** signed flow, maker/taker, adverse-selection, and
Kyle-lambda estimates must be sourced from on-chain `OrderFilled` events, NOT
from feed-inferred direction. Feed-inferred direction agrees with ground truth
on only ~59% of buckets and flips sign on the majority of markets. Any
feed-based signed-flow result is treated as a measurement artifact until the
on-chain join is reproduced.

---

## 2. Global decision rules (identical to v2, unchanged)

- **Train/eval split:** temporal. Evaluate only on `endDate >= SPLIT_DATE`
  (`SPLIT_DATE = 2026-10-31`). No random split, no resampling on eval.
- **Multiple testing:** Benjamini-Hochberg FDR at 10%, applied across all
  hypotheses and sub-groups within a family on the same eval set.
- **Significance:** FDR-adjusted p < 0.10 **and** the family's minimum effect.
- **Minimum effective N:** 200 independent-ish event clusters (global floor);
  ≥20 clusters per category before a category sub-result is reported.
- **Independence unit:** parent-event cluster, never raw contract.
- **Look-ahead guard:** every feature and every skill/flow score uses only
  information available *before* t (`t^-`). Outcomes are labels only, never
  features.

---

## 3. Hypotheses

Notation: `p` = executable price, `p_hat` = model probability, `Y` = binary
outcome, `τ` = time-to-resolution, `R` = realized return, `OFI` = signed order
flow imbalance, `w` = wallet.

### A1 — Calibration / Favorite–Longshot (ACTIVE)

**H1 (contract-level FLB).** Realized return is decreasing in price: low-price
contracts overperform quoted probability.
`H0: E[Y − p | p] = 0` for all price buckets.
Test: cluster-mean residual by price decile, null-centered cluster bootstrap,
BH-FDR. Min effect: ≥1pp mean residual in ≥1 bucket. Min N: 200 clusters.
Gate: FDR-significant **and** ≥1pp. (Cardozo: <10¢ loses 19.3¢/$.)

**H2 (grouping dependence).** The FLB sign/magnitude depends on equal-weight vs.
parent-event-weight aggregation.
`H0: sign(E[R|p]) identical under both aggregations.`
Test: compute the residual both ways; report the discrepancy explicitly. This is
a measurement-robustness meta-hypothesis, not a trade.

**H3 (category heterogeneity).** FLB is present in Crypto/Politics and absent in
Sports. `H0: residual zero in every category × bucket cell.`
Min N: 200 clusters overall, ≥20/category.

**H4 (longshot exploitability horizon).** Low-price overpricing is exploitable
only for a sufficiently low discount rate (Page–Clemen). `H0: excess return
after time-value of money is zero.`

### A2 — Time-to-resolution (ACTIVE)

**H5 (TTR miscalibration).** `|E[Y − p | p, τ]|` is increasing in τ.
`H0: |residual| constant across τ buckets.` Min effect: ≥1pp spread between
nearest and farthest τ buckets. (Page–Clemen; already implemented.)

### A3 — Shock continuation vs reversion (PARTIAL)

**H6 (shock reversion).** Large |Δmid| reverts when the move is into thin books
(liquidity shock) and continues when confirmed by depth/volume response.
`H0: subsequent Δmid independent of shock classification.`
Min effect: ≥0.5 tick. **Classifier frozen ex ante** (v2.1 rule); no post-hoc
threshold tuning.

### A4 — Cross-market consistency (PARTIAL)

**H7 (no-arbitrage violation).** Executable prices of exhaustive/mutually-
exclusive outcomes deviate from 1 by more than round-trip cost.
`H0: no violation exceeds transaction cost.` Min effect: violation ≥ cost on ≥1%
of constraint instances. (Saguillo's within-market rebalancing is the same
family at the condition-set level.)

### A5 — Combinatorial arbitrage (NEEDS_DATA)

**H8 (combinatorial mispricing).** Dependent-market mispricing (Saguillo 2025)
is executable net of fees/slippage. `H0: net profit ≤ 0 after costs.`
Test: reconstruct the dependency graph, find violations, and require executable
depth at the violation's own timestamp. Min N: 200 independent violations.

**H9 (realized extraction).** A non-trivial share of combinatorial violations
was actually traded (not just quoted). `H0: violation frequency equals execution
frequency.`

### A6 — Microstructure (NEEDS_DATA — on-chain)

**H10 (signed flow predicts).** On-chain signed flow predicts short-horizon Δp
(Kyle lambda > 0). `H0: λ = 0.` Source: on-chain `OrderFilled` only.

**H11 (measurement negative control).** Feed-inferred signed flow does NOT
predict Δp once on-chain flow is controlled. `H0: feed-flow coefficient = 0.`
This is a mandatory pre-test: if it fails, no feed-based flow feature is trusted.

### A7 — Informed wallet flow (NOT_RUN — no public identity on US; dropped)

**H12 (persistent skill).** A minority of wallets is persistently skilled
(Gomez-Cram: ~3%). `H0: skill is not persistent across periods.`
Skill must be computed from `t^-` information only — never from future outcomes.

**H13 (informed-flow predicts).** `InformedFlow_t = Σ_w Skill_w(t^-) · SignedVol_{w,t}`
predicts subsequent Δp. `H0: coefficient = 0.`

**H14 (look-ahead negative control).** Wallet skill computed with future outcomes
produces spurious OOS predictive power that disappears under `t^-` skill. This
is a permanent integrity test, not a trading hypothesis.

### A8 — Execution / adverse selection (NEEDS_DATA)

**H15 (execution dominates).** Execution quality (arrival-price slippage,
maker-vs-taker, adverse selection) explains more realized-PnL variance than
directional accuracy (Della Vedova 2026). `H0: directional accuracy dominates.`

**H16 (adverse selection).** Post-trade markout is negative on average and
asymmetric (worse for aggressive orders). `H0: mean markout = 0.`

### A9 — External fair-value anchors (NOT_RUN — requires cross-venue; dropped)

**H17 (cross-market wedge).** The Polymarket-vs-Deribit implied wedge predicts
Polymarket price convergence (Fabi 2026). `H0: wedge is uninformative of
subsequent move.`

**H18 (wedge structure).** The wedge is systematic in maturity, payoff structure,
and volatility regime. `H0: wedge is zero conditional on those variables.`

---

## 4. Acceptance / rejection (per hypothesis)

- **SUPPORTED:** FDR-significant, ≥ minimum effect, holds on the temporal eval
  set, survives the cluster rule, and (where applicable) passes the measurement
  negative control.
- **NOT SUPPORTED:** any of the above fails. Not-supported is an informative
  outcome, not a failure.
- **DEFERRED / NEEDS_DATA:** the required data feed is not yet collected. No
  partial test is performed on a substitute feed.

## 5. Reporting format

Per-family attribution table (v2.2 rule, unchanged): one row per family with
`Family | Primary metric | Net edge | PnL | N_eff | CI | Verdict`. A single
aggregate headline is prohibited. The `E[PnL_OOS − costs] > 0` gate is reported
per family, with executable-depth and point-in-time caveats stated explicitly.

## 6. Version history

- **v3.0** — extends v2 with the 2025–2026 empirical literature: execution-aware
  alpha decomposition, the on-chain measurement gate (Dubach), combinatorial
  arbitrage, informed-flow with `t^-` skill, and external fair-value anchors.
  Written while MODEL PERFORMANCE: LOCKED, ALPHA: UNKNOWN.
