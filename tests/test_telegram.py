"""Telegram templates, the outbox sender, the owner guard and secret redaction."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app import telegram as tg
from app.runtime import SecretFilter
from app.store import Store
from tests.app_harness import FakeTelegram, make_runtime


@pytest.fixture()
def runtime(tmp_path):
    runtime, _fake_ctrader, telegram = make_runtime(tmp_path)
    runtime.telegram_fake = telegram
    return runtime


# ------------------------------------------------------------------ templates
def test_dynamic_values_are_html_escaped():
    """Failure mode T1: an unescaped `<` breaks the whole message."""
    text = tg.rejected_message("XAUUSD", "LONG", ["RR 1.2 < 1:2 & risky"], "XAU-0916-A1B2")
    assert "&lt;" in text and "&amp;" in text
    assert "<b>" in text  # our own markup survives


def test_enter_message_shows_the_chase_limit_and_validity():
    text = tg.enter_message(
        {
            "symbol": "XAUUSD",
            "direction": "LONG",
            "entry": 5656.6,
            "spread": 0.2,
            "stop_loss": 5643.0,
            "tp1": 5675.0,
            "tp2": 5700.0,
            "tp3": None,
            "rr": 3.2,
            "target_label": "TP2",
            "chase_limit": 5657.18,
            "valid_until": "2026-09-16 08:15",
            "model": "ICT_2022",
            "ltf": "M5",
            "window": "NY_2022",
            "checks": "SSL ✓ · CISD ✓",
            "score": 5,
            "id": "XAU-0916-A1B2",
            "time": "2026-09-16 08:10",
        },
        2,
    )
    assert "HYR TANI — BUY XAUUSD" in text
    assert "Mos hyr nëse çmimi &gt; 5657.18" in text
    assert "vlen deri 2026-09-16 08:15 NY" in text
    assert "TP3" not in text
    assert "Score: 5/7" in text


def test_short_enter_message_uses_the_bid_side():
    text = tg.enter_message(
        {
            "symbol": "BTCUSD",
            "direction": "SHORT",
            "entry": 90000.0,
            "spread": 20.0,
            "stop_loss": 91000.0,
            "tp1": 88000.0,
            "tp2": None,
            "tp3": None,
            "rr": 2.0,
            "target_label": "TP1",
            "chase_limit": 89500.0,
            "valid_until": "2026-09-16 08:15",
            "model": "OTE",
            "ltf": "M5",
            "window": "NY_KZ",
            "checks": "BSL ✓",
            "score": 3,
            "id": "BTC-0916-A1B2",
            "time": "2026-09-16 08:10",
        },
        2,
    )
    assert "🔴" in text and "SELL BTCUSD" in text
    assert "(bid)" in text and "Mos hyr nëse çmimi &lt;" in text


def test_long_messages_split_on_line_boundaries():
    """Failure mode T2: the Bot API refuses anything over 4096 characters."""
    text = "\n".join(f"line {i} " + "x" * 60 for i in range(200))
    parts = tg.split_message(text)
    assert len(parts) > 1
    assert all(len(part) <= tg.MAX_MESSAGE for part in parts)
    assert "".join(part.replace("\n", "") for part in parts) == text.replace("\n", "")


# --------------------------------------------------------------------- sender
def test_sender_marks_messages_sent_once(tmp_path):
    store = Store(str(tmp_path / "v.db"))
    store.queue_message("s1:ENTER", "42", "hello")
    fake = FakeTelegram()
    sender = tg.OutboxSender(store, fake.client(), "42")
    assert asyncio.run(sender.drain_once()) == 1
    assert asyncio.run(sender.drain_once()) == 0
    assert fake.texts() == ["hello"]


def test_sender_honours_retry_after(tmp_path):
    """Failure mode T3: flood control must be waited out, not hammered."""
    store = Store(str(tmp_path / "v.db"))
    store.queue_message("s1:ENTER", "42", "hello")
    slept: list[float] = []

    async def call(method, payload):
        raise tg.TelegramError("Too Many Requests", retry_after=7)

    async def sleep(seconds):
        slept.append(seconds)

    sender = tg.OutboxSender(store, tg.TelegramClient("t", call), "42", sleep=sleep)
    assert asyncio.run(sender.drain_once()) == 0
    assert slept == [7.0]
    row = store.pending_messages()[0]
    assert row["attempts"] == 1 and "Too Many" in row["last_error"]


def test_sender_retries_after_a_network_error(tmp_path):
    store = Store(str(tmp_path / "v.db"))
    store.queue_message("s1:ENTER", "42", "hello")
    attempts = {"n": 0}

    async def call(method, payload):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("connection reset")
        return {"message_id": 1}

    sender = tg.OutboxSender(store, tg.TelegramClient("t", call), "42")
    assert asyncio.run(sender.drain_once()) == 0
    assert asyncio.run(sender.drain_once()) == 1
    assert store.pending_messages() == []


# ------------------------------------------------------------------ commands
def owner_update(text: str, chat_id: str = "42", message_id: int = 7) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text, "message_id": message_id}}


def test_strangers_are_ignored_silently(runtime):
    fake = runtime.telegram_fake
    asyncio.run(runtime.handle_update(owner_update("/status", chat_id="999"), fake.client()))
    assert fake.sent == []


def test_owner_commands_answer(runtime):
    fake = runtime.telegram_fake
    for command in ("/help", "/status", "/active", "/rules", "/stats 7"):
        asyncio.run(runtime.handle_update(owner_update(command), fake.client()))
    texts = fake.texts()
    assert len(texts) == 5
    assert "/selftest" in texts[0]
    assert "Gjendja" in texts[1]
    assert "S'ka setup aktive" in texts[2]
    assert "Pragjet (STRICT)" in texts[3]


def test_pause_and_resume(runtime):
    fake = runtime.telegram_fake
    asyncio.run(runtime.handle_update(owner_update("/pause"), fake.client()))
    assert runtime.engine.paused()
    asyncio.run(runtime.handle_update(owner_update("/resume"), fake.client()))
    assert not runtime.engine.paused()


def test_ctrader_message_is_deleted_after_reading(runtime):
    """Failure mode T6: the pasted token must not stay visible in the chat."""
    fake = runtime.telegram_fake
    asyncio.run(runtime.handle_update(owner_update("/ctrader reset"), fake.client()))
    methods = [c["method"] for c in fake.sent]
    assert "deleteMessage" in methods
    deleted = next(c for c in fake.sent if c["method"] == "deleteMessage")
    assert deleted["message_id"] == 7


def test_start_shows_the_chat_id_when_it_is_not_configured(tmp_path):
    """Failure mode T4: the owner needs the id before the bot can message them."""
    runtime, _fake_ctrader, telegram = make_runtime(tmp_path, TELEGRAM_CHAT_ID="")
    asyncio.run(runtime.handle_update(owner_update("/start", chat_id="12345"), telegram.client()))
    assert "12345" in telegram.texts()[0]


def test_unknown_command(runtime):
    assert "panjohur" in asyncio.run(runtime.run_command("/nope"))


# ----------------------------------------------------------------- redaction
def test_secrets_never_reach_the_logs(caplog):
    """Failure mode T7: a token in a log line is a leaked token."""
    secret = "super-secret-token-value"
    logger = logging.getLogger("redaction-test")
    logger.addFilter(SecretFilter(lambda: [secret]))
    with caplog.at_level(logging.INFO, logger="redaction-test"):
        logger.info("calling with Authorization: Bearer %s", secret)
    assert secret not in caplog.text
    assert "***" in caplog.text
