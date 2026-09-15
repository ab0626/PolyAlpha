#!/usr/bin/env python3
"""Standalone model training script.

Trains probability calibrators on historical data and saves parameters.

Usage:
    python scripts/train.py --config config/base.toml --method isotonic
    python scripts/train.py --config config/base.toml --method temperature --db data/research.db
"""

import sys
from pathlib import Path

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polyalpha.train import main

if __name__ == "__main__":
    main()
