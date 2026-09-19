"""Albanian Telegram templates, the outbox sender and the owner command poller.

`parse_mode=HTML`, so every dynamic value is escaped (failure mode T1) and long messages are split
on line boundaries (T2).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
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
DIRECTION_WORD = {"LONG": "BLERJE", "SHORT": "SHITJE"}


def _head(emoji: str, title: str, symbol: str, direction: str) -> str:
    return f"{emoji} <b>{title}</b> — {esc(DIRECTION_WORD.get(direction, direction))} {esc(symbol)}"


def _targets_line(targets: list[float], rr: list[float], decimals: int) -> str:
    if not targets:
        return "Objektivat: -"
    parts = []
    for i, target in enumerate(targets):
        multiple = f" ({rr[i]:.1f}R)" if i < len(rr) else ""
        parts.append(f"TP{i + 1} {num(target, decimals)}{multiple}")
    return " · ".join(parts)


def _evidence_lines(data: dict[str, Any]) -> list[str]:
    """What confirmed, how strong it is, and which of the three actually appeared.

    The strength is the headline: the owner reads 2/3 and knows two independent confirmations landed
    on the same zone, whichever two they happened to be.
    """
    signals = data.get("signals") or []
    if not signals:
        return []
    core = data.get("core") or []
    strength = data.get("strength", len(core))
    lines = []
    if strength:
        lines.append(f"Konfirmimi: <b>{strength}/3</b> — {esc(data.get('strength_text', ''))}")
        lines.append("  " + " + ".join(esc(code) for code in core))
    other = [code for code, _detail in signals if code not in core]
    if other:
        lines.append("Mbështetje: " + " + ".join(esc(code) for code in other))
    lines += [f"  • {esc(detail)}" for _code, detail in signals]
    return lines


def _notes_lines(notes: list[str]) -> list[str]:
    return [f"ℹ️ {esc(note)}" for note in (notes or [])[:4]]


def _race_warning(data: dict[str, Any], decimals: int) -> list[str]:
    """TP1 nearer than the zone means the move can finish without you, and the setup then cancels."""
    distance, to_tp1 = data.get("distance"), data.get("tp1_distance")
    if distance is None or to_tp1 is None or to_tp1 >= distance:
        return []
    return [
        f"⚠️ TP1 është më afër se zona ({num(to_tp1, decimals)} kundrejt {num(distance, decimals)}) — "
        "lëvizja mund të mbarojë pa ty dhe setupi anulohet."
    ]


def registered_message(data: dict[str, Any], decimals: int) -> str:
    """Every accepted setup is acknowledged in full: nothing is ever silently refused."""
    lines = [
        _head("📝", "SETUP I REGJISTRUAR", data["symbol"], data["direction"]),
        f"Zona: {num(data['zone_low'], decimals)} – {num(data['zone_high'], decimals)}",
        f"SL: {num(data['stop'], decimals)} · R: {num(data['risk'], decimals)}",
        _targets_line(data.get("targets", []), data.get("rr", []), decimals),
        f"Çmimi tani: {num(data.get('price'), decimals)} · largësia nga zona: {num(data.get('distance'), decimals)}",
        "Po monitorohet. Vendimi vjen kur zona të preket.",
    ]
    lines += _race_warning(data, decimals)
    lines += _notes_lines(data.get("notes", []))
    lines.append(f"ID: {esc(data['setup_id'])}")
    return "\n".join(lines)


def approach_message(data: dict[str, Any], decimals: int) -> str:
    return "\n".join(
        [
            _head("👀", "ÇMIMI PO AFROHET", data["symbol"], data["direction"]),
            f"Çmimi: {num(data.get('price'), decimals)} · zona: {num(data['zone_low'], decimals)}"
            f" – {num(data['zone_high'], decimals)}",
            f"Largësia: {num(data.get('distance'), decimals)}",
            f"ID: {esc(data['setup_id'])}",
        ]
    )


def touch_message(data: dict[str, Any], decimals: int) -> str:
    return "\n".join(
        [
            _head("🎯", "ZONA U PREK", data["symbol"], data["direction"]),
            f"Çmimi: {num(data.get('price'), decimals)} · spread: {num(data.get('spread'), decimals)}",
            "Po mbledh evidencë live. Asnjë hyrje pa konfirmim.",
            f"ID: {esc(data['setup_id'])}",
        ]
    )


def evidence_message(data: dict[str, Any]) -> str:
    """Progress at the zone: what has confirmed so far and what is still holding it back."""
    lines = [
        _head("🔎", "EVIDENCA PO NDËRTOHET", data["symbol"], data["direction"]),
    ]
    lines += _evidence_lines(data) or ["Evidenca: ende asnjë sinjal"]
    for hold in data.get("holds", []):
        lines.append(f"⏸️ {esc(hold)}")
    lines.append(f"Nevojiten {esc(data.get('score_min', 3))} pikë dhe një sinjal kryesor.")
    lines.append(f"ID: {esc(data['setup_id'])}")
    return "\n".join(lines)


def enter_message(data: dict[str, Any], decimals: int) -> str:
    lines = [
        _head("✅", "HYR TANI", data["symbol"], data["direction"]),
        f"Hyrje: {num(data['entry'], decimals)} (treg)",
        f"SL: {num(data['stop'], decimals)} · R: {num(data['risk'], decimals)}",
        _targets_line(data.get("targets", []), data.get("rr", []), decimals),
        f"🛡️ Siguro fitimet te {num(data['secure_at'], decimals)} ({esc(data.get('secure_why', ''))})",
    ]
    lines += _evidence_lines(data)
    lines.append(
        f"Zona u prek {esc(data.get('touch_ny', '-'))} NY · {esc(data.get('bars', 0))} qirinj M1 "
        f"· spread {num(data.get('spread'), decimals)}"
    )
    lines += _notes_lines(data.get("notes", []))
    lines.append(f"ID: {esc(data['setup_id'])}")
    return "\n".join(lines)


def limit_message(data: dict[str, Any], decimals: int) -> str:
    lines = [
        _head("⏳", "LIMIT — MOS E NDIQ", data["symbol"], data["direction"]),
        (
            "Konfirmimi erdhi te gjysma e shtrenjtë e zonës — hyrja vendoset më poshtë."
            if data.get("reason") == "premium"
            else f"Konfirmimi erdhi, por çmimi iku {data.get('advance_r', 0):.2f}R nga zona."
        ),
        f"Urdhër limit: {num(data['entry'], decimals)} ({esc(data.get('entry_why', ''))})",
        f"SL: {num(data['stop'], decimals)} · R: {num(data['risk'], decimals)}",
        _targets_line(data.get("targets", []), data.get("rr", []), decimals),
        f"🛡️ Siguro fitimet te {num(data['secure_at'], decimals)} ({esc(data.get('secure_why', ''))})",
    ]
    lines += _evidence_lines(data)
    lines += _notes_lines(data.get("notes", []))
    lines.append(f"ID: {esc(data['setup_id'])}")
    return "\n".join(lines)


def filled_message(data: dict[str, Any], decimals: int) -> str:
    return "\n".join(
        [
            _head("📥", "LIMIT U MBUSH", data["symbol"], data["direction"]),
            f"Hyrje: {num(data['entry'], decimals)} · SL: {num(data['stop'], decimals)}",
            f"🛡️ Siguro fitimet te {num(data['secure_at'], decimals)} ({esc(data.get('secure_why', ''))})",
            f"ID: {esc(data['setup_id'])}",
        ]
    )


def secure_message(data: dict[str, Any], decimals: int) -> str:
    return "\n".join(
        [
            _head("🛡️", "SIGURO FITIMET", data["symbol"], data["direction"]),
            f"Çmimi arriti {num(data['secure_at'], decimals)} ({esc(data.get('secure_why', ''))})"
            f" = {data.get('secure_r', 0):.2f}R",
            "Mbyll një pjesë dhe vendos SL-në te hyrja. Këtu çmimi kthehet më shpesh.",
            f"ID: {esc(data['setup_id'])}",
        ]
    )


def breakeven_message(data: dict[str, Any], decimals: int) -> str:
    return "\n".join(
        [
            _head("🔁", "SL NË HYRJE (BE)", data["symbol"], data["direction"]),
            f"Çmimi kaloi 1R ({num(data.get('price'), decimals)}). Tregtia nuk mund të humbasë më.",
            f"ID: {esc(data['setup_id'])}",
        ]
    )


def reversal_message(data: dict[str, Any], decimals: int) -> str:
    return "\n".join(
        [
            _head("⚠️", "SHENJA KTHIMI", data["symbol"], data["direction"]),
            f"Struktura mikro u thye kundër teje te {num(data.get('price'), decimals)} para TP1.",
            "Mbro fitimin: mbyll pjesë ose ngushto SL-në.",
            f"ID: {esc(data['setup_id'])}",
        ]
    )


def tp_message(data: dict[str, Any], decimals: int) -> str:
    n = data.get("n", 1)
    emoji = "🏁" if n >= 3 else "🎯"
    trail = "Trego SL-në te niveli i sigurimit." if n == 1 else "Trego SL-në te TP-ja e mëparshme."
    return "\n".join(
        [
            _head(emoji, f"TP{n} U ARRIT", data["symbol"], data["direction"]),
            f"Çmimi: {num(data.get('price'), decimals)} · {data.get('r_multiple', 0):.2f}R",
            trail,
            f"ID: {esc(data['setup_id'])}",
        ]
    )


def sl_message(data: dict[str, Any], decimals: int) -> str:
    return "\n".join(
        [
            _head("🛑", "SL U PREK", data["symbol"], data["direction"]),
            f"Çmimi: {num(data.get('price'), decimals)}. Tregtia mbaroi.",
            f"ID: {esc(data['setup_id'])}",
        ]
    )


def cancel_message(data: dict[str, Any], decimals: int) -> str:
    """One of the only two cancellations there are (docs/VALIDATOR.md §1.3)."""
    if data.get("reason") == "TP1_FIRST":
        why = "TP1 u prek para se çmimi të hynte në zonë — lëvizja shkoi pa ty."
    else:
        why = "SL u prek para se çmimi të hynte në zonë — ideja u thye para hyrjes."
    lines = [
        _head("❌", "SETUPI U ANULUA", data["symbol"], data["direction"]),
        why,
        f"Çmimi: {num(data.get('price'), decimals)}",
    ]
    if data.get("zone_crossed"):
        lines.append(
            "ℹ️ Zona u prek në të njëjtën periudhë, pa asnjë konfirmim mes tyre — "
            "prandaj asnjë hyrje: setupi u filtrua."
        )
    lines.append(f"ID: {esc(data['setup_id'])}")
    return "\n".join(lines)


def zone_failed_message(data: dict[str, Any], decimals: int) -> str:
    """Not a cancellation: the zone broke, so the evidence starts over if price comes back."""
    return "\n".join(
        [
            _head("⚠️", "ZONA PO THYHET", data["symbol"], data["direction"]),
            f"Çmimi mbylli përtej zonës te {num(data.get('price'), decimals)}.",
            "Evidenca u rivendos. Setupi mbetet gjallë — nëse çmimi kthehet, konfirmimi fillon nga e para.",
            f"ID: {esc(data['setup_id'])}",
        ]
    )


def manual_cancel_message(setup_id: str) -> str:
    return f"🚫 Setupi {esc(setup_id)} u anulua me dorë."


def unusable_message(reason: str) -> str:
    return f"⚠️ Setupi s'u lexua dot: {esc(reason)}. Dërgo së paku hyrjen dhe SL-në."


DATA_DOWN = "📡 <b>TË DHËNAT RANË</b> — cTrader: {reason}\nMonitorimi është në pauzë: asnjë HYR pa të dhëna."
AUTH_EXPIRED = (
    "🔑 <b>TOKENI I CTRADER SKADOI</b>\n"
    "1) Hap cTrader Web (IC Markets) → Settings → Remote MCP\n"
    "2) Kopjo konfigurimin\n"
    "3) Dërgoje këtu: /ctrader KONFIGURIMI"
)
RESTORED = "✅ Të dhënat u rikthyen. Periudha e humbur u kontrollua: {summary}."
TRADING_PROFILE = (
    "⚠️ Tokeni i cTrader ka profil tregtimi. Validatori përdor vetëm mjete read-only, "
    "por përdor një token vetëm-lexim nëse mundesh."
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
    "/selftest – kontrollo lidhjet, botin dërgues dhe bisedën\n"
    "/id – cila është kjo bisedë (punon edhe nga një chat tjetër)\n"
    "/ctrader KONFIGURIMI – rinovo tokenin e cTrader\n"
    "/ctrader reset – kthehu te variablat e Railway\n"
    "/revoke_all – shkëput Gemini\n"
    "/rules – si vendos validatori"
)


def daily_report(data: dict[str, Any]) -> str:
    return (
        f"📊 <b>RAPORTI {esc(data['date'])}</b>\n"
        f"Setup: {data['n']} · Hyrje: {data['ent']} · Anuluar para hyrjes: {data['cancel']}\n"
        f"TP1+ {data['win']} · SL {data['loss']} · në pritje {data['open']}"
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

    async def send_message(self, chat_id: str, text: str, parse_mode: str | None = "HTML") -> dict[str, Any]:
        result: dict[str, Any] = {}
        for part in split_message(text):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            result = await self._call("sendMessage", payload)
        return result

    async def delete_message(self, chat_id: str, message_id: int) -> None:
        try:
            await self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError as exc:
            logger.info("deleteMessage failed: %s", exc)

    async def get_me(self) -> dict[str, Any]:
        """Which bot this token actually is. A rotated token sends to a different conversation."""
        return await self._call("getMe", {})

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        """Which conversation the messages land in. `ok` from sendMessage does not mean "you saw it"."""
        return await self._call("getChat", {"chat_id": chat_id})

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        result = await self._call(
            "getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]}
        )
        return result if isinstance(result, list) else []


MAX_SEND_ATTEMPTS = 5
TAG_PATTERN = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """The same message without markup, for the retry after Telegram refuses to parse it."""
    return html.unescape(TAG_PATTERN.sub("", text))


class OutboxSender:
    """Drains the outbox at-least-once, honouring 429 `retry_after` (failure mode T3).

    One message must never block the ones behind it. A message the API refuses (malformed markup,
    an entity it cannot parse) is retried once as plain text and then dropped after
    `MAX_SEND_ATTEMPTS`; the queue keeps moving either way. Before this, a single rejected message
    stopped every later alert for good — including ENTER and the cancellations.
    """

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
                if exc.retry_after:  # rate limited: the whole queue waits, in order
                    self.store.mark_failed(row["id"], exc.message)
                    await self._sleep(min(float(exc.retry_after), 60.0))
                    return sent
                if await self._send_plain(chat_id, row):
                    sent += 1
                    continue
                self._failed(row, exc.message)
                continue  # the API refused this message, not the connection: keep going
            except Exception as exc:  # network trouble: the next pass retries everything
                self._failed(row, repr(exc))
                return sent
            self.store.mark_sent(row["id"])
            sent += 1
        return sent

    async def _send_plain(self, chat_id: str, row) -> bool:
        try:
            await self.client.send_message(chat_id, strip_html(row["text"]), parse_mode=None)
        except Exception:  # noqa: BLE001 - the plain retry is a bonus, never a new failure mode
            return False
        self.store.mark_sent(row["id"])
        logger.warning("outbox message %s was sent without markup", row["dedupe_key"])
        return True

    def _failed(self, row, error: str) -> None:
        self.store.mark_failed(row["id"], error)
        if row["attempts"] + 1 >= MAX_SEND_ATTEMPTS:
            # Marked dropped, never sent: a message nobody received must not be countable as one
            # that arrived, or "I got no alert" has no trace anywhere.
            self.store.mark_dropped(row["id"], error)
            logger.error(
                "dropping outbox message %s after %s attempts: %s", row["dedupe_key"], MAX_SEND_ATTEMPTS, error
            )
