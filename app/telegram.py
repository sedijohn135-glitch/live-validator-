"""Albanian Telegram templates, the outbox sender and the owner command poller.

`parse_mode=HTML`, so every dynamic value is escaped (failure mode T1) and long messages are split
on line boundaries (T2).
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MAX_MESSAGE = 4096
API_BASE = "https://api.telegram.org"


def esc(value: Any) -> str:
    """HTML-escape any dynamic value."""
    if value is None:
        return "-"
    return html.escape(str(value), quote=False)


def num(value: float | None, decimals: int) -> str:
    if value is None:
        return "-"
    return f"{value:.{decimals}f}"


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split on line boundaries so HTML tags are never cut in half."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        chunk = line if len(line) <= limit else line[:limit]
        if size + len(chunk) + 1 > limit and current:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(chunk)
        size += len(chunk) + 1
    if current:
        parts.append("\n".join(current))
    return parts


# --------------------------------------------------------------------- templates
def enter_message(data: dict[str, Any], decimals: int) -> str:
    long = data["direction"] == "LONG"
    icon = "🟢" if long else "🔴"
    side = "BUY" if long else "SELL"
    price_kind = "ask" if long else "bid"
    comparison = "&gt;" if long else "&lt;"
    lines = [
        f"{icon} <b>HYR TANI — {side} {esc(data['symbol'])}</b>",
        f"Çmimi: <b>@{num(data['entry'], decimals)}</b> ({price_kind}) · spread {num(data['spread'], decimals)}",
        f"SL: <b>{num(data['stop_loss'], decimals)}</b>",
    ]
    tps = f"TP1: {num(data['tp1'], decimals)}"
    if data.get("tp2") is not None:
        tps += f" · TP2: {num(data['tp2'], decimals)}"
    if data.get("tp3") is not None:
        tps += f" · TP3: {num(data['tp3'], decimals)}"
    lines.append(tps)
    lines.append(f"RR: 1:{data['rr']:.2f} ({esc(data['target_label'])})")
    lines.append(
        f"⛔ Mos hyr nëse çmimi {comparison} {num(data['chase_limit'], decimals)} "
        f"· vlen deri {esc(data['valid_until'])} NY"
    )
    lines.append(f"Model: {esc(data['model'])} · LTF {esc(data['ltf'])} · {esc(data['window'])}")
    lines.append(f"Konfirmime: {esc(data['checks'])}")
    score = f"Score: {data['score']}/7"
    if data.get("pda_unverified"):
        score += " · ⚠️ PDA e paverifikuar"
    if data.get("news_down"):
        score += " · ⚠️ filtri i lajmeve jashtë funksionit"
    if data.get("delay_s"):
        score += f" · vonesë {int(data['delay_s'])}s"
    lines.append(score)
    lines.append(f"Burimi: IC Markets cTrader · ID: {esc(data['id'])} · {esc(data['time'])} NY")
    return "\n".join(lines)


def armed_message(data: dict[str, Any], decimals: int) -> str:
    side = "BUY" if data["direction"] == "LONG" else "SELL"
    lines = [
        f"🎯 <b>SETUP NË MONITORIM</b> — {side} {esc(data['symbol'])}",
        f"Zona: {num(data['entry_low'], decimals)} – {num(data['entry_high'], decimals)} "
        f"· SL {num(data['stop_loss'], decimals)}",
    ]
    tps = f"TP1 {num(data['tp1'], decimals)}"
    if data.get("tp2") is not None:
        tps += f" · TP2 {num(data['tp2'], decimals)}"
    if data.get("tp3") is not None:
        tps += f" · TP3 {num(data['tp3'], decimals)}"
    rr = data.get("rr_plan")
    lines.append(tps + (f" · RR plan 1:{rr:.2f}" if rr else ""))
    lines.append(f"Model: {esc(data['model'])} · LTF {esc(data['ltf'])}")
    lines.append(f"Pret: {esc(data['waits_for'])}")
    expires = f"Skadon: {esc(data['expires'])} NY"
    if data.get("news"):
        expires += f" · Lajme: {esc(data['news'])}"
    lines.append(expires)
    lines.append(f"ID: {esc(data['id'])}")
    return "\n".join(lines)


def rejected_message(symbol: str, direction: str, reasons: list[str], setup_id: str) -> str:
    side = "BUY" if direction == "LONG" else "SELL"
    lines = [f"❌ <b>SETUP I REFUZUAR</b> — {side} {esc(symbol)}"]
    lines += [f"• {esc(reason)}" for reason in reasons]
    lines.append(f"Mos hyr. ID: {esc(setup_id)}")
    return "\n".join(lines)


def invalidated_message(symbol: str, direction: str, reason: str, setup_id: str, time_ny: str, replay: bool) -> str:
    side = "BUY" if direction == "LONG" else "SELL"
    suffix = " (gjatë ndërprerjes)" if replay else ""
    return (
        f"⛔ <b>MOS HYR — SETUP I ANULUAR</b> — {side} {esc(symbol)}\n"
        f"Arsyeja: {esc(reason)}{suffix}\n"
        f"ID: {esc(setup_id)} · {esc(time_ny)} NY"
    )


def expired_message(symbol: str, direction: str, setup_id: str, time_ny: str) -> str:
    side = "BUY" if direction == "LONG" else "SELL"
    return (
        f"⌛ <b>SKADOI</b> — {side} {esc(symbol)}: nuk u konfirmua deri {esc(time_ny)} NY. "
        f"Mos hyr. ID: {esc(setup_id)}"
    )


def missed_message(symbol: str, direction: str, reason: str, setup_id: str) -> str:
    side = "BUY" if direction == "LONG" else "SELL"
    return (
        f"⚠️ <b>KONFIRMIM I HUMBUR</b> — {side} {esc(symbol)}: {esc(reason)}. "
        f"Mos e ndiq çmimin. ID: {esc(setup_id)}"
    )


def replaced_message(old_id: str, new_id: str) -> str:
    return f"♻️ Setup {esc(old_id)} u zëvendësua nga {esc(new_id)}."


def cancelled_message(setup_id: str) -> str:
    return f"🗑️ Setup {esc(setup_id)} u anulua me kërkesë."


def exit_message(symbol: str, direction: str, reason: str, setup_id: str) -> str:
    side = "BUY" if direction == "LONG" else "SELL"
    return f"🚪 <b>DIL NGA TREGU</b> — {side} {esc(symbol)}: {esc(reason)}. ID: {esc(setup_id)}"


def tp_message(symbol: str, direction: str, n: int, r_multiple: float, setup_id: str) -> str:
    side = "BUY" if direction == "LONG" else "SELL"
    return f"✅ TP{n} u arrit — {esc(symbol)} {side} (+{r_multiple:.2f}R) · ID {esc(setup_id)}"


def sl_message(symbol: str, direction: str, setup_id: str) -> str:
    side = "BUY" if direction == "LONG" else "SELL"
    return f"❌ SL u godit — {esc(symbol)} {side} (−1R) · ID {esc(setup_id)}"


def timeout_message(symbol: str, direction: str, setup_id: str) -> str:
    side = "BUY" if direction == "LONG" else "SELL"
    return f"⏹️ Ndjekja mbaroi pa TP/SL — {esc(symbol)} {side} · ID {esc(setup_id)}"


DATA_DOWN = "📡 <b>TË DHËNAT RANË</b> — cTrader: {reason}\nMonitorimi është në pauzë: asnjë HYR pa të dhëna."
AUTH_EXPIRED = (
    "🔑 <b>TOKENI I CTRADER SKADOI</b>\n"
    "1) Hap cTrader Web (IC Markets) → Settings → Remote MCP\n"
    "2) Kopjo konfigurimin\n"
    "3) Dërgoje këtu: /ctrader KONFIGURIMI"
)
RESTORED = "✅ Të dhënat u rikthyen. Periudha e humbur u kontrollua: {summary}."
TRADING_PROFILE = (
    "⚠️ Tokeni i cTrader ka leje tregtimi. Kodi s'i përdor kurrë; "
    "për siguri përdor profilin vetëm-të-dhëna nëse ofrohet."
)
NO_VOLUME = "⚠️ S'ka Volume në Railway: lidhja me Gemini dhe setup-et humbin në çdo deploy. Shto Volume te /data."
GEMINI_LINKED = "🔗 Gemini u lidh me validatorin."
LOGIN_ATTACK = "🚨 Shumë tentativa të gabuara hyrjeje. Faqja u bllokua 1 orë."
MARKET_CLOSED_NOTICE = "ℹ️ Tregu duket i mbyllur (festë?)."

HELP_TEXT = (
    "/status – gjendja e sistemit\n"
    "/active – setup-et aktive\n"
    "/cancel ID – anulo një setup\n"
    "/pause · /resume – ndal/rifillo mesazhet HYR\n"
    "/stats 7 ose /stats 30 – statistika\n"
    "/selftest – kontrollo lidhjet\n"
    "/ctrader KONFIGURIMI – rinovo tokenin e cTrader\n"
    "/ctrader reset – kthehu te variablat e Railway\n"
    "/revoke_all – shkëput Gemini\n"
    "/rules – pragjet aktive"
)


def daily_report(data: dict[str, Any]) -> str:
    return (
        f"📊 <b>RAPORTI {esc(data['date'])}</b>\n"
        f"Setup: {data['n']} · Refuzuar {data['rej']} · Anuluar {data['inv']} · "
        f"Skaduar {data['exp']} · Humbur {data['mis']}\n"
        f"HYR: {data['ent']} → TP1+ {data['win']} · SL {data['loss']} · pa rezultat {data['open']}\n"
        f"Filtri: {data['saves']} humbje të shmangura · {data['missed_wins']} fitore të humbura\n"
        f"Limit i verbër (krahasim): {data['blind_w']} fitore / {data['blind_l']} humbje"
    )


# ------------------------------------------------------------------------ client
SendFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class TelegramError(Exception):
    message: str
    retry_after: float | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class TelegramClient:
    """Minimal Bot API client. `call` is injectable so tests use a fake API."""

    def __init__(self, token: str, call: SendFn | None = None) -> None:
        self.token = token
        self._call = call or self._http_call

    async def _http_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx2

        url = f"{API_BASE}/bot{self.token}/{method}"
        timeout = httpx2.Timeout(connect=10.0, read=35.0, write=10.0, pool=10.0)
        async with httpx2.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
            data = response.json()
        if not data.get("ok"):
            retry_after = (data.get("parameters") or {}).get("retry_after")
            raise TelegramError(str(data.get("description") or "telegram error"), retry_after)
        return data.get("result") or {}

    async def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for part in split_message(text):
            result = await self._call(
                "sendMessage",
                {"chat_id": chat_id, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True},
            )
        return result

    async def delete_message(self, chat_id: str, message_id: int) -> None:
        try:
            await self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError as exc:
            logger.info("deleteMessage failed: %s", exc)

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        result = await self._call(
            "getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]}
        )
        return result if isinstance(result, list) else []


class OutboxSender:
    """Drains the outbox at-least-once, honouring 429 `retry_after` (failure mode T3)."""

    def __init__(self, store, client: TelegramClient, chat_id: str, sleep=asyncio.sleep) -> None:
        self.store = store
        self.client = client
        self.chat_id = chat_id
        self._sleep = sleep

    async def drain_once(self) -> int:
        sent = 0
        for row in self.store.pending_messages():
            chat_id = row["chat_id"] or self.chat_id
            if not chat_id:
                return sent
            try:
                await self.client.send_message(chat_id, row["text"])
            except TelegramError as exc:
                self.store.mark_failed(row["id"], exc.message)
                if exc.retry_after:
                    await self._sleep(min(float(exc.retry_after), 60.0))
                return sent
            except Exception as exc:  # network trouble: retry on the next pass
                self.store.mark_failed(row["id"], repr(exc))
                return sent
            self.store.mark_sent(row["id"])
            sent += 1
        return sent
