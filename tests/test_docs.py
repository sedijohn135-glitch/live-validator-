"""The owner docs must stay true: tool names, parameter names and the Railway rules."""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_tools import scenario_app, tools_of

GEMINI = Path("docs/GEMINI.md")
VALIDATOR = Path("docs/VALIDATOR.md")
SPARK_SKILL = Path("spark-skill/live-validator/SKILL.md")
SETUP = Path("docs/SETUP_SQ.md")
RULES = Path("docs/RULES_SQ.md")

# Response fields the docs name, which are not tool parameters.
RESPONSE_FIELDS = {"fvgs", "levels", "atr", "swings", "candles", "notes", "usable", "status"}


def registered(tmp_path):
    server, *_ = scenario_app(tmp_path)
    return {t.name: t for t in tools_of(server)}


def test_every_tool_named_in_the_guides_exists(tmp_path):
    tools = registered(tmp_path)
    for path in (GEMINI, SPARK_SKILL):
        named = set(re.findall(r"`(market_\w+|setup_\w+|validator_\w+)`", path.read_text()))
        assert named, f"{path} must name the tools"
        assert named <= set(tools), f"{path} names tools that do not exist: {sorted(named - set(tools))}"


def test_every_parameter_the_guides_promise_exists(tmp_path):
    tools = registered(tmp_path)
    properties = set(tools["setup_submit"].input_schema["properties"])
    for path in (GEMINI, SPARK_SKILL):
        text = path.read_text()
        names: set[str] = set()
        for line in text.splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            names.update(re.findall(r"`(\w+)`", cells[0] if cells else ""))
        names -= RESPONSE_FIELDS
        missing = names - properties - set(tools)
        assert missing == set(), f"{path} names parameters that do not exist: {sorted(missing)}"


def test_the_guides_promise_that_nothing_is_rejected(tmp_path):
    for path in (GEMINI, SPARK_SKILL, RULES, VALIDATOR):
        text = path.read_text().lower()
        assert "reject" in text or "refuzo" in text, f"{path} must address rejection explicitly"


def test_the_validator_spec_documents_every_signal():
    from app.evidence import WEIGHTS

    text = VALIDATOR.read_text()
    for signal in WEIGHTS:
        assert signal in text, f"{signal} is missing from docs/VALIDATOR.md"


def test_the_validator_spec_cites_its_sources():
    text = VALIDATOR.read_text()
    assert "## Sources consulted" in text
    assert len(re.findall(r"- \[.+?\]\(https?://", text)) >= 6


def test_spark_skill_meets_the_upload_requirements():
    text = SPARK_SKILL.read_text()
    assert text.startswith("---\n")
    front = text.split("---")[1]
    assert re.search(r"^name:\s*live-validator\s*$", front, re.MULTILINE)
    description = re.search(r"^description:\s*(.+)$", front, re.MULTILINE)
    assert description and len(description.group(1)) <= 1024
    assert len(text) < 20_000


def test_no_railway_config_as_code():
    for name in ("railway.json", "railway.toml"):
        assert not Path(name).exists(), f"{name} must not exist: Railway is configured in its UI"


def test_setup_doc_covers_the_required_variables():
    text = SETUP.read_text()
    for variable in ("OWNER_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "PUBLIC_BASE_URL"):
        assert variable in text, f"{variable} is missing from docs/SETUP_SQ.md"
    assert "Volume" in text and "/data" in text


def test_no_money_or_position_sizing_logic_anywhere():
    """The validator decides the moment, never the size."""
    for path in Path("app").glob("*.py"):
        text = path.read_text().lower()
        for word in ("lot_size", "position_size", "risk_percent", "account_balance", "equity"):
            assert word not in text, f"{path} mentions {word}"


def test_owner_docs_are_written_for_the_owner():
    text = RULES.read_text()
    assert "def " not in text and "python" not in text.lower()
    assert "HYR TANI" in text and "SIGURO FITIMET" in text


def test_dockerfile_binds_the_platform_port_and_runs_as_root():
    text = Path("Dockerfile").read_text()
    assert "$PORT" in text
    assert "USER " not in text  # Railway volumes are root-owned


def test_readme_is_short_and_links_the_docs():
    text = Path("README.md").read_text()
    assert len(text) < 6000
    for path in ("docs/VALIDATOR.md", "docs/SETUP_SQ.md", "docs/RULES_SQ.md"):
        assert path in text, f"README must link {path}"
