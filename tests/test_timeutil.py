from datetime import datetime, timedelta

import pytest

from app.timeutil import (
    NY,
    active_macro,
    active_windows,
    candle_is_closed,
    friday_cutoff_reached,
    in_lunch,
    in_model_windows,
    is_market_open,
    model_windows,
    next_window_end,
    ny_string,
    parse_ny,
)


def ny(text: str) -> datetime:
    return parse_ny(text)


def test_kill_zones_on_a_normal_day():
    assert "NY_KZ" in active_windows(ny("2026-09-16 08:10"))
    assert "NY_2022" in active_windows(ny("2026-09-16 08:10"))
    assert "NY_2022" not in active_windows(ny("2026-09-16 09:30"))
    assert active_windows(ny("2026-09-16 02:30")) == ["LONDON_KZ"]


def test_window_is_half_open():
    assert "NY_2022" in active_windows(ny("2026-09-16 07:00"))
    assert "NY_2022" not in active_windows(ny("2026-09-16 09:00"))


@pytest.mark.parametrize("day", ["2026-03-09", "2026-11-02", "2026-09-16"])
def test_kill_zones_survive_dst_changes(day):
    """The day after each US DST switch must still put 08:10 NY inside the NY kill zone."""
    moment = ny(f"{day} 08:10")
    assert "NY_KZ" in active_windows(moment)
    assert moment.tzinfo is NY
    assert ny_string(moment.timestamp()) == f"{day} 08:10"


def test_dst_day_has_23_or_25_real_hours():
    """Wall-clock window maths stays on the NY grid while real elapsed time shifts by an hour."""
    spring = ny("2026-03-09 00:00").timestamp() - ny("2026-03-08 00:00").timestamp()
    autumn = ny("2026-11-02 00:00").timestamp() - ny("2026-11-01 00:00").timestamp()
    assert spring == 23 * 3600
    assert autumn == 25 * 3600


def test_macros():
    assert active_macro(ny("2026-09-16 08:05")) == "08:00"
    assert active_macro(ny("2026-09-16 08:11")) is None
    assert active_macro(ny("2026-09-16 13:25")) == "13:20"


def test_lunch_block():
    assert in_lunch(ny("2026-09-16 12:30"))
    assert not in_lunch(ny("2026-09-16 13:00"))


def test_market_hours_gold():
    assert is_market_open("XAUUSD", ny("2026-09-18 16:00"))  # Friday before the close
    assert not is_market_open("XAUUSD", ny("2026-09-18 17:30"))
    assert not is_market_open("XAUUSD", ny("2026-09-19 10:00"))  # Saturday
    assert not is_market_open("XAUUSD", ny("2026-09-20 17:30"))  # Sunday before 18:00
    assert is_market_open("XAUUSD", ny("2026-09-20 18:30"))


def test_daily_break_applies_to_btc():
    assert not is_market_open("BTCUSD", ny("2026-09-16 17:30"))
    assert is_market_open("BTCUSD", ny("2026-09-19 10:00"))


def test_friday_cutoff():
    assert friday_cutoff_reached("XAUUSD", ny("2026-09-18 15:45"), "15:30")
    assert not friday_cutoff_reached("XAUUSD", ny("2026-09-18 15:00"), "15:30")
    assert not friday_cutoff_reached("BTCUSD", ny("2026-09-18 15:45"), "15:30")


def test_model_windows_silver_bullet_follows_formation():
    formed = ny("2026-09-16 03:20")
    assert [w.name for w in model_windows("SILVER_BULLET", formed, False)] == ["LONDON_SB"]
    assert in_model_windows("SILVER_BULLET", formed, False, ny("2026-09-16 03:40")) == "LONDON_SB"
    assert in_model_windows("SILVER_BULLET", formed, False, ny("2026-09-16 10:30")) is None


def test_model_2_weekday_rules():
    formed = ny("2026-09-15 06:30")  # Tuesday
    assert in_model_windows("MODEL_2", formed, False, ny("2026-09-15 07:00")) == "MODEL_2"
    assert in_model_windows("MODEL_2", formed, False, ny("2026-09-16 07:00")) is None  # Wednesday, STRICT
    assert in_model_windows("MODEL_2", formed, True, ny("2026-09-16 07:00")) == "MODEL_2"


def test_or_first_fvg_windows():
    london = ny("2026-09-16 01:40")
    assert in_model_windows("OR_FIRST_FVG", london, False, ny("2026-09-16 03:00")) == "OR_LONDON"
    assert in_model_windows("OR_FIRST_FVG", london, False, ny("2026-09-16 08:00")) is None
    new_york = ny("2026-09-16 07:10")
    assert in_model_windows("OR_FIRST_FVG", new_york, False, ny("2026-09-16 08:00")) == "OR_NY"


def test_next_window_end_picks_the_last_window_before_the_horizon():
    now = ny("2026-09-16 08:10")
    windows = model_windows("ICT_2022", None, False)
    end = next_window_end(windows, now, now + timedelta(hours=8))
    assert ny_string(end.timestamp()) == "2026-09-16 09:00"


def test_parse_ny_rejects_aware_strings():
    with pytest.raises(ValueError):
        parse_ny("2026-09-16T08:00:00Z")


def test_candle_closed_needs_grace():
    open_ts = ny("2026-09-16 08:00").timestamp()
    assert not candle_is_closed(open_ts, 300, open_ts + 300, 2)
    assert candle_is_closed(open_ts, 300, open_ts + 302, 2)
