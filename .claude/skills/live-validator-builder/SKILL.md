---
name: live-validator-builder
description: Complete build spec for the live-validator project - one Railway-hosted Python service that is (1) an OAuth-protected remote MCP server for Gemini Spark exposing read-only IC Markets cTrader market data plus setup-validation tools, (2) a 24/7 live monitor that validates or invalidates ICT v11 trade setups with deterministic rules, and (3) a Telegram notifier that sends ENTER NOW / INVALIDATED messages. Use this skill whenever working in the live-validator repo - building from scratch, continuing, fixing a bug, changing a validation rule, the cTrader adapter, MCP tools, OAuth for Gemini, Telegram messages or the Railway deploy - even if the user only says continue, fix, deploy or add a rule.
---

# live-validator — build skill

## Mission (read this first, it decides every trade-off)

The owner analyses XAUUSD / BTCUSD with an ICT "v11" prompt inside **Gemini Spark**. Gemini pulls live
data from this service, produces a setup and submits it here. This service then watches the live market and
decides — with deterministic, testable rules — whether the setup **validates** (Telegram: `HYR TANI — BUY @price, SL, TP`)
or **invalidates / expires / is missed** (Telegram: `MOS HYR`). The owner then opens the trade manually on his phone.

Success is measured by only two things:
1. When the service says ENTER, the setup tends to succeed.
2. When the service blocks a setup, that setup would have failed (a "save").

Everything else is secondary. Previously the owner placed blind limit orders from screenshots; this service
replaces "blind limit" with "live confirmation".

## Non-negotiable constraints

- **Never trade.** The code must be physically unable to place, modify or close orders. The cTrader Remote MCP
  exposes trading tools — only an allowlist of read-only tools may ever be called (see `references/ctrader-remote-mcp.md`).
- **No money logic.** No lot size, balance, equity, risk %, position sizing. Not in code, not in messages.
- **One source of truth for prices.** Gemini analyses exactly the data the monitor watches (same adapter, same decoding).
- **No false ENTER.** On any doubt (stale data, gap, restart, spread spike, price ran away) the answer is NOT enter.
  A missed trade is acceptable; a wrong ENTER is not.
- **The owner does not code and has no computer.** Only a phone. Never ask him technical questions. Decide, document
  the decision, move on. The only things he provides are secrets (tokens, password) set in Railway or Telegram.
- **Telegram texts are Albanian** (templates in `references/telegram-messages-sq.md`). Code, comments, tests: English.
- **Ponytail mindset**: simplest correct solution, stdlib first, no speculative abstractions, but never skip a
  safety rule or a test listed in this skill.

## References — read progressively (token budget matters)

| When | Read |
|---|---|
| Phase 0 | `references/claude-md.md` |
| Phase 1 (before planning) | `references/architecture.md`, `references/failure-modes.md` |
| Rules / engine work | `references/validation-rules.md` (the core; follow it exactly) |
| cTrader adapter | `references/ctrader-remote-mcp.md` + the official Spotware docs it points to |
| MCP tools + OAuth | `references/mcp-oauth.md` |
| Telegram | `references/telegram-messages-sq.md` |
| Docs for the owner | `references/deploy-railway-sq.md`, `references/gemini-v11-addendum.md` |

Do not load all references at once. Use grep to find sections.

## Phase 0 — bootstrap (do this before anything else)

1. If this skill arrived as `live-validator-builder.zip` in the repo root: extract it so that
   `.claude/skills/live-validator-builder/SKILL.md` exists (`unzip` or `python -m zipfile -e`), delete the zip,
   commit `chore: add live-validator-builder skill`.
2. Create `CLAUDE.md` in the repo root with the exact content of `references/claude-md.md`.
3. `git add CLAUDE.md && git commit -m "docs: add CLAUDE.md project rules" && git push` immediately.
4. Detect the repo/branch from `git remote -v` and `git branch`; never rename the repo (its name may end with a hyphen).

## Phase 1 — understand and plan (cheap, short)

- Use `agent-skills:spec` to write `docs/SPEC.md` as a SHORT index (max ~80 lines): goals, links to the skill
  references, and a "Decisions & deviations" log. Do not copy the rules into it.
- Use `agent-skills:plan` to break work into the build order below with acceptance criteria.
- Use `agent-skills:source-driven-development` before touching any external API: verify the installed MCP Python SDK
  (`mcp==2.2.0`, verified facts in `references/mcp-oauth.md`) by reading its source in site-packages, and read the
  Spotware cTrader Remote MCP docs listed in `references/ctrader-remote-mcp.md`.
- Record every deviation from this skill (with reason) in `docs/SPEC.md`.

## Phase 2 — build in this order, each step gated by green tests

Use `agent-skills:build` / `agent-skills:incremental-implementation`; `agent-skills:test-driven-development` for
steps 2–4; `agent-skills:doubt-driven-development` for steps 4, 6, 8 (highest stakes). Commit after each green gate.

1. **Scaffold** — `pyproject.toml` + `uv.lock` (Python 3.12, exact pins), `app/` package per architecture, `config.py`,
   `store.py` (SQLite schema), `/health`, `Dockerfile`, `.gitignore`, `.env.example`, minimal GitHub Actions test
   workflow. Gate: `uv run pytest` passes; server starts with `PORT=8080` and `/health` returns 200 without any secrets.
2. **Time & primitives** — NY time, windows, macros, market hours, candles, closed-candle logic, ATR, swings, FVG,
   price decoding. Gate: unit tests incl. DST transition days.
3. **Intake gate** (rules G-01…G-20) as pure functions over an injected market context. Gate: pass+fail test per rule.
4. **Engine** — state machine, event ordering, invalidations, triggers, model overrides, score, outcomes, shadow
   tracking, replay after gaps, idempotent outbox. Gate: all golden scenarios in `validation-rules.md` §14 pass.
5. **Telegram** — outbox sender, command poller, templates, owner guard. Gate: tests with a fake Bot API.
6. **cTrader adapter** — MCP client, allowlist, discovery, symbol map, decoding/calibration, rate limits,
   auth-expiry handling, credential hot-swap. Gate: tests against a fake cTrader MCP server that ALSO exposes trading
   tools (they must be unreachable).
7. **MCP tools** — 6 tools, flat schemas, JSON text results. Gate: schema-lint test + in-process client e2e
   (snapshot → submit → simulated feed → ENTER in outbox exactly once).
8. **OAuth for Gemini** — SQLite-backed provider, login page, DCR. Gate: full OAuth e2e test incl. restart persistence
   and public Host header acceptance.
9. **Extras that serve the mission** — news blackout (fail-open), `/stats`, daily report, `/selftest`. Gate: tests.

## Phase 3 — verification

- Full `uv run pytest -q` green; `uv run ruff check` clean.
- If Docker is available, `docker build .` and run `/health`. If not, reproduce the Dockerfile steps in a clean venv
  (`uv sync --frozen --no-dev`) and start the server.
- Run `agent-skills:security-and-hardening` over OAuth, login, Telegram guard, allowlist, secret redaction.
- Run `agent-skills:review` and `ponytail:ponytail-review`; apply only fixes that keep all tests green.
- Walk `references/failure-modes.md` top to bottom and tick every row (mitigation present + test or documented reason).

## Phase 4 — owner docs (Albanian, phone-friendly)

- `docs/SETUP_SQ.md` from `references/deploy-railway-sq.md` (keep steps exact; update env var names if you changed any).
- `docs/GEMINI_V11_ADDENDUM.md` from `references/gemini-v11-addendum.md`; a test must assert that every tool name in it
  and every name in the parameter column of its section D table exists in the registered MCP tools (snapshot field
  names like `fvgs`/`levels` are response fields, not parameters).
- `docs/RULES_SQ.md`: one page in Albanian explaining what makes a setup ENTER / MOS HYR (plain words, no code).
- `README.md`: 15 lines max, Albanian, links to the three docs.

## Phase 5 — ship

- All work must end on `main` (Railway deploys `main`). If you can push to `main`, do it. If the environment only
  lets you push a working branch, push it, open a PR to `main`, and tell the owner in Albanian exactly how to merge
  from the GitHub phone app (Pull requests → PR → Merge pull request → Confirm merge).
- Run `agent-skills:ship` checklist.
- Final message to the owner, in Albanian, max 25 lines: what was built, the exact next steps from
  `docs/SETUP_SQ.md` (Railway → variables → volume → domain → Telegram → cTrader config → Gemini link → `/selftest`),
  and the Railway variables list.

## Definition of done

- [ ] CLAUDE.md committed first; skill extracted under `.claude/skills/`.
- [ ] Every rule in `validation-rules.md` implemented with its exact default and a test (or a logged deviation).
- [ ] Golden scenarios pass; ENTER can never be sent twice for one setup (restart test).
- [ ] Trading tools unreachable (test); no money/lot logic anywhere (grep test for `create_order`, `close_position`,
      `amend_`, `cancel_order` outside the denylist constant).
- [ ] MCP tool schemas are flat (lint test); OAuth e2e passes; token survives restart.
- [ ] `/health` is 200 even with missing/expired cTrader credentials (deploy must never fail because of data).
- [ ] No `railway.json` / `railway.toml` (Railway config-as-code is deprecated for new services).
- [ ] Docs in Albanian present; addendum-vs-tools test passes; everything merged to `main`.
