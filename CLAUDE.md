# Rregulla të detyrueshme për këtë projekt

Ti ke të instaluar këto plugins:
• agent-skills (Osmani)
• ponytail
• graphify
• ruflo

RREGULLA TË FORTA:
1. Gjithmonë përdor mentalitetin e ponytail (zgjidhja më e thjeshtë dhe minimale e mundshme). Mos e mbingarko kodin.
2. Kur fillon një detyrë të re, së pari përdor skill-et e agent-skills (plan → build → review).
3. Përdor graphify kur duhet të kuptosh strukturën e projektit.
4. Përdor ruflo kur ke nevojë për planifikim më kompleks.
5. Mos i harxhosh tokens duke bërë gjithçka manualisht. Gjithmonë prefero skills-et e pluginsave.
6. Unë nuk di kod. Prandaj ti duhet të zgjedhësh dhe të përdorësh skills-et automatikisht, pa ma kërkuar mua.

Çdo herë që fillon punë, vepro sipas këtyre rregullave.

## Harta e skill-eve (zgjidhi vetë, automatikisht)
- Burimi i kërkesave: `docs/VALIDATOR.md` (kontrata e validatorit universal, v2).
  `.claude/skills/live-validator-builder/` mbetet vetëm për infrastrukturën (Railway, cTrader, OAuth, Telegram);
  rregullat e v11 aty janë histori — validatori nuk refuzon më asnjë setup.
- Detyrë e re: `agent-skills:spec` (i shkurtër) → `agent-skills:plan` → `agent-skills:build`.
- Rregullat e validimit / engine: `agent-skills:test-driven-development` + `agent-skills:doubt-driven-development`.
- MCP SDK, cTrader, Railway, Telegram API: `agent-skills:source-driven-development` (lexo burimin, mos hamendëso).
- OAuth, login, allowlist cTrader, sekretet: `agent-skills:security-and-hardening`.
- Test që dështon / gabim: `agent-skills:debugging-and-error-recovery`.
- Para commit-it të madh: `agent-skills:review` + `ponytail:ponytail-review`.
- Para deploy: `agent-skills:ship`.
- Sesion i ri mbi kod ekzistues: `graphify:graphify` (jo në repo bosh).
- Vendim arkitekturor: `ruflo-adr:adr-create` ose `ruflo-goals:goal-plan` vetëm nëse mjetet e ruflo janë aktive.

## Mbrojtje nga harxhimi i tokens
- Nëse një plugin kërkon server/mjet që s'është aktiv (p.sh. ruflo MCP), anashkaloje menjëherë; mos e rregullo.
- Mos lexo skedarë të mëdhenj të plotë: përdor grep dhe intervale rreshtash.
- Mos shto librari pa nevojë. Mos krijo `railway.json`/`railway.toml`.
- Mos më pyet për zgjedhje teknike: vendos, shkruaje te `docs/SPEC.md` (Decisions), vazhdo.

## Rregulla të projektit (asnjëherë mos i shkel)
- Kodi NUK dërgon kurrë urdhra tregtimi. Vetëm mjetet read-only të cTrader (allowlist).
- Pa lot, balancë, rrezik %. Hyrja në treg është vendim i pronarit.
- Në dyshim (të dhëna të vjetra, ndërprerje, spread i lartë, çmimi iku) → MOS HYR.
- Mesazhet e Telegram në shqip. Kodi dhe testet në anglisht.
- Puna përfundon gjithmonë në `main` (Railway ndërton `main`).
