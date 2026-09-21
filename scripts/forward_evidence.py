#!/usr/bin/env python3
"""Forward-evidence evaluation report (thin wrapper over the module)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.us.forward_evidence import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
