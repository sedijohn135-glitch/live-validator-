# Validation rules (authoritative)

Contents: 0 Principles · 1 Primitives · 2 Setup input · 3 Intake gate · 4 Time · 5 State machine · 6 Invalidation ·
7 Triggers · 8 Model overrides · 9 Score · 10 After ENTER · 11 Shadow & stats · 12 Gaps & restarts ·
13 Defaults · 14 Golden tests

## 0. Principles

- Derived from the owner's ICT v11 prompt (sections cited as v11 §x). Where v11 is written for forex/indices
  (pips, handles), thresholds are ATR-normalised per symbol.
- **Confirmation over limit.** v11 often says "entry within the FVG". The validator never sends ENTER on a mere touch
  (except where v11 itself defines the confirmation: Turtle Soup Deferred rejection candle, Venom). It waits for a
  lower-timeframe change of state after the touch. This is the owner's explicit goal.
- Every rule is a pure function with an ID, returns `(passed: bool, code, reason_sq, data)`, and has tests.
- Intake evaluates ALL rules and returns every failure (not just the first).
- All thresholds live in `config.py` with the defaults of §13; both profiles are tested.

## 1. Primitives and conventions

- **Prices** are display floats after decoding (see ctrader reference). Candles are **bid** candles. Quotes have bid+ask.
- **Timeframes**: M1=60 s, M5=300, M15=900, M30=1800, H1=3600, H4=14400, D1=86400, W1=604800. Order M1<M5<…<W1.
- **Candle** `{t (open time, UTC epoch s), o, h, l, c}`. **Closed** iff `now >= t + tf + CLOSE_GRACE_S` AND it was fetched
  after that instant. Never evaluate a rule on a forming candle.
- `body_top=max(o,c)`, `body_bot=min(o,c)`, `body=body_top-body_bot`, `range=h-l`; bullish `c>o`, bearish `c<o`.
- **Body close beyond level X** (LONG side): `c < X`. (SHORT side: `c > X`.) Continuous CFD markets have no intraday gaps,
  so close is used as "body close".
- **ATR14(tf)**: mean of the last 14 true ranges of closed candles, `TR=max(h-l, |h-prev_c|, |l-prev_c|)`.
- **Strict swing high** at i: `h[i] > h[i-1] and h[i] > h[i+1]` (v11 §1.2); swing low mirrored. Needs i+1 closed.
- **FVG** with middle candle m (candles m-1, m, m+1):
  - bullish wick-FVG: `l[m+1] > h[m-1]` → zone `[h[m-1], l[m+1]]`; bullish body-FVG (v11 §5.1): `body_bot[m+1] > body_top[m-1]` → `[body_top[m-1], body_bot[m+1]]`.
  - bearish mirrored: wick `h[m+1] < l[m-1]` → `[h[m+1], l[m-1]]`; body `body_top[m+1] < body_bot[m-1]` → `[body_top[m+1], body_bot[m-1]]`.
  - `CE = (low+high)/2`. `formed_at` = open time of m.
- **tol(tf)** = `max(LEVEL_TOL_ATR × ATR14(tf), 2 × median_spread_60m, 3 × tick)`; tick = 10^-display_decimals.
- **median_spread_60m**: median of quote spreads sampled in the last 60 min; if < 30 samples use `MAX_SPREAD_ABS/3`.
- **respect_tf** = the lower of (`pda_timeframe`, M15). Body-respect rules run on respect_tf closes.
- **primary_tp** = `tp2` if given else `tp1`. **entry_ref** (planning) = CE of the entry zone.
- **LONG side words**: BUY, entry uses ask, exits use bid. SHORT: SELL, entry bid, exits ask (≈ bid candle + median spread).
- **NY time**: `zoneinfo.ZoneInfo("America/New_York")`; windows are half-open `[start, end)` and are checked against the
  **close time** of the deciding candle (or the quote time for quote-based decisions).
- **Efficiency ratio** ER over last 24 ltf closes: `|c_k - c_{k-24}| / Σ|c_i - c_{i-1}|`.
- **Session levels** (NY day = 00:00–24:00 NY): Asian range = M5 candles opened in [19:00 previous day, 00:00) (v11 §3.1);
  London range = M5 in [02:00, 05:00); NY midnight open = open of M5 at 00:00; 6 AM open = open of M5 at 06:00;
  PDH/PDL = previous closed D1; PWH/PWL = previous closed W1; lookback high/low = extremes of ≤250 closed D1 candles
  (ATH proxy, cached 6 h).

## 2. Setup input (`setup_submit`, flat parameters)

Required: `symbol` (XAUUSD|BTCUSD) · `direction` (LONG|SHORT) · `entry_model` (enum below) · `htf_timeframe` (D1|H4|H1) ·
`htf_bias` (BULLISH|BEARISH) · `ltf` (M1|M5|M15) · `entry_low` · `entry_high` · `stop_loss` · `tp1` ·
`invalidation_level` · `pda_type` (enum below) · `pda_timeframe` (M1|M5|M15|M30|H1|H4|D1) · `pda_low` · `pda_high` ·
`pda_formed_at_ny` ("YYYY-MM-DD HH:MM", NY, open time of the PDA candle / FVG middle candle, as printed by the snapshot) ·
`opposing_liquidity_level` · `opposing_liquidity_taken` (bool) · `price_at_analysis` · `checklist_positive` (int) ·
`checklist_negative` (int) · `confidence` (0–100) · `rationale` (≤ 600 chars).

Optional: `tp2` · `tp3` · `invalidation_timeframe` (M1|M5|M15|H1; default = ltf) · `pda_mean_threshold` ·
`range_high` · `range_low` (HTF dealing range for premium/discount) · `model_ref_level` (Venom BISI close; Model 2 6 AM
price) · `kill_zone` (text) · `macro` (text) · `valid_until_ny` · `client_ref`.

`entry_model` enum → v11: ICT_2022 (§5.1) · MARKET_ANCHOR (§5.2) · MODEL_2 (§5.3) · TURTLE_SOUP (§5.4) ·
TURTLE_SOUP_DEFERRED (§5.4.1) · SILVER_BULLET (§5.5) · OTE (§5.6/§3.6) · IOFED (§5.7) · LOW_RESISTANCE_RUN (§5.8) ·
SM_THREE_STAGE (§5.10) · VENOM (§5.11) · OR_FIRST_FVG (§3.7) · OR_PM_FIRST_FVG (§3.10) · LUNCH_MACRO_PM (§3.9).

`pda_type` enum: FVG · INVERSION_FVG · ORDER_BLOCK · BREAKER_BLOCK · MITIGATION_BLOCK · REJECTION_BLOCK · LIQUIDITY_VOID ·
SUSPENSION_BLOCK · BPR · OLD_HIGH_LOW · OTE_ZONE.

Normalisation: if `entry_low > entry_high` (or pda_low > pda_high) swap and add warning. Reject naive/unparseable times.

## 3. Intake gate (at `setup_submit`)

Result: `ARMED` (Telegram "SETUP NË MONITORIM"), `REJECTED` (Telegram "SETUP I REFUZUAR" with all reasons), or
`DUPLICATE` (returns the existing id, no message). Hard-level helper `hard_level` is defined in §6 L-03.

- **G-01 Schema** — all required fields, finite positive prices, enums valid, rationale length. Else REJECTED.
- **G-02 Data** — symbol resolved, quote age ≤ `QUOTE_MAX_AGE_S`×3, candles for ltf/respect_tf/pda_tf/H1/D1 available.
  Never arm blind: failure = REJECTED "të dhënat mungojnë".
- **G-03 Bias** — LONG⇔BULLISH, SHORT⇔BEARISH.
- **G-04 Ordering** — LONG: `stop_loss < entry_low < entry_high < tp1 < tp2 < tp3` (optional ones skipped).
  SHORT: `stop_loss > entry_high > entry_low > tp1 > tp2 > tp3`. Entry zone must intersect `[pda_low-tol, pda_high+tol]`.
- **G-05 Invalidation placement** — LONG: `stop_loss ≤ invalidation_level ≤ entry_high`; SHORT: `entry_low ≤ invalidation_level ≤ stop_loss`.
- **G-06 Price drift (hallucination guard)** — `|price_at_analysis − live_bid| ≤ max(PRICE_DRIFT_ATR_H1 × ATR14(H1), PRICE_DRIFT_PCT% × live_bid)`.
- **G-07 Level range** — every level within `LEVEL_RANGE_ATR_D1 × ATR14(D1)` of live bid.
- **G-08 Not already invalid** — LONG: `live_bid > stop_loss`, the last closed respect_tf candle `c ≥ hard_level`
  (wicks may be beyond it, closes may not), and the last closed invalidation_tf candle `c ≥ invalidation_level`. SHORT mirrored.
- **G-09 Not late** — LONG: `live_bid < tp1`; SHORT: `live_bid > tp1`.
- **G-10 Planned RR** — `|primary_tp − entry_ref| / |entry_ref − stop_loss| ≥ RR_MIN_PLAN` (v11 checklist "Minimum RR 1:2").
- **G-11 SL distance** — `risk = |entry_ref − stop_loss|`: `risk ≥ SL_MIN_ATR_LTF × ATR14(ltf)`, `risk ≥ SPREAD_SL_MULT × median_spread`,
  `risk ≤ SL_MAX_ATR_H1 × ATR14(H1)`.
- **G-12 v11 checklist** — `checklist_positive ≥ CHECKLIST_MIN_POS` and `checklist_negative ≤ CHECKLIST_MAX_NEG`
  (v11 §11: 7+ positive valid; 3+ negative PASS).
- **G-13 PDA exists** — scan pda_timeframe candles with open time in `formed_at ± 3 bars`:
  - FVG / LIQUIDITY_VOID / BPR: a wick- or body-FVG in trade direction whose zone matches: both edges within `2×tol` OR
    `overlap / min(width_declared, width_found) ≥ 0.6`. For OR models (§8) the middle candle must also be displacement
    (`body ≥ DISP_BODY_ATR × ATR14` and `body/range ≥ DISP_BODY_RANGE`).
  - INVERSION_FVG / SUSPENSION_BLOCK: a matching FVG in the OPPOSITE direction, and a later close beyond its far edge
    in trade direction (the inversion, by a displacement candle — v11 rule 32).
  - ORDER_BLOCK / MITIGATION_BLOCK: a candle opposite to trade direction whose range or body matches (2×tol), followed
    within 5 candles by a close beyond its opposite extreme, with an FVG in trade direction within those candles (v11 §6.3).
  - OLD_HIGH_LOW: a strict swing (low for LONG, high for SHORT) within `2×tol` of the zone's far edge.
  - BREAKER_BLOCK / REJECTION_BLOCK / OTE_ZONE: not machine-verifiable → `UNVERIFIED` (allowed unless `STRICT_PDA_VERIFY`; score penalty P-3).
  - Result `VERIFIED | NOT_FOUND | UNVERIFIED`; NOT_FOUND ⇒ REJECTED "PDA nuk u gjet në të dhëna".
- **G-14 PDA not already failed** — from the candle after formation (after the inversion candle for inversion types) to
  now, no respect_tf close beyond `hard_level`.
- **G-15 Opposing liquidity** (v11 §5.8, rule 17):
  - placement LONG `stop_loss − tol ≤ level ≤ entry_high + tol` (SHORT mirrored);
  - reality: a strict swing (lows for LONG) on M5/M15/H1 within the last 5 days, or a session level (Asian, London,
    PDL/PDH, PWL/PWH, NY midnight open), within `2×tol(M15)` of the level;
  - claim: if `opposing_liquidity_taken = true`, data must show a trade through it within `LIQ_WINDOW_H`
    (LONG `min(low) ≤ level`). A false claim ⇒ REJECTED. If false, the sweep becomes a trigger requirement (T-01).
- **G-16 Model constraints** — §8 intake column (direction, required fields, formation windows, ATH context, SB distance).
- **G-17 ATH short filter** (v11 rule 2) — SHORT with `lookback_high − live_bid ≤ ATH_PROX_ATR_D1 × ATR14(D1)` ⇒ REJECTED
  unless `entry_model = SM_THREE_STAGE`.
- **G-18 Time feasibility & expiry** — `expires_at = min(valid_until_ny, now + lifetime(model), end of the last allowed
  window (§4) that starts before now + lifetime, XAUUSD Friday cutoff / daily 17:00 NY close)`. Lifetime =
  `MAX_SETUP_LIFETIME_H` (MODEL_2: 24 h). REJECTED if no allowed window overlaps `[now, expires_at)` or `expires_at < now + 5 min`.
- **G-19 Duplicate** — fingerprint `symbol|direction|entry_low|entry_high|stop_loss|tp1` (rounded to display decimals)
  within `DEDUP_WINDOW_MIN` ⇒ DUPLICATE (Gemini retries must not create twins).
- **G-20 Replace** — on ARMED, an existing ARMED/IN_ZONE setup on the same symbol becomes `REPLACED` (message). A TRIGGERED
  one stays; an opposite-direction trigger is blocked while it is unresolved (T-09).

Intake response `computed`: CE, entry_ref, rr_plan, ATR values, tol, pda_check, liquidity_check, expires_at_ny,
allowed windows, `waits_for_sq` (plain Albanian: what must happen before ENTER).

## 4. Time rules (NY)

| Window | Time | | Window | Time |
|---|---|---|---|---|
| LONDON_OR | 01:30–02:00 | | AM_SB | 10:00–11:00 |
| LONDON_KZ | 02:00–05:00 | | LONDON_CLOSE | 10:00–12:00 |
| LONDON_SB | 03:00–04:00 | | **LUNCH (block)** | 12:00–13:00 |
| NY_OR | 07:00–07:30 | | PM_OR | 13:30–14:00 |
| NY_KZ | 07:00–10:00 | | PM_SB | 14:00–15:00 |
| NY_2022 | 07:00–09:00 | | PM_SESSION | 13:30–16:00 |
| EQUITIES_OR | 09:30–10:00 (indices only → never for XAUUSD/BTCUSD) | | LAST_HOUR | 15:00–16:00 |

Macros (±10 min, v11 §3.3): 02:33, 04:03, 08:00, 09:00, 10:00, 11:00, 12:00, 13:20, 15:00, 15:15, 15:40, 15:50, 16:00.

Model → windows where ENTER may be sent (STRICT; BALANCED differences in brackets):
ICT_2022 NY_2022 · MARKET_ANCHOR / OTE / IOFED / LOW_RESISTANCE_RUN / VENOM: LONDON_KZ, NY_KZ, LONDON_CLOSE, PM_SESSION ·
TURTLE_SOUP(_DEFERRED): LONDON_KZ, NY_KZ · SM_THREE_STAGE: LONDON_KZ, NY_KZ, PM_SESSION ·
SILVER_BULLET: only the SB window that contains `formed_at` · MODEL_2: 06:00–10:00 on Tuesday [Tue–Thu] ·
OR_FIRST_FVG: formed in LONDON_OR → 02:00–05:00; formed in NY_OR → 07:30–10:00 · OR_PM_FIRST_FVG: formed in PM_OR →
14:00–16:00 · LUNCH_MACRO_PM: 14:00–15:00 [13:30–16:00].

Global blocks (checked at every trigger): LUNCH; market closed (XAUUSD Fri 17:00 → Sun 18:00 and daily 17:00–18:00);
XAUUSD Friday after `FRIDAY_CUTOFF_NY`; BTCUSD Saturday/Sunday unless `BTC_WEEKEND_ENTRIES`; news blackout
(USD high-impact events, `NEWS_BEFORE_MIN` before to `NEWS_AFTER_MIN` after; feed down ⇒ no blackout + warning line in
the ENTER message); global `/pause`; data PAUSED.

## 5. State machine

States: `ARMED → IN_ZONE → TRIGGERED` and terminal `INVALIDATED | EXPIRED | MISSED | CANCELLED | REPLACED` (plus
`REJECTED` at intake). After TRIGGERED the setup tracks outcome `TP1 → TP2 → TP3 | SL | TIMEOUT` and may emit one EXIT.
`PAUSED` is a flag (data outage), not a state.

Event pipeline per symbol: collect newly closed candles of all subscribed timeframes; sort by close time, then timeframe
ascending (M1 first); for each setup apply in this order:
1. L-01 SL touch (M1 events and quotes) → 2. L-02 invalidation close → 3. L-03 body respect → 4. L-04 expiry →
5. tap detection (ARMED→IN_ZONE) → 6. trigger evaluation on ltf close (IN_ZONE→TRIGGERED/MISSED/stay) →
7. post-trigger tracking (§10).

Subscriptions per active setup: M1 (always), ltf, respect_tf, invalidation_tf. Keep ≥ 1500 M1 and ≥ 300 bars of other
timeframes in memory; fetch 300 bars of each at arming.

**Tap** — LONG: M1 `l ≤ entry_high` or quote `bid ≤ entry_high`; SHORT: `h ≥ entry_low` or `bid ≥ entry_low`.
Setups already inside the zone at arming start IN_ZONE. `tap_extreme` = lowest low (LONG) / highest high (SHORT) since
the tap; `x` = index of the ltf candle holding it (moves when a new extreme prints).

Every transition: one DB transaction = state update + event row + outbox row (dedupe key `setup_id:event`).

## 6. Hard invalidation (before ENTER)

- **L-01 SL touch** — LONG `l ≤ stop_loss` or `bid ≤ stop_loss` ⇒ INVALIDATED "SL u prek para hyrjes". SHORT mirrored (bid).
- **L-02 Invalidation close** — invalidation_tf close beyond `invalidation_level` ⇒ INVALIDATED (v11 box "if price closes…").
- **L-03 Body respect** on respect_tf closes: close beyond `hard_level` ⇒ INVALIDATED. `hard_level` (LONG; SHORT mirrored):
  - FVG, LIQUIDITY_VOID, BPR, OTE_ZONE: CE if `CE_HARD_FVG` else pda_low (v11 §6.2; BALANCED: CE close → penalty P-2 "heavy").
  - INVERSION_FVG, SUSPENSION_BLOCK: CE always (v11 §3.7, rules 19 & 32).
  - ORDER_BLOCK, MITIGATION_BLOCK: `pda_mean_threshold` if given else CE (v11 §6.3).
  - BREAKER_BLOCK, REJECTION_BLOCK: pda_low. OLD_HIGH_LOW: `pda_low − tol`.
  - Model overrides: OR_FIRST_FVG, OR_PM_FIRST_FVG, LUNCH_MACRO_PM, MARKET_ANCHOR → CE; SILVER_BULLET → pda_low ("bodies remain within the FVG").
- **L-04 Expiry** — `now ≥ expires_at` ⇒ EXPIRED.

## 7. Trigger (default path, evaluated at each closed ltf candle k while IN_ZONE)

ENTER only if ALL pass; otherwise stay IN_ZONE, except where MISSED is stated.

- **T-01 Opposing liquidity taken** — LONG: `min(low)` over `[k_close − LIQ_WINDOW_H, k_close] ≤ opposing_liquidity_level`
  (SHORT: `max(high) ≥ level`). Use M5 candles for the window (fetch/keep ≥ 24–36 h of M5) plus M1 since arming.
- **T-02 CISD** (v11 §6.3 "OB = CISD"; §7.1) — LONG: from x walk back ≤ `CISD_LOOKBACK` bars to the nearest bearish candle j,
  extend back over consecutive bearish candles to `start`; `cisd = max(o[start..j])`. Pass if `k > x` and `c[k] > cisd`.
  If no bearish run is found: MSS fallback — last strict swing high s in `[x − MSS_LOOKBACK, x)`; pass if `c[k] > h[s]`.
  SHORT mirrored (bullish run, `min(open)`, swing low).
- **T-03 Displacement** — in `(x, k]`: a candle in trade direction with `body ≥ DISP_BODY_ATR × ATR14(ltf)` and
  `body/range ≥ DISP_BODY_RANGE`, OR a wick-FVG in trade direction whose m+1 ≤ k and m ≥ x.
- **T-04 Freshness** — `k − x ≤ CONFIRM_MAX_BARS`.
- **T-05 Time** — k close time inside the model's windows (§4) and no global block.
- **T-06 Spread** — `spread ≤ min(MAX_SPREAD_ABS, SPREAD_SPIKE_MULT × median_spread_60m)`. If it fails for 2 consecutive
  ltf closes while T-01..T-05 hold ⇒ MISSED "spread i lartë".
- **T-07 Price & RR at decision** — fresh quote (age ≤ `QUOTE_MAX_AGE_S`); `entry = ask` (LONG) / `bid` (SHORT).
  `chase_limit` (LONG) = `min(entry_high + CHASE_MAX_RISK_FRAC × (entry_high − stop_loss), (primary_tp + RR_MIN_TRIGGER × stop_loss)/(1 + RR_MIN_TRIGGER))`;
  SHORT mirrored with max. Pass if entry within chase_limit and `entry − stop_loss ≥ SPREAD_SL_MULT × spread`.
  Fail while T-01..T-05 hold ⇒ MISSED "çmimi iku — mos e ndiq" (v11 rule 21 "do not chase").
- **T-08 Data integrity** — not PAUSED; no missing ltf bar between x and k (refetch once; still missing ⇒ no decision this bar).
- **T-09 Conflict** — no unresolved TRIGGERED setup in the opposite direction on the same symbol.
- **T-10 Score** — §9 total ≥ `SCORE_MIN`.

On pass: state TRIGGERED, store entry, chase_limit, score breakdown; outbox ENTER message valid for `ENTER_VALID_MIN`.
If `/pause` is active ⇒ MISSED "pauzë".

## 8. Model overrides

| Model | Intake (G-16) | Trigger replaces T-02/T-03 with |
|---|---|---|
| ICT_2022 | pda_type in FVG family | default |
| MARKET_ANCHOR | LONG only; `lookback_high − bid ≤ ATH_MODEL_PROX_ATR × ATR14(D1)`; pda_type INVERSION_FVG | default |
| MODEL_2 | a Tuesday [Tue–Thu] 06:00–10:00 window before expiry | default + LONG `entry < six_am_open` (SHORT `>`), six_am_open computed by the service |
| TURTLE_SOUP | `range_high/low` required; LONG entry_ref below range 50% (SHORT above) | default (T-01 = the breach) |
| TURTLE_SOUP_DEFERRED | as TURTLE_SOUP; pda_type in INVERSION_FVG/ORDER_BLOCK/FVG | **rejection candle** on ltf close k: LONG `l[k] ≤ entry_high`, `l[k] ≥ CE − tol`, `body_bot[k] > entry_high`; plus a displacement candle (T-03 test) between the breach and k. Tap and trigger may be the same candle. |
| SILVER_BULLET | `formed_at` inside an SB window; `|primary_tp − entry_ref| ≥ max(SB_MIN_DOL[symbol], 1.5 × ATR14(M5))` | default, same SB window only |
| OTE | none extra | default |
| IOFED | pda_type in FVG family | **new FVG inside**: after the tap, a wick-FVG in trade direction whose zone overlaps the PDA zone; trigger at its m+1 close |
| LOW_RESISTANCE_RUN | none extra | default |
| SM_THREE_STAGE | SHORT only; ATH context as MARKET_ANCHOR | default |
| VENOM | LONG only; `model_ref_level` required; verify a bearish FVG (SIBI) followed within 8 candles by a bullish FVG (BISI) whose candle close ≈ model_ref_level (2×tol); `stop_loss <` lowest low between them | **price trigger**: quote `ask ≤ model_ref_level` inside windows (no CISD); T-01, T-05…T-09 still apply |
| OR_FIRST_FVG | `formed_at` in LONDON_OR or NY_OR (never EQUITIES_OR); liquidity taken BEFORE `formed_at`; displacement middle candle | default |
| OR_PM_FIRST_FVG | `formed_at` in PM_OR; same as OR | default |
| LUNCH_MACRO_PM | PM inversion arrays (v11 §3.10): the PDA may be an AM (07:00–12:00) opposite-direction FVG declared INVERSION_FVG; G-13's inversion-close requirement is waived for this model | default |

## 9. Score (T-10)

+1 each: **S-1** discount/premium (range given; LONG entry_ref ≤ range 50%, SHORT ≥) · **S-2** trigger inside a macro ·
**S-3** strongest displacement body in `(x,k]` ≥ 1.5 × ATR14(ltf) · **S-4** shallow tap (LONG tap_extreme ≥ CE) ·
**S-5** session sweep today before k (LONG low < Asian low, or < London low after 05:00; SHORT highs) · **S-6** immediate
rebalance: ≥ 2 same-direction FVGs from different timeframes overlap the entry zone (v11 rule 30) ·
**S-7** calm spread (≤ 1.5 × median).
−1 each: **P-1** chop ER < 0.25 (v11 §5.9) · **P-2** heavy (BALANCED only, CE body close on plain FVG) ·
**P-3** PDA UNVERIFIED · **P-4** first NY day after a USD bank holiday (feed; v11 rule 23).
Max 7. The ENTER message shows the breakdown.

## 10. After ENTER

- **LP-01 Exit** — first invalidation_tf close beyond `invalidation_level` ⇒ one EXIT message.
- **LP-02 Three arrays broken** (v11 rule 7) — at trigger collect same-direction ltf FVGs located between stop_loss and
  entry (incl. the entry PDA); each is broken by a close beyond its far edge; 3 broken ⇒ one EXIT message.
- **LP-03 Outcome** — LONG: TP hit when bid ≥ tp (M1 high or quote), SL when bid ≤ stop_loss; SHORT: TP when
  `ask ≤ tp`, SL when `ask ≥ stop_loss` (ask ≈ bid candle + median spread). Same M1 candle touches TP and SL ⇒ SL (conservative).
  Continue TP1→TP2→TP3 until SL (original) or horizon `OUTCOME_HORIZON_H` (MODEL_2: Thursday 10:00 NY; XAUUSD stops at Friday close) ⇒ TIMEOUT.
  Messages: TP1, TP2, TP3, SL (short lines). R multiple uses the actual entry.

## 11. Shadow tracking and stats (measures the mission)

- For every setup that passed G-01…G-07 but did not reach TRIGGERED (REJECTED, INVALIDATED, EXPIRED, MISSED, REPLACED)
  AND for every TRIGGERED setup, compute the **blind-limit outcome** the owner would have had before this system:
  fill at the first touch of the zone edge (LONG entry_high, SHORT entry_low) within 24 h of creation, then first of
  stop_loss / tp1 on M1 (same candle ⇒ LOSS). Values: `WIN | LOSS | NO_FILL`. Evaluate lazily once the horizon ends
  (chunked `get_trendbars` M1 calls).
- `/stats [7|30]` and the daily report: ENTER count with TP1+/SL/timeout; blocked count with shadow WIN/LOSS/NO_FILL;
  **saves** = blocked & shadow LOSS; **missed wins** = blocked & shadow WIN; confirmation vs blind-limit results for
  triggered setups.

## 12. Data gaps, restarts, late triggers

- Outage: no successful quote for 20 s during market hours ⇒ PAUSED (no triggers). Telegram alert after
  `DATA_OUTAGE_ALERT_S`, repeated at most hourly. Auth-type errors alert immediately with renewal instructions.
- Recovery or process start: for every non-terminal setup backfill all subscribed timeframes from `last_processed_at`
  and replay events chronologically with `replay=True`. In replay, invalidation/expiry apply normally (message notes
  "gjatë ndërprerjes"); a trigger becomes MISSED "konfirmimi ndodhi gjatë ndërprerjes", unless the trigger candle closed
  ≤ `LATE_TRIGGER_MAX_S` ago and T-05…T-10 pass NOW with a live quote ⇒ TRIGGERED with "(vonesë N s)".
- Market closed periods are not outages (no alerts). Quotes stale > 10 min inside expected hours without auth error ⇒
  one notice "tregu duket i mbyllur (festë?)".

## 13. Defaults

| Parameter | STRICT | BALANCED |
|---|---|---|
| RR_MIN_PLAN / RR_MIN_TRIGGER | 2.0 / 2.0 | 2.0 / 1.5 |
| CHECKLIST_MIN_POS / CHECKLIST_MAX_NEG | 7 / 2 | 6 / 2 |
| SCORE_MIN | 3 | 2 |
| CE_HARD_FVG | true | false |
| SL_MIN_ATR_LTF / SL_MAX_ATR_H1 | 0.5 / 3.0 | 0.4 / 4.0 |
| SPREAD_SL_MULT | 3 | 2 |
| CHASE_MAX_RISK_FRAC | 0.35 | 0.5 |
| CISD_LOOKBACK / MSS_LOOKBACK / CONFIRM_MAX_BARS | 10 / 20 / 12 | 10 / 20 / 20 |
| DISP_BODY_ATR / DISP_BODY_RANGE | 0.8 / 0.55 | 0.6 / 0.5 |
| LEVEL_TOL_ATR | 0.15 | 0.25 |
| PRICE_DRIFT_ATR_H1 / PRICE_DRIFT_PCT | 2.0 / 0.25 | 3.0 / 0.4 |
| LEVEL_RANGE_ATR_D1 | 6 | 8 |
| MAX_SETUP_LIFETIME_H | 8 | 12 |
| LIQ_WINDOW_H | 24 | 36 |
| SPREAD_SPIKE_MULT | 2.5 | 3.0 |
| NEWS_BEFORE_MIN / NEWS_AFTER_MIN | 15 / 15 | 10 / 10 |
| ATH_PROX_ATR_D1 / ATH_MODEL_PROX_ATR | 1.0 / 1.5 | 0.5 / 2.0 |
| STRICT_PDA_VERIFY | false | false |
| FRIDAY_CUTOFF_NY (XAUUSD) | 15:30 | 16:00 |
| ENTER_VALID_MIN / LATE_TRIGGER_MAX_S | 5 / 90 | 5 / 120 |
| OUTCOME_HORIZON_H | 24 | 24 |
| QUOTE_POLL_S / QUOTE_MAX_AGE_S / CLOSE_GRACE_S | 2 / 5 / 2 | same |
| DATA_OUTAGE_ALERT_S / DEDUP_WINDOW_MIN | 60 / 15 | same |

Per symbol: MAX_SPREAD_ABS XAUUSD 0.80, BTCUSD 60 · SB_MIN_DOL XAUUSD 10.0, BTCUSD 300 · display decimals 2 / 2 ·
PRICE_BANDS XAUUSD [1500, 14000], BTCUSD [25000, 240000] (each band narrower than 10× so decoding is unambiguous).

## 14. Golden tests (synthetic candle series; all must pass in both profiles unless noted)

1. Clean ICT_2022 LONG: SSL sweep → FVG → tap above CE → bearish run → CISD close with displacement at 08:10 NY → exactly one ENTER → TP1 message.
2. Fake-out: tap → M5 close below CE ⇒ INVALIDATED, no ENTER; later SL hit ⇒ shadow LOSS counted as a save (STRICT).
3. Runaway: valid CISD but ask beyond chase_limit ⇒ MISSED, no ENTER.
4. Time: LOW_RESISTANCE_RUN with all trigger conditions true at 12:20 NY ⇒ no ENTER (lunch). ICT_2022 whose CISD closes at 09:05 NY ⇒ no ENTER; the setup is EXPIRED at 09:00 (end of its last window).
5. Outage: feed down during the trigger bar, restored 5 min later ⇒ MISSED (not late ENTER); restored 40 s later with conditions still valid ⇒ TRIGGERED with delay note.
6. Restart after the ENTER transaction but before sending ⇒ message sent exactly once.
7. SHORT near lookback high with ICT_2022 ⇒ REJECTED (G-17); same with SM_THREE_STAGE passes G-17.
8. Hallucinated levels (price_at_analysis 2650 while bid 5650) ⇒ REJECTED G-06/G-07.
9. Declared FVG absent in data ⇒ REJECTED G-13.
10. `opposing_liquidity_taken=true` but never traded through ⇒ REJECTED G-15.
11. Turtle Soup Deferred: wick to CE with body above zone ⇒ ENTER on that candle; wick below CE ⇒ no trigger.
12. Venom: ask reaches BISI close inside NY_KZ ⇒ ENTER; SHORT Venom ⇒ REJECTED.
13. Silver Bullet FVG formed 04:05 NY ⇒ REJECTED; formed 03:20 with DOL too close ⇒ REJECTED.
14. Spread spike 3× median for 2 bars ⇒ MISSED.
15. Duplicate submit within 15 min ⇒ DUPLICATE, one setup row, one ARMED message.
16. DST: kill zones correct on 2026-03-09 and 2026-11-02 (days after US DST changes) and on a normal day.
17. After ENTER: three same-direction FVGs broken ⇒ one EXIT message; invalidation close ⇒ at most one EXIT total per reason.
