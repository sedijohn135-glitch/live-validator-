"""cTrader adapter: the read-only allowlist, credential parsing, decoding and error handling."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import ctrader
from app.config import load_settings
from app.ctrader import (
    ALLOWED_TOOLS,
    KNOWN_TRADING_TOOLS,
    AuthError,
    CTraderClient,
    DataError,
    ForbiddenTool,
    RateLimiter,
    mask,
    parse_config,
    resolve_credentials,
)
from app.store import Store
from tests.fake_ctrader import FakeCTrader


def make_client(tmp_path, fake: FakeCTrader | None = None, **env):
    fake = fake or FakeCTrader()
    settings = load_settings({"CTRADER_MCP_URL": "https://ctrader.test/mcp", "CTRADER_MCP_TOKEN": "tok" * 8, **env})
    store = Store(str(tmp_path / "v.db"))
    return CTraderClient(settings, store, connector=fake.connector()), fake, store


def with_client(tmp_path, body, fake: FakeCTrader | None = None, discover: bool = True, **env):
    """Run `body(client, fake, store)` inside one event loop: an MCP session cannot outlive its loop."""
    client, fake, store = make_client(tmp_path, fake, **env)

    async def main():
        try:
            if discover:
                await client.discover()
            return await body(client, fake, store)
        finally:
            await client.aclose()

    return asyncio.run(main())


# ------------------------------------------------------------------ allowlist
@pytest.mark.parametrize("tool", sorted(KNOWN_TRADING_TOOLS))
def test_trading_tools_are_refused_without_touching_the_network(tmp_path, tool):
    async def body(client, fake, _store):
        with pytest.raises(ForbiddenTool):
            await client.call(tool, {})
        assert fake.calls == []
        assert fake.trading_calls == []

    with_client(tmp_path, body, discover=False)


def test_allowlist_is_exactly_the_four_read_only_tools():
    assert ALLOWED_TOOLS == {"get_version", "get_symbols", "get_spot_prices", "get_trendbars"}
    assert not ALLOWED_TOOLS & KNOWN_TRADING_TOOLS


def test_trading_tool_names_appear_only_in_the_denylist_constant():
    """Failure mode C1: no code path anywhere in `app/` can name a trading tool."""
    offenders: list[str] = []
    for path in sorted(Path("app").rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if any(name in line for name in ("create_order", "close_position", "amend_", "cancel_order")):
                if path.name == "ctrader.py" and "amend_order" in line and "create_order" in line:
                    continue  # the single KNOWN_TRADING_TOOLS definition
                offenders.append(f"{path}:{number}: {line.strip()}")
    assert offenders == []


def test_discovery_records_the_profile_and_never_calls_trading_tools(tmp_path):
    async def body(client, fake, store):
        info = await client.discover()
        assert info["profile"] == "trading"  # the fake exposes them; we only report it
        assert set(KNOWN_TRADING_TOOLS).issubset(set(info["tools"]))
        assert fake.trading_calls == []
        assert store.get_json("ctrader_tools")
        assert client.symbols["XAUUSD"].symbol_id == 41

    with_client(tmp_path, body, discover=False)


def test_data_profile_is_reported_when_trading_tools_are_absent(tmp_path):
    async def body(client, _fake, _store):
        assert (await client.discover())["profile"] == "data"

    with_client(tmp_path, body, fake=FakeCTrader(include_trading=False), discover=False)


# ---------------------------------------------------------------- credentials
def test_parse_config_accepts_a_pasted_json_config():
    raw = json.dumps(
        {
            "mcpServers": {
                "ctrader": {
                    "serverUrl": "https://mcp.ctrader.com/abc/",
                    "headers": {"Authorization": "Bearer secrettoken1234"},
                }
            }
        }
    )
    creds = parse_config(raw)
    assert creds.url == "https://mcp.ctrader.com/abc"
    assert creds.token == "secrettoken1234"


def test_parse_config_accepts_the_snippet_ctrader_web_shows():
    """cTrader Web prints the body without the outer braces; the URL must not keep its quote."""
    snippet = (
        '"url": "https://mcp.ctrader.com/trading",\n'
        '"headers": {\n'
        '  "Authorization": "Bearer eyJwbGF0Zm9ybSI6ImN0cmFkZXIifQ.abc-123_XY"\n'
        "}"
    )
    creds = parse_config(snippet)
    assert creds.url == "https://mcp.ctrader.com/trading"
    assert creds.token == "eyJwbGF0Zm9ybSI6ImN0cmFkZXIifQ.abc-123_XY"

    braced = "{" + snippet + "}"
    assert parse_config(braced).url == creds.url
    assert parse_config(braced).token == creds.token


def test_parse_config_accepts_a_bare_token_with_a_known_url():
    creds = parse_config("abcdefghijklmnopqrst", known_url="https://mcp.ctrader.com/x")
    assert creds.token == "abcdefghijklmnopqrst"
    assert creds.url == "https://mcp.ctrader.com/x"


def test_parse_config_rejects_noise():
    assert parse_config("") is None
    assert parse_config("hello") is None


def test_credential_precedence(tmp_path):
    settings = load_settings(
        {
            "CTRADER_MCP_CONFIG": json.dumps({"url": "https://env/mcp", "token": "envtoken12345678"}),
            "CTRADER_MCP_URL": "https://pair/mcp",
            "CTRADER_MCP_TOKEN": "pairtoken1234567",
        }
    )
    assert resolve_credentials(settings, None).source == "env_config"
    override = json.dumps({"url": "https://tg/mcp", "token": "tgtoken123456789"})
    assert resolve_credentials(settings, override).source == "telegram"
    bare = load_settings({"CTRADER_MCP_URL": "https://pair/mcp", "CTRADER_MCP_TOKEN": "Bearer pairtoken1234567"})
    creds = resolve_credentials(bare, None)
    assert creds.source == "env_pair" and creds.token == "pairtoken1234567"


def test_tokens_are_masked():
    assert mask("abcdefghijklmnop") == "abcd…mnop"
    assert mask("short") == "…"
    assert mask("") == "-"


def test_hot_swap_reconnects_and_rediscovers(tmp_path):
    async def body(client, _fake, store):
        new_config = json.dumps({"url": "https://new/mcp", "token": "newtoken12345678"})
        creds = await client.swap_credentials(new_config)
        assert creds.source == "telegram" and creds.url == "https://new/mcp"
        assert store.get_kv("ctrader_override")
        await client.swap_credentials(None)
        assert store.get_kv("ctrader_override") is None
        assert client.credentials.source == "env_pair"

    with_client(tmp_path, body)


# ------------------------------------------------------------------- decoding
def test_pipettes_are_decoded_with_the_symbol_digits(tmp_path):
    async def body(client, _fake, _store):
        assert client.decode("XAUUSD", 5654520) == pytest.approx(5654.52)

    with_client(tmp_path, body)


def test_wrong_digits_are_auto_calibrated_against_the_price_band(tmp_path):
    async def body(client, _fake, _store):
        client.symbols["XAUUSD"] = ctrader.SymbolInfo("XAUUSD", 41, 5)  # wrong metadata
        assert client.decode("XAUUSD", 5654520) == pytest.approx(5654.52)
        assert client.symbols["XAUUSD"].calibrated
        assert any("kalibrua" in w for w in client.warnings)

    with_client(tmp_path, body)


def test_undecodable_price_disables_the_symbol(tmp_path):
    async def body(client, _fake, _store):
        assert client.decode("XAUUSD", 7) is None
        assert not client.symbols["XAUUSD"].enabled
        assert any("PRICE_DIGITS" in w for w in client.warnings)

    with_client(tmp_path, body)


def test_display_prices_pass_through_when_already_fractional(tmp_path):
    async def body(client, _fake, _store):
        assert client.decode("XAUUSD", 5654.52) == pytest.approx(5654.52)
        assert client.decode("XAUUSD", 1.5) is None  # outside the band

    with_client(tmp_path, body)


def test_cross_check_disables_a_symbol_whose_candles_disagree(tmp_path):
    async def body(client, _fake, _store):
        assert client.cross_check("XAUUSD", 5654.52, 5655.0)
        assert not client.cross_check("XAUUSD", 5654.52, 4000.0)
        assert not client.symbols["XAUUSD"].enabled

    with_client(tmp_path, body)


# --------------------------------------------------------------------- quotes
def test_quotes_are_batched_and_decoded(tmp_path):
    async def body(client, fake, _store):
        quotes = await client.quotes(["XAUUSD", "BTCUSD"])
        assert quotes["XAUUSD"].bid == pytest.approx(5654.52)
        assert quotes["XAUUSD"].spread == pytest.approx(0.25)
        batched = [c for c in fake.calls if c[0] == "get_spot_prices"]
        assert len(batched) == 1 and batched[0][1]["symbolIds"] == [41, 101]

    with_client(tmp_path, body)


def test_unknown_symbol_ids_never_reach_the_batch(tmp_path):
    """Failure mode C3: one bad id would silently empty the whole batch."""

    async def body(client, fake, _store):
        client.symbols["GHOST"] = ctrader.SymbolInfo("GHOST", 999999, 2, enabled=False)
        quotes = await client.quotes(["XAUUSD", "GHOST"])
        assert "XAUUSD" in quotes
        assert 999999 not in [c for c in fake.calls if c[0] == "get_spot_prices"][-1][1]["symbolIds"]

    with_client(tmp_path, body)


# -------------------------------------------------------------------- candles
def test_candles_are_decoded_and_forming_bars_dropped(tmp_path):
    async def body(client, _fake, _store):
        end = 1789560000.0
        client.clock = lambda: end
        candles = await client.candles("XAUUSD", "M5", count=20)
        assert candles and all(c.t + 300 <= end for c in candles)
        assert candles == sorted(candles, key=lambda c: c.t)
        assert 5000 < candles[0].o < 7000

    with_client(tmp_path, body)


def test_unsupported_timeframe_is_refused_before_the_call(tmp_path):
    async def body(client, fake, _store):
        before = len(fake.calls)
        with pytest.raises(DataError):
            await client.candles("XAUUSD", "M2", count=10)
        assert len(fake.calls) == before

    with_client(tmp_path, body)


def test_wide_history_windows_are_chunked(tmp_path):
    """Failure mode C5: a request window wider than 720 hours is rejected by the server."""

    async def body(client, fake, _store):
        end = 1789560000.0
        client.clock = lambda: end
        await client.candles("XAUUSD", "D1", count=250)
        windows = [c[1] for c in fake.calls if c[0] == "get_trendbars"]
        assert len(windows) >= 3
        assert all(w["to"] - w["from"] <= 720 * 3600 * 1000 for w in windows)

    with_client(tmp_path, body)


# --------------------------------------------------------------------- errors
@pytest.mark.parametrize("message", ["401 Unauthorized", "token expired", "invalid token", "session closed"])
def test_auth_style_errors_are_classified_as_auth(tmp_path, message):
    async def body(client, fake, _store):
        fake.fail_next = RuntimeError(message)
        with pytest.raises(AuthError):
            await client.call("get_version")
        assert client.status == "auth_error"

    with_client(tmp_path, body)


def test_other_errors_are_data_errors(tmp_path):
    async def body(client, fake, _store):
        fake.fail_next = RuntimeError("502 UNKNOWN_SYMBOL")
        with pytest.raises(DataError):
            await client.call("get_version")
        assert client.status == "down"

    with_client(tmp_path, body)


def test_plain_string_errors_are_handled(tmp_path):
    async def body(client, fake, _store):
        fake.fail_next = RuntimeError("rate limit exceeded, retry")
        with pytest.raises(DataError):
            await client.call("get_symbols")

    with_client(tmp_path, body)


def test_rate_limiter_spaces_historical_calls():
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    clock = iter([0.0, 0.0, 0.0, 0.0])
    limiter = RateLimiter(general_per_s=20.0, historical_per_s=4.0)

    async def run():
        await limiter.acquire(True, sleep=fake_sleep, clock=lambda: next(clock))
        await limiter.acquire(True, sleep=fake_sleep, clock=lambda: next(clock))

    asyncio.run(run())
    assert slept and slept[-1] == pytest.approx(0.25)


def test_no_trading_call_was_recorded_by_any_test(tmp_path):
    async def body(client, fake, _store):
        await client.quotes(["XAUUSD"])
        assert fake.trading_calls == []

    with_client(tmp_path, body)


def test_unresolved_symbol_is_an_error_not_silent_emptiness(tmp_path):
    """A missing symbol must surface as a data error; silence would be reported as health."""

    async def body(client, _fake, _store):
        client.symbols.pop("XAUUSD", None)
        with pytest.raises(DataError, match="nuk u zgjidh"):
            await client.candles("XAUUSD", "M5", count=5)
        with pytest.raises(DataError, match="asnjë simbol"):
            await client.quotes(["XAUUSD"])

    with_client(tmp_path, body)


def test_disabled_symbol_is_an_error(tmp_path):
    async def body(client, _fake, _store):
        info = client.symbols["XAUUSD"]
        client.symbols["XAUUSD"] = ctrader.SymbolInfo(info.name, info.symbol_id, info.digits, enabled=False)
        with pytest.raises(DataError, match="çaktivizuar"):
            await client.candles("XAUUSD", "M5", count=5)

    with_client(tmp_path, body)


def test_a_missing_symbol_warning_names_what_ctrader_offers(tmp_path):
    """The owner needs the real broker name to put in SYMBOL_MAP."""

    async def body(client, _fake, _store):
        client.settings.symbols = ("XAUUSD", "XAUEUR")
        await client.load_symbols()
        assert "XAUEUR" not in client.symbols
        warning = next(w for w in client.warnings if "XAUEUR" in w)
        assert "XAUUSD" in warning  # the near-miss cTrader does offer

    with_client(tmp_path, body)


def test_argument_names_follow_the_live_schema(tmp_path):
    """Failure mode C4 in the field: this broker's build names the batch `symbolId`, not `symbolIds`."""

    async def body(client, fake, _store):
        assert "symbolId" in client._properties("get_spot_prices")
        args = client.build_args("get_spot_prices", {"symbol_ids": [41, 101]})
        assert args == {"symbolId": [41, 101]}
        quotes = await client.quotes(["XAUUSD", "BTCUSD"])
        assert quotes["XAUUSD"].bid == pytest.approx(5654.52)

    with_client(tmp_path, body, fake=FakeCTrader(spot_param="symbolId"))


def test_argument_names_still_work_on_the_documented_build(tmp_path):
    async def body(client, _fake, _store):
        args = client.build_args("get_spot_prices", {"symbol_ids": [41]})
        assert args == {"symbolIds": [41]}
        assert (await client.quotes(["XAUUSD"]))["XAUUSD"].bid == pytest.approx(5654.52)

    with_client(tmp_path, body)


def test_a_scalar_argument_is_never_sent_as_a_list(tmp_path):
    async def body(client, _fake, _store):
        args = client.build_args("get_trendbars", {"symbol_id": 41, "period": "M_5", "from": 1, "to": 2})
        assert args == {"symbolId": 41, "period": "M_5", "fromTimestamp": 1, "toTimestamp": 2}

    with_client(tmp_path, body)
