# Instalimi hap pas hapi (vetëm me telefon)

Koha: ~30–40 minuta. Emrat e butonave mund të ndryshojnë pak; kërko fjalën më të afërt.

## Para se të fillosh
- Llogari Railway (hyr me GitHub). Plani **Hobby (~5 $/muaj)** rekomandohet: monitorimi duhet të punojë 24/7.
- Gemini: abonim AI Pro ose Ultra, llogari personale Google, gjuha e Gemini në anglisht, "Keep Activity" ndezur.
  Google i kërkon këto për "Custom apps for Spark" (aktualisht edhe mosha 18+ dhe SHBA).
- IC Markets cTrader Web me llogarinë ku do tregtosh (çmimet e mesazheve vijnë nga ky feed; ekzekuto te i njëjti broker).

## 1) Railway — shërbimi
1. railway.com → **New Project** → **Deploy from GitHub repo** → zgjidh repon e validatorit.
2. Railway gjen `Dockerfile` dhe fillon build-in. Mos krijo asnjë skedar konfigurimi.
3. Hap shërbimin → **Settings**:
   - **Networking → Generate Domain** → kopjo adresën, p.sh. `https://live-validator-xxxx.up.railway.app`
     dhe vendose te variabla `PUBLIC_BASE_URL`
   - **Healthcheck Path**: `/health`
   - Sigurohu që "Serverless / App Sleeping" është **OFF**.
   - Branch: `main`.

## 2) Volume (pa këtë humbet gjithçka në çdo deploy)
Në kanavacën e projektit: **+ Create / New → Volume** → lidhe me shërbimin → **Mount path: `/data`**.

## 3) Telegram bot
1. Telegram → **@BotFather** → `/newbot` → emër → kopjo **tokenin**.
2. Hap botin tënd → shtyp **Start**.
3. (Chat ID e merr në hapin 5.)

## 4) Variablat në Railway (shërbimi → **Variables** → New Variable)
| Emri | Vlera |
|---|---|
| `TELEGRAM_BOT_TOKEN` | tokeni nga BotFather |
| `CTRADER_MCP_CONFIG` | shiko hapin 6 |
| `TELEGRAM_CHAT_ID` | shiko hapin 5 |
| `MCP_AUTH` | `open` — Gemini lidhet direkt nga URL-ja, pa faqe fjalëkalimi |
| `OWNER_PASSWORD` | duhet vetëm nëse **nuk** e vendos `MCP_AUTH=open` |
Nuk ka më `VALIDATOR_PROFILE`: validatori universal ka një mënyrë të vetme pune.

**Për `MCP_AUTH`:**
- `open` — si një server MCP i thjeshtë: ngjit URL-në te Gemini dhe mbaron. Por kushdo që e di adresën
  e Railway-t mund të dërgojë një setup dhe të shkaktojë një mesazh 🟢 HYR TANI që Gemini nuk e ka dërguar.
- Pa e vendosur fare (parazgjedhja) — `/mcp` mbrohet: te Gemini shkruan `OWNER_PASSWORD` një herë.
  Mund ta ndërrosh kurdo duke shtuar ose hequr variablën; `/status` dhe `/selftest` tregojnë gjendjen.
Pas ndryshimit të variablave Railway bën redeploy vetë (ose shtyp **Deploy**).

## 5) Chat ID
Shkruaji botit `/start`. Nëse `TELEGRAM_CHAT_ID` s'është vendosur, boti të kthen numrin. Vendose te Railway si
`TELEGRAM_CHAT_ID`. Pas redeploy, `/start` duhet të thotë që je pronari.

## 6) Tokeni i cTrader (IC Markets)
1. Hap **cTrader Web** në browser, hyr me cTID, zgjidh llogarinë e duhur.
2. **Settings → Remote MCP** → kopjo **konfigurimin** (përmban adresën dhe tokenin). Nëse ofrohet profil vetëm
   të dhëna ("data"), zgjidh atë.
3. Ngjite te Railway si `CTRADER_MCP_CONFIG` — ose dërgoje direkt botit: `/ctrader KONFIGURIMI` (boti e fshin mesazhin).
4. Kur sesioni i cTrader Web skadon, boti të njofton 🔑 — përsërit hapat 1–3 me `/ctrader`.

## 7) Kontroll
Botit: `/selftest` → të gjitha rreshtat ✅. Nëse diçka është ❌, lexo rreshtin (tregon çfarë mungon).

## 8) Lidh Gemini
1. Hap **gemini.google.com** në browser (nëse s'shfaqet opsioni, zgjidh "Desktop site").
   **Browser-i ka rëndësi:** Opera dhe Chrome punojnë rrjedhshëm. Brave e ngrin Spark-un në mes
   të analizës (Shields e ndërpret rrjedhën e mendimeve) — ose përdor Opera/Chrome, ose fik
   Shields për `gemini.google.com`.
2. **Settings → Connected Apps → Custom apps for Spark → Add**.
3. **MCP Server URL**: `https://<domeni-yt>.up.railway.app/mcp` (me `/mcp` në fund, pa `/` pas tij).
4. **Client ID** dhe **Client secret** (te "Additional settings") **lëri bosh**.
5. **Next**:
   - me `MCP_AUTH=open` → lidhet menjëherë, s'ka faqe fjalëkalimi;
   - pa `MCP_AUTH` → hapet faqja e validatorit → shkruaj `OWNER_PASSWORD` → **Lejo**. Boti njofton 🔗.
6. Vetëm nëse Gemini këmbëngul për "client ID / secret" (dhe nuk je në `open`): vendos te Railway
   `OAUTH_STATIC_CLIENT_ID`, `OAUTH_STATIC_CLIENT_SECRET` (dy tekste të rastësishme që i zgjedh ti) dhe
   `OAUTH_STATIC_REDIRECT_URIS` (adresa e ridrejtimit që tregon Gemini), prit redeploy, pastaj fut të
   njëjtat vlera te Gemini.

## 9) Skill-i në Gemini Spark
Ngarko `spark-skill/live-validator/SKILL.md`, ose ngjit në skill-in tënd tekstin e plotë nga
`docs/GEMINI.md`. Strategjia jote mbetet e pandryshuar — ndryshon vetëm nga vijnë të dhënat dhe
kush vendos momentin e hyrjes.

## 10) Përdorimi i përditshëm
- Te Gemini shkruan: `xauusd` ose `btcusd` (për siguri: `@live-validator xauusd`).
- Gemini analizon dhe e dërgon setup-in vetë, pa të pyetur. Të gjitha mjetet i janë deklaruar si
  "vetëm lexim", ndaj Spark nuk kërkon **Allow**. Nëse Google e shfaq prapë një herë atë pyetje,
  vjen nga ana e tyre — shtype dhe vazhdon.
- Telegram: 🎯 = po monitorohet · 🟢 HYR TANI = hyr · ⛔/⌛/⚠️ = mos hyr · ❌ = u refuzua.
- Te 🟢: mos hyr nëse çmimi ka kaluar kufirin "Mos hyr nëse…" ose koha "vlen deri" ka kaluar.
- Lot, rrezik, hyrje apo jo: vendim yti. Sistemi nuk tregton kurrë.

## Probleme të shpeshta
| Shenja | Zgjidhja |
|---|---|
| Deploy "healthcheck failed" | kontrollo që Healthcheck Path = `/health`; shiko Deploy Logs |
| Gemini s'lidhet | URL duhet të mbarojë me `/mcp`; domeni i gjeneruar; ose `MCP_AUTH=open`, ose `OWNER_PASSWORD` i vendosur |
| Gemini u shkëput pas deploy | mungon Volume te `/data` |
| 🔑 token skadoi | cTrader Web → Remote MCP → `/ctrader …` |
| S'vjen asnjë mesazh | `/start` te boti; `TELEGRAM_CHAT_ID` i saktë; `/selftest` |
| Çmimi te mesazhi ndryshon nga aplikacioni yt | po tregton te broker/platformë tjetër; përdor IC Markets |
| Spark ngrin gjatë analizës, pa gabim | browser-i: Brave e bllokon rrjedhën; përdor Opera ose Chrome, ose fik Shields për `gemini.google.com` |
