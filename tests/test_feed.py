"""Live-feed incidents that actually happened, each reproduced and then fixed.

These are about the cTrader link and the health report, not about validation rules: they survived
the rewrite because the incidents were real.
"""

from __future__ import annotations

import asyncio

from app.runtime import REPORT_TIMEFRAMES
from tests.app_harness import make_runtime
from tests.helpers import flat_series
from tests.synth import ts


def runtime_with_candles(tmp_path, now: float | None = None):
    """A runtime holding a full set of timeframes, as it would after a healthy start."""
    runtime, _fake, _tg = make_runtime(tmp_path)
    now = ts("2026-09-18 10:00") if now is None else now
    runtime.clock = lambda: now
    for timeframe in REPORT_TIMEFRAMES:
        bars = flat_series(timeframe, 60, now, 4300.0, spread=0.6)
        runtime.candles.merge("XAUUSD", timeframe, bars, now + 10**6)
    return runtime, now


# ------------------------------------------------------- the daily break is not a token problem
def test_a_closed_market_is_never_reported_as_an_expired_token():
    """The false alarm: 'trading session is closed' used to raise the 🔑 token warning and pause."""
    from app.ctrader import AuthError, DataError, classify

    for message in (
        "trading session is closed",
        "market session closed for maintenance",
        "quote session unavailable",
        "no data for this session",
    ):
        assert isinstance(classify(RuntimeError(message)), DataError), message
    for message in (
        "401 Unauthorized",
        "403 Forbidden",
        "token expired",
        "invalid token",
        "Your session has expired, please log in again",
        "Authorization header missing",
    ):
        assert isinstance(classify(RuntimeError(message)), AuthError), message


# ---------------------------------------- a token that dies twice must warn twice
def test_the_token_warning_repeats_instead_of_firing_once_forever(tmp_path):
    """The bug: a fixed dedupe key meant the second expiry, weeks later, warned nobody."""
    from app.ctrader import AuthError

    runtime, _fake, _tg = make_runtime(tmp_path)
    now = ts("2026-09-17 09:00")
    runtime.clock = lambda: now

    runtime._on_auth_error(AuthError("token expired"))
    runtime._on_auth_error(AuthError("token expired"))  # same hour: one warning is enough
    assert len(_auth_messages(runtime)) == 1

    now = ts("2026-09-17 11:00")  # still broken two hours later
    runtime._on_auth_error(AuthError("token expired"))
    assert len(_auth_messages(runtime)) == 2

    runtime._on_data_ok()  # owner pasted a new token
    now = ts("2026-09-17 11:10")
    runtime._on_auth_error(AuthError("token expired"))  # and it died again straight away
    assert len(_auth_messages(runtime)) == 3


def _auth_messages(runtime) -> list:
    return [row for row in runtime.store.pending_messages(50) if row["dedupe_key"].startswith("sys:auth_expired")]


def test_the_account_number_is_readable_from_the_token(tmp_path):
    """A cTrader account switch invalidates the token silently: /selftest must show which account."""
    import base64
    import json

    from app.ctrader import Credentials, account_hint

    payload = json.dumps({"platform": "ctrader", "account": 10099943, "env": "demo"}).encode()
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=") + ".c2lnbmF0dXJlLXBhcnQ"
    assert account_hint(token) == "10099943"
    assert Credentials("https://ctrader.test/mcp", token, "env_pair").account == "10099943"

    assert account_hint("") == ""
    assert account_hint("not-a-token") == ""
    assert account_hint("tok" * 8) == ""  # the harness token carries no account


# --------------------------------------------- a dead MCP session must rebuild itself
def test_a_dead_mcp_session_is_reconnected_not_reported_hourly():
    """The incident: 'Session not found; re-initialize' every hour from 01:44 to 04:45, no recovery."""
    from app.ctrader import AuthError, DataError, classify, is_transient

    for message in (
        "Session not found; re-initialize",
        "MCP error: session not found",
        "404 Not Found",
        "Invalid session id, please reinitialize",
        "the session was terminated by the server",
    ):
        assert isinstance(classify(RuntimeError(message)), DataError), message
        assert is_transient(DataError(message)), message
    # An expired token still is not a hiccup: reconnecting with it would loop.
    assert isinstance(classify(RuntimeError("your session has expired, please log in")), AuthError)


def test_the_engine_rebuilds_a_broken_link_by_itself(tmp_path):
    """Whatever wording the failure arrives in, the link is rebuilt instead of idling for hours."""
    runtime, _fake, _tg = make_runtime(tmp_path)
    now = ts("2026-09-18 02:00")
    runtime.clock = lambda: now
    reconnects = []

    async def fake_reconnect():
        reconnects.append(True)

    runtime.ctrader.reconnect = fake_reconnect

    runtime.data_status = "ok"
    asyncio.run(runtime._heal_connection(now))
    assert len(reconnects) == 0  # a healthy feed is left alone

    runtime.data_status = "down"
    asyncio.run(runtime._heal_connection(now))
    asyncio.run(runtime._heal_connection(now + 30))  # not more often than every two minutes
    assert len(reconnects) == 1
    asyncio.run(runtime._heal_connection(now + 200))
    assert len(reconnects) == 2

    runtime.data_status = "auth_error"  # a reconnect cannot fix a dead token
    asyncio.run(runtime._heal_connection(now + 500))
    assert len(reconnects) == 2


# ----------------------------------------------------------- the report must say why
def test_a_down_feed_always_says_why(tmp_path):
    """Without Railway logs the owner has only /status and the snapshot: both must carry the reason."""
    from app.ctrader import DataError

    runtime, now = runtime_with_candles(tmp_path)
    runtime._on_data_error(DataError("get_spot_prices refused: market data unavailable"))

    block = runtime.data_block("XAUUSD", True, now)
    assert block["usable"] is True  # cached bars still serve
    assert "market data unavailable" in block["detail"]
    assert "market data unavailable" in runtime.health()["data"]["detail"]
    assert "Arsyeja:" in runtime._status_text()


def test_h4_is_reported_but_never_blocks_a_snapshot(tmp_path):
    """H4 feeds the secure level and the H4 FVGs, so its bar count belongs in the report."""
    runtime, now = runtime_with_candles(tmp_path)
    block = runtime.data_block("XAUUSD", True, now)
    assert "H4" in block["candles_held"]
    assert block["candles_held"]["H4"] > 0
    assert block["usable"] is True

    thin, thin_now = runtime_with_candles(tmp_path / "thin")
    thin.candles._series.pop(("XAUUSD", "H4"), None)
    thin_block = thin.data_block("XAUUSD", True, thin_now)
    assert thin_block["candles_held"]["H4"] == 0
    assert thin_block["usable"] is True


def test_the_runtime_watches_the_states_the_engine_actually_uses(tmp_path):
    """The rewrite renamed every state: a stale name here would silently stop the quote polling."""
    from app.engine import OPEN_STATES

    runtime, now = runtime_with_candles(tmp_path)
    assert runtime._symbols_to_watch() == []

    result = runtime.engine.submit({"symbol": "XAUUSD", "entry": 4290.0, "stop_loss": 4280.0})
    assert result["status"] == "registered"
    assert runtime.store.get_setup(result["setup_id"])["state"] in OPEN_STATES
    assert "XAUUSD" in runtime._symbols_to_watch()
    assert runtime.health()["active_setups"] == 1


def test_a_setup_left_by_the_previous_validator_never_breaks_the_answer(tmp_path):
    """The live database still holds v11 rows: they are ignored, not decoded, and never crash."""
    import json

    runtime, now = runtime_with_candles(tmp_path)
    with runtime.store.transaction() as conn:
        runtime.store.insert_setup(
            conn,
            {
                "id": "XAU-0917-OLD1",
                "symbol": "XAUUSD",
                "direction": "SHORT",
                "model": "SILVER_BULLET",
                "state": "ARMED",
                "payload_json": json.dumps({"entry_model": "SILVER_BULLET", "checklist_positive": 8}),
                "computed_json": "{}",
                "fingerprint": "old",
                "created_at": now - 3600,
            },
        )
    assert runtime.engine.status()["active"] == []  # the old state is not one the engine watches
    assert runtime.engine.status("XAU-0917-OLD1")["legacy"] is True
    assert runtime._symbols_to_watch() == []


# ------------------------------------------- the engine must never wait for the history
def test_the_engine_ticks_even_while_the_history_is_still_loading(tmp_path):
    """The incident: a setup sat inside its zone untouched because startup was still fetching bars.

    Loading the deep history is dozens of chunked calls. It used to run inside the engine loop before
    its first pass, so the validator was blind for as long as it took — and a hung call blinded it
    for good. Startup now has its own task and the loop starts immediately.
    """
    import asyncio

    runtime, _fake, _tg = make_runtime(tmp_path)
    started = asyncio.Event()

    async def never_finishes(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(3600)

    runtime.ctrader.discover = never_finishes

    async def drive():
        await runtime.run_forever()
        try:
            await asyncio.wait_for(started.wait(), timeout=2.0)
            names = {task.get_name() for task in runtime._tasks}
            assert {"startup", "engine", "lease"} <= names
            engine = next(task for task in runtime._tasks if task.get_name() == "engine")
            await asyncio.sleep(0.1)
            assert not engine.done(), "the engine loop must not be blocked by startup"
        finally:
            await runtime.stop(timeout=1.0)

    asyncio.run(drive())


def test_status_says_whether_the_engine_is_ticking(tmp_path):
    runtime, now = runtime_with_candles(tmp_path)

    health = runtime.health()
    assert health["engine"]["ticks"] == 0
    assert health["engine"]["last_tick_age_s"] is None
    assert "Motori:" in runtime._status_text()

    runtime.ticks, runtime.last_tick_at, runtime.has_lease = 42, now, True
    assert "punon" in runtime._status_text()

    runtime.last_tick_at = now - 600
    text = runtime._status_text()
    assert "600s më parë" in text

    runtime.has_lease = False
    runtime.engine_error = "RuntimeError: boom"
    text = runtime._status_text()
    assert "pa lease" in text and "boom" in text


def test_a_tick_is_skipped_until_the_symbols_resolve(tmp_path):
    """Polling before discovery only raises, and a raised tick used to look like a dead feed."""
    import asyncio

    runtime, _fake, _tg = make_runtime(tmp_path)
    runtime.ctrader.symbols = {}
    asyncio.run(runtime.tick())
    assert runtime.ticks == 0


def test_discovery_is_retried_with_a_fresh_connection_and_never_timed_out(tmp_path):
    """A 45 s timeout around discover() cancelled it mid-call and left the session unusable.

    Symbols then never resolved, so the whole feed stayed down: status `down`, no quotes, no candles.
    A retry must rebuild the connection instead of cancelling the call.
    """
    import asyncio

    runtime, _fake, _tg = make_runtime(tmp_path)
    calls = {"discover": 0, "reconnect": 0}
    now = ts("2026-09-18 14:30")
    runtime.clock = lambda: now

    async def slow_discover():
        calls["discover"] += 1
        await asyncio.sleep(0.05)  # longer than any timeout would have allowed
        if calls["discover"] < 2:
            raise RuntimeError("get_symbols failed")
        runtime.ctrader.symbols = {"XAUUSD": object()}
        return {"tools": (), "profile": "data", "version": "x"}

    async def fake_reconnect():
        calls["reconnect"] += 1

    runtime.ctrader.symbols = {}
    runtime.ctrader.discover = slow_discover
    runtime.ctrader.reconnect = fake_reconnect

    asyncio.run(runtime._ensure_discovered(now))
    assert calls == {"discover": 1, "reconnect": 1}, "a failed discovery must get a fresh connection"

    now += 1  # too soon: the retry is throttled
    asyncio.run(runtime._ensure_discovered(now))
    assert calls["discover"] == 1

    now += 60
    asyncio.run(runtime._ensure_discovered(now))
    assert calls["discover"] == 2
    assert runtime.ctrader.symbols, "the second attempt resolved the symbols"

    now += 60  # resolved symbols are never rediscovered
    asyncio.run(runtime._ensure_discovered(now))
    assert calls["discover"] == 2


def test_a_service_without_credentials_is_not_a_broken_feed(tmp_path):
    """`not_configured` must survive the discovery retry: there is nothing to reconnect to."""
    import asyncio

    runtime, _fake, _tg = make_runtime(tmp_path, CTRADER_MCP_TOKEN="", CTRADER_MCP_URL="")
    runtime.ctrader.symbols = {}
    runtime.ctrader.credentials = None
    asyncio.run(runtime._ensure_discovered(runtime.clock()))
    assert runtime.data_status == "not_configured"
    assert runtime.health()["data"]["ctrader"] == "not_configured"


def test_the_snapshot_says_when_the_engine_is_not_running(tmp_path):
    """A healthy feed with a dead loop looked perfect from the model's side (incident 2026-09-18)."""
    runtime, now = runtime_with_candles(tmp_path)

    block = runtime.data_block("XAUUSD", True, now)
    assert block["engine_ticking"] is False
    assert "nuk po monitorohen" in block["engine_note"]

    runtime.last_tick_at = now - 5
    block = runtime.data_block("XAUUSD", True, now)
    assert block["engine_ticking"] is True
    assert "engine_note" not in block

    runtime.last_tick_at = now - 600
    assert runtime.data_block("XAUUSD", True, now)["engine_ticking"] is False


def test_status_shows_a_stuck_outbox(tmp_path):
    """No alert arriving used to look exactly like no alert being due."""
    runtime, now = runtime_with_candles(tmp_path)
    assert "Mesazhe në pritje" not in runtime._status_text()

    runtime.store.queue_message("stuck", "42", "text", now - 900)
    pending = runtime.store.pending_messages(1)[0]
    runtime.store.mark_failed(pending["id"], "Bad Request: chat not found")

    health = runtime.health()
    assert health["outbox"]["pending"] == 1
    assert health["outbox"]["attempts"] == 1
    text = runtime._status_text()
    assert "Mesazhe në pritje: 1" in text
    assert "900s" in text
    assert "chat not found" in text
