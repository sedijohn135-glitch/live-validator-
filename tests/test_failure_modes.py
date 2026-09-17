"""Direct coverage for failure-modes.md rows that no other test already proves."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import telegram as tg
from app.engine import Engine
from app.rules import t07_price
from app.setup_model import normalise
from app.store import Store
from tests.app_harness import make_runtime
from tests.test_golden import BASE_SETUP, clean_long_scenario, settings_for, ts


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
        assert body["profile"] == "STRICT"
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
def test_enter_is_refused_on_a_stale_quote(tmp_path):
    """E3: a decision on an old price is a decision on a price that no longer exists."""
    scenario = clean_long_scenario()
    ctx = scenario.context_provider()("XAUUSD", ts("2026-09-16 08:10") + 2)
    setup = normalise(dict(BASE_SETUP))
    assert t07_price(setup, ctx).passed
    ctx.quote_ts = ctx.now_ts - 30
    result = t07_price(setup, ctx)
    assert not result.passed and "vjetër" in result.text


# ----------------------------------------------------------------------- E11
def test_rounding_never_changes_a_decision(tmp_path):
    """E11: comparisons use full precision; rounding happens only in the message text."""
    scenario = clean_long_scenario()
    ctx = scenario.context_provider()("XAUUSD", ts("2026-09-16 08:10") + 2)
    setup = normalise(dict(BASE_SETUP))
    limit = t07_price(setup, ctx).data["chase_limit"]
    ctx.ask = limit + 1e-9
    assert not t07_price(setup, ctx).passed
    ctx.ask = limit - 1e-9
    assert t07_price(setup, ctx).passed


# ----------------------------------------------------------------------- E10
def test_btc_weekend_entries_are_off_by_default(tmp_path):
    from datetime import datetime

    from app.rules import t05_time
    from app.timeutil import NY

    scenario = clean_long_scenario()
    ctx = scenario.context_provider()("BTCUSD", ts("2026-09-16 08:10"))
    setup = normalise(dict(BASE_SETUP, symbol="BTCUSD"))
    saturday = datetime(2026, 9, 19, 8, 10, tzinfo=NY)
    assert not t05_time(setup, ctx, saturday, btc_weekend=False).passed
    assert t05_time(setup, ctx, saturday, btc_weekend=True).data.get("window") is not None


# ------------------------------------------------------------------------ C8
def test_rate_limit_errors_are_data_errors_not_crashes(tmp_path):
    from app.ctrader import DataError, classify

    assert isinstance(classify(RuntimeError("429 Too Many Requests")), DataError)


# ----------------------------------------------------------------------- E9
def test_gold_setups_expire_at_the_friday_cutoff(tmp_path):
    """E9: a gold position must not be left hanging over the weekend gap."""
    scenario = clean_long_scenario(day="2026-09-18")  # Friday
    store = Store(str(tmp_path / "v.db"))
    clock = {"now": ts("2026-09-18 08:10")}
    engine = Engine(
        settings_for("STRICT"),
        store,
        scenario.context_provider(),
        clock=lambda: clock["now"],
        id_factory=lambda symbol, _now: f"{symbol[:3]}-0918-TEST",
    )
    result = engine.submit(dict(BASE_SETUP, pda_formed_at_ny="2026-09-18 07:35", price_at_analysis=5656.4))
    assert result["status"] == "ARMED", result["reasons"]
    assert result["computed"]["expires_at_ny"] <= "2026-09-18 15:30"


# ----------------------------------------------------------------------- D12
def test_a_broken_environment_never_crashes_the_process(tmp_path):
    from app.config import load_settings

    settings = load_settings({"VALIDATOR_PROFILE": "🙂", "SYMBOL_MAP": "{{{", "PRICE_DIGITS": "5"})
    assert settings.profile.name == "STRICT"
    assert settings.warnings
    assert json.dumps(settings.warnings)  # serialisable for /health
