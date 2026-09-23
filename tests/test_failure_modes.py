"""Direct coverage for failure-modes.md rows that no other test already proves."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import telegram as tg
from app.engine import OPEN_STATES
from app.evidence import evaluate
from app.runtime import ENGINE_WATCHDOG_S
from app.setup_model import normalise
from tests.app_harness import make_runtime
from tests.synth import Feed, Tape


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ------------------------------------------------------------------ D1 / D7
@pytest.mark.slow
def test_server_binds_the_platform_port_and_answers_health(tmp_path):
    """D1: Railway's healthcheck fails if the app does not bind 0.0.0.0:$PORT."""
    port = free_port()
    env = {
        **os.environ,
        "PORT": str(port),
        "DATA_DIR": str(tmp_path),
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        "CTRADER_MCP_CONFIG": "",
        "NEWS_FILTER": "off",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--proxy-headers",
            "--forwarded-allow-ips",
            "*",
            "--workers",
            "1",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        body = None
        while time.time() < deadline:
            try:
                import httpx

                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
                if response.status_code == 200:
                    body = response.json()
                    break
            except Exception:
                time.sleep(0.4)
        assert body is not None, "the server never answered /health"
        assert body["ok"] is True
        assert body["data"]["ctrader"] == "not_configured"
        assert body["profile"] == "UNIVERSAL"
    finally:
        process.terminate()
        process.wait(timeout=15)


def test_new_york_timezone_is_available():
    """D7: python:3.12-slim has no tz database without the tzdata dependency."""
    from zoneinfo import ZoneInfo

    assert ZoneInfo("America/New_York") is not None
    assert "tzdata" in Path("pyproject.toml").read_text()


# ------------------------------------------------------------------------ D8
def test_a_second_instance_without_the_lease_sends_nothing(tmp_path):
    """D8: two instances during a redeploy must not both drain the outbox."""
    first, _fake_a, telegram_a = make_runtime(tmp_path)
    second, _fake_b, telegram_b = make_runtime(tmp_path)
    assert first.store.acquire_lease("engine", first.holder, 30.0, first.clock())
    assert not second.store.acquire_lease("engine", second.holder, 30.0, second.clock())

    first.store.queue_message("s1:ENTER", "42", "hello")
    sender = tg.OutboxSender(second.store, telegram_b.client(), "42")
    assert not second.has_lease  # the loop would skip draining entirely
    assert asyncio.run(tg.OutboxSender(first.store, telegram_a.client(), "42").drain_once()) == 1
    assert asyncio.run(sender.drain_once()) == 0
    assert telegram_b.texts() == []


# ----------------------------------------------------------------------- D10
def test_ci_uses_a_frozen_lockfile():
    workflow = Path(".github/workflows/test.yml").read_text()
    assert "uv sync --frozen" in workflow
    assert Path("uv.lock").exists()


# ------------------------------------------------------------------------ E3
ZONE = {"entry_low": 4295.0, "entry_high": 4300.0, "stop_loss": 4288.0, "tp1": 4330.0}


def confirmed_tape() -> tuple[Tape, float]:
    tape = Tape(price=4306.0)
    tape.drift(30, step=-0.25, span=0.5)
    touch = tape.now
    tape.sweep(low=4291.0, close=4298.0)
    atr = tape.context().atr("M1") or 1.0
    tape.push(4298.0, 4298.0 + 2 * atr, 4297.8, 4298.0 + 1.8 * atr)
    return tape, touch


def test_enter_is_held_on_a_stale_quote(tmp_path):
    """E3: a decision on an old price is a decision on a price that no longer exists."""
    tape, touch = confirmed_tape()
    setup = normalise(ZONE)
    assert evaluate(setup, tape.context(), touch).ready
    stale = evaluate(setup, tape.context(quote_ts=tape.now - 45), touch)
    assert not stale.ready
    assert "DATA" in stale.holds
    assert stale.confirmed, "stale data holds the entry; it never kills the setup"


# ----------------------------------------------------------------------- E11
def test_rounding_never_changes_a_decision(tmp_path):
    """E11: comparisons use full precision; rounding happens only in the message text."""
    tape, touch = confirmed_tape()
    setup = normalise(ZONE)
    edge = setup.zone_high + 0.35 * setup.risk
    assert not evaluate(setup, tape.context(bid=edge - 1e-9), touch).late
    assert evaluate(setup, tape.context(bid=edge + 1e-9), touch).late


# ----------------------------------------------------------------------- E10
def test_no_clock_and_no_calendar_can_cancel_a_setup(tmp_path):
    """E10: the universal validator has no session, no cutoff and no expiry (docs/VALIDATOR.md §1.3)."""
    feed = Feed(tmp_path, start_ny="2026-09-18 15:00", price=4320.0)  # Friday afternoon
    setup_id = feed.submit(**ZONE)["setup_id"]
    for _ in range(50):
        feed.tape.drift(60, step=0.0, span=0.4)
        feed.tick(price=4315.0)
    assert feed.state(setup_id) == "WATCHING"
    assert feed.outcome(setup_id) == ""


# ------------------------------------------------------------------------ C8
def test_rate_limit_errors_are_data_errors_not_crashes(tmp_path):
    from app.ctrader import DataError, classify

    assert isinstance(classify(RuntimeError("429 Too Many Requests")), DataError)


# ------------------------------------------------------------------------ E9
def test_a_setup_waits_instead_of_expiring_over_the_weekend(tmp_path):
    """E9 rewritten: the owner asked for a validator, not a gatekeeper — only SL or TP1 close it."""
    feed = Feed(tmp_path, start_ny="2026-09-18 16:00", price=4320.0)
    setup_id = feed.submit(**ZONE)["setup_id"]
    feed.tape.drift(600, step=0.0, span=0.4)  # ten hours of nothing
    feed.tick(price=4316.0)
    assert feed.state(setup_id) == "WATCHING"


# ----------------------------------------------------------------------- D12
def test_a_broken_environment_never_crashes_the_process(tmp_path):
    from app.config import load_settings

    settings = load_settings({"VALIDATOR_PROFILE": "🙂", "SYMBOL_MAP": "{{{", "PRICE_DIGITS": "5"})
    assert settings.profile.name == "UNIVERSAL"
    assert settings.warnings
    assert json.dumps(settings.warnings)  # serialisable for /health


def test_the_feed_returning_refills_the_period_it_was_blind_for(tmp_path):
    """The restore message says the missing period was checked; it has to be true.

    Setups are judged on the range price covered since the last pass. After an outage that range
    lives in candles nobody fetched, so three bars of routine refresh leave the hole in place.
    """
    runtime, ctrader, _telegram = make_runtime(tmp_path)

    runtime.engine.submit({"symbol": "XAUUSD", "entry": 4300.0, "stop_loss": 4290.0, "tp1": 4330.0})

    async def scenario():
        await runtime.ctrader.discover()  # symbols resolved: the engine loop would be running
        runtime.data_status = "down"
        runtime._on_data_ok()
        assert runtime._backfill_after_outage is True
        ctrader.calls.clear()
        try:
            await runtime.tick()
        finally:
            await runtime.ctrader.aclose()

    asyncio.run(scenario())
    windows = [
        payload["to"] - payload["from"]
        for name, payload in ctrader.calls
        if name == "get_trendbars" and payload["period"] == "M_1"
    ]
    assert windows, "M1 must be refetched when the feed returns"
    assert max(windows) >= 3600_000, f"the blind period must be refetched, widest window {windows}"
    assert runtime._backfill_after_outage is False, "the backfill runs once, not on every tick"


def test_a_blip_is_not_announced_as_an_outage(tmp_path):
    """Three "the data is back" in four minutes, with no "the data is down" between them.

    The alert claims monitoring is paused, but entries only stop once the quote is older than
    OUTAGE_PAUSE_S. Announcing anything shorter trains the owner to ignore the one message that
    should mean something.
    """
    runtime, _ctrader, _telegram = make_runtime(tmp_path)
    clock = {"now": 1_800_000_000.0}
    runtime.clock = lambda: clock["now"]

    runtime._on_data_error(RuntimeError("Server returned an error response"))
    clock["now"] += 2.0
    runtime._on_data_error(RuntimeError("Server returned an error response"))
    runtime._on_data_ok()
    assert runtime.store.outbox_health()["pending"] == 0, "a two-second blip says nothing"

    runtime._on_data_error(RuntimeError("Server returned an error response"))
    clock["now"] += 120.0
    runtime._on_data_error(RuntimeError("Server returned an error response"))
    texts = [row["text"] for row in runtime.store.pending_messages(10)]
    assert any("TË DHËNAT RANË" in text for text in texts), texts

    runtime._on_data_ok()
    texts = [row["text"] for row in runtime.store.pending_messages(10)]
    assert any("Të dhënat u rikthyen" in text for text in texts), "an announced outage gets an answer"


def test_the_engine_keeps_ticking_with_nothing_to_watch(tmp_path):
    """The deadlock the owner hit: no setups, so no ticks, so the snapshot said the engine was dead.

    Gemini reads `engine_ticking: false` and refuses to register anything, which guarantees there
    will still be nothing to watch on the next pass. The loop has to run whether or not it has work.
    """
    runtime, ctrader, _telegram = make_runtime(tmp_path)

    async def scenario():
        await runtime.ctrader.discover()
        assert runtime.store.setups_in_state(OPEN_STATES) == [], "nothing registered"
        try:
            await runtime.tick()
        finally:
            await runtime.ctrader.aclose()

    asyncio.run(scenario())

    assert runtime.ticks == 1
    assert runtime.last_tick_at, "an idle pass is still a pass"
    assert runtime.health()["engine"]["last_tick_age_s"] is not None, "and it is reported as alive"


def test_a_closed_connection_is_rebuilt_not_retried(tmp_path):
    """"Connection closed" means the link is gone: three retries on it are three failures.

    This is what left the feed down for sixteen minutes with the quote frozen — the error was
    treated as transient, so it was retried on the very session that had died.
    """
    from app.ctrader import DataError, is_session_error, is_transient

    exc = DataError("Connection closed")
    assert is_transient(exc)
    assert is_session_error(exc), "a dead link needs a new one, not another attempt on the old one"

    runtime, ctrader, _telegram = make_runtime(tmp_path)

    async def scenario():
        await runtime.ctrader.discover()
        ctrader.fail_next = RuntimeError("Connection closed")
        before = len([c for c in ctrader.calls if c[0] == "__connect__"])
        try:
            assert await runtime.ctrader.call("get_version"), "the reconnect must deliver a result"
            return len([c for c in ctrader.calls if c[0] == "__connect__"]) - before
        finally:
            await runtime.ctrader.aclose()

    assert asyncio.run(scenario()) >= 1, "the client reconnected"


def test_a_background_loop_that_dies_is_restarted(tmp_path, monkeypatch):
    """The loops catch Exception, which is not enough.

    A CancelledError leaking out of the MCP client's task group is a BaseException: it ends the
    task with no line in the log, and nothing brought it back. The engine sat dead for a day that
    way, with a setup registered and the web server answering normally in front of it.
    """
    from app import runtime as runtime_module

    runtime, _ctrader, _telegram = make_runtime(tmp_path)
    monkeypatch.setattr(runtime_module, "SUPERVISOR_RESTART_S", 0.0)
    starts: list[int] = []

    async def flaky() -> None:
        starts.append(1)
        if len(starts) == 1:
            raise asyncio.CancelledError  # leaked from a library, not a shutdown
        if len(starts) == 2:
            raise RuntimeError("boom")
        await asyncio.sleep(3600)

    async def main() -> int:
        task = asyncio.create_task(runtime._supervise("test", flaky))
        for _ in range(200):
            if len(starts) >= 3:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return len(starts)

    assert asyncio.run(main()) >= 3, "a dead loop must come back, whatever killed it"


def test_the_watchdog_replaces_an_engine_that_froze(tmp_path):
    """A task can be alive and still stuck inside an await that never returns.

    From outside that is indistinguishable from a dead one, and it lasts just as long. The
    supervisor cannot see it — only the absence of progress can.
    """
    runtime, _ctrader, _telegram = make_runtime(tmp_path)
    clock = {"now": 1_800_000_000.0}
    runtime.clock = lambda: clock["now"]
    runtime.has_lease = True
    runtime.startup_done = True

    async def main() -> tuple[bool, bool]:
        stuck = asyncio.create_task(asyncio.sleep(3600), name="engine")
        runtime._tasks = [stuck]
        runtime.last_tick_at = clock["now"]

        fresh = await runtime.restart_engine_if_stuck()  # ticking: nothing to do
        clock["now"] += ENGINE_WATCHDOG_S + 1
        replaced = await runtime.restart_engine_if_stuck()

        for task in runtime._tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait(runtime._tasks, timeout=1.0)
        return fresh, replaced

    was_fresh, was_replaced = asyncio.run(main())
    assert was_fresh is False, "a ticking engine is left alone"
    assert was_replaced is True
    assert runtime.engine_restarts == 1
    assert runtime.health()["engine"]["restarts"] == 1, "and the owner can see it happened"
