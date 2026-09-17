"""The owner docs must stay true: tool names, parameter names and the Railway rules."""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_tools import scenario_app, tools_of

ADDENDUM = Path("docs/GEMINI_V11_ADDENDUM.md")
SETUP = Path("docs/SETUP_SQ.md")
RULES = Path("docs/RULES_SQ.md")

# Response fields the addendum names, which are not tool parameters.
RESPONSE_FIELDS = {"fvgs", "levels", "atr", "swings", "candles", "low", "high", "formed_at", "reasons"}


def registered(tmp_path):
    server, _runtime, _clock, _tg = scenario_app(tmp_path)
    return {t.name: t for t in tools_of(server)}


def test_every_tool_named_in_the_addendum_exists(tmp_path):
    tools = registered(tmp_path)
    text = ADDENDUM.read_text()
    named = set(re.findall(r"`(market_\w+|setup_\w+|validator_\w+)`", text))
    assert named, "the addendum must name the tools"
    missing = named - set(tools)
    assert missing == set(), f"the addendum names tools that do not exist: {sorted(missing)}"


def test_every_parameter_in_section_d_exists(tmp_path):
    """Section D maps each v11 box line to a real `setup_submit` parameter."""
    tools = registered(tmp_path)
    properties = set(tools["setup_submit"].input_schema["properties"])
    text = ADDENDUM.read_text()
    section = text.split("### D.")[1].split("### E.")[0]
    names: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("| v11 box") or set(line) <= set("|- "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        names.update(re.findall(r"`(\w+)`", cells[1]))
    names -= RESPONSE_FIELDS
    assert names, "section D must name parameters"
    missing = names - properties
    assert missing == set(), f"the addendum names parameters that do not exist: {sorted(missing)}"


def test_addendum_entry_models_match_the_code():
    from app.config import ENTRY_MODELS, PDA_TYPES

    text = ADDENDUM.read_text()
    for model in ENTRY_MODELS:
        assert model in text, f"{model} is missing from the addendum"
    for pda in PDA_TYPES:
        assert pda in text, f"{pda} is missing from the addendum"


def test_no_railway_config_as_code():
    """Failure mode D3: Railway ignores these for new services."""
    assert not Path("railway.json").exists()
    assert not Path("railway.toml").exists()


def test_setup_doc_covers_the_required_variables():
    text = SETUP.read_text()
    for variable in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OWNER_PASSWORD", "CTRADER_MCP_CONFIG"):
        assert variable in text
    assert "/health" in text  # the healthcheck path the owner must set
    assert "/mcp" in text  # the exact URL Gemini needs
    assert "Volume" in text
    assert "Sleeping" in text or "sleeping" in text  # failure mode D9


MONEY_WORDS = ("lot_size", "lotsize", "position_size", "risk_percent", "risk_pct", "account_balance", "equity")


def test_no_money_or_position_sizing_logic_anywhere():
    """The owner decides size; the code must not contain a single lot or balance calculation."""
    offenders: list[str] = []
    for path in sorted(Path("app").rglob("*.py")):
        lowered = path.read_text().lower()
        for word in MONEY_WORDS:
            if word in lowered:
                offenders.append(f"{path}: {word}")
    assert offenders == []


def test_owner_docs_are_written_for_the_owner():
    """Albanian, phone-friendly, and honest that the system never trades."""
    for path in (SETUP, RULES, Path("README.md")):
        text = path.read_text()
        assert "ë" in text, f"{path} does not look Albanian"
    rules = RULES.read_text()
    assert "nuk hyn kurrë vetë" in rules
    assert "HYR TANI" in rules and "MOS HYR" in rules


def test_dockerfile_binds_the_platform_port_and_runs_as_root():
    dockerfile = Path("Dockerfile").read_text()
    assert "0.0.0.0" in dockerfile and "${PORT:-8080}" in dockerfile
    assert "--proxy-headers" in dockerfile and "--forwarded-allow-ips" in dockerfile
    assert "USER " not in dockerfile  # Railway volumes are mounted as root (failure mode D5)
    assert "playwright" not in dockerfile.lower()


def test_readme_is_short_and_links_the_three_docs():
    lines = Path("README.md").read_text().strip().splitlines()
    assert len(lines) <= 15
    text = "\n".join(lines)
    for link in ("docs/SETUP_SQ.md", "docs/GEMINI_V11_ADDENDUM.md", "docs/RULES_SQ.md"):
        assert link in text
