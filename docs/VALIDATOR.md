# Universal live validator (v2)

The validator is a **live evidence engine**, not a strategy engine. It accepts any setup produced by
any prompt or method and answers one question, continuously, from live data:

> Is there enough evidence, right now, at this price, to enter — and if not now, at what price?

It never judges the idea. It has no opinion about the time of day, the session, the kill zone, the
model, premium/discount, the bias, liquidity, news or the checklist. Those belong to whoever wrote
the setup. The validator owns the moment of entry and the protection of the profit.

## 1. Contract

### 1.1 Input (`setup_submit`)

Required: `symbol`, `stop_loss`, and either `entry` or `entry_low` + `entry_high`.
Optional: `direction`, `tp1`, `tp2`, `tp3`, `zone_low`/`zone_high`, `label`, `note`, `client_ref`.

Everything else the older build demanded is gone.

### 1.2 There are no rejections

A submission is **always** accepted and always produces a live setup. Missing or contradictory
fields are repaired, never refused:

| Situation | What the validator does |
|---|---|
| `direction` missing | inferred from the geometry: SL below entry → LONG, SL above → SHORT |
| `direction` contradicts the geometry | geometry wins; the mismatch is reported as a note |
| only `entry` given | a zone is built: `entry ± max(2 × median spread, 0.15 × ATR(M5))` |
| `entry_low` > `entry_high` | swapped |
| no TPs | TP1/TP2/TP3 derived at 1R / 2R / 3R |
| a TP sits on the wrong side of entry | dropped, and the remaining targets renumbered |
| `stop_loss` on the wrong side of the zone | the zone edge decides the side; the stop is pushed one buffer beyond the far edge |
| price already past the entry | the setup is registered and immediately evaluated for a LIMIT re-entry |

Only a payload with no usable number at all (no stop, no entry) cannot become a setup. That is a
parse failure, reported as such, not a verdict on the trade.

### 1.3 There are exactly three cancellations

1. **Stop touched before the entry was touched** → `CANCELLED_SL_FIRST`.
2. **TP1 touched before the entry was touched** → `CANCELLED_TP1_FIRST` (the move happened without
   you; chasing it is a new idea, not this one).
3. **A candle closed beyond the head of the quasimodo** → `CANCELLED_ZONE_BROKEN`. Strategy step 6:
   *nëse një qiri mbyllet jashtë zonës së QM, setup-i është i anuluar*. The head is the far edge of
   the entry zone, and the stop sits a buffer beyond it — so cancelling on the close costs less than
   waiting for the stop. If the owner is already in, the message says to close manually.

This was two until the strategy was written out in full; the third is the owner's own invalidation
rule, and it fires earlier than the stop rather than in addition to it.

Nothing else cancels a setup. No expiry, no session end, no news, no "too old". A setup that is
never touched simply waits.

## 2. What counts as evidence

Evidence is collected **only after price touches the zone**, from closed M1 candles (M5 corroborates
where it is closed). Each signal is independent and cheap to compute; the verdict is a balance, not
a checklist.

Every signal must clear a **materiality floor**: a multiple of ATR(M1), with the live spread as the
hard floor underneath. Without one, ordinary chop inside a zone confirms itself — a one-tick dip
below the edge is not a liquidity sweep, a doji is not a rejection, and a two-tick swing is not
structure. (That was a real defect, found in production on 2026-09-18.)

| Signal | Weight | Definition (LONG; mirror for SHORT) |
|---|---|---|
| `RECLAIM` | support | Price took out the liquidity level — the zone low, or the running low if price had already traded under it — by ≥ 0.25 × ATR, and within 3 M1 candles closed back past that level by ≥ 0.20 × ATR. A sweep and reclaim inside one candle counts only if that candle is a proper rejection candle. |
| `REJECTION` | support | A candle of range ≥ 0.60 × ATR wicks into the zone and closes ≥ 0.20 × ATR beyond its edge, with a wick ≥ 55 % of the range and the close in the top third, **or** an engulfing candle with body ≥ 0.60 × ATR closing past the previous candle's extreme. |
| `ZONE_BREAK` | **confirmation** | The nearest opposing demand (short) or supply (long), broken: a close ≥ 0.15 × ATR beyond the most recent opposing swing — and that swing must itself stand ≥ 0.50 × ATR above the low that followed it. Read on **M1, M5 and M15**, because the nearest zone can live on any of them; when more than one has broken, the level nearest the entry zone is the one reported. |
| `AO_DIV` | **confirmation** | Awesome Oscillator divergence on the M1 series (SMA5 − SMA34 of the median price): price made a new extreme past the previous swing by ≥ 0.15 × ATR and the oscillator did not follow. Read on the whole series, not only since the touch — the divergence usually forms before price arrives. |
| `QUASIMODO` | **confirmation** | A left shoulder, a head that takes the liquidity beyond it by ≥ 0.15 × ATR, a close through the neckline between them, and price back at the shoulder. The return must come **after** the break: a dip to the shoulder while the neckline still holds is the pattern forming, not the pattern. |
| `MOMENTUM` | support | An M1 candle in the trade direction with body ≥ 0.9 × ATR **and** its close in the top third of its range: an impulse, not a wide candle that gave it back. |
| `ABSORPTION` | support | Three consecutive M1 closes holding the zone's better half, while at least one of them was pressed into the worse half. Drifting through the zone is not a defence. |

**Verdict: enter when `ZONE_BREAK` is present. Nothing substitutes for it.**

That is strategy step 3, and it is the whole gate: before any entry price must break the nearest
opposing zone — a demand area or recent support for a sell, a supply area or recent resistance for
a buy. Without it the verdict holds on `BREAK`. *Instant Entry është e ndaluar.*

The other two are read for **strength**, never for entry:

```
ZONE_BREAK                        1/3  konfirmim
ZONE_BREAK + one other            2/3  konfirmim i fortë
ZONE_BREAK + AO_DIV + QUASIMODO   3/3  konfirmim shumë i fortë
```

`AO_DIV` is strategy step 2, and step 2 says it plainly: *divergjenca NUK është sinjal hyrjeje, por
paralajmërim për t'u përgatitur për setupin QM*. It raises the strength of an entry the break has
already made; it never makes one. `QUASIMODO` forming live says the pattern the analysis drew is
still the pattern the market is trading.

The supporting signals — reclaim, rejection, momentum, absorption — are reported with the entry and
never produce one.

Worked examples:

- `ZONE_BREAK` → 1/3 ✅ the nearest M5 demand gave way
- `ZONE_BREAK` + `QUASIMODO` → 2/3 ✅
- `ZONE_BREAK` + `AO_DIV` + `QUASIMODO` → 3/3 ✅
- `AO_DIV` + `RECLAIM` + `MOMENTUM` → ❌ held on `BREAK`: the warning came, the break did not
- `MOMENTUM` + `ABSORPTION` → ❌ the zone reacted, but nothing broke

### 2.1 Holds — reasons to wait, never to cancel

A hold delays the ENTER message and is reported with its reason. The setup stays alive.

| Hold | Condition | Why |
|---|---|---|
| `BREAK` | `ZONE_BREAK` is absent — the nearest opposing demand or supply still holds | strategy step 3: *nëse nuk ka thyerje zone, anuloje çdo setup. Instant Entry është e ndaluar* |
| `SPREAD` | spread > max(3 × median spread of the last hour, 0.5 × ATR(M1)) | entering into a spread spike pays the spike |
| `KNIFE` | the last 3 M1 candles travelled > 2.5 × ATR(M1) against the trade | a falling knife is not a rejection |
| `DATA` | quote older than 30 s, synthetic price, or a gap in the M1 series | no evidence without data |
| `FRESH` | no closed M1 candle since the touch | the first tick into a zone is not evidence |
| `REACTION` | price has come less than 0.50 × ATR off the extreme made since the touch | price sitting on the low it just made has defended nothing, whatever the patterns say |

## 3. Not early, not late

`advance` = how far price has already travelled from the entry edge of the zone toward TP1,
expressed in R (`R = |entry − stop|` of the original idea).

| Where price is | Verdict |
|---|---|
| inside the zone, in its better half | **ENTER NOW** at the live price |
| inside the zone, in its expensive half | **LIMIT** in the better half — a LONG is never filled above the middle of its own demand zone, because a worse fill is a wider stop and a smaller R on the same idea |
| outside the zone, ≤ 0.35 R beyond | **ENTER NOW** at the live price |
| outside the zone, > 0.35 R beyond | **LIMIT** — the move left without you; a pullback entry is computed instead |

The LIMIT price is the first of these that lies between the live price and the zone:

1. the 50 % level of the confirmation candle (equilibrium of the candle that produced the evidence),
2. the M1 fair value gap created by the confirmation move (its consequent encroachment),
3. the zone edge itself.

## 4. Recalculation of entry, stop and targets

Performed at the moment of the verdict, from live data, and reported in full.

- `struct_extreme` = the extreme of the confirmation sequence (lowest low since the touch for a
  LONG, highest high for a SHORT).
- `buffer` = `max(1.5 × ATR(M1), 2 × median spread, 2 × tick)`.
  Structure gives the level; ATR gives the room. A normal wick must not take the stop.
- `stop_struct` = `struct_extreme − buffer` (LONG).
- **New stop** = `stop_struct`, clamped so that it is
  - never wider than the original stop (the idea's invalidation is the outer bound), and
  - never tighter than `entry_new − 0.35 × R_original` (a stop two ticks away is not a stop).
- **Targets**: the original TP1/TP2/TP3 are kept — they are the idea. Their R multiples are
  recomputed from the new entry and stop and reported. A target already passed is marked as such.

## 5. Securing the profit

The point the owner insists on: price can turn before TP1. The validator computes a **secure
level** at entry time and watches it.

`secure_at` = the nearest genuine obstacle between the entry and TP1:

1. the nearest opposing swing high/low on M5 or M15,
2. the nearest session level (PDH, PDL, Asian/London high/low, NY midnight open, PWH, PWL),
3. the nearest round number (XAUUSD: every 5; BTCUSD: every 250),
4. and in any case not further than 1.0 R.

Whichever of those is closest to the entry, but at least 0.5 R away. If nothing qualifies,
`secure_at = entry + 1.0 R`.

Post-entry alerts, in order:

| Event | Message |
|---|---|
| price reaches `secure_at` | 🛡️ SIGURO FITIMET — close part, stop to break-even |
| price reaches 1.0 R | 🔁 SL → BE |
| TP1 hit | 🎯 TP1 — stop to the secure level |
| opposite micro-shift before TP1 | ⚠️ reversal forming — protect what you have |
| TP2 / TP3 hit, stop hit | 🏁 / 🛑 |

## 5.1 A zone that breaks is not a zone that holds

Two consecutive M1 closes beyond the far edge by ≥ 0.50 × ATR mean the zone is being broken. That is
**not** a cancellation — only the stop and TP1 cancel — but the evidence gathered so far describes a
defence that failed, so it is discarded and the watch returns to waiting. If price comes back, the
confirmation starts from zero.

## 6. States and messages

```
REGISTERED → APPROACH → ZONE_TOUCH → (HOLD…) → ENTERED | LIMIT_SET → MANAGED → CLOSED
                     ↘ CANCELLED_SL_FIRST | CANCELLED_TP1_FIRST
```

Every transition sends a Telegram message in Albanian, with the numbers that produced it. Progress
messages that are not transitions (distance to the zone, a new evidence signal) are throttled to one
per 5 minutes per setup, so the detail never becomes noise.

## 7. Symbol constants

Nothing is hardcoded in price terms except the round-number grid; everything else is derived from
live ATR, live spread and the instrument's tick.

| | XAUUSD | BTCUSD |
|---|---|---|
| round-number grid | 5.0 | 250.0 |
| decimals | 2 | 2 |

## 8. Deliberately out of scope

- **Volume confirmation.** The REST proxy returns bid candles; their volume is a tick count, not
  traded volume, and it is not comparable between sessions. A signal that cannot be trusted is worse
  than no signal.
- **Order placement.** The service is read-only and always will be.
- **Lot size, risk %, balance.** The owner decides the size; the validator decides the moment.

## Sources consulted for the confirmation and protection rules

- [Rejection Block Trading: The ICT Wick-Based Entry Explained](https://grandalgo.com/blog/rejection-block-trading)
- [Wick Rejection Patterns in Futures Trading Explained](https://justintrading.com/wick-rejection-patterns-futures/)
- [Break and Retest Strategy in Trading — FXOpen](https://fxopen.com/blog/en/how-can-you-use-a-break-and-retest-strategy-in-trading/)
- [Best Stop Loss Strategy for XAU/USD](https://www.dominionmarkets.com/best-stop-loss-strategy-xau-usd-gold/)
- [Liquidity Sweeps and Stop Hunts on XAUUSD — Vantage](https://www.vantagemarkets.com/en-za/academy/liquidity-sweeps-and-stop-hunts-on-xauusd/)
- [The Gold Wick Trap That Hunts Your Stop Loss](https://fxnx.com/en/blog/gold-price-action-mastering-xauusd-candlesticks-wick-trap)
- [The 1-Minute Bitcoin Scalping Strategy](https://tradelikemaster.com/blog/bitcoin-scalping-strategy)
- [Best Stop Loss for BTC Scalping](https://cryptotradetool.com/blog/best-stop-loss-for-btc-scalping/)
- [Average True Range (ATR) in Crypto — Mudrex](https://mudrex.com/learn/average-true-range-crypto/)
- [Multi Take-Profit Strategies: TP1/TP2/TP3 with Partial Closes](https://tradehookx.com/blog/multi-take-profit-strategies)
- [Partial Profits: The Break-Even Math](https://www.edgeflo.com/blog/partial-profits-trading)
- [Dynamic reward/risk ratio and risk management — Tradeciety](https://tradeciety.com/how-to-manage-risk-as-a-trader-become-a-professional-risk-manager)
