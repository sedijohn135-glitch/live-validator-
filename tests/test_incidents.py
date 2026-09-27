"""The live trades that shaped the evidence rules, replayed through the whole engine on real bars.

Two setups confirmed on the same REJECTION + MOMENTUM = 3. One ran to TP3, the other was stopped in
six minutes. What separated them was not the score but two facts the engine was not checking: the
losing short's REJECTION was sixteen bars and a full round trip through the zone old, and none of
the three rising lows under it had broken. A rule that stops the loss earns its place only if the
win still gets in.
"""

from __future__ import annotations

from tests.incidents import A3XV, A3XV_REGISTERED, A3XV_START, HBZA, HBZA_REGISTERED, HBZA_START
from tests.synth import Feed, ts


def run(tmp_path, start, registered, bars, payload):
    """History up to the registration minute, then the setup, then the live minutes one by one."""
    feed = Feed(tmp_path, start_ny=start, price=bars[0][0])
    feed.spread = 0.27
    first_live = int((ts(registered) - ts(start)) // 60)
    for o, h, low, c in bars[:first_live]:
        feed.tape.push(o, h, low, c)
    feed.bid = bars[first_live - 1][3]
    setup_id = feed.submit(**payload)["setup_id"]
    states = []
    for o, h, low, c in bars[first_live:]:
        feed.tape.push(o, h, low, c)
        feed.tick(price=c)
        states.append(feed.state(setup_id))
    return feed, setup_id, states


def test_the_losing_short_never_enters(tmp_path):
    """XAU-0925-HBZA: shorting into rising lows on a stale rejection. It must not be an entry."""
    feed, setup_id, states = run(
        tmp_path,
        HBZA_START,
        HBZA_REGISTERED,
        HBZA,
        {
            "entry_low": 4270.014428571429,
            "entry_high": 4272.245571428572,
            "stop_loss": 4276.296071428572,
            "tp1": 4254.49,
            "tp2": 4244.28,
            "tp3": 4235.41,
            "direction": "SHORT",
        },
    )
    assert "AT_ZONE" in states, "the zone was reached — this is a real test of the verdict"
    assert "ENTERED" not in states and "LIMIT" not in states, states
    assert not any("HYR TANI" in text for text in feed.messages())


def test_the_winning_long_still_enters(tmp_path):
    """XAU-0924-A3XV: the same score, a fresh reaction and a structure that did break. Still an entry."""
    feed, setup_id, states = run(
        tmp_path,
        A3XV_START,
        A3XV_REGISTERED,
        A3XV,
        {
            "entry_low": 4265.5,
            "entry_high": 4268.1,
            "stop_loss": 4264.1103571428575,
            "tp1": 4277.89,
            "tp2": 4285.06,
            "tp3": 4288.4,
            "direction": "LONG",
        },
    )
    assert "ENTERED" in states or "LIMIT" in states, states
