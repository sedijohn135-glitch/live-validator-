"""New York time, kill zones, macros, market hours and closed-candle logic.

Every window is half-open `[start, end)` in New York time and is checked against the close time of
the deciding candle (validation-rules §1, §4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

NY_FMT = "%Y-%m-%d %H:%M"


@dataclass(frozen=True)
class Window:
    name: str
    start_min: int
    end_min: int
    weekdays: tuple[int, ...] | None = None  # Monday = 0

    def contains(self, moment: datetime) -> bool:
        ny = to_ny(moment)
        if self.weekdays is not None and ny.weekday() not in self.weekdays:
            return False
        minutes = ny.hour * 60 + ny.minute
        return self.start_min <= minutes < self.end_min

    def start_on(self, day: date) -> datetime:
        return ny_datetime(day, self.start_min)

    def end_on(self, day: date) -> datetime:
        return ny_datetime(day, self.end_min)


def _w(name: str, start: str, end: str, weekdays: tuple[int, ...] | None = None) -> Window:
    return Window(name, _hhmm(start), _hhmm(end), weekdays)


def _hhmm(value: str) -> int:
    hh, mm = value.split(":")
    return int(hh) * 60 + int(mm)


WINDOWS: dict[str, Window] = {
    w.name: w
    for w in (
        _w("LONDON_OR", "01:30", "02:00"),
        _w("LONDON_KZ", "02:00", "05:00"),
        _w("LONDON_SB", "03:00", "04:00"),
        _w("NY_OR", "07:00", "07:30"),
        _w("NY_KZ", "07:00", "10:00"),
        _w("NY_2022", "07:00", "09:00"),
        _w("EQUITIES_OR", "09:30", "10:00"),
        _w("AM_SB", "10:00", "11:00"),
        _w("LONDON_CLOSE", "10:00", "12:00"),
        _w("LUNCH", "12:00", "13:00"),
        _w("PM_OR", "13:30", "14:00"),
        _w("PM_SB", "14:00", "15:00"),
        _w("PM_SESSION", "13:30", "16:00"),
        _w("LAST_HOUR", "15:00", "16:00"),
    )
}

# Windows advertised in the snapshot: EQUITIES_OR is for indices only and never applies here.
SNAPSHOT_WINDOWS = tuple(n for n in WINDOWS if n not in ("EQUITIES_OR", "LUNCH"))


MACROS: tuple[str, ...] = (
    "02:33",
    "04:03",
    "08:00",
    "09:00",
    "10:00",
    "11:00",
    "12:00",
    "13:20",
    "15:00",
    "15:15",
    "15:40",
    "15:50",
    "16:00",
)
MACRO_HALF_WIDTH_MIN = 10

def to_ny(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime")
    return moment.astimezone(NY)


def to_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime")
    return moment.astimezone(UTC)


def ny_datetime(day: date, minutes: int) -> datetime:
    """A NY-local datetime `minutes` after midnight, DST-safe (24:00 rolls to the next day)."""
    base = datetime.combine(day, time(0, 0), tzinfo=NY)
    return base + timedelta(minutes=minutes)


def from_epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def epoch(moment: datetime) -> float:
    return moment.timestamp()


def ny_string(moment: datetime | float) -> str:
    value = from_epoch(moment) if isinstance(moment, (int, float)) else moment
    return to_ny(value).strftime(NY_FMT)


def parse_ny(text: str) -> datetime:
    """Parse `YYYY-MM-DD HH:MM` as New York time. Raises `ValueError` on anything else."""
    cleaned = (text or "").strip().replace("T", " ")
    if cleaned.endswith("Z") or "+" in cleaned:
        raise ValueError("pda_formed_at_ny must be a naive New York time 'YYYY-MM-DD HH:MM'")
    parsed = datetime.strptime(cleaned[:16], NY_FMT)
    return parsed.replace(tzinfo=NY)


def active_windows(moment: datetime, include_lunch: bool = False) -> list[str]:
    names = [n for n in SNAPSHOT_WINDOWS if WINDOWS[n].contains(moment)]
    if include_lunch and WINDOWS["LUNCH"].contains(moment):
        names.append("LUNCH")
    return names


def next_windows(moment: datetime, limit: int = 4) -> list[tuple[str, str]]:
    ny = to_ny(moment)
    minutes_now = ny.hour * 60 + ny.minute
    upcoming: list[tuple[int, str, str]] = []
    for name in SNAPSHOT_WINDOWS:
        window = WINDOWS[name]
        if window.start_min > minutes_now:
            upcoming.append((window.start_min, name, f"{window.start_min // 60:02d}:{window.start_min % 60:02d}"))
    upcoming.sort()
    return [(name, label) for _, name, label in upcoming[:limit]]


def active_macro(moment: datetime) -> str | None:
    ny = to_ny(moment)
    minutes = ny.hour * 60 + ny.minute
    for macro in MACROS:
        centre = _hhmm(macro)
        if abs(minutes - centre) <= MACRO_HALF_WIDTH_MIN:
            return macro
    return None


def in_lunch(moment: datetime) -> bool:
    return WINDOWS["LUNCH"].contains(moment)


def is_market_open(symbol: str, moment: datetime) -> bool:
    """CFD hours. XAUUSD: Fri 17:00 → Sun 18:00 closed. Daily break 17:00–18:00 for both symbols."""
    ny = to_ny(moment)
    minutes = ny.hour * 60 + ny.minute
    weekday = ny.weekday()  # Mon=0 … Sun=6
    if 17 * 60 <= minutes < 18 * 60:
        return False
    if symbol.upper() == "XAUUSD":
        if weekday == 4 and minutes >= 17 * 60:
            return False
        if weekday == 5:
            return False
        if weekday == 6 and minutes < 18 * 60:
            return False
    return True


def is_weekend(moment: datetime) -> bool:
    return to_ny(moment).weekday() >= 5


def friday_cutoff_reached(symbol: str, moment: datetime, cutoff: str) -> bool:
    ny = to_ny(moment)
    if symbol.upper() != "XAUUSD" or ny.weekday() != 4:
        return False
    return ny.hour * 60 + ny.minute >= _hhmm(cutoff)


def daily_close(moment: datetime) -> datetime:
    """Next 17:00 NY at or after `moment`."""
    ny = to_ny(moment)
    today_close = ny_datetime(ny.date(), 17 * 60)
    return today_close if ny < today_close else ny_datetime(ny.date() + timedelta(days=1), 17 * 60)


def friday_close(moment: datetime, cutoff: str) -> datetime | None:
    """The XAUUSD Friday cutoff of the current week if it is still ahead of `moment`."""
    ny = to_ny(moment)
    days_ahead = (4 - ny.weekday()) % 7
    friday = ny.date() + timedelta(days=days_ahead)
    cut = ny_datetime(friday, _hhmm(cutoff))
    return cut if cut > ny else None


def next_weekday_at(moment: datetime, weekday: int, minutes: int) -> datetime:
    """The next occurrence of `weekday` at `minutes` past New York midnight, at or after `moment`."""
    ny = to_ny(moment)
    ahead = (weekday - ny.weekday()) % 7
    candidate = ny_datetime(ny.date() + timedelta(days=ahead), minutes)
    if candidate <= ny:
        candidate = ny_datetime(ny.date() + timedelta(days=ahead + 7), minutes)
    return candidate


def candle_is_closed(open_ts: float, tf_seconds: int, now_ts: float, grace_s: float) -> bool:
    return now_ts >= open_ts + tf_seconds + grace_s


def candle_close_time(open_ts: float, tf_seconds: int) -> float:
    return open_ts + tf_seconds
