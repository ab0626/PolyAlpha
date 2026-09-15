#!/usr/bin/env python3
"""Standalone strategy comparison script.

Usage:
    python scripts/compare.py --reports report1.json report2.json [--output out.json]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polyalpha.cli import main

if __name__ == "__main__":
    sys.argv.insert(1, "compare")
    main()
