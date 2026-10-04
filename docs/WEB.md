# Web console

The owner's browser view of the desk: `https://<elastic-ip>` (code: `src/shortbot/web/`).

## What it shows and does
The page has two views, switched in the header (or the menu on a phone) and remembered per browser.

**Basic** is the owner's glance: is the desk safe, how is the money, what is on the book.
- **Overview**: a banner when something needs the owner (kill switch not ACTIVE, guardian down, a short without a
  stop, a governor pause, old code); equity with day/week/open P&L and the realized-P&L curve; one card per open
  short (a price ladder from targets to stop, the thesis and plan one tap away, *Move stop* and *Close*); resting
  entries with *Cancel*; the risk room (daily loss cap, risk at the stops, exposure, drawdown); the urgent
  unacknowledged alerts; the last closed trades. It refreshes every 60 s. `trading_state` runs the guardian
  watchdog on each refresh, so a dead guardian trips HALTED here as it does in the SessionStart hook.
- **Alerts**: grouped by kind (40 repeats of one guardian error read as one line), filters for new and urgent.
- **Trades**: totals, each setup's numbers, every closed trade with its thesis, exit reason and lesson.
- The menu: theme, auto-refresh, **Halt trading**, log out.

**Advanced** adds what the agent and a researcher need:
- on the Overview: the risk parameters (overrides beside the `.env` baseline and bounds), the governor in full, the
  brief Claude gets at session start, and the raw JSON behind every card;
- on Trades: the rulebook and the reports (24 h audit, research report, calibration, setups, the full journal);
- **Commands**: every desk command, as a form generated from its `commands.py` declaration, the same one its MCP
  tool and `shortbot` subcommand come from. It runs as actor `web:<command>` under the command's weight class;
- **Docs**: `CLAUDE.md`, `README.md`, `docs/**` and the skills, read-only.

A button on a card (*Close*, *Move stop*, *Cancel*, *Halt trading*) opens that same command form in a sheet.
- A **write** shows what it will send and asks once more, then goes through the gateway as an intent, like an MCP
  call. It is journalled before it runs, and every veto, budget and kill-switch check applies.
- On code older than the code on disk, writes are refused (#68).
- `shortbot audit --actor web` lists what was done here.

On a phone the tabs are a bottom bar and the sheet rises from the bottom. The code is plain files under
`web/static/` (no build step): `core.js` (API, formats), `viz.js` (meter, ladder, sparkline), `cards.js` and
`home.js` (Overview), `views.js` (Alerts, Trades), `console.js` (forms, reports, docs), `app.js` (shell).

Not here, on purpose: everything in `shortbot-admin` (clearing the kill switch, resetting the equity peak, the setup
ladder clears). These stay at a real terminal on the server, as `docs/RISK_GOVERNANCE.md` sets out. `.env` and
live mode stay human-only file edits.

## Security
- One owner password, stored as an scrypt hash in `data/web_auth.json` (mode 0600). Sessions live only in memory
  (12 h; a restart logs everyone out). One IP gets 5 failed logins per 15 min.
- The cookie is HttpOnly, Secure and SameSite=Strict. Every POST needs the `X-Shortbot` header, which a cross-site
  form can't send. The CSP is `'self'` only: no CDN, no inline script.
- The app listens on 127.0.0.1:8080. Caddy terminates TLS on 443 with a Let's Encrypt **IP certificate** (the 6-day
  `shortlived` profile, renewed by Caddy) and redirects 80 to 443. Port 80 must stay open for the ACME challenge.

## Operate
```bash
# on the server, once: the password (prompted; --generate prints a random one) and the public IP for Caddy
cd ~/binance_bot
docker compose run --rm --no-deps web shortbot-web passwd
echo "SHORTBOT_PUBLIC_IP=$(curl -s https://checkip.amazonaws.com)" > data/web.env
docker compose up -d --build            # guardian + web + caddy
docker compose logs -f caddy web        # "certificate obtained successfully" and "Uvicorn running"
```
- **Change the password:** run `passwd` again, then `docker compose restart web`.
- **After a code change:** `deploy/sync.sh`, then `docker compose up -d --build`. That rebuilds the guardian and the
  web image together.
- **Locally, without TLS:** `SHORTBOT_WEB_INSECURE_COOKIE=1 .venv/bin/shortbot-web serve`, then open
  http://127.0.0.1:8080. Never run that against the live desk from a second machine: it is a second process on the
  same journal.
