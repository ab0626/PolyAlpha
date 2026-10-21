"""v1.2 Mode B boundary-eligibility tests."""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from execution_calibration_modeb import boundary_eligible


def test_boundary_guard():
    D = Decimal
    # YES: ask=1.00 ineligible; ask=0.98 eligible
    assert boundary_eligible("YES", D("1.00"), D("0.97")) is False
    assert boundary_eligible("YES", D("0.98"), D("0.96")) is True
    # NO: yes_bid=0.00 ineligible (no_ask=1.00); yes_bid=0.02 eligible (no_ask=0.98)
    assert boundary_eligible("NO", D("1.00"), D("0.00")) is False
    assert boundary_eligible("NO", D("0.98"), D("0.02")) is True