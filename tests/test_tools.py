"""MCP tool surface: flat schemas, annotations, snapshot size and a full submit-to-ENTER pass."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from app import tools as tools_module
from app.market import CandleStore
from app.runtime import Runtime
from app.store import Store
from tests.app_harness import make_runtime
from tests.scenario import Scenario
from tests.test_golden import BASE_SETUP, clean_long_scenario, ts

FORBIDDEN_KEYS = ("$ref", "$defs", "anyOf", "oneOf", "allOf", "not", "definitions")
ALLOWED_TYPES = {"string", "number", "integer", "boolean"}


class ScenarioRuntime(Runtime):
    """A runtime whose market data comes from a synthetic scenario instead of the network."""

    def __init__(self, scenario: Scenario, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.scenario = scenario
        self._provider = scenario.context_provider()

    def _load(self) -> None:
        context = self._provider("XAUUSD", self.clock())
        self.candles = CandleStore(close_grace_s=0.0)
        for timeframe in ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"):
            series = context.store.series("XAUUSD", timeframe)
            if series:
                self.candles.merge("XAUUSD", timeframe, series, self.clock() + 10**6)
        if context.bid is not None:
            from app.ctrader import Quote

            self.quotes["XAUUSD"] = Quote("XAUUSD", context.bid, context.ask, self.clock())
            self.spread_samples.setdefault("XAUUSD", __import__("collections").deque(maxlen=1800)).extend(
                [context.ask - context.bid] * 60
            )
        self.last_quote_at = self.clock()
        self.data_status = "ok"

    async def ensure_history(self, symbol: str, timeframes=None) -> None:
        self._load()

    async def refresh_candles(self, symbol: str, timeframes, count: int = 3) -> None:
        self._load()

    async def refresh_quotes_for(self, symbol: str) -> None:
        self._load()

    async def refresh_quotes(self) -> None:
        self._load()


def scenario_app(tmp_path):
    """The real tool registration on a bare MCP server: no lifespan, no background tasks."""
    runtime_base, _fake, telegram = make_runtime(tmp_path)
    scenario = clean_long_scenario()
    clock = {"now": ts("2026-09-16 07:46")}
    runtime = ScenarioRuntime(
        scenario,
        runtime_base.settings,
        Store(str(tmp_path / "validator.db")),
        ctrader=runtime_base.ctrader,
        telegram=telegram.client(),
        clock=lambda: clock["now"],
    )
    server = MCPServer(name="live-validator-test")
    tools_module.register(server, runtime)
    return server, runtime, clock, telegram


def tools_of(server):
    async def main():
        async with Client(server) as client:
            listed = await client.list_tools()
            return list(getattr(listed, "tools", listed))

    return asyncio.run(main())


# ------------------------------------------------------------------ schema lint
def test_every_tool_schema_is_flat(tmp_path):
    """Failure mode G1: Gemini's function calling rejects nested or union schemas."""
    server, _runtime, _clock, _tg = scenario_app(tmp_path)
    tools = tools_of(server)
    assert {t.name for t in tools} == {
        "market_snapshot",
        "market_candles",
        "validator_rules",
        "setup_submit",
        "setup_status",
        "setup_cancel",
    }
    for tool in tools:
        schema = tool.input_schema
        assert schema["type"] == "object"
        text = json.dumps(schema)
        for key in FORBIDDEN_KEYS:
            assert key not in text, f"{tool.name} uses {key}"
        for name, prop in schema.get("properties", {}).items():
            assert prop.get("type") in ALLOWED_TYPES, f"{tool.name}.{name} is not a primitive"
            assert prop.get("description"), f"{tool.name}.{name} has no description"
            assert "items" not in prop and "properties" not in prop
        for name in schema.get("required", []):
            assert name in schema["properties"]


def test_tool_annotations_mark_reads_and_writes(tmp_path):
    server, _runtime, _clock, _tg = scenario_app(tmp_path)
    by_name = {t.name: t for t in tools_of(server)}
    for name in ("market_snapshot", "market_candles", "validator_rules", "setup_status"):
        assert by_name[name].annotations.read_only_hint is True
        assert by_name[name].annotations.open_world_hint is False
    assert by_name["setup_submit"].annotations.read_only_hint is False
    assert by_name["setup_submit"].annotations.idempotent_hint is True
    assert by_name["setup_submit"].annotations.destructive_hint is False
    assert by_name["setup_cancel"].annotations.destructive_hint is True


def test_optional_parameters_are_not_required(tmp_path):
    server, _runtime, _clock, _tg = scenario_app(tmp_path)
    submit = next(t for t in tools_of(server) if t.name == "setup_submit")
    required = set(submit.input_schema["required"])
    assert "tp2" not in required and "range_high" not in required
    assert {"symbol", "direction", "entry_low", "rationale"}.issubset(required)


# ------------------------------------------------------------------- behaviour
def call(server, name: str, arguments: dict) -> dict:
    async def main():
        async with Client(server) as client:
            result = await client.call_tool(name, arguments)
            text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
            return json.loads(text)

    return asyncio.run(main())


def test_snapshot_is_compact_and_new_york_timed(tmp_path):
    server, _runtime, _clock, _tg = scenario_app(tmp_path)
    payload = call(server, "market_snapshot", {"symbol": "XAUUSD"})
    assert payload["schema"] == "snapshot/1"
    assert payload["time"]["ny"].startswith("2026-09-16")
    assert payload["time"]["active_windows"]
    assert payload["quote"]["bid"] == pytest.approx(5658.0)
    assert payload["candles"]["M5"][0][0].startswith("2026-09-")
    assert len(payload["candles"]["M5"]) == 96
    assert len(json.dumps(payload)) < 60_000  # failure mode G12


def test_validator_rules_lists_the_active_profile(tmp_path):
    server, _runtime, _clock, _tg = scenario_app(tmp_path)
    payload = call(server, "validator_rules", {})
    assert payload["profile"] == "STRICT"
    assert payload["thresholds"]["rr_min_plan"] == 2.0
    assert "ICT_2022" in payload["entry_models"]
    assert "pda_formed_at_ny" in payload["required_fields"]


def test_submit_snapshot_feed_then_exactly_one_enter(tmp_path):
    """Snapshot → submit → simulated feed → one ENTER in the outbox, end to end through MCP."""
    server, runtime, clock, _tg = scenario_app(tmp_path)
    call(server, "market_snapshot", {"symbol": "XAUUSD"})

    payload = {k: v for k, v in BASE_SETUP.items() if v is not None}
    result = call(server, "setup_submit", payload)
    assert result["status"] == "ARMED", result["reasons"]
    setup_id = result["setup_id"]
    assert result["computed"]["expires_at_ny"] == "2026-09-16 09:00"

    status = call(server, "setup_status", {"setup_id": setup_id})
    assert status["state"] == "ARMED"

    clock["now"] = ts("2026-09-16 08:10") + 2
    asyncio.run(runtime.tick())
    asyncio.run(runtime.tick())

    enters = runtime.store.query("SELECT dedupe_key FROM outbox WHERE dedupe_key LIKE '%:ENTER'")
    assert len(enters) == 1
    assert runtime.store.get_setup(setup_id)["state"] == "TRIGGERED"


def test_duplicate_submit_through_the_tool(tmp_path):
    server, _runtime, _clock, _tg = scenario_app(tmp_path)
    payload = {k: v for k, v in BASE_SETUP.items() if v is not None}
    first = call(server, "setup_submit", payload)
    second = call(server, "setup_submit", payload)
    assert first["status"] == "ARMED"
    assert second["status"] == "DUPLICATE" and second["setup_id"] == first["setup_id"]


def test_cancel_through_the_tool(tmp_path):
    server, _runtime, _clock, _tg = scenario_app(tmp_path)
    payload = {k: v for k, v in BASE_SETUP.items() if v is not None}
    setup_id = call(server, "setup_submit", payload)["setup_id"]
    assert call(server, "setup_cancel", {"setup_id": setup_id})["status"] == "CANCELLED"
    assert call(server, "setup_cancel", {"setup_id": setup_id})["status"] == "ALREADY_CLOSED"


def test_market_candles_returns_closed_bars_only(tmp_path):
    server, _runtime, _clock, _tg = scenario_app(tmp_path)
    payload = call(server, "market_candles", {"symbol": "XAUUSD", "timeframe": "M5", "count": 10})
    assert payload["count"] == 10
    assert all(len(row) == 5 for row in payload["candles"])
