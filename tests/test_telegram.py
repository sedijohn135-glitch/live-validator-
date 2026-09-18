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
ENTER = {
    "symbol": "XAUUSD",
    "direction": "LONG",
    "setup_id": "XAU-0918-A1B2",
    "entry": 4298.4,
    "stop": 4291.2,
    "risk": 7.2,
    "targets": [4330.0, 4360.0],
    "rr": [4.39, 8.56],
    "secure_at": 4305.0,
    "secure_why": "swing M5",
    "signals": [("RECLAIM", "likuiditeti u mor & çmimi u kthye"), ("MOMENTUM", "trup i fortë")],
    "score": 3,
    "bars": 4,
    "spread": 0.27,
    "touch_ny": "09:41",
}


def test_dynamic_values_are_html_escaped():
    """Failure mode T1: an unescaped `<` breaks the whole message."""
    text = tg.evidence_message(
        {
            "symbol": "XAUUSD",
            "direction": "LONG",
            "setup_id": "XAU-0918-A1B2",
            "signals": [("REJECTION", "spread < 0.3 & i qetë")],
            "score": 2,
            "holds": ["spread > 1.0"],
        }
    )
    assert "&lt;" in text and "&amp;" in text and "&gt;" in text
    assert "<b>" in text  # our own markup survives


def test_the_enter_message_carries_the_whole_decision():
    text = tg.enter_message(ENTER, 2)
    assert "HYR TANI" in text and "BLERJE XAUUSD" in text
    assert "Hyrje: 4298.40" in text
    assert "SL: 4291.20" in text
    assert "TP1 4330.00 (4.4R)" in text and "TP2 4360.00 (8.6R)" in text
    assert "Siguro fitimet te 4305.00 (swing M5)" in text
    assert "RECLAIM + MOMENTUM (3 pikë)" in text
    assert "XAU-0918-A1B2" in text


def test_the_limit_message_says_how_far_the_price_ran():
    text = tg.limit_message({**ENTER, "advance_r": 0.62, "entry_why": "50% i qiriut"}, 2)
    assert "LIMIT" in text
    assert "0.62R" in text
    assert "50% i qiriut" in text


def test_a_short_setup_reads_as_a_sell():
    text = tg.registered_message(
        {
            "symbol": "BTCUSD",
            "direction": "SHORT",
            "setup_id": "BTC-0918-Z9Z9",
            "zone_low": 76000.0,
            "zone_high": 76200.0,
            "stop": 76800.0,
            "risk": 700.0,
            "targets": [74000.0],
            "rr": [3.0],
            "price": 75500.0,
            "distance": 500.0,
            "notes": ["objektivat mungonin — u llogaritën te 1R, 2R, 3R"],
        },
        2,
    )
    assert "SHITJE BTCUSD" in text
    assert "Zona: 76000.00 – 76200.00" in text
    assert "ℹ️" in text


def test_the_two_cancellations_read_differently():
    base = {"symbol": "XAUUSD", "direction": "LONG", "setup_id": "X", "price": 4287.0}
    assert "SL u prek para" in tg.cancel_message({**base, "reason": "SL_FIRST"}, 2)
    assert "TP1 u prek para" in tg.cancel_message({**base, "reason": "TP1_FIRST"}, 2)


def test_the_protection_messages_exist_for_every_step():
    base = {"symbol": "XAUUSD", "direction": "LONG", "setup_id": "X", "price": 4310.0}
    assert "SIGURO FITIMET" in tg.secure_message({**base, "secure_at": 4305.0, "secure_why": "PDH", "secure_r": 0.9}, 2)
    assert "SL NË HYRJE" in tg.breakeven_message(base, 2)
    assert "SHENJA KTHIMI" in tg.reversal_message(base, 2)
    assert "TP2 U ARRIT" in tg.tp_message({**base, "n": 2, "r_multiple": 2.1}, 2)
    assert "SL U PREK" in tg.sl_message(base, 2)


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
    assert "Si vendos validatori" in texts[3]


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
