# Energy E1 Result — v1 (frozen)

- **Run date:** 2026-10-07
- **Spec:** `docs/ENERGY_E1_SPEC.md` v1 (unchanged)
- **Evaluator:** commit `be99257`
- **Report artifact:** `data/energy/reports/e1-20261007-162950.json` (gitignored)

## Verdict

**`INSUFFICIENT_PIT_LIQUIDITY / HISTORICAL_QUOTE_QUALITY`**

This is **not** `NOT_SUPPORTED`. The calibration hypothesis was not tested: the
historical Kalshi 1-minute candle record could not support the frozen
fixed-horizon observation rule at acceptable point-in-time quality.

## Evidence (612 settled `KXTXERCOTPEAKD` markets)

| horizon | present | missing | Brier (mid) | flag |
|---:|---:|---:|---:|---|
| 24h | 0 | 100% | — | INSUFFICIENT_DATA |
| 12h | 307 | 50% | 0.106 | LOW_QUALITY |
| 6h | 100 | 84% | 0.037 | LOW_QUALITY |
| 1h | 85 | 86% | 0.009 | LOW_QUALITY |

## Root cause (verified on a single market)

ERCOT peak-load markets trade **sporadically**: a sample market had 390 one-minute
candles over a 24h life with a ~5-hour gap in its final hours. "Price at a fixed
ex-ante horizon" reconstructed from trades is therefore fragile — the last
executable quote can be hours stale. A stale candle would produce deceptively
clean Brier scores by measuring a different state of the market.

## Interpretation discipline (frozen)

- The low 6h/1h Brier is **not** evidence of good calibration: those horizons are
  near-resolution (probabilities pinned to 0/1).
- No tolerance, horizon, bin, or split is changed to rescue the result.

## Decision

- Do **not** widen the tolerance: that would answer a different question ("most
  recent trade within a wider stale window") than the one preregistered.
- The replacement is **prospective fixed-cadence order-book capture** (running),
  which records true executable quotes independent of trade occurrence.

## Version history

- **v1** — initial result record (2026-10-07).