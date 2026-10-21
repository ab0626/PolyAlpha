# Polymarket Microstructure Evidence V1 — Result (immutable)

**No promotable book-state alpha found in Polymarket Microstructure Evidence V1.
P1/P2/P5 killed for futility; P3/P4 remain unresolved below the first
40-cluster milestone.**

## Pinned evaluation boundary + runtime

| Field | Value |
|---|---|
| contract_sha256 | `5155b8e7e3c7f023fda09f9ba9b4a4b56ff742ce1ca73fa732891b032e686515` |
| fee model | `taker_fee(p) = 0.0695 * p * (1-p)` |
| fee source | Polymarket **US** venue documentation (docs.polymarket.us/fees) — not Polymarket.com |
| evaluator commit | `7ef9ac0` |
| runtime anchor | `7a4a0f9` |
| evaluation cutoff | `2026-10-21T05:49:22.571814+00:00` |
| source files | 90 |
| source manifest hash | `a1812b4fb79f109c43fa5ff371b7e987d9f557f7c0437b9b5b0d317c1a3d034d` (3,443,082,066 bytes) |
| books admitted | 439,787 |
| trades admitted | 2,329 |
| bootstrap seed | 42 |
| cluster definition | `(marketSlug, UTC day)` |
| Holm family | P1–P5 primary endpoints |

## Decisions

| H | Horizon | Clusters | Gross | Net | 95% CI | Holm p | Decision |
|---|---|---|---|---|---|---|---|
| P1 imbalance | 1s | 186 | +0.0001 | −0.0168 | [−0.0193, −0.0146] | 1.0 | **KILL** |
| P2 microprice | 5s | 194 | −0.0001 | −0.0174 | [−0.0203, −0.0154] | 1.0 | **KILL** |
| P3 trade flow | 5s | 5 | +0.0008 | −0.0178 | [−0.0189, −0.0143] | 1.0 | CONTINUE |
| P4 adverse selection | 1s | 3 | −0.0004 | −0.0190 | [−0.0208, −0.0176] | 0.0 | CONTINUE |
| P5 liquidity withdrawal | 30s | 187 | −0.0001 | −0.0174 | [−0.0211, −0.0145] | 1.0 | **KILL** |

```
P1 imbalance           KILL
P2 microprice          KILL
P3 trade flow          CONTINUE — 5 clusters
P4 adverse selection   CONTINUE — 3 clusters
P5 liquidity withdrawal KILL

promoted   0
killed     3
unresolved 2
post-hoc tuning 0
```

## Scientific conclusion

Kalshi: obvious book-state signals ≈ 0 gross; thin books + large spread/cost.
Polymarket: obvious book-state signals ≈ 0 gross; deep/tight books + smaller cost.

**Inference: the Kalshi null was not merely a thin-liquidity artifact.** The
preregistered H/P1–P5 book-state family has now failed to produce predictive
gross edge on two materially different prediction-market microstructures.

## Regime finding (pre-registered, not post-hoc)

Higher liquidity improves net only through lower transaction cost; the gross
signal remains approximately zero across the pre-registered depth quartiles
(net −0.019 in Q0 → −0.014 in Q3, all gross ≈ 0). This closes the "maybe it only
works in liquid markets" escape hatch without post-hoc slicing.

## Fee-model note

The archive is the **Polymarket US** venue (`gateway.polymarket.us` /
`api.polymarket.us`), so the uniform Θ=0.0695 schedule applies — not the
category-based Polymarket.com schedule. A per-market `feesEnabled` metadata
manifest (to prove no fee-free markets are over-charged) remains an open
follow-up requiring the live REST market-metadata query; it does not invalidate
this result, which used the correct venue fee.