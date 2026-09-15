#!/usr/bin/env python3
"""Standalone bias checking script.

Usage:
    python scripts/bias.py --report report.json [--output out.json]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polyalpha.cli import main

if __name__ == "__main__":
    sys.argv.insert(1, "bias")
    main()
