"""Provider adapters for shadow-external-v0 (Phase 1).

FRED/ALFRED -> EIA -> NWS -> SEC -> Fed -> GDELT. FRED and EIA need free API keys
loaded from env; NWS/SEC/Fed/GDELT are keyless. Observation-only; honest about
freshness/coverage.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from .core import (
    SCHEMA_VERSION,
    AlertValue,
    Coverage,
    ExternalObservation,
    FilingValue,
    Freshness,
    ScalarValue,
    SourceClass,
    TextValue,
    observation_id,
    revision_key,
    value_hash,
)

_UA = {"User-Agent": "polyalpha-research/0.1"}


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    if dt is None:
        try:
            dt = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _parse_rss_date(value: str | None) -> datetime | None:
    if not value:
        return None
    iso = _iso(value)
    if iso:
        return iso
    try:
        dt = parsedate_to_datetime(value.strip())
        return dt.astimezone(UTC) if dt else None
    except (ValueError, TypeError):
        return None


def _text(element, *names: str) -> str | None:
    for name in names:
        child = element.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return None


def _parse_rss(xml_bytes: bytes) -> list[tuple[str, str | None, str | None, str | None, datetime | None, datetime | None]]:
    """Return [(event_id, title, link, summary, published, claimed)] for RSS/Atom."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    channel = root.find("channel")
    if channel is not None:
        items = []
        for item in channel.findall("item"):
            eid = _text(item, "guid") or _text(item, "link") or _text(item, "title")
            if not eid:
                continue
            pub = _parse_rss_date(_text(item, "pubDate"))
            items.append((eid, _text(item, "title"), _text(item, "link"),
                          _text(item, "description"), pub, pub))
        return items
    items = []
    for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
        eid = _text(entry, "{http://www.w3.org/2005/Atom}id") or _text(entry, "{http://www.w3.org/2005/Atom}link")
        if not eid:
            continue
        published = _parse_rss_date(_text(entry, "{http://www.w3.org/2005/Atom}published"))
        updated = _parse_rss_date(_text(entry, "{http://www.w3.org/2005/Atom}updated"))
        items.append((eid, _text(entry, "{http://www.w3.org/2005/Atom}title"),
                      _text(entry, "{http://www.w3.org/2005/Atom}link"),
                      _text(entry, "{http://www.w3.org/2005/Atom}summary") or _text(entry, "{http://www.w3.org/2005/Atom}content"),
                      published or updated, published))
    return items


def _get_json(url: str, timeout: float = 20.0, retries: int = 3) -> dict:
    delay = 2.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2.0
                continue
            raise


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"missing environment variable {name}")
    return value


def _obs(
    provider: str, source_class: SourceClass, freshness: Freshness, coverage: Coverage,
    external_id: str, value, event_time: datetime | None, published: datetime | None,
    publisher: str | None, url: str | None, region: str | None, revision: str | None,
    claimed: datetime | None = None, transport: str = "REST",
) -> ExternalObservation:
    vh = value_hash(value)
    rk = revision_key(provider, external_id, event_time, revision, vh)
    oid = observation_id(SCHEMA_VERSION, rk)
    return ExternalObservation(
        observation_id=oid, provider=provider, source_class=source_class,
        external_id=external_id, value=value, publisher_or_authority=publisher,
        event_time=event_time, provider_published_at=published, provider_updated_at=None,
        source_claimed_publish_at=claimed if claimed is not None else published,
        request_started_wall_ns=0, received_wall_ns=0, received_monotonic_ns=0,
        region=region, source_url=url, raw_payload_sha256="", content_sha256="",
        collector_session_id="", fetch_sequence=0, transport=transport,
        freshness_class=freshness, coverage_class=coverage,
        provider_revision=revision, value_hash=vh,
    )


class FredProvider:
    """FRED/ALFRED series observations (vintage-aware)."""

    name = "fred"
    source_class = SourceClass.MACRO
    freshness_class = Freshness.HISTORICAL
    coverage_class = Coverage.FULL

    def __init__(self, series_ids: list[str], vintage_start: str | None = None):
        self.series_ids = series_ids
        self.vintage_start = vintage_start

    def fetch(self, since: datetime | None) -> list[ExternalObservation]:
        key = _env("FRED_API_KEY")
        out: list[ExternalObservation] = []
        for sid in self.series_ids:
            url = f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}&api_key={key}&file_type=json"
            if self.vintage_start:
                url += f"&realtime_start={self.vintage_start}"
            data = _get_json(url)
            for row in data.get("observations", []):
                raw = row.get("value", ".")
                if raw in (".", "", None):
                    continue
                try:
                    v = float(raw)
                except ValueError:
                    continue
                period = row.get("date")
                vintage = row.get("realtime_end")
                value = ScalarValue(value=v, frequency=row.get("frequency_short"),
                                    period=period, as_of=_iso(vintage), revision=vintage)
                out.append(_obs(
                    self.name, self.source_class, self.freshness_class, self.coverage_class,
                    external_id=f"{sid}:{period}", value=value, event_time=_iso(period),
                    published=_iso(vintage), publisher="FRED", url=url, region="US",
                    revision=vintage, claimed=_iso(vintage),
                ))
        return out


class EiaProvider:
    """EIA energy series (multidimensional structured data)."""

    name = "eia"
    source_class = SourceClass.ENERGY
    freshness_class = Freshness.HISTORICAL
    coverage_class = Coverage.FULL

    def __init__(self, routes: list[str]):
        self.routes = routes

    def fetch(self, since: datetime | None) -> list[ExternalObservation]:
        key = _env("EIA_API_KEY")
        out: list[ExternalObservation] = []
        for route in self.routes:
            url = f"https://api.eia.gov/v2/{route}/data/?api_key={key}&data[0]=value"
            data = _get_json(url)
            series_id = data.get("response", {}).get("id", route)
            for row in data.get("response", {}).get("data", []):
                period = row.get("period")
                try:
                    v = float(row.get("value"))
                except (TypeError, ValueError):
                    continue
                value = ScalarValue(value=v, units=row.get("units"), period=period)
                out.append(_obs(
                    self.name, self.source_class, self.freshness_class, self.coverage_class,
                    external_id=f"{series_id}:{period}", value=value, event_time=_iso(period),
                    published=None, publisher="EIA", url=url, region="US", revision=None,
                ))
        return out


class NwsProvider:
    """NWS weather alerts (keyless)."""

    name = "nws"
    source_class = SourceClass.WEATHER
    freshness_class = Freshness.NEAR_REAL_TIME
    coverage_class = Coverage.FULL

    def __init__(self, state_codes: list[str] | None = None):
        self.state_codes = state_codes

    def fetch(self, since: datetime | None) -> list[ExternalObservation]:
        urls = [f"https://api.weather.gov/alerts/active/area/{s}" for s in (self.state_codes or [])]
        urls = urls or ["https://api.weather.gov/alerts/active"]
        out: list[ExternalObservation] = []
        for url in urls:
            data = _get_json(url)
            for feat in data.get("features", []):
                props = feat.get("properties", {}) or {}
                eid = props.get("id") or feat.get("id")
                if not eid:
                    continue
                value = AlertValue(
                    severity=props.get("severity"), certainty=props.get("certainty"),
                    urgency=props.get("urgency"), effective=_iso(props.get("effective")),
                    expires=_iso(props.get("expires")),
                )
                out.append(_obs(
                    self.name, self.source_class, self.freshness_class, self.coverage_class,
                    external_id=eid, value=value, event_time=_iso(props.get("effective")),
                    published=_iso(props.get("sent")), publisher=props.get("senderName"),
                    url=url, region=props.get("areaDesc"), revision=None,
                ))
        return out


class SecProvider:
    """SEC EDGAR filings (keyless)."""

    name = "sec_edgar"
    source_class = SourceClass.REGULATORY
    freshness_class = Freshness.NEAR_REAL_TIME
    coverage_class = Coverage.FULL

    def __init__(self, ciks: list[str]):
        self.ciks = ciks

    def fetch(self, since: datetime | None) -> list[ExternalObservation]:
        out: list[ExternalObservation] = []
        for cik in self.ciks:
            url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
            data = _get_json(url)
            recent = data.get("filings", {}).get("recent", {})
            accs = recent.get("accessionNumber", [])
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            for i, acc in enumerate(accs):
                form = forms[i] if i < len(forms) else None
                fdate = dates[i] if i < len(dates) else None
                value = FilingValue(accession=acc, form=form, cik=cik, filing_date=_iso(fdate))
                out.append(_obs(
                    self.name, self.source_class, self.freshness_class, self.coverage_class,
                    external_id=f"{cik}:{acc}", value=value, event_time=_iso(fdate),
                    published=_iso(fdate), publisher="SEC", url=url, region="US", revision=None,
                ))
        return out


class RssProvider:
    """Official RSS/Atom feed (keyless)."""

    def __init__(self, name: str, source_class: SourceClass, feed_urls: list[str]):
        self.name = name
        self.source_class = source_class
        self.freshness_class = Freshness.NEAR_REAL_TIME
        self.coverage_class = Coverage.FULL
        self.feed_urls = feed_urls

    def fetch(self, since: datetime | None) -> list[ExternalObservation]:
        out: list[ExternalObservation] = []
        for url in self.feed_urls:
            try:
                req = urllib.request.Request(url, headers=_UA)
                with urllib.request.urlopen(req, timeout=20.0) as resp:
                    body = resp.read()
            except Exception:  # noqa: BLE001
                continue
            for eid, title, link, summary, published, claimed in _parse_rss(body):
                if since is not None and published is not None and published <= since:
                    continue
                value = TextValue(headline=title, summary=summary, language=None)
                out.append(_obs(
                    self.name, self.source_class, self.freshness_class, self.coverage_class,
                    external_id=eid, value=value, event_time=published, published=published,
                    publisher=None, url=link, region=None, revision=None, claimed=claimed,
                ))
        return out


class GdeltProvider:
    """GDELT DOC 2.0 broad news (DELAYED ~15m, keyless)."""

    name = "gdelt"
    source_class = SourceClass.NEWS
    freshness_class = Freshness.DELAYED
    coverage_class = Coverage.FULL

    def __init__(self, query: str, minutes_back: int = 30):
        self.query = query
        self.minutes_back = minutes_back

    def fetch(self, since: datetime | None) -> list[ExternalObservation]:
        url = (
            f"https://api.gdeltproject.org/api/v2/doc/doc?query={urllib.parse.quote(self.query)}"
            f"&mode=artlist&maxrecords=250&format=json&timespan={self.minutes_back}m"
        )
        data = _get_json(url)
        out: list[ExternalObservation] = []
        for art in data.get("articles", []):
            u = art.get("url")
            if not u:
                continue
            value = TextValue(headline=art.get("title"), summary=None, language=art.get("language"))
            published = _iso(art.get("seendate")) or _iso(art.get("date"))
            out.append(_obs(
                self.name, self.source_class, self.freshness_class, self.coverage_class,
                external_id=u, value=value, event_time=published, published=published,
                publisher=art.get("domain"), url=u, region=None, revision=None,
            ))
        return out
