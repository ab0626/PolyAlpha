#!/usr/bin/env python3
"""Standalone walk-forward evaluation script.

Usage:
    python scripts/walk_forward.py --csv data.csv [--output out.json]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polyalpha.cli import main

if __name__ == "__main__":
    sys.argv.insert(1, "walk-forward")
    main()
