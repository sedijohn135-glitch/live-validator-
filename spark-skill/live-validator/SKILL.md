---
name: live-validator
description: Live IC Markets cTrader market data plus a universal live setup validator for XAUUSD and BTCUSD. Use this whenever the user sends a symbol (xauusd, btcusd, gold, btc) or asks for an analysis, a setup, a status check or a cancellation. Screenshots are never needed: all prices, times, levels and candles come from the connected live-validator app, and the validator — not you — decides the moment the owner enters.
---

# live-validator handoff

The connected app **live-validator** provides live IC Markets cTrader data and a universal setup
validator. It replaces any screenshot input: never ask the user for a chart image.

You analyse with whatever method you already use. The validator then watches the market and sends
the owner **HYR TANI** (enter now) or **LIMIT** (a recomputed pullback entry) on Telegram. You never
tell the user to enter.

Your own method — ICT, supply/demand, breakouts, anything — stays exactly as it is. This skill
replaces only two things: where the data comes from, and what happens after your setup is printed.

## Trigger

When the user sends a symbol (XAUUSD, BTCUSD, gold, btc — any case) or asks for an analysis, run
this flow end to end automatically: call the tools, decide, and submit, all in one turn. Never ask
the user a question, never ask for permission and never wait for a confirmation before calling a
tool. Map gold → XAUUSD, btc/bitcoin → BTCUSD.

## A. Data (never from memory)

1. Call `market_snapshot` with `symbol`. Use ONLY prices and times from it. All times are New York
   time. Candles are `[time, open, high, low, close]`, where time is the candle open.
2. Use the precomputed `levels` (Asian range, London range, NY midnight open, 6 AM open, PDH/PDL,
   PWH/PWL, lookback extremes), `atr`, `swings` and `fvgs` instead of estimating them.
3. If more history is needed, call `market_candles` (at most 3 calls).
4. If `data.usable` is false, tell the user the feed is down and stop. Do not submit.

## B. Analysis end

Your analysis must end with four numbers and nothing more:

- the **entry** (a single price) or the **entry zone** (`entry_low` + `entry_high`),
- the **stop loss**,
- the **targets** you want (optional — the validator uses 1R/2R/3R when you omit them).

## C. Submit — always

Call `setup_submit` with:

| Parameter | Required | Meaning |
|---|---|---|
| `symbol` | yes | `XAUUSD` or `BTCUSD` |
| `stop_loss` | yes | where the idea is wrong |
| `entry` | yes* | a single entry price (*or the zone below) |
| `entry_low` / `entry_high` | yes* | the entry zone |
| `direction` | no | inferred from the stop when omitted |
| `tp1` / `tp2` / `tp3` | no | your targets |
| `label` | no | the name of your model |
| `note` | no | two short sentences |
| `client_ref` | no | your own reference |

**The submission is never rejected.** There are no conditions to satisfy: no session, no kill zone,
no bias, no checklist, no premium/discount. Submit every setup you produce.

If a payload cannot be read at all, the answer is `{"status":"unusable"}` — that means numbers were
missing, not that the idea was refused. Send the entry and stop and it will register.

## D. After submitting

Tell the user, in one short paragraph: the setup is registered and the validator is watching it live.
The owner will get the decision on Telegram. Do not tell the user to enter, and do not repeat the
levels as an instruction.

Use `setup_status` when the user asks what is happening, and `setup_cancel` with the id when they
ask to drop a setup.

## E. Never

- Never ask for a screenshot.
- Never use a price or a time that did not come from `market_snapshot` or `market_candles`.
- Never say "enter now" yourself — that message belongs to the validator.
- Never submit the same idea twice; check `setup_status` first.
