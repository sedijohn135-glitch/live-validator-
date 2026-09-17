"""Builds the real ASGI app against fakes, for the OAuth, HTTP and MCP integration tests."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any

from app import telegram as tg
from app.config import load_settings
from app.ctrader import CTraderClient
from app.main import build_app
from app.runtime import Runtime
from app.store import Store
from tests.fake_ctrader import FakeCTrader

BASE_URL = "https://validator.up.railway.app"
PASSWORD = "a-very-long-password"


@dataclass
class FakeTelegram:
    """Records Bot API calls and never blocks the command poller."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        self.sent.append({"method": method, **payload})
        if method == "getUpdates":
            pending, self.updates = self.updates, []
            return pending
        return {"message_id": len(self.sent)}

    def client(self) -> tg.TelegramClient:
        return tg.TelegramClient("fake-token", self.call)

    def texts(self) -> list[str]:
        return [c["text"] for c in self.sent if c["method"] == "sendMessage"]


def make_runtime(tmp_path, **env) -> tuple[Runtime, FakeCTrader, FakeTelegram]:
    settings = load_settings(
        {
            "PUBLIC_BASE_URL": BASE_URL,
            "OWNER_PASSWORD": PASSWORD,
            "TELEGRAM_BOT_TOKEN": "fake-token",
            "TELEGRAM_CHAT_ID": "42",
            "CTRADER_MCP_URL": "https://ctrader.test/mcp",
            "CTRADER_MCP_TOKEN": "tok" * 8,
            "NEWS_FILTER": "off",
            "DATA_DIR": str(tmp_path),
            **env,
        }
    )
    store = Store(str(tmp_path / "validator.db"))
    fake_ctrader = FakeCTrader()
    telegram = FakeTelegram()
    ctrader = CTraderClient(settings, store, connector=fake_ctrader.connector())
    runtime = Runtime(settings, store, ctrader=ctrader, telegram=telegram.client())
    return runtime, fake_ctrader, telegram


def make_app(tmp_path, **env):
    runtime, fake_ctrader, telegram = make_runtime(tmp_path, **env)
    return build_app(runtime), runtime, fake_ctrader, telegram


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge
