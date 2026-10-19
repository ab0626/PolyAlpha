"""ERCOT GEN-533-UI "Actual Loads of Forecast Zones" intraday display capture.

The live display (EMIL GEN-533-UI) updates hourly at :20 past the hour and shows
the current-day hourly TOTAL load by forecast zone. This is the intraday realized
load that the daily NP6-346 settlement report (``actuals.jsonl``, published the
next morning) does NOT provide at trade time. It is the authoritative realized-
path source for the LIVE_CALIBRATION_V0 crossing gate.

Fetch + parse the static HTML table. Fail-closed: any fetch/parse/validation
error returns ``None`` so a broken feed can never be silently treated as "no
realized load" (which would be a spurious NO signal).
"""

from __future__ import annotations

import html
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

DISPLAY_URL = "https://www.ercot.com/content/cdr/html/actual_loads_of_forecast_zones.html"
ERCOT_TZ = ZoneInfo("America/Chicago")
_UA = {"User-Agent": "Mozilla/5.0"}


@dataclass(frozen=True)
class IntradayActuals:
    """Parsed GEN-533-UI display snapshot.

    ``last_update`` is the display's "Last Date and Time" (CT); ``rows`` are
    ``(oper_day, hour_ending, total_mw)`` tuples for every displayed hour.
    """

    last_update: datetime | None
    rows: tuple[tuple[str, str, Decimal], ...]


def _norm_day(s: str) -> str:
    s = s.strip()
    if "/" in s:
        m, d, y = s.split("/")
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return s


def _dec(v: str) -> Decimal | None:
    try:
        return Decimal(v.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _parse_last_update(text: str) -> datetime | None:
    # e.g. "Last Date and Time: Oct 18, 2026 22:20"
    m = re.search(r"Last Date and Time:\s*([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2})", text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%b %d, %Y %H:%M").replace(tzinfo=ERCOT_TZ)
    except ValueError:
        return None


def parse_intraday_html(text: str) -> IntradayActuals:
    """Parse the GEN-533-UI HTML table into (last_update, rows)."""
    last_update = _parse_last_update(text)
    rows: list[tuple[str, str, Decimal]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S | re.I):
        cells = [
            html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        ]
        if not cells:
            continue
        if cells[0].lower() in ("oper day", "oper"):
            continue
        if len(cells) < 7:
            continue
        day = _norm_day(cells[0])
        hour = cells[1].strip()
        total = _dec(cells[6])
        if day and hour and total is not None:
            rows.append((day, hour, total))
    return IntradayActuals(last_update=last_update, rows=tuple(rows))


def fetch_intraday_actuals(
    url: str = DISPLAY_URL, timeout: float = 30
) -> IntradayActuals | None:
    """Fetch + parse the live display; ``None`` on any error (fail-closed)."""
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
        parsed = parse_intraday_html(text)
        if parsed.last_update is None or not parsed.rows:
            return None
        return parsed
    except Exception:  # noqa: BLE001 — fail closed on any transport/parse error
        return None


def running_max(
    rows: tuple[tuple[str, str, Decimal], ...] | list[tuple[str, str, Decimal]],
    operating_day: str,
) -> Decimal | None:
    """Max hourly TOTAL for ``operating_day``, or None if no rows match."""
    day = _norm_day(operating_day)
    vals = [t for (d, _h, t) in rows if _norm_day(d) == day]
    return max(vals) if vals else None