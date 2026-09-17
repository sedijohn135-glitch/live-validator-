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
| `OWNER_PASSWORD` | fjalëkalim i gjatë (≥ 12 shenja) — do ta shkruash vetëm një herë te Gemini |
| `CTRADER_MCP_CONFIG` | shiko hapin 6 |
| `TELEGRAM_CHAT_ID` | shiko hapin 5 |
Opsionale: `VALIDATOR_PROFILE` = `STRICT` (parazgjedhje) ose `BALANCED`.
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
2. **Settings → Connected Apps → Custom apps for Spark → Add**.
3. Emri: `live-validator` · URL: `https://<domeni-yt>.up.railway.app/mcp` (me `/mcp` në fund, pa `/` pas tij).
4. **Next** → hapet faqja e validatorit → shkruaj `OWNER_PASSWORD` → **Lejo**. Boti njofton 🔗.
5. Nëse Gemini kërkon "client ID / secret" (Advanced): vendos te Railway `OAUTH_STATIC_CLIENT_ID`,
   `OAUTH_STATIC_CLIENT_SECRET` (dy tekste të rastësishme që i zgjedh ti) dhe `OAUTH_STATIC_REDIRECT_URIS`
   (adresa e ridrejtimit që tregon Gemini), prit redeploy, pastaj fut të njëjtat vlera te Gemini.

## 9) Skill-i v11 në Gemini Spark
Hap skill-in tënd v11 dhe ngjit në fund tekstin e plotë nga `docs/GEMINI_V11_ADDENDUM.md`. Ruaj.

## 10) Përdorimi i përditshëm
- Te Gemini shkruan: `xauusd` ose `btcusd` (për siguri: `@live-validator xauusd`).
- Gemini analizon; kur dërgon setup-in mund të të kërkojë **Confirm** — shtype (rregull i Google).
- Telegram: 🎯 = po monitorohet · 🟢 HYR TANI = hyr · ⛔/⌛/⚠️ = mos hyr · ❌ = u refuzua.
- Te 🟢: mos hyr nëse çmimi ka kaluar kufirin "Mos hyr nëse…" ose koha "vlen deri" ka kaluar.
- Lot, rrezik, hyrje apo jo: vendim yti. Sistemi nuk tregton kurrë.

## Probleme të shpeshta
| Shenja | Zgjidhja |
|---|---|
| Deploy "healthcheck failed" | kontrollo që Healthcheck Path = `/health`; shiko Deploy Logs |
| Gemini s'lidhet | URL duhet të mbarojë me `/mcp`; domeni i gjeneruar; `OWNER_PASSWORD` i vendosur |
| Gemini u shkëput pas deploy | mungon Volume te `/data` |
| 🔑 token skadoi | cTrader Web → Remote MCP → `/ctrader …` |
| S'vjen asnjë mesazh | `/start` te boti; `TELEGRAM_CHAT_ID` i saktë; `/selftest` |
| Çmimi te mesazhi ndryshon nga aplikacioni yt | po tregton te broker/platformë tjetër; përdor IC Markets |
