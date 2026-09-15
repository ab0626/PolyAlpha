"""Calibration Analysis Notebook.

This notebook evaluates model probability calibration:
- Reliability diagrams
- Brier score decomposition
- Calibration by category
- Temperature scaling analysis
- Walk-forward calibration stability

Usage:
    jupyter notebook notebooks/02_calibration.ipynb
    # or run as a script:
    python -m notebooks.calibration_analysis
"""

# %%
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from decimal import Decimal
from polyalpha.calibration import metrics, TemperatureScaling

D = Decimal

# %%
# Example: Synthetic calibration data
# Replace with real backtest predictions
probabilities = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
outcomes = [0, 0, 0, 0, 1, 1, 1, 1, 1]

result = metrics(probabilities, outcomes)
print("Calibration Metrics:")
print(f"  Brier Score: {result['brier']:.4f}")
print(f"  Log Loss: {result['log_loss']:.4f}")
print(f"  ECE: {result['ece']:.4f}")
print(f"  Sample Size: {result['sample_size']}")

# %%
# Reliability Diagram
print("\nReliability Diagram:")
print(f"{'Bucket':<12} {'Count':>6} {'Predicted':>10} {'Observed':>10}")
print("-" * 42)
for bucket in result.get("reliability", []):
    if bucket["count"] > 0:
        print(
            f"{bucket['lower']:.1f}-{bucket['upper']:.1f}    "
            f"{bucket['count']:>6} "
            f"{bucket['predicted']:>10.3f} "
            f"{bucket['observed']:>10.3f}"
        )

# %%
# Temperature Scaling
ts = TemperatureScaling()
ts.fit(probabilities, outcomes)
print(f"\nTemperature: {ts.temperature:.3f}")

for p in [0.3, 0.5, 0.7]:
    calibrated = ts.predict(p)
    print(f"  {p:.1f} -> {calibrated:.3f}")

print("\nNotebook stub - connect to backtest results for real analysis")
