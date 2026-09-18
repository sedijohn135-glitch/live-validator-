# Sending a setup from any prompt

The validator is strategy-agnostic. Whatever method produced the idea — ICT, supply/demand, a
breakout plan, a hand-drawn level — it is registered and watched. There is no rejection.

## A. The tools

| Tool | Use |
|---|---|
| `market_snapshot` | live prices, session levels, ATR, swings, FVGs, candles — the only source of prices and times |
| `market_candles` | more candles of one timeframe when the snapshot is not enough |
| `validator_rules` | what counts as evidence and how the stop is recomputed |
| `setup_submit` | register the setup for live validation |
| `setup_status` | the state of one setup or of all of them |
| `setup_cancel` | stop watching one setup |

## B. Prices come from the snapshot, never from memory

Call `market_snapshot` first and take every number from it. The snapshot is bid data; the validator
compares your levels against the same feed.

## C. What `setup_submit` needs

Required:

| Parameter | Meaning |
|---|---|
| `symbol` | `XAUUSD` or `BTCUSD` |
| `stop_loss` | where the idea is wrong |
| `entry` **or** `entry_low` + `entry_high` | a single price or a zone |

Optional, and nothing else is ever needed:

| Parameter | Meaning |
|---|---|
| `direction` | `LONG` / `SHORT`; inferred from the stop when omitted |
| `tp1`, `tp2`, `tp3` | targets; when omitted the validator uses 1R / 2R / 3R |
| `label` | free name of your model, for the owner's reference |
| `note` | two short sentences of reasoning |
| `client_ref` | your own reference |

Nothing about time, session, kill zone, bias, premium/discount, liquidity or a checklist is asked
for, because none of it changes the validator's answer.

## D. What happens next

1. The setup is registered and the owner gets a card on Telegram.
2. When price reaches the zone, the validator collects live evidence (see `validator_rules`).
3. It answers **ENTER NOW** at the live price, or **LIMIT** with a recomputed pullback entry when
   price has already run more than 0.35R from the zone.
4. It recomputes the stop from the confirmation structure, keeps your targets, and names the level
   where the owner should secure profit.
5. The setup is cancelled only if the stop or TP1 is reached before the entry was ever touched.

## E. Repairs instead of refusals

A contradictory payload is repaired and the repair is reported in `notes`:

- a stop on the wrong side of the zone is pushed past the far edge,
- a direction that contradicts the geometry is corrected,
- targets on the wrong side are dropped,
- an inverted zone is swapped.

So a submission never has to be retried with "better" numbers.

## F. Rules for you

- Do not resubmit the same idea repeatedly; one setup per idea. Use `setup_status` to check it.
- Do not ask the owner to enter — the validator does that, on Telegram, when the evidence is there.
- Do not invent prices or times. If `market_snapshot` reports `usable: false`, say so and stop.
