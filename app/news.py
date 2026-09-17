"""Optional USD high-impact news blackout. Fails open: a broken feed never blocks a trade."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.timeutil import NY, from_epoch, to_ny

logger = logging.getLogger(__name__)

REFRESH_S = 4 * 3600


@dataclass
class NewsEvent:
    ts: float
    title: str
    currency: str = "USD"
    impact: str = "High"


@dataclass
class NewsCache:
    """Holds the parsed calendar. `ok` is False while the feed is unreachable (fail-open)."""

    events: list[NewsEvent] = field(default_factory=list)
    fetched_at: float = 0.0
    ok: bool = True
    error: str = ""

    def stale(self, now: float) -> bool:
        return now - self.fetched_at > REFRESH_S

    def blackout(self, now: float, before_min: int, after_min: int) -> str | None:
        """The title of the event whose blackout window contains `now`, if any."""
        if not self.ok:
            return None
        for event in self.events:
            if event.ts - before_min * 60 <= now <= event.ts + after_min * 60:
                return event.title
        return None

    def upcoming(self, now: float, hours: float = 12.0) -> list[list[str]]:
        horizon = now + hours * 3600
        out = []
        for event in sorted(self.events, key=lambda e: e.ts):
            if now <= event.ts <= horizon:
                out.append([to_ny(from_epoch(event.ts)).strftime("%H:%M"), event.title])
        return out[:5]

    def post_holiday(self, now: float) -> bool:
        """True on the first New York weekday after a USD bank holiday (v11 rule 23).

        On a Monday the gap covers the whole weekend, so every skipped day is checked.
        """
        ny_now = to_ny(from_epoch(now))
        if ny_now.weekday() >= 5:
            return False
        back = 3 if ny_now.weekday() == 0 else 1
        return any(_is_holiday(ny_now - timedelta(days=days)) for days in range(1, back + 1))


US_HOLIDAYS_MD = {
    (1, 1),
    (7, 4),
    (12, 25),
    (11, 27),  # Thanksgiving (approximate: the feed is the authority when available)
}


def _is_holiday(moment: datetime) -> bool:
    return (moment.month, moment.day) in US_HOLIDAYS_MD


def parse_feed(payload: str, impacts: tuple[str, ...] = ("High",)) -> list[NewsEvent]:
    """Parse the Forex Factory weekly JSON. Unknown shapes yield no events rather than raising."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    events: list[NewsEvent] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if str(item.get("country") or item.get("currency") or "").upper() != "USD":
            continue
        if str(item.get("impact") or "").capitalize() not in impacts:
            continue
        stamp = _parse_time(item.get("date") or item.get("dateline"))
        if stamp is None:
            continue
        events.append(NewsEvent(stamp, str(item.get("title") or "USD event")))
    return events


def _parse_time(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=NY)
    return parsed.timestamp()


async def fetch(url: str, cache: NewsCache, now: float | None = None, fetcher=None) -> NewsCache:
    """Refresh the cache. Any failure leaves `ok=False` and no blackout (failure mode E13)."""
    now = time.time() if now is None else now
    try:
        if fetcher is None:
            import httpx2

            async with httpx2.AsyncClient(timeout=httpx2.Timeout(15.0)) as client:
                response = await client.get(url)
                payload = response.text
        else:
            payload = await fetcher(url)
        events = parse_feed(payload)
        cache.events = events
        cache.ok = True
        cache.error = ""
    except Exception as exc:  # noqa: BLE001 - fail open on any feed problem
        cache.ok = False
        cache.error = str(exc)[:200]
        logger.info("news feed unavailable: %s", cache.error)
    cache.fetched_at = now
    return cache
