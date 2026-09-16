# Telegram messages (Albanian) — `parse_mode=HTML`, escape every dynamic value

Placeholders in `{}`. Prices rounded to display decimals. Times are New York (`HH:MM NY`). `{side}` = BUY|SELL.
Lines in `[ ]` are included only when the value exists. Keep messages short: the owner reads them on a lock screen.

## ENTER (LONG shown; SHORT uses 🔴, SELL, bid, and "&lt;")

```
🟢 <b>HYR TANI — BUY {symbol}</b>
Çmimi: <b>@{entry}</b> (ask) · spread {spread}
SL: <b>{sl}</b>
TP1: {tp1}[ · TP2: {tp2}][ · TP3: {tp3}]
RR: 1:{rr} ({target_label})
⛔ Mos hyr nëse çmimi &gt; {chase_limit} · vlen deri {valid_until} NY
Model: {model} · LTF {ltf} · {window}
Konfirmime: {checks}
Score: {score}/7[ · ⚠️ PDA e paverifikuar][ · ⚠️ filtri i lajmeve jashtë funksionit][ · vonesë {delay}s]
Burimi: IC Markets cTrader · ID: {id} · {time} NY
```
`{checks}` example: `SSL ✓ · CISD ✓ · Displacement ✓ · PDA ✓ · Macro ✓`.

## Lifecycle

ARMED
```
🎯 <b>SETUP NË MONITORIM</b> — {side} {symbol}
Zona: {entry_low} – {entry_high} · SL {sl}
TP1 {tp1}[ · TP2 {tp2}][ · TP3 {tp3}] · RR plan 1:{rr_plan}
Model: {model} · LTF {ltf}
Pret: {waits_for}
Skadon: {expires} NY[ · Lajme: {news}]
ID: {id}
```
REJECTED
```
❌ <b>SETUP I REFUZUAR</b> — {side} {symbol}
• {reason_1}
• {reason_2}
Mos hyr. ID: {id}
```
INVALIDATED
```
⛔ <b>MOS HYR — SETUP I ANULUAR</b> — {side} {symbol}
Arsyeja: {reason}[ (gjatë ndërprerjes)]
ID: {id} · {time} NY
```
EXPIRED: `⌛ <b>SKADOI</b> — {side} {symbol}: nuk u konfirmua deri {time} NY. Mos hyr. ID: {id}`
MISSED: `⚠️ <b>KONFIRMIM I HUMBUR</b> — {side} {symbol}: {reason}. Mos e ndiq çmimin. ID: {id}`
REPLACED: `♻️ Setup {old_id} u zëvendësua nga {new_id}.`
CANCELLED: `🗑️ Setup {id} u anulua me kërkesë.`
EXIT: `🚪 <b>DIL NGA TREGU</b> — {side} {symbol}: {reason}. ID: {id}`
TP: `✅ TP{n} u arrit — {symbol} {side} (+{r}R) · ID {id}`
SL: `❌ SL u godit — {symbol} {side} (−1R) · ID {id}`
TIMEOUT: `⏹️ Ndjekja mbaroi pa TP/SL — {symbol} {side} · ID {id}`

## Reason texts (code → Albanian)

G-03 "Drejtimi nuk përputhet me bias-in HTF" · G-04 "Nivelet janë në rend të gabuar" · G-05 "Niveli i invalidimit është
jashtë vendit" · G-06 "Çmimi i analizës s'përputhet me çmimin live ({live})" · G-07 "Nivele shumë larg çmimit" ·
G-08 "Setup-i është tashmë i pavlefshëm" · G-09 "Lëvizja ka ndodhur (çmimi përtej TP1)" · G-10 "RR {rr} &lt; 1:{min}" ·
G-11 "SL shumë i ngushtë/i gjerë për volatilitetin" · G-12 "Checklist v11: {pos} pozitive / {neg} negative" ·
G-13 "PDA nuk u gjet në të dhëna" · G-14 "PDA ka dështuar tashmë (trup mbylli përtej)" · G-15 "Likuiditeti kundërt
s'është real / s'është marrë" · G-16 "Kushtet e modelit {model} s'plotësohen: {detail}" · G-17 "SHORT pranë ATH pa
konfirmim institucional" · G-18 "S'ka Kill Zone të vlefshme para skadimit" · L-01 "SL u prek para hyrjes" ·
L-02 "Mbyllje {tf} përtej invalidimit {level}" · L-03 "Trupi mbylli përtej {level_name} {level} ({tf})" ·
T-06 "spread i lartë ({spread})" · T-07 "çmimi iku përtej {chase_limit}" · replay "konfirmimi ndodhi gjatë ndërprerjes" ·
pause "pauzë aktive".

`{waits_for}` examples: "prekje të zonës → CISD në M5 + displacement · SSL duhet të merret" ·
"qiri refuzimi (fitil deri CE, trup jashtë zonës) në M5" · "çmimi ≤ {level} (mbyllja BISI)".

## System

DATA DOWN
```
📡 <b>TË DHËNAT RANË</b> — cTrader: {reason}
Monitorimi është në pauzë: asnjë HYR pa të dhëna.
```
AUTH EXPIRED
```
🔑 <b>TOKENI I CTRADER SKADOI</b>
1) Hap cTrader Web (IC Markets) → Settings → Remote MCP
2) Kopjo konfigurimin
3) Dërgoje këtu: /ctrader KONFIGURIMI
```
RESTORED: `✅ Të dhënat u rikthyen. Periudha e humbur u kontrollua: {summary}.`
TRADING PROFILE: `⚠️ Tokeni i cTrader ka leje tregtimi. Kodi s'i përdor kurrë; për siguri përdor profilin vetëm-të-dhëna nëse ofrohet.`
NO VOLUME: `⚠️ S'ka Volume në Railway: lidhja me Gemini dhe setup-et humbin në çdo deploy. Shto Volume te /data.`
GEMINI LINKED: `🔗 Gemini u lidh me validatorin.`
LOGIN ATTACK: `🚨 Shumë tentativa të gabuara hyrjeje. Faqja u bllokua 1 orë.`

DAILY REPORT (17:05 NY)
```
📊 <b>RAPORTI {date}</b>
Setup: {n} · Refuzuar {rej} · Anuluar {inv} · Skaduar {exp} · Humbur {mis}
HYR: {ent} → TP1+ {win} · SL {loss} · pa rezultat {open}
Filtri: {saves} humbje të shmangura · {missed_wins} fitore të humbura
Limit i verbër (krahasim): {blind_w} fitore / {blind_l} humbje
```

## Commands help (`/help`)
```
/status – gjendja e sistemit
/active – setup-et aktive
/cancel ID – anulo një setup
/pause · /resume – ndal/rifillo mesazhet HYR
/stats 7 ose /stats 30 – statistika
/selftest – kontrollo lidhjet
/ctrader KONFIGURIMI – rinovo tokenin e cTrader
/ctrader reset – kthehu te variablat e Railway
/revoke_all – shkëput Gemini
/rules – pragjet aktive
```
