"""Strategy Evaluation Notebook.

Compares strategy performance across multiple dimensions:
- Equity curves and drawdown
- Risk-adjusted returns (Sharpe-like, Sortino-like)
- Profit factor and win rate
- Category-level performance
- Walk-forward stability
- Calibration quality

Usage:
    python -m notebooks.strategy_evaluation
"""

# %%
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from decimal import Decimal

D = Decimal

# %%
# Load configuration
from polyalpha.config import load_toml

config = load_toml("config/base.toml")
risk = config.get("risk", {})
print("Risk config:", risk)

# %%
# Example: Performance metrics computation
print("\n--- Performance Metrics Demo ---")

from polyalpha.performance import (
    brier_score,
    drawdown_series,
    profit_factor,
    sortino_ratio,
    trade_metrics,
)

# Example equity curve (from a backtest)
equities = [10000, 10100, 9950, 10200, 10300, 10150, 10400, 10500, 10350, 10600]
dd = drawdown_series(equities)

print(f"\nStarting equity: ${equities[0]:,.2f}")
print(f"Final equity:    ${equities[-1]:,.2f}")
print(f"Total return:    {(equities[-1]/equities[0] - 1)*100:.2f}%")
print(f"Max drawdown:    {max(dd)*100:.2f}%")

# Sharpe-like ratio
returns = [(equities[i] - equities[i-1]) / equities[i-1] for i in range(1, len(equities))]
from statistics import mean, stdev
avg_return = mean(returns)
vol = stdev(returns) if len(returns) > 1 else 1
sharpe = avg_return / vol if vol > 0 else 0
print(f"Sharpe-like:     {sharpe:.4f}")

# Sortino-like ratio
downside = [r for r in returns if r < 0]
downside_vol = (sum(r**2 for r in downside) / len(downside))**0.5 if downside else 0.001
sortino = avg_return / downside_vol if downside_vol > 0 else 0
print(f"Sortino-like:    {sortino:.4f}")

# %%
# Trade metrics
print("\n--- Trade Metrics ---")

# Example trades: list of (pnl,) tuples
example_trades = [
    (D("50"),), (D("-30"),), (D("80"),), (D("-20"),),
    (D("40"),), (D("-10"),), (D("60"),), (D("-5"),),
]
metrics = trade_metrics(example_trades)
print(f"  Total trades:  {metrics['total_trades']}")
print(f"  Winners:       {metrics['winners']}")
print(f"  Losers:        {metrics['losers']}")
print(f"  Win rate:      {metrics['win_rate']}")
print(f"  Profit factor: {metrics['profit_factor']}")

# %%
# Brier score and calibration
print("\n--- Calibration Quality ---")

predictions = [0.6, 0.7, 0.4, 0.8, 0.5, 0.9, 0.3, 0.65]
outcomes = [1, 1, 0, 1, 0, 1, 0, 1]

brier = brier_score(predictions, outcomes)
print(f"Brier score: {brier:.4f}  (0=perfect, 1=worst)")

# Calibration buckets
from polyalpha.calibration import metrics as cal_metrics

cal = cal_metrics(predictions, outcomes)
print(f"Log loss:    {cal['log_loss']:.4f}")
print(f"ECE:         {cal['ece']:.4f}")

print("\nCalibration buckets:")
for bucket in cal.get("buckets", []):
    print(f"  {bucket.get('bucket', '?'):>8s}: "
          f"n={bucket.get('count', 0):3d}  "
          f"pred={bucket.get('mean_predicted', 0):.3f}  "
          f"actual={bucket.get('actual_frequency', 0):.3f}")

# %%
# Strategy comparison
print("\n--- Strategy Comparison ---")
print("To compare strategies:")
print("  1. Run backtests with different configs:")
print("     polyalpha paper-run --db data/polyalpha.db --exit-policy hold")
print("     polyalpha paper-run --db data/polyalpha.db --exit-policy edge")
print("  2. Compare results:")
print("     polyalpha compare report_hold.json report_edge.json")
print("  3. Run bias checks:")
print("     polyalpha bias report_edge.json")

# %%
# Walk-forward analysis
print("\n--- Walk-Forward Analysis ---")
print("To run walk-forward validation:")
print("  1. Generate forecast CSV from model predictions")
print("  2. Run:")
print("     polyalpha walk-forward forecasts.csv \\")
print("       --first-test 2026-07-01 --end 2026-12-31 \\")
print("       --window-days 30 --output wf_results.json")
print("  3. Inspect per-fold calibration and stability")
