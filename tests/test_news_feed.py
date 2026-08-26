"""Unit tests for provider-neutral news adapters."""

from datetime import datetime, timezone
from types import SimpleNamespace

from news_feed import AlpacaNewsSource, NewsItem, deduplicate_news, parse_rss_payload


RSS = b"""
<rss version="2.0"><channel>
  <item><guid>one</guid><title>CPI cools</title>
    <description><![CDATA[<b>Inflation summary</b>]]></description>
    <link>https://example.test/cpi</link>
    <pubDate>Mon, 24 Aug 2026 12:00:00 GMT</pubDate></item>
  <item><guid>one</guid><title>CPI cools revised</title>
    <description>Updated summary</description>
    <link>https://example.test/cpi</link>
    <pubDate>Mon, 24 Aug 2026 12:05:00 GMT</pubDate></item>
</channel></rss>
"""


def test_rss_parser_cleans_metadata_and_deduplicates():
    items = parse_rss_payload(RSS, "https://example.test/feed", symbols=("SPY",))
    assert len(items) == 2
    assert items[0].headline == "CPI cools"
    assert items[0].summary == "Inflation summary"
    assert items[0].published_at.tzinfo == timezone.utc

    unique = deduplicate_news(items)
    assert len(unique) == 1
    assert unique[0].headline == "CPI cools revised"


def test_rss_parser_collapses_whitespace_after_html_cleanup():
    payload = b"""
    <rss version="2.0"><channel>
      <item><guid>whitespace</guid>
        <title>  CPI\n             cools   </title>
        <description><![CDATA[ <b> Inflation   summary </b>\n
          &nbsp; details ]]></description>
        <link>https://example.test/whitespace</link>
        <pubDate>Mon, 24 Aug 2026 12:00:00 GMT</pubDate></item>
    </channel></rss>
    """

    items = parse_rss_payload(payload, "https://example.test/feed")

    assert items[0].headline == "CPI cools"
    assert items[0].summary == "Inflation summary details"


def test_alpaca_news_source_maps_response_and_request():
    created = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    article = SimpleNamespace(
        id="alpaca-1",
        headline="ETF headline",
        summary="Short summary",
        source="Newswire",
        url="https://example.test/story",
        created_at=created,
        updated_at=created,
        symbols=["SPY"],
    )

    class FakeClient:
        def __init__(self):
            self.request = None

        def get_news(self, request):
            self.request = request
            return SimpleNamespace(data={"news": [article]})

    fake = FakeClient()
    config = SimpleNamespace(
        alpaca=SimpleNamespace(api_key="key", secret_key="secret")
    )
    source = AlpacaNewsSource(config, client=fake)
    result = source.fetch_since(
        ["spy"], datetime(2026, 8, 24, 11, tzinfo=timezone.utc)
    )

    assert result[0].item_id == "alpaca-1"
    assert result[0].symbols == ("SPY",)
    assert fake.request.limit == 50
    assert fake.request.symbols == "SPY"


def test_alpaca_news_source_follows_page_tokens():
    created = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    first = SimpleNamespace(
        id="page-1",
        headline="First",
        summary="",
        source="A",
        url="https://example.test/1",
        created_at=created,
        updated_at=created,
        symbols=["SPY"],
    )
    second = SimpleNamespace(
        id="page-2",
        headline="Second",
        summary="",
        source="B",
        url="https://example.test/2",
        created_at=created,
        updated_at=created,
        symbols=["SPY"],
    )

    class PagedClient:
        def __init__(self):
            self.calls = []

        def get_news(self, request):
            self.calls.append(getattr(request, "page_token", None))
            if len(self.calls) == 1:
                return SimpleNamespace(data={"news": [first]}, next_page_token="next")
            return SimpleNamespace(data={"news": [second]}, next_page_token=None)

    client = PagedClient()
    source = AlpacaNewsSource(
        SimpleNamespace(alpaca=SimpleNamespace(api_key="k", secret_key="s")),
        client=client,
    )
    result = source.fetch_since(["SPY"], created - __import__("datetime").timedelta(minutes=1))
    assert [item.item_id for item in result] == ["page-1", "page-2"]
    assert len(client.calls) == 2
