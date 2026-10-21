# Microstructure Evidence V1 — H1–H5 Result (immutable)

**No promotable fast-horizon alpha found in the first Kalshi energy H1–H5
evaluation. H1/H2/H5 are killed for futility; H3/H4 remain unresolved pending
≥40 independent clusters.**

## Pinned metadata

| Field | Value |
|---|---|
| contract_sha256 | `c422b6e948fdeae5d384839178bb32bfc97c70c9904cff007142eba20593a23b` |
| evaluated_at | `2026-10-21T05:30:52.419191+00:00` |
| data cutoff | `data/energy/kalshi/ws/2026-10-21.jsonl` (live-growing at evaluation time) |
| venue + lineage | Kalshi production WS, scoped energy lineage |
| forward partition | `venue_ts >= 2026-10-21T00:00:00+00:00` |
| evaluator | `scripts/microstructure_evaluate.py` (frozen in `microstructure-evidence-v1`) |
| bootstrap | seed `42`, `n_boot=2000`, day-clustered |
| multiple testing | Holm-Bonferroni across the 5 primary endpoints (H1–H5) |
| cost model | `net = gross - 0.5*spread(t+h) - 0.07*mid(t+h)` |
| cluster | `(ticker, operating_day)` — never tick count |
| milestones | `N ∈ {40, 80, 160, 320}`; PROMOTE at ≥160, KILL at ≥40 with net CI upper < 0 |

## Results

| H | Frozen hypothesis | Horizon | Raw obs | Labeled obs | Clusters | Gross | Net | 95% CI | raw p | Holm p | Milestone? | Decision |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| H1 | order-book imbalance → markout | 1s | 142,365 | 142,365 | 130 | −0.000097 | **−0.045089** | [−0.070052, −0.033151] | 1.0 | 1.0 | YES (≥40) | **KILL** |
| H2 | microprice displacement → markout | 5s | 177,401 | 177,401 | 132 | −0.000230 | **−0.053557** | [−0.087250, −0.039279] | 1.0 | 1.0 | YES (≥40) | **KILL** |
| H3 | aggressive trade flow → continuation | 5s | 198 | 198 | 24 | −0.000126 | −0.051572 | [−0.077817, −0.038234] | 1.0 | 1.0 | NO (<40) | **CONTINUE** |
| H4 | maker-taker adverse selection | 1s | 128 | 128 | 26 | −0.000039 | −0.057603 | [−0.088590, −0.040619] | 0.0 | 0.0 | NO (<40) | **CONTINUE** |
| H5 | liquidity withdrawal → movement | 30s | 165,681 | 165,681 | 123 | +0.000343 | **−0.069253** | [−0.107826, −0.048317] | 1.0 | 1.0 | YES (≥40) | **KILL** |

## Interpretation

Two distinct facts must not be conflated:

1. **Predictive gross edge**: H1/H2/H5 ≈ 0 (and H3/H4 ≈ 0). None of the
   five frozen features carries directional information about future mid at its
   primary horizon on this venue/universe.
2. **Economic result after frozen costs**: strongly negative (net ≈ −0.045 to
   −0.069). This is the ~5¢/contract cost (half-spread + 7% taker fee), not a
   directional signal being wrong. There is no gross edge to overcome the cost.

**H4 note**: with only 26 independent clusters, H4 currently shows no detectable
gross adverse-selection effect, but remains underpowered and CONTINUE until the
first 40-cluster milestone. It is *not* a negative conclusion.

**H3 note**: 24 independent clusters — underpowered, CONTINUE. Not a negative
conclusion.

## Implementation-history note

The initial evaluator implementation incorrectly allowed KILL below the first
`N=40` milestone. This was corrected **before** the evaluator was frozen.
Measurements/effect estimates were unchanged. Final decisions shown here follow
the frozen contract.

## Frozen decisions

```
H1 imbalance            KILL
H2 microprice           KILL
H3 trade flow           CONTINUE (24/40 clusters)
H4 adverse selection    CONTINUE (26/40 clusters)
H5 liquidity withdrawal KILL

promoted families       0
new hypotheses          0
post-hoc tuning         0
```