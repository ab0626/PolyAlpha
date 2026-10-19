"""Tests for the GEN-533-UI intraday actual-load capture (fetch/parse/running_max)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from polyalpha.energy.intraday_actuals import (
    IntradayActuals,
    parse_intraday_html,
    running_max,
)

ERCOT_TZ = ZoneInfo("America/Chicago")

SAMPLE = """
<html><body>
Last Date and Time: Oct 18, 2026 22:20
<table>
<tr><th>Oper Day</th><th>Hour Ending</th><th>NORTH</th><th>SOUTH</th><th>WEST</th><th>HOUSTON</th><th>TOTAL</th></tr>
<tr><td>10/18/2026</td><td>0100</td><td>12902.88</td><td>11350.73</td><td>8509.96</td><td>10500.59</td><td>43264.17</td></tr>
<tr><td>10/18/2026</td><td>1600</td><td>14885.89</td><td>13204.37</td><td>9392.89</td><td>12494.99</td><td>49978.14</td></tr>
<tr><td>10/18/2026</td><td>1700</td><td>15491.51</td><td>13586.02</td><td>9478.59</td><td>12800.44</td><td>51356.56</td></tr>
</table>
</body></html>
"""


def test_parse_intraday_html():
    parsed = parse_intraday_html(SAMPLE)
    assert parsed.last_update == datetime(2026, 10, 18, 22, 20, tzinfo=ERCOT_TZ)
    assert len(parsed.rows) == 3
    assert parsed.rows[0] == ("2026-10-18", "0100", Decimal("43264.17"))


def test_running_max():
    parsed = parse_intraday_html(SAMPLE)
    assert running_max(parsed.rows, "2026-10-18") == Decimal("51356.56")
    assert running_max(parsed.rows, "10/18/2026") == Decimal("51356.56")  # normalized
    assert running_max(parsed.rows, "2026-10-19") is None


def test_empty_rows_running_max_none():
    assert running_max([], "2026-10-18") is None


def test_intraday_actuals_roundtrip():
    a = IntradayActuals(
        last_update=datetime(2026, 10, 18, 22, 20, tzinfo=ERCOT_TZ),
        rows=(("2026-10-18", "0100", Decimal("1.0")),),
    )
    assert running_max(a.rows, "2026-10-18") == Decimal("1.0")