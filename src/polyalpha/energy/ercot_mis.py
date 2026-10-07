"""ERCOT MIS (Market Information System) anonymous data access.

The classic public path (no API key) used by the ERCOT website and by gridstatus:

    GET https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={id}
        -> document list with DocID / FriendlyName / PublishDate
    GET https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={DocID}
        -> the CSV (zipped)

Each document carries a PublishDate, which is the point-in-time clock for "when
this became knowable". The "Actual System Load by Forecast Zone" CSV is the exact
settlement source for the Kalshi ``KXTXERCOTPEAKD`` ladder (highest ``TOTAL``).

Report type ids (numeric, as used by IceDocListJsonWS):
    ACTUAL_SYSTEM_LOAD_BY_FORECAST_ZONE = 14836
"""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

_MIS_DOCLIST = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS"
_MIS_DOWNLOAD = "https://www.ercot.com/misdownload/servlets/mirDownload"
_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

ACTUAL_SYSTEM_LOAD_BY_FORECAST_ZONE = 14836
SEVEN_DAY_LOAD_FORECAST_BY_FORECAST_ZONE = 12311
ACTUAL_SYSTEM_LOAD_BY_WEATHER_ZONE = 13101


@dataclass(frozen=True)
class MisDocument:
    doc_id: str
    friendly_name: str
    publish_date: str      # ISO-ish, e.g. "2026-10-07T05:50:00-05:00"
    constructed_name: str


def list_documents(report_type_id: int) -> list[MisDocument]:
    url = f"{_MIS_DOCLIST}?reportTypeId={report_type_id}&_{int(time.time())}"
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    docs = data.get("ListDocsByRptTypeRes", {}).get("DocumentList", [])
    out = []
    for d in docs:
        doc = d.get("Document", {})
        out.append(MisDocument(
            doc_id=doc.get("DocID", ""),
            friendly_name=doc.get("FriendlyName", ""),
            publish_date=doc.get("PublishDate", ""),
            constructed_name=doc.get("ConstructedName", ""),
        ))
    return out


def download_document(doc_id: str) -> bytes:
    url = f"{_MIS_DOWNLOAD}?doclookupId={doc_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _csv_from_zip(blob: bytes) -> list[list[str]]:
    z = zipfile.ZipFile(io.BytesIO(blob))
    name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
    text = z.read(name).decode("utf-8", "replace")
    return list(csv.reader(io.StringIO(text)))


def _dec(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _norm_day(oper_day: str) -> str:
    """Normalize ``MM/DD/YYYY`` to ``YYYY-MM-DD``."""
    if "/" in oper_day:
        m, d, y = oper_day.split("/")
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return oper_day


def parse_actual_load(blob: bytes) -> list[tuple[str, str, Decimal]]:
    """Return (oper_day, hour_ending, total_mw) rows from the actual-load CSV."""
    rows = _csv_from_zip(blob)
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    ti = header.index("TOTAL")
    oi = header.index("OperDay")
    hi = header.index("HourEnding")
    out = []
    for r in rows[1:]:
        if len(r) <= max(ti, oi, hi):
            continue
        total = _dec(r[ti])
        if total is None:
            continue
        out.append((r[oi], r[hi], total))
    return out


def daily_peak(rows: list[tuple[str, str, Decimal]]) -> dict[str, Decimal]:
    """Map operating day -> max hourly TOTAL load (MW)."""
    peaks: dict[str, Decimal] = {}
    for oper_day, _hour, total in rows:
        peaks[oper_day] = max(peaks.get(oper_day, Decimal("-Infinity")), total)
    return peaks


def historical_daily_peaks(report_type_id: int, limit: int | None = None) -> dict[str, Decimal]:
    """Download every actual-load document and return {oper_day: peak_mw}.

    ``limit`` caps the number of documents (most-recent-first) for fast backfills.
    """
    docs = list_documents(report_type_id)
    docs = [d for d in docs if d.friendly_name.lower().endswith("csv")]
    docs.sort(key=lambda d: d.publish_date, reverse=True)
    if limit:
        docs = docs[:limit]
    peaks: dict[str, Decimal] = {}
    for d in docs:
        try:
            rows = parse_actual_load(download_document(d.doc_id))
        except Exception:  # noqa: BLE001 - best-effort backfill
            continue
        for day, peak in daily_peak(rows).items():
            day = _norm_day(day)
            peaks[day] = max(peaks.get(day, Decimal("-Infinity")), peak)
    return peaks


def parse_forecast(blob: bytes) -> dict[str, Decimal]:
    """Return {delivery_day: peak_system_total} from a 7-day load-forecast CSV."""
    rows = _csv_from_zip(blob)
    if not rows:
        return {}
    header = [h.strip() for h in rows[0]]
    di = header.index("DeliveryDate")
    si = header.index("SystemTotal")
    by_day: dict[str, Decimal] = {}
    for r in rows[1:]:
        if len(r) <= max(di, si):
            continue
        v = _dec(r[si])
        if v is None:
            continue
        day = _norm_day(r[di])
        by_day[day] = max(by_day.get(day, Decimal("-Infinity")), v)
    return by_day


def forecast_peaks(report_type_id: int, limit: int | None = None) -> dict[tuple[str, str], Decimal]:
    """Return {(publish_day, delivery_day): peak_system_total}.

    ``publish_day`` is the forecast-vintage date (YYYY-MM-DD); the latest forecast
    on a given publish day wins for a given delivery day.
    """
    docs = [d for d in list_documents(report_type_id) if d.friendly_name.lower().endswith("csv")]
    docs.sort(key=lambda d: d.publish_date, reverse=True)
    if limit:
        docs = docs[:limit]
    out: dict[tuple[str, str], Decimal] = {}
    for d in docs:
        try:
            peaks = parse_forecast(download_document(d.doc_id))
        except Exception:  # noqa: BLE001
            continue
        pub_day = d.publish_date[:10]
        for day, peak in peaks.items():
            out[(pub_day, day)] = peak  # later publish wins within a day
    return out
