"""MCP tool surface: flat schemas, read-only annotations, and a submit that is never refused."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from app import tools as tools_module
from tests.synth import Tape, TapeRuntime

FORBIDDEN_KEYS = ("$ref", "$defs", "anyOf", "oneOf", "allOf", "not", "definitions")
ALLOWED_TYPES = {"string", "number", "integer", "boolean"}
TOOL_NAMES = {
    "market_snapshot",
    "market_candles",
    "validator_rules",
    "setup_submit",
    "setup_status",
    "setup_cancel",
}


def scenario_app(tmp_path):
    """The real tool registration on a bare MCP server: no lifespan, no background tasks."""
    tape = Tape(price=4300.0)
    tape.drift(400, step=0.02, span=0.6)
    runtime, telegram = TapeRuntime.build(tmp_path, tape)
    server = MCPServer(name="live-validator-test")
    tools_module.register(server, runtime)
    return server, runtime, tape, telegram


def tools_of(server):
    async def main():
        async with Client(server) as client:
            listed = await client.list_tools()
            return list(getattr(listed, "tools", listed))

    return asyncio.run(main())


def call(server, name: str, arguments: dict) -> dict:
    async def main():
        async with Client(server) as client:
            result = await client.call_tool(name, arguments)
            text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
            return json.loads(text)

    return asyncio.run(main())


# ------------------------------------------------------------------ schema lint
def test_every_tool_schema_is_flat(tmp_path):
    """Failure mode G1: Gemini's function calling rejects nested or union schemas."""
    server, *_ = scenario_app(tmp_path)
    tools = tools_of(server)
    assert {t.name for t in tools} == TOOL_NAMES
    for tool in tools:
        schema = tool.input_schema
        assert schema["type"] == "object"
        text = json.dumps(schema)
        for key in FORBIDDEN_KEYS:
            # As a JSON key, so a parameter called "note" is not mistaken for the "not" keyword.
            assert f'"{key}":' not in text, f"{tool.name} uses {key}"
        for name, prop in schema.get("properties", {}).items():
            assert prop.get("type") in ALLOWED_TYPES, f"{tool.name}.{name} is not a primitive"
            assert prop.get("description"), f"{tool.name}.{name} has no description"
            assert "items" not in prop and "properties" not in prop
        for name in schema.get("required", []):
            assert name in schema["properties"]


def test_no_enum_ever_advertises_an_empty_value(tmp_path):
    """Gemini rejects an enum containing "", and a rejected declaration fails the whole app.

    The symptom is brutal and gives no clue: every Spark task dies at "Thinking it through…" the
    moment the app is synced. Optional means a plain string with a default, never an enum member.
    """
    server, *_ = scenario_app(tmp_path)
    for tool in tools_of(server):
        for name, prop in tool.input_schema.get("properties", {}).items():
            values = prop.get("enum")
            if values is None:
                continue
            assert all(isinstance(v, str) and v.strip() for v in values), f"{tool.name}.{name}: {values}"


def test_every_tool_is_advertised_as_a_read_so_gemini_never_asks_to_confirm(tmp_path):
    """Deviation D14: a confirmation tap per analysis is what the owner asked us to remove."""
    server, *_ = scenario_app(tmp_path)
    by_name = {t.name: t for t in tools_of(server)}
    assert set(by_name) == TOOL_NAMES
    for name, tool in by_name.items():
        assert tool.annotations.read_only_hint is True, name
        assert tool.annotations.open_world_hint is False, name
        assert tool.annotations.destructive_hint is not True, name
    for name in ("setup_submit", "setup_cancel"):
        assert by_name[name].annotations.idempotent_hint is True, name


def test_submit_asks_for_almost_nothing(tmp_path):
    """The universal contract: a symbol, a stop and an entry. Everything else is optional."""
    server, *_ = scenario_app(tmp_path)
    submit = next(t for t in tools_of(server) if t.name == "setup_submit")
    required = set(submit.input_schema["required"])
    assert required == {"symbol", "stop_loss"}
    properties = set(submit.input_schema["properties"])
    assert {"entry", "entry_low", "entry_high", "tp1", "tp2", "tp3", "direction", "label"} <= properties
    for gone in ("entry_model", "htf_bias", "checklist_positive", "kill_zone", "pda_type", "valid_until_ny"):
        assert gone not in properties, f"{gone} belongs to the old strategy-bound validator"


# ------------------------------------------------------------------- behaviour
def test_snapshot_is_compact_and_new_york_timed(tmp_path):
    server, _runtime, tape, _tg = scenario_app(tmp_path)
    payload = call(server, "market_snapshot", {"symbol": "XAUUSD"})
    assert payload["schema"] == "snapshot/1"
    assert payload["time"]["ny"].startswith("2026-09-")
    assert payload["quote"]["bid"] == pytest.approx(tape.price)
    assert payload["candles"]["M5"][0][0].startswith("2026-09-")
    assert len(json.dumps(payload)) < 60_000  # failure mode G12


def test_validator_rules_describes_the_evidence_not_a_strategy(tmp_path):
    server, *_ = scenario_app(tmp_path)
    payload = call(server, "validator_rules", {})
    assert payload["schema"] == "rules/2"
    entry = payload["entry"]
    assert entry["confirmations"] == ["ZONE_BREAK", "AO_DIV", "QUASIMODO"]
    assert "any one of the three" in entry["rule"]
    assert entry["strength"] == {"1": "konfirmim", "2": "konfirmim i fortë", "3": "konfirmim shumë i fortë"}
    assert entry["zone_break_timeframes"] == ["M1", "M5", "M15"], "the nearest zone can live on any"
    assert "stands in for any other" in entry["substitution"]
    assert len(payload["cancellations"]) == 2
    assert "never rejects" in payload["principle"]


def test_a_setup_is_registered_through_the_tool_and_never_rejected(tmp_path):
    server, runtime, tape, _tg = scenario_app(tmp_path)
    call(server, "market_snapshot", {"symbol": "XAUUSD"})
    price = tape.price
    result = call(
        server,
        "setup_submit",
        {"symbol": "XAUUSD", "entry": price - 5, "stop_loss": price - 12, "tp1": price + 20},
    )
    assert result["status"] == "registered"
    setup_id = result["setup_id"]

    status = call(server, "setup_status", {"setup_id": setup_id})
    assert status["state"] == "WATCHING"
    cards = runtime.store.query("SELECT text FROM outbox WHERE dedupe_key LIKE ?", (f"{setup_id}:registered",))
    assert len(cards) == 1


def test_even_a_contradictory_setup_is_accepted_and_repaired(tmp_path):
    server, *_ = scenario_app(tmp_path)
    result = call(
        server,
        "setup_submit",
        {"symbol": "XAUUSD", "direction": "LONG", "entry": 4300.0, "stop_loss": 4310.0, "tp1": 4280.0},
    )
    assert result["status"] == "registered"
    assert result["setup"]["direction"] == "SHORT"
    assert result["notes"], "a repair must be reported"


def test_cancel_through_the_tool(tmp_path):
    server, _runtime, tape, _tg = scenario_app(tmp_path)
    setup_id = call(
        server,
        "setup_submit",
        {"symbol": "XAUUSD", "entry": tape.price - 5, "stop_loss": tape.price - 12},
    )["setup_id"]
    assert call(server, "setup_cancel", {"setup_id": setup_id})["status"] == "cancelled"
    assert call(server, "setup_cancel", {"setup_id": setup_id})["status"] == "already_closed"


def test_market_candles_returns_closed_bars_only(tmp_path):
    server, *_ = scenario_app(tmp_path)
    payload = call(server, "market_candles", {"symbol": "XAUUSD", "timeframe": "M5", "count": 10})
    assert payload["count"] == 10
    assert all(len(row) == 5 for row in payload["candles"])
