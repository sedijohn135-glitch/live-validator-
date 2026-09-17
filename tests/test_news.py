"""The news blackout: correct while the feed works, invisible when it does not."""

from __future__ import annotations

import asyncio
import json

from app.news import NewsCache, NewsEvent, fetch, parse_feed
from tests.helpers import ts


def test_parse_feed_keeps_only_usd_high_impact():
    payload = json.dumps(
        [
            {"title": "CPI m/m", "country": "USD", "impact": "High", "date": "2026-09-16T08:30:00-04:00"},
            {"title": "Retail", "country": "USD", "impact": "Low", "date": "2026-09-16T08:30:00-04:00"},
            {"title": "ECB", "country": "EUR", "impact": "High", "date": "2026-09-16T08:30:00-04:00"},
            {"title": "broken", "country": "USD", "impact": "High", "date": "not a date"},
        ]
    )
    events = parse_feed(payload)
    assert [e.title for e in events] == ["CPI m/m"]


def test_parse_feed_survives_garbage():
    assert parse_feed("not json") == []
    assert parse_feed('{"unexpected": true}') == []


def test_blackout_window():
    cache = NewsCache(events=[NewsEvent(ts("2026-09-16 08:30"), "CPI m/m")], ok=True)
    assert cache.blackout(ts("2026-09-16 08:20"), 15, 15) == "CPI m/m"
    assert cache.blackout(ts("2026-09-16 08:44"), 15, 15) == "CPI m/m"
    assert cache.blackout(ts("2026-09-16 08:46"), 15, 15) is None
    assert cache.blackout(ts("2026-09-16 08:10"), 15, 15) is None


def test_a_broken_feed_never_blocks_a_trade():
    """Failure mode E13: fail open, with a warning line in the ENTER message."""
    cache = NewsCache(events=[NewsEvent(ts("2026-09-16 08:30"), "CPI m/m")], ok=False)
    assert cache.blackout(ts("2026-09-16 08:25"), 15, 15) is None


def test_fetch_failure_marks_the_cache_not_ok():
    cache = NewsCache()

    async def failing(_url):
        raise OSError("dns failure")

    asyncio.run(fetch("https://example.invalid/feed", cache, ts("2026-09-16 08:00"), fetcher=failing))
    assert cache.ok is False and "dns" in cache.error
    assert cache.blackout(ts("2026-09-16 08:00"), 15, 15) is None


def test_fetch_success_populates_events():
    cache = NewsCache()
    payload = json.dumps(
        [{"title": "NFP", "country": "USD", "impact": "High", "date": "2026-09-18T08:30:00-04:00"}]
    )

    async def ok(_url):
        return payload

    asyncio.run(fetch("https://example.test/feed", cache, ts("2026-09-16 08:00"), fetcher=ok))
    assert cache.ok and [e.title for e in cache.events] == ["NFP"]
    assert cache.upcoming(ts("2026-09-18 06:00"), hours=12) == [["08:30", "NFP"]]


def test_post_holiday_flag():
    cache = NewsCache()
    assert cache.post_holiday(ts("2026-07-06 09:00"))  # Monday after 4 July
    assert not cache.post_holiday(ts("2026-09-16 09:00"))
