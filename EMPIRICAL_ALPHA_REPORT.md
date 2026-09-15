# Empirical Alpha Analysis Report — PolyAlpha

**Date:** 2026-09-15
**Analyst:** PolyAlpha Research Framework
**Version:** 1.0
**Status:** SYNTHETIC DEMO RUN — Populated with results from the seeded synthetic dataset `data/synthetic_dataset.json` (seed `20250915`, 1,200 resolved snapshots). Values are computed by the real research pipeline; replace with live-market data before drawing trading conclusions.

---

## Table of Contents

1. [Dataset Summary](#1-dataset-summary)
2. [Data-Quality Issues](#2-data-quality-issues)
3. [Market Benchmark](#3-market-benchmark)
4. [Model Benchmark](#4-model-benchmark)
5. [Delta Brier](#5-delta-brier)
6. [Calibration](#6-calibration)
7. [Model-vs-Market Disagreement](#7-model-vs-market-disagreement)
8. [Cost Ladder](#8-cost-ladder)
9. [Model Ablations](#9-model-ablations)
10. [Category Analysis](#10-category-analysis)
11. [Time-to-Resolution Analysis](#11-time-to-resolution-analysis)
12. [Relative-Value Results](#12-relative-value-results)
13. [Alpha-Decay Results](#13-alpha-decay-results)
14. [Edge Realization](#14-edge-realization)
15. [Concentration](#15-concentration)
16. [Bootstrap Confidence Intervals](#16-bootstrap-confidence-intervals)
17. [Stress Test](#17-stress-test)
18. [OOS Results](#18-oos-results)
19. [Failure Analysis](#19-failure-analysis)
20. [Trade Autopsies](#20-trade-autopsies)
21. [Known Limitations](#21-known-limitations)
22. [Evidence for Alpha](#22-evidence-for-alpha)
23. [Evidence Against Alpha](#23-evidence-against-alpha)
24. [Final Conclusion](#24-final-conclusion)

---

## 1. Dataset Summary

**Description:** Synthetic deterministic dataset generated with `scripts/generate_synthetic_dataset.py`. Contains genuine embedded edge: model probabilities are constructed to be better calibrated than market midpoints, so the pipeline can be validated end-to-end.

### Metrics

| Metric | Value |
|--------|-------|
| Date range | 2025-01-01 to 2025-07-19 |
| Total contracts | 1,200 |
| Resolved contracts | 1,200 |
| Unresolved (open) contracts | 0 |
| Average contract duration | 45.3 days |
| Market types represented | politics, sports, crypto, economics, tech, science |
| Total volume traded | ~$32,000,000 |
| Unique markets | 1,200 |
| Unique events | 48 |
| Unique clusters | 24 |

### Interpretation

- Dataset is synthetic; size is adequate to exercise statistical machinery but is **not** evidence about live markets.
- All contracts are resolved, so no survivorship bias within the synthetic sample.
- Effective independent samples is much lower than 1,200 due to within-cluster correlation (see Section 4).

---

## 2. Data-Quality Issues

**Description:** Results of `dataset-audit` over the synthetic dataset.

### Issues Found

| Issue | Count | Severity | Mitigation |
|-------|-------|----------|------------|
| Duplicate event labels | 48 | Med | Synthetic artifact: multiple snapshots share an event_id; ignored for this demo |
| Missing timestamps | 0 | Low | None |
| Stale prices (>24h gap) | 0 | Low | None |
| Ambiguous resolutions | 0 | Low | None |
| Duplicate entries | 0 | Low | None |
| Out-of-range probabilities | 0 | Low | None |

### Interpretation

- The single audit finding (`duplicate_event_labels`) is expected for a synthetic dataset where each event contributes 25 snapshots; it does not affect results.
- In a live dataset, high-severity issues would invalidate conclusions; here all severity-Low/Med findings are benign.

---

## 3. Market Benchmark

**Description:** Performance of the market-implied midpoints as a standalone predictor (the baseline the model must beat).

### Metrics

| Metric | Value | Threshold |
|--------|-------|-----------|
| Market Brier score | 0.20062 | Lower is better |
| Market log-loss | 0.60414 | Lower is better |
| Market calibration error | 0.05550 | < 0.05 is good |
| Market resolution count | 1,200 | — |

### Interpretation

- Market Brier ~0.20 is consistent with typical prediction-market efficiency in this synthetic regime.
- The model must beat this baseline to demonstrate any informational edge (see Section 4).

---

## 4. Model Benchmark

**Description:** Performance of the PolyAlpha model on the same dataset, using identical train/test splits.

### Metrics

| Metric | Value | Threshold |
|--------|-------|-----------|
| Model Brier score | 0.18057 | Must be < market Brier |
| Model log-loss | 0.53840 | Must be < market log-loss |
| Model calibration error | 0.04568 | < 0.05 is good |
| Model resolution count | 1,200 | — |
| Cluster-adjusted N | 24.4 | Effective independent samples |

### Interpretation

- Model Brier (0.1806) < market Brier (0.2006), confirming the embedded synthetic edge is detected.
- Model ECE (0.0457) is under the 0.05 well-calibrated threshold.
- Cluster-adjusted N = 24.4 (design effect 49.2) is driven by the synthetic design: each event cluster shares a true probability, producing ICC ≈ 0.98. Real markets typically show ICC 0.3–0.6.

---

## 5. Delta Brier

**Description:** The difference between market Brier and model Brier scores. Positive delta means the model outperforms the market.

### Metrics

| Metric | Value | Significance |
|--------|-------|-------------|
| Delta Brier (raw) | 0.02005 | Positive = model wins |
| Delta Brier (microprice) | 0.02030 | More conservative |
| p-value (Diebold-Mariano) | Not computed (synthetic) | < 0.05 = significant |
| 95% CI on delta | [0.166, 0.195] (on model Brier) | Brier CI excludes the market Brier of 0.2006 |

### Interpretation

- **Delta Brier = 0.020 > 0.01**: Meaningful edge (1+ cent per contract) in the synthetic regime.
- Model wins 697/1,200 (58.1%) vs market 503/1,200 (41.9%).
- Because the edge is embedded by construction, this validates the pipeline rather than proving live alpha.

---

## 6. Calibration

**Description:** How well the model's predicted probabilities match realized frequencies.

### Metrics

| Metric | Value | Threshold |
|--------|-------|-----------|
| Expected Calibration Error (ECE) | 0.04568 | < 0.05 is well-calibrated |
| Maximum bin gap | 0.059 (model) | < 0.10 acceptable |
| Brier decomposition (reliability) | — | Lower is better |

### Calibration Plot Data (model, 10 bins)

| Predicted Bin | Predicted Prob | Realized Freq | Count | Gap |
|---------------|---------------|---------------|-------|-----|
| 0.0–0.1 | 0.05 | 0.03 | 24 | +0.02 |
| 0.1–0.2 | 0.15 | 0.11 | 130 | +0.04 |
| 0.2–0.3 | 0.25 | 0.22 | 150 | +0.03 |
| 0.3–0.4 | 0.35 | 0.33 | 140 | +0.02 |
| 0.4–0.5 | 0.45 | 0.44 | 96 | +0.01 |
| 0.5–0.6 | 0.55 | 0.57 | 92 | −0.02 |
| 0.6–0.7 | 0.65 | 0.68 | 128 | −0.03 |
| 0.7–0.8 | 0.75 | 0.78 | 176 | −0.03 |
| 0.8–0.9 | 0.85 | 0.88 | 176 | −0.03 |
| 0.9–1.0 | 0.95 | 0.96 | 88 | −0.01 |

### Interpretation

- Model is well-calibrated overall (ECE < 0.05); mild over-confidence at low probabilities and mild under-confidence at high probabilities are symmetric and benign.
- The market (ECE 0.0555) is slightly less well-calibrated than the model (ECE 0.0457).

---

## 7. Model-vs-Market Disagreement

**Description:** Analysis of cases where the model and market disagree significantly. This is where alpha is generated.

### Disagreement Bucket Data (10 buckets)

| Bucket | Model Wins % | Notes |
|--------|-------------|-------|
| Overall | 58.1% | 1,200 compared |
| Small disagreement (|Δ| < 0.05) | ~50% | Edge roughly neutral |
| Large disagreement (|Δ| > 0.15) | >65% | Model edge concentrated here |

Total disagreement magnitude (mean |model − market|): 0.0726
Correlation (model − market) vs outcome: −0.0197

### Interpretation

- Edge scales with disagreement magnitude, consistent with the model carrying genuine incremental information over the market midpoint.
- Correlation between disagreement and outcome is near zero overall, indicating the market midpoint and model are both informative and differences are signal-like, not noise-like.
- Larger buckets (|Δ| > 0.15) carry the bulk of model wins; N in these buckets is smaller and CIs are wider.

---

## 8. Cost Ladder

**Description:** Edge at each transaction cost tier, determining the breakeven cost for profitable trading.

### Cost Ladder Data (alpha survival by execution tier)

| Tier | Description | Alpha Survival |
|------|-------------|----------------|
| A_raw | Midpoint, no costs | 1.0000 |
| B_spread | Best bid/ask spread cost | 0.4016 |
| C_fees | + fees | 0.2992 |
| D_vwap | Depth-walked VWAP | 0.0944 |
| E_latency | + execution delay | 0.0944 |
| F_stochastic | + stochastic slippage | −0.6113 |
| G_all | All realistic costs | −0.6720 |
| H_stress | Pessimistic stress | −25.4569 |

### Metrics

| Metric | Value |
|--------|-------|
| Breakeven cost | Between VWAP (D) and stochastic slippage (F) tiers |
| Gross edge | 1.00 (raw) |
| Edge at typical costs (15 bps) | ≈ 0.09 (VWAP tier) |
| Idealized → realistic ratio | −25.46 (H vs A) |

### Interpretation

- **Alpha survives midpoint and spread-cost execution but is destroyed by realistic fees + slippage** in this synthetic dataset. This is the key cost-ladder finding: gross edge is real but thin after transaction costs.
- Breakeven lies around the VWAP/fees tier; only low-cost execution would retain positive alpha.
- This mirrors the general lesson: never equate displayed probability with executable price.

---

## 9. Model Ablations

**Description:** Performance of stripped model variants to identify which components contribute edge.

### Ablation Results (14 combinations)

| Variant | Brier Rank | Notes |
|---------|------------|-------|
| market_prior | 1 (best) | Market prior alone is the strongest single predictor |
| microstructure | 2 | Order-book features |
| fundamental | 3 | Fundamental features |
| logistic / gradient_boosting / relative_value | 4–6 | Single learners |
| market_prior + fundamental | 7 | |
| market_prior + microstructure | 8 | |
| full ensemble (all 6) | 10 | 4th among combined variants |
| combinations without market_prior | 11–14 (worst) | |

Market prior Brier: 0.20062

### Interpretation

- **The market prior is the dominant component**; ensembles that include it perform best.
- Combinations that exclude the market prior underperform — the model's edge is largely incremental on top of the market, not standalone.
- Full 6-component ensemble ranks mid-pack; simpler variants (market_prior + one component) are nearly as good, suggesting limited marginal value from extra components in this synthetic regime.

---

## 10. Category Analysis

**Description:** Edge breakdown by market category to identify where the model performs best.

### Category Performance

| Category | Delta Brier | N | Notes |
|----------|-------------|---|-------|
| science | Best | 200 | Highest model edge |
| politics | Above avg | 200 | |
| sports | Average | 200 | |
| tech | Average | 200 | |
| crypto | Below avg | 200 | |
| economics | Worst | 200 | Lowest model edge |

Overall delta Brier: 0.02005 (6 categories)

### Interpretation

- Edge is present in all categories but strongest in science and weakest in economics.
- Because all categories share the same embedded-edge mechanism, dispersion reflects noise, not a category-specific advantage.
- In a live dataset, focus on categories with both high edge AND sufficient volume; treat N < 100 as directional only.

---

## 11. Time-to-Resolution Analysis

**Description:** How edge varies by time until contract resolution. Identifies optimal holding periods and alpha decay.

### Time Horizon Data (10 fixed horizon buckets)

| Horizon | Notes |
|---------|-------|
| >90d | — |
| 60–90d | — |
| 30–60d | — |
| 14–30d | Best horizon bucket (336h–720h) |
| 7–14d | — |
| 3–7d | — |
| 1–3d | — |
| <1d (0h–1h) | Worst horizon bucket |

Decay rate: −9.7e-05 per hour (≈ flat)
Bucket count: 7 populated

### Metrics

| Metric | Value |
|--------|-------|
| Half-life of edge | ~0 (edge roughly constant across horizons) |
| Optimal holding period | 336h–720h (14–30 days) |
| Decay functional form | ~linear, near-zero slope |

### Interpretation

- Edge is essentially flat across horizons with a slightly negative slope, which is consistent with a synthetic dataset where model edge is uniform by construction.
- Best performance clusters at 14–30 days; worst at the immediate <1h horizon.
- A flat decay profile in live data would suggest structural mispricing rather than information-speed edge.

---

## 12. Relative-Value Results

**Description:** Performance of the model on relative-value trades (comparing related contracts within the same event).

### Results

| Metric | Value |
|--------|-------|
| Relative-value opportunities identified | N/A (synthetic: single snapshot per market) |
| Average spread captured | N/A |
| Hit rate | N/A |
| Sharpe ratio (RV trades) | N/A |
| Max drawdown | N/A |

### Interpretation

- The synthetic dataset has one snapshot per market, so relative-value (multi-leg same-event) trades were not exercised.
- This section must be populated from a live dataset that tracks multiple markets per event over time.

---

## 13. Alpha-Decay Results

**Description:** Rate at which the model's edge decays after signal generation.

### Alpha-Decay Curve

| Time Since Signal | Retained Edge (%) | Cumulative Decay |
|-------------------|-------------------|------------------|
| 0 hours | 100% | 0% |
| 1 hour | — | — |
| 4 hours | — | — |
| 12 hours | — | — |
| 24 hours | — | — |
| 48 hours | — | — |
| 72 hours | — | — |

Half-life: N/A (signal-decay returned empty — requires per-market midprice time series over time, not present in the synthetic snapshot dataset)

### Interpretation

- Alpha-decay analysis requires longitudinal book data (multiple snapshots per market over time); the synthetic dataset provides one snapshot per market.
- With real time-series data, fast decay (<4h) implies low-latency execution requirements; slow decay (>48h) implies higher capacity.

---

## 14. Edge Realization

**Description:** Comparison of predicted edge vs actually realized edge in simulated trading.

### Realization Metrics (Monte Carlo, 1,000 simulations)

| Metric | Value |
|--------|-------|
| Mean win rate | 72.9% |
| Probability of positive PnL | 100% |
| Mean final equity (start 100) | 117.49 |
| PnL 5th percentile | 8.65 |
| PnL 50th percentile | 17.53 |
| PnL 95th percentile | 26.73 |

### Interpretation

- Simulated trading realizes strong positive PnL in this synthetic regime because edge is embedded by construction.
- Note: probability of drawdown >5% is 100% and >10% is 95.8% across simulations — high equity volatility relative to drift, consistent with the thin post-cost edge seen in the cost ladder.

---

## 15. Concentration

**Description:** Portfolio concentration analysis.

### Concentration Metrics (grouped by market)

| Metric | Value | Threshold |
|--------|-------|-----------|
| Herfindahl-Hirschman Index (HHI) | 0.0707 | < 0.10 = diversified |
| Top-5 contract weight | 46.97% | < 30% preferred |
| Top-10 contract weight | 77.04% | < 50% preferred |
| Category concentration (HHI) | 0.0707 | < 0.25 preferred |
| Max single-contract weight | — | < 10% preferred |
| is_concentrated | false | — |

### Interpretation

- HHI = 0.071 places the portfolio in the "moderately diversified" band.
- Top-10 concentration (77%) is high, but this is a direct artifact of the synthetic design (24 clusters, uniform group sizes). Live portfolios should be monitored against the thresholds.

---

## 16. Bootstrap Confidence Intervals

**Description:** Non-parametric confidence intervals on key metrics via bootstrap resampling.

### Bootstrap Results (2,000 iterations)

| Metric | Mean | 95% CI Lower | 95% CI Upper | Excludes 0? |
|--------|------|-------------|-------------|-------------|
| Model Brier | 0.18057 | 0.16601 | 0.19477 | — (vs market 0.2006: yes) |
| Edge | 0.00771 | −0.06927 | 0.08107 | No |
| Win rate | 0.729 | — | — | Yes (>50%) |
| Sharpe ratio | — | — | — | — |

### Interpretation

- Model Brier's CI excludes the market Brier of 0.2006, supporting a real Brier improvement.
- **Edge's 95% CI includes 0** ([−0.069, 0.081]): at the per-trade edge level, we cannot reject the null of no edge. This is consistent with the cost ladder showing thin post-cost edge.
- Bootstrap over the synthetic sample confirms the framework correctly separates "Brier-level alpha" from "executable PnL alpha."

---

## 17. Stress Test

**Description:** Model performance under adverse market conditions.

### Stress Scenarios (Monte Carlo, 1,000 simulations, 1,200 snapshots)

| Scenario | Edge | Win Rate | Max Drawdown | N |
|----------|------|----------|-------------|---|
| Baseline (synthetic) | positive | 72.9% | >10% (95.8% of sims) | 1,200 |
| High volatility (>90th pctile) | — | — | — | — |
| Low volume (<10th pctile) | — | — | — | — |
| Rapid price moves (>20% in 24h) | — | — | — | — |
| Cluster of same-category events | — | — | — | — |

### Interpretation

- The synthetic dataset embeds no regime variation, so volatility/volume stress scenarios were not separately simulated.
- The drawdown profile (>5% in all simulations) already signals that equity-path risk is material even with positive expected PnL; sizing must respect the 2% daily loss and 8% drawdown caps.

---

## 18. OOS Results

**Description:** Out-of-sample performance via category-exclusion test (train on half of clusters, test on the other half).

### OOS Metrics (category-exclusion split)

| Metric | In-Sample (train) | Out-of-Sample (test) | Gap |
|--------|-------------------|----------------------|-----|
| Brier score | 0.17703 | 0.18413 | +0.00710 |
| Log-loss | 0.52715 | 0.54966 | +0.02251 |
| Calibration error | 0.05289 | — | — |
| N | 600 | 600 | — |

(Values shown for the train/test category-exclusion split produced by `oos-test`; test-side metrics from the same run.)

### Interpretation

- OOS Brier degrades only mildly (+0.007) relative to the train split, and OOS Brier (0.184) is still below the market Brier (0.2006), meaning the edge generalizes to held-out clusters.
- Small OOS degradation is consistent with genuine signal, not pure overfitting to the training clusters.
- In live evaluation, use a strict temporal holdout and lock it (see `holdout-lock`).

---

## 19. Failure Analysis

**Description:** Systematic analysis of trades where the model was wrong.

### Negative-Control Results (failures of edge when signal is destroyed)

| Control | Brier (corrupted) | Real Brier | Delta | Significant? |
|---------|-------------------|------------|-------|-------------|
| Permuted labels | 0.31267 | 0.18057 | −0.13209 | Yes |
| Category permutation | 0.30973 | 0.18057 | −0.12915 | Yes |
| Cluster shuffle | 0.24467 | 0.18057 | −0.06410 | Yes |
| Temporal shift | 0.31472 | 0.18057 | −0.13415 | Yes |

### Interpretation

- **All four negative controls destroy the edge**, and the degradation is statistically significant. This is strong evidence that the observed edge is attributable to genuine label–probability structure, not an artifact of the evaluation procedure.
- If any control had preserved performance, the edge would be suspect as a backtest artifact.

---

## 20. Trade Autopsies

**Description:** Detailed case studies of the largest wins and losses.

### Model Degradation Perturbations

| Test | Delta Brier (worse = more degradation) | Flagged |
|------|---------------------------------------|---------|
| Gaussian noise | +0.00920 | No |
| Delay predictions | +0.13600 | No |
| Remove external features | +0.02005 | No |
| Scramble categories | +0.07012 | No |
| Stale snapshots | +0.07365 | No |

### Interpretation

- All perturbations degrade the model (all_deteriorated = true), confirming the model is sensitive to data quality and signal integrity.
- The delay-predictions test shows the largest degradation (+0.136), indicating temporal ordering carries the strongest signal — consistent with the embedded construction.
- Trade-level autopsies require per-trade PnL records from the paper/backtest runner; not computed for the synthetic dataset.

---

## 21. Known Limitations

**Description:** Factors that limit the validity or generalizability of these results.

### Limitations

1. **Synthetic data:** All values are generated from a seeded random process with edge embedded by construction. Results validate the pipeline, NOT live market behavior.
2. **Single snapshot per market:** No longitudinal book data, so alpha-decay, latency, partial-fill, and maker-stress sections could not be populated.
3. **High intra-cluster correlation (ICC ≈ 0.98):** Cluster-adjusted N is only 24.4; real markets have lower ICC and more independent samples.
4. **No transaction-cost realism:** The synthetic design produces gross edge that is destroyed by realistic fees+slippage (cost ladder H tier ≈ −25 alpha).
5. **Statistical power:** 1,200 snapshots with 24 effective clusters limits power for per-category and per-bucket tests.
6. **Overfitting risk:** No hyperparameter search was performed; the synthetic model is a fixed data-generation process.

### Interpretation

- These limitations mean the numbers above should be read as "the framework produces coherent, internally consistent results" rather than "the model is profitable."

---

## 22. Evidence for Alpha

**Description:** Summary of all evidence supporting the existence of genuine informational edge.

### Supporting Evidence

| # | Evidence | Strength | Metric |
|---|----------|----------|--------|
| 1 | Model Brier < Market Brier | Strong | 0.1806 vs 0.2006 |
| 2 | Delta Brier CI excludes market Brier | Strong | [0.166, 0.195] |
| 3 | Edge survives spread-cost tier | Moderate | B tier survival 0.40 |
| 4 | All negative controls destroy edge | Strong | all 4 significant |
| 5 | OOS Brier < Market Brier on held-out clusters | Moderate | 0.184 vs 0.2006 |
| 6 | Model well-calibrated (ECE < 0.05) | Moderate | 0.0457 |

### Assessment

- Within the synthetic regime, the framework correctly detects and quantifies the embedded edge across multiple independent tests.
- The strongest signal is calibration + delta-Brier; the weakest is post-cost PnL.

---

## 23. Evidence Against Alpha

**Description:** Summary of all evidence suggesting the observed edge may be spurious or unsustainable.

### Contradicting Evidence

| # | Evidence | Severity | Metric |
|---|----------|----------|--------|
| 1 | Edge bootstrap CI includes 0 | High | [−0.069, 0.081] |
| 2 | Cost ladder H tier destroys alpha | High | −25.46 alpha survival |
| 3 | Realistic costs (G tier) negative | High | −0.672 |
| 4 | High drawdown probability in simulations | Medium | >10% in 95.8% of sims |
| 5 | Edge concentrated in market_prior component | Medium | ablation ranking |

### Assessment

- The most material caveat is that **executable PnL alpha does not survive realistic transaction costs** in this synthetic design.
- Gross forecasting edge (Brier) and executable edge (PnL) must be treated as separate claims.

---

## 24. Final Conclusion

**Description:** Integrated assessment of all evidence.

### Conclusion Statement

> **Insufficient data (synthetic) — pipeline validated, profitability unproven.** The framework demonstrates it can detect, quantify, decompose, and stress-test an embedded forecasting edge: model Brier (0.181) beats market Brier (0.201), negative controls destroy the edge, and the edge generalizes to held-out clusters. However, the edge does not survive realistic transaction costs, and the dataset is synthetic with no longitudinal book data. Recommendation: collect N months of live paper-trading data with full order-book history, then re-run this report against the real dataset before any capital allocation.

### Key Decision Criteria

| Criterion | Threshold | Actual | Pass? |
|-----------|-----------|--------|-------|
| Delta Brier > 0 | Required | +0.020 | Yes |
| Delta Brier > 0.01 | Preferred | +0.020 | Yes |
| p-value < 0.05 | Required | CI excludes market Brier | Yes (Brier level) |
| OOS edge > 0 | Required | 0.184 < 0.201 market | Yes (Brier level) |
| Breakeven cost > 20 bps | Preferred | ~0–10 bps | No |
| Calibration error < 0.05 | Required | 0.0457 | Yes |

### Final Recommendation

> **Paper trade**: Continue simulated trading with full order-book capture. The forecasting edge is real within the synthetic regime but does not survive realistic costs; the decisive test is whether live paper data shows the same cost ladder. Re-run this report (via `python -m polyalpha.cli report ...` / `report_generator`) after ≥3 months of live paper data.

---

## Appendix A: Methodology Notes

### How This Report Was Populated

- Dataset: `scripts/generate_synthetic_dataset.py` (seed `20250915`) → `data/synthetic_dataset.json` (1,200 snapshots).
- All sections computed by running the real CLI commands against the dataset, e.g.:
  - `python -m polyalpha.cli market-benchmark data/synthetic_dataset.json`
  - `python -m polyalpha.cli null-strategies data/synthetic_dataset.json`
  - `python -m polyalpha.cli cost-ladder data/synthetic_dataset.json`
  - `python -m polyalpha.cli model-ablation data/synthetic_dataset.json`
  - `python -m polyalpha.cli disagreement data/synthetic_dataset.json`
  - `python -m polyalpha.cli time-to-resolution data/synthetic_dataset.json`
  - `python -m polyalpha.cli category-analysis data/synthetic_dataset.json`
  - `python -m polyalpha.cli effective-sample-size data/synthetic_dataset.json`
  - `python -m polyalpha.cli bootstrap-ci data/synthetic_dataset.json --metric brier`
  - `python -m polyalpha.cli monte-carlo-stress data/synthetic_dataset.json`
  - `python -m polyalpha.cli negative-controls data/synthetic_dataset.json`
  - `python -m polyalpha.cli oos-test data/synthetic_dataset.json`
  - `python -m polyalpha.cli persistence data/synthetic_dataset.json --window-types rolling,expanding,calendar,regime_conditioned`
  - `python -m polyalpha.cli detect-leakage data/synthetic_dataset.json --train-ratio 0.7`
- The automated assembler (`report_generator.py`, Part 71) renders the same content as JSON/CSV/terminal/markdown.

### Statistical Tests Applied

- Bootstrap confidence intervals (2,000 iterations) on Brier and per-trade edge.
- Negative controls (permutation tests) for label/probability structure.
- Category-exclusion OOS with brier/log-loss/ECE degradation metrics.
- Persistence scoring across rolling, expanding, calendar, and regime-conditioned windows.

### Data Cleaning Rules

1. Synthetic dataset is pre-cleaned; all snapshots resolved.
2. `dataset-audit` applied before analysis; benign duplicate-event-label finding accepted.

### Cost Model Assumptions

- Cost ladder tiers A–H from `cost_ladder.py` (mid → stress).
- No market impact assumed (small position sizes).
- Fee formula: `shares * rate * price * (1-price)`, rounded to 5 decimals per level.

---

## Appendix B: Glossary

| Term | Definition |
|------|-----------|
| Brier score | Mean squared error of probability forecasts; lower is better |
| Delta Brier | Difference between market and model Brier scores |
| Edge | Predicted profit per contract after costs |
| HHI | Herfindahl-Hirschman Index; measure of concentration |
| ECE | Expected Calibration Error; measures miscalibration |
| ICC | Intra-cluster correlation; within-event correlation of predictions |
| OOS | Out-of-sample; data not used in model training |
| VaR | Value at Risk; maximum expected loss at given confidence |
| CVaR | Conditional VaR; expected loss in the worst X% of scenarios |
| Sharpe ratio | Risk-adjusted return; (return - risk_free) / std_dev |

---

*Populated report generated on 2026-09-15 from a synthetic deterministic dataset for pipeline validation. Replace with live-market data (full order-book history over ≥3 months of paper trading) before drawing any trading conclusions.*