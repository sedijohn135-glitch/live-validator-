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

SB_WINDOWS = ("LONDON_SB", "AM_SB", "PM_SB")

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

MODEL_WINDOW_NAMES: dict[str, tuple[str, ...]] = {
    "ICT_2022": ("NY_2022",),
    "MARKET_ANCHOR": ("LONDON_KZ", "NY_KZ", "LONDON_CLOSE", "PM_SESSION"),
    "OTE": ("LONDON_KZ", "NY_KZ", "LONDON_CLOSE", "PM_SESSION"),
    "IOFED": ("LONDON_KZ", "NY_KZ", "LONDON_CLOSE", "PM_SESSION"),
    "LOW_RESISTANCE_RUN": ("LONDON_KZ", "NY_KZ", "LONDON_CLOSE", "PM_SESSION"),
    "VENOM": ("LONDON_KZ", "NY_KZ", "LONDON_CLOSE", "PM_SESSION"),
    "TURTLE_SOUP": ("LONDON_KZ", "NY_KZ"),
    "TURTLE_SOUP_DEFERRED": ("LONDON_KZ", "NY_KZ"),
    "SM_THREE_STAGE": ("LONDON_KZ", "NY_KZ", "PM_SESSION"),
}


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


def model_windows(model: str, formed_at: datetime | None, balanced: bool) -> list[Window]:
    """Windows where ENTER may be sent for `model` (validation-rules §4)."""
    if model in MODEL_WINDOW_NAMES:
        return [WINDOWS[n] for n in MODEL_WINDOW_NAMES[model]]
    if model == "SILVER_BULLET":
        if formed_at is None:
            return []
        return [WINDOWS[n] for n in SB_WINDOWS if WINDOWS[n].contains(formed_at)]
    if model == "MODEL_2":
        weekdays = (1, 2, 3) if balanced else (1,)
        return [_w("MODEL_2", "06:00", "10:00", weekdays)]
    if model == "OR_FIRST_FVG":
        if formed_at is None:
            return []
        if WINDOWS["LONDON_OR"].contains(formed_at):
            return [_w("OR_LONDON", "02:00", "05:00")]
        if WINDOWS["NY_OR"].contains(formed_at):
            return [_w("OR_NY", "07:30", "10:00")]
        return []
    if model == "OR_PM_FIRST_FVG":
        if formed_at is None or not WINDOWS["PM_OR"].contains(formed_at):
            return []
        return [_w("OR_PM", "14:00", "16:00")]
    if model == "LUNCH_MACRO_PM":
        return [_w("LUNCH_MACRO_PM", "13:30", "16:00") if balanced else _w("LUNCH_MACRO_PM", "14:00", "15:00")]
    return []


def in_model_windows(model: str, formed_at: datetime | None, balanced: bool, moment: datetime) -> str | None:
    for window in model_windows(model, formed_at, balanced):
        if window.contains(moment):
            return window.name
    return None


def next_window_end(windows: list[Window], moment: datetime, horizon: datetime) -> datetime | None:
    """The end of the last window that starts before `horizon` (validation-rules G-18)."""
    ny = to_ny(moment)
    best: datetime | None = None
    day = ny.date() - timedelta(days=1)
    limit = to_ny(horizon).date() + timedelta(days=1)
    while day <= limit:
        for window in windows:
            if window.weekdays is not None and day.weekday() not in window.weekdays:
                continue
            start = window.start_on(day)
            end = window.end_on(day)
            if end <= ny:
                continue
            if start < horizon and (best is None or end > best):
                best = end
        day += timedelta(days=1)
    return best


def candle_is_closed(open_ts: float, tf_seconds: int, now_ts: float, grace_s: float) -> bool:
    return now_ts >= open_ts + tf_seconds + grace_s


def candle_close_time(open_ts: float, tf_seconds: int) -> float:
    return open_ts + tf_seconds
