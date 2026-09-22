"""Primary-source RSS news provider (no API key required).

Parses RSS 2.0 / Atom feeds from authoritative publishers (BLS, Fed, Treasury,
SEC, agency press releases, league/team feeds) into ``RawNewsItem`` records.
For a primary-source feed, the item's publish time IS the primary clock t1, so
``source_claimed_publish_at`` and ``provider_published_at`` coincide here —
unlike a wire vendor, where they differ.

Stdlib only (xml.etree + urllib + email), no feedparser dependency.
"""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from .news import NewsRole, RawNewsItem


def _parse_rss_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:  # Atom / ISO 8601
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        pass
    try:  # RSS 2.0 RFC 822
        dt = parsedate_to_datetime(value.strip())
        return dt.astimezone(UTC) if dt else None
    except (ValueError, TypeError):
        return None


def _text(element: Any, *names: str) -> str | None:
    for name in names:
        child = element.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return None


def _parse_feed(xml_bytes: bytes) -> list[RawNewsItem]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    # RSS 2.0: <channel><item>...
    channel = root.find("channel")
    if channel is not None:
        publisher = _text(channel, "title")
        items: list[RawNewsItem] = []
        for item in channel.findall("item"):
            guid = _text(item, "guid") or _text(item, "link") or _text(item, "title")
            if not guid:
                continue
            items.append(RawNewsItem(
                provider_event_id=guid,
                publisher=publisher,
                url=_text(item, "link"),
                headline=_text(item, "title"),
                body_or_summary=_text(item, "description"),
                provider_published_at=_parse_rss_date(_text(item, "pubDate")),
                provider_updated_at=None,
                source_claimed_publish_at=_parse_rss_date(_text(item, "pubDate")),
            ))
        return items

    # Atom: <feed><entry>...
    publisher = _text(root, "title")
    items = []
    for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
        eid = _text(entry, "{http://www.w3.org/2005/Atom}id") or _text(
            entry, "{http://www.w3.org/2005/Atom}link")
        title = _text(entry, "{http://www.w3.org/2005/Atom}title")
        if not eid:
            continue
        items.append(RawNewsItem(
            provider_event_id=eid,
            publisher=publisher,
            url=_text(entry, "{http://www.w3.org/2005/Atom}link"),
            headline=title,
            body_or_summary=_text(entry, "{http://www.w3.org/2005/Atom}summary")
            or _text(entry, "{http://www.w3.org/2005/Atom}content"),
            provider_published_at=_parse_rss_date(_text(
                entry, "{http://www.w3.org/2005/Atom}published")
                or _text(entry, "{http://www.w3.org/2005/Atom}updated")),
            provider_updated_at=_parse_rss_date(_text(entry, "{http://www.w3.org/2005/Atom}updated")),
            source_claimed_publish_at=_parse_rss_date(_text(
                entry, "{http://www.w3.org/2005/Atom}published")),
        ))
    return items


class RssNewsProvider:
    """A primary-source RSS provider (role=PRIMARY), no API key."""

    name: str
    role = NewsRole.PRIMARY

    def __init__(self, name: str, feed_urls: list[str], timeout: float = 20.0):
        self.name = name
        self.feed_urls = feed_urls
        self.timeout = timeout

    def fetch(self, since: datetime | None) -> list[RawNewsItem]:
        out: list[RawNewsItem] = []
        for url in self.feed_urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "polyalpha-research/0.1"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
            except Exception:  # noqa: BLE001 - a feed outage must not kill the pass
                continue
            for item in _parse_feed(body):
                if since is not None and item.provider_published_at is not None:
                    if item.provider_published_at <= since:
                        continue
                out.append(item)
        return out
