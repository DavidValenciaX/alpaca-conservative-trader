"""News ingestion adapters used by the fundamental analysis service.

The adapters return a small, provider-neutral ``NewsItem`` model.  They never
scrape article pages: only metadata and summaries supplied by Alpaca or RSS
feeds are passed to the analysis layer.
"""

from __future__ import annotations

import calendar
import hashlib
import html
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import struct_time
from typing import Any, Iterable, List, Optional, Protocol, Sequence
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from config import AppConfig
from logger import get_logger

try:  # Optional at import time so the technical bot still runs before install.
    import feedparser  # type: ignore
except ImportError:  # pragma: no cover - exercised only in minimal installations
    feedparser = None


log = get_logger()


class NewsFeedError(Exception):
    """Raised when a news provider cannot return data."""


@dataclass(frozen=True)
class NewsItem:
    """Provider-neutral news metadata and summary."""

    item_id: str
    headline: str
    summary: str
    source: str
    url: str
    published_at: datetime
    updated_at: datetime
    symbols: tuple[str, ...] = ()
    category: str = "market_news"

    @property
    def dedupe_key(self) -> str:
        """Return a stable key even when a feed omits an explicit ID."""
        if self.url:
            return f"url:{self.url}"
        if self.item_id:
            return f"{self.source}:{self.item_id}"
        raw = f"{self.source}|{self.url}|{self.headline}|{self.published_at.isoformat()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_context(self, max_chars: int = 1200) -> dict[str, Any]:
        """Return bounded text suitable for an LLM prompt."""
        return {
            "id": self.dedupe_key,
            "headline": self.headline[:max_chars],
            "summary": self.summary[:max_chars],
            "source": self.source,
            "url": self.url,
            "published_at": self.published_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "symbols": list(self.symbols),
            "category": self.category,
        }


class NewsSource(Protocol):
    """Interface implemented by all news providers."""

    def fetch_since(
        self,
        symbols: Sequence[str],
        since: datetime,
        until: Optional[datetime] = None,
    ) -> List[NewsItem]:
        """Return items published or updated in the requested interval."""


def _as_utc(value: Optional[datetime], fallback: Optional[datetime] = None) -> datetime:
    if value is None:
        return fallback or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    return re.sub(r"<[^>]+>", " ", text).strip()


def _parse_feed_datetime(value: Any, fallback: Optional[datetime] = None) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value, fallback)
    if isinstance(value, struct_time):
        return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), fallback)
        except ValueError:
            try:
                return _as_utc(parsedate_to_datetime(value), fallback)
            except (TypeError, ValueError, OverflowError):
                pass
    return fallback or datetime.now(timezone.utc)


def _xml_value(entry: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        node = entry.find(name)
        if node is not None and node.text:
            return node.text
    return ""


def parse_rss_payload(
    payload: bytes,
    feed_url: str,
    symbols: Sequence[str] = (),
) -> List[NewsItem]:
    """Parse RSS/Atom bytes without making a network call.

    ``feedparser`` is preferred when installed.  A small stdlib XML fallback
    keeps the feature importable in a minimal environment and makes malformed
    feeds fail locally instead of affecting the trading loop.
    """
    if feedparser is not None:
        parsed = feedparser.parse(payload)
        if getattr(parsed, "bozo", False) and not getattr(parsed, "entries", None):
            raise NewsFeedError(f"Invalid RSS payload from {feed_url}")
        entries = []
        for entry in getattr(parsed, "entries", []):
            published = _parse_feed_datetime(
                getattr(entry, "published_parsed", None)
                or getattr(entry, "updated_parsed", None)
            )
            updated = _parse_feed_datetime(
                getattr(entry, "updated_parsed", None), published
            )
            link = str(getattr(entry, "link", "") or "")
            item_id = str(getattr(entry, "id", "") or link)
            entries.append(
                NewsItem(
                    item_id=item_id,
                    headline=_clean_text(getattr(entry, "title", "")),
                    summary=_clean_text(
                        getattr(entry, "summary", "")
                        or getattr(entry, "description", "")
                    ),
                    source=f"rss:{feed_url}",
                    url=link,
                    published_at=published,
                    updated_at=updated,
                    symbols=tuple(symbols),
                    category="macro_rss",
                )
            )
        return entries

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise NewsFeedError(f"Invalid RSS payload from {feed_url}: {exc}") from exc

    entries: List[NewsItem] = []
    for entry in root.iter():
        tag = entry.tag.rsplit("}", 1)[-1].lower()
        if tag not in {"item", "entry"}:
            continue
        link = _xml_value(entry, ("link", "{http://www.w3.org/2005/Atom}link"))
        if not link:
            atom_link = entry.find("{http://www.w3.org/2005/Atom}link")
            link = str(atom_link.attrib.get("href", "")) if atom_link is not None else ""
        published_raw = _xml_value(
            entry,
            (
                "pubDate",
                "published",
                "updated",
                "{http://www.w3.org/2005/Atom}published",
                "{http://www.w3.org/2005/Atom}updated",
            ),
        )
        published = _parse_feed_datetime(published_raw)
        entries.append(
            NewsItem(
                item_id=_xml_value(
                    entry,
                    (
                        "guid",
                        "id",
                        "{http://www.w3.org/2005/Atom}id",
                    ),
                )
                or link,
                headline=_clean_text(
                    _xml_value(entry, ("title", "{http://www.w3.org/2005/Atom}title"))
                ),
                summary=_clean_text(
                    _xml_value(
                        entry,
                        (
                            "description",
                            "summary",
                            "content",
                            "{http://www.w3.org/2005/Atom}summary",
                        ),
                    )
                ),
                source=f"rss:{feed_url}",
                url=link,
                published_at=published,
                updated_at=published,
                symbols=tuple(symbols),
                category="macro_rss",
            )
        )
    return entries


def deduplicate_news(items: Iterable[NewsItem], limit: int = 100) -> List[NewsItem]:
    """Deduplicate by provider ID/URL and keep newest items first."""
    newest: dict[str, NewsItem] = {}
    for item in items:
        key = item.dedupe_key
        current = newest.get(key)
        if current is None or item.updated_at > current.updated_at:
            newest[key] = item
    return sorted(newest.values(), key=lambda item: item.updated_at, reverse=True)[:limit]


class AlpacaNewsSource:
    """Fetch market news through the Alpaca News API."""

    def __init__(self, config: AppConfig, client: Optional[NewsClient] = None) -> None:
        self._client = client or NewsClient(
            api_key=config.alpaca.api_key,
            secret_key=config.alpaca.secret_key,
        )

    def fetch_since(
        self,
        symbols: Sequence[str],
        since: datetime,
        until: Optional[datetime] = None,
    ) -> List[NewsItem]:
        normalized = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
        if not normalized:
            return []
        request = NewsRequest(
            start=_as_utc(since),
            end=_as_utc(until) if until else None,
            sort="desc",
            symbols=",".join(normalized),
            limit=50,
            include_content=False,
            exclude_contentless=False,
        )
        raw_items: List[Any] = []
        page_token: Optional[str] = None
        for _ in range(10):
            request_kwargs = {
                "start": _as_utc(since),
                "end": _as_utc(until) if until else None,
                "sort": "desc",
                "symbols": ",".join(normalized),
                "limit": 50,
                "include_content": False,
                "exclude_contentless": False,
            }
            if page_token:
                request_kwargs["page_token"] = page_token
            request = NewsRequest(**request_kwargs)
            response = self._client.get_news(request)
            data = getattr(response, "data", None)
            if isinstance(data, dict):
                for values in data.values():
                    if isinstance(values, list):
                        raw_items.extend(values)
            elif isinstance(response, list):
                raw_items.extend(response)
            next_token = getattr(response, "next_page_token", None)
            if not next_token and isinstance(data, dict):
                next_token = data.get("next_page_token")
            page_token = str(next_token) if next_token else None
            if not page_token:
                break

        result: List[NewsItem] = []
        for article in raw_items:
            published = _as_utc(getattr(article, "created_at", None))
            updated = _as_utc(getattr(article, "updated_at", None), published)
            result.append(
                NewsItem(
                    item_id=str(getattr(article, "id", "")),
                    headline=_clean_text(getattr(article, "headline", "")),
                    summary=_clean_text(getattr(article, "summary", "")),
                    source=_clean_text(getattr(article, "source", "alpaca")),
                    url=str(getattr(article, "url", "") or ""),
                    published_at=published,
                    updated_at=updated,
                    symbols=tuple(
                        str(symbol).upper()
                        for symbol in (getattr(article, "symbols", None) or normalized)
                    ),
                    category="market_news",
                )
            )
        return result


class RssNewsSource:
    """Fetch and parse a collection of official RSS/Atom feeds."""

    def __init__(self, urls: Sequence[str], timeout_seconds: float = 10.0) -> None:
        self._urls = tuple(str(url).strip() for url in urls if str(url).strip())
        self._timeout_seconds = timeout_seconds

    def fetch_since(
        self,
        symbols: Sequence[str],
        since: datetime,
        until: Optional[datetime] = None,
    ) -> List[NewsItem]:
        result: List[NewsItem] = []
        end = _as_utc(until) if until else datetime.now(timezone.utc)
        failed = 0
        for url in self._urls:
            try:
                request = Request(
                    url,
                    headers={"User-Agent": "trading-bot/1.0 (+economic-news-reader)"},
                )
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    payload = response.read()
                entries = parse_rss_payload(payload, url, symbols)
                result.extend(
                    item
                    for item in entries
                    if _as_utc(item.updated_at) >= _as_utc(since)
                    and _as_utc(item.updated_at) <= end
                )
            except Exception as exc:
                failed += 1
                log.warning(f"RSS source failed for {url}: {exc}")
        if self._urls and failed == len(self._urls):
            raise NewsFeedError("all configured RSS feeds failed")
        return deduplicate_news(result)


def retry_delay(attempt: int, base_seconds: float = 1.0) -> float:
    """Return an exponential backoff delay for provider callers."""
    return base_seconds * (2 ** max(0, attempt - 1))
