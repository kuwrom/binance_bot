# Web console

The owner's browser view of the desk: `https://<elastic-ip>` (code: `src/shortbot/web/`).

## What it shows and does
The page has two views, switched in the header (or the menu on a phone) and remembered per browser.

**Basic** is what a person at the screen needs, in the order they need it:
- **Overview**: is the desk safe, how is the money, what is on the book and is it working.
  - A banner when something needs the owner: kill switch not ACTIVE, guardian down, a short without a stop, a
    governor pause, old code.
  - Equity with day/week/open P&L and the realized-P&L curve.
  - One card per open short: P&L in $, % and R, a price ladder from targets to stop, what it loses if stopped, its
    setup and conviction, and the **odds to the next levels** (the guardian's last ladder pass: model probability
    against the no-edge baseline, reward and EV in R, the state score and what the ladder rule would hold). Thesis and
    plan are one tap away, with *Move stop* and *Close*.
  - Resting entries with their fill and target odds at placement, and *Cancel*.
  - The risk room (daily loss cap, risk at the stops, exposure, drawdown), the urgent unacknowledged alerts, the last
    closed trades.
  - It refreshes every 60 s. `trading_state` runs the guardian watchdog on each refresh, so a dead guardian trips
    HALTED here as it does in the SessionStart hook.
- **Radar**: what the system is looking at. The guardian's latest scan as stored in the journal (no exchange call):
  tradable candidates best score first with the setup, tape state and what scored; the vetoed ones with the reason;
  the watchlist. A search box (also in the header) opens **a coin's page**: where it stands (open short, resting
  entry, tradable candidate, vetoed, watchlist, not in the current scan, never scored) and why, its last scan and
  score history, the session's read, our past trades on it. Buttons run the live reads on demand: *Read it now*
  (`read_coin`), *Size a short* (`risk_budget`), *Chart*, *Watch*.
- **Results**: does it work. Our trades by setup (win rate, avg R, P&L), then the **measured edge** by setup from
  `calibration_report` (observed target-first rate against the no-edge baseline, marked *edge*, *not proven* or
  *too few*), then every closed trade with its thesis, exit reason and lesson.
- **Alerts**: grouped by kind (40 repeats of one guardian error read as one line), filters for new and urgent.
- The menu: theme, auto-refresh, **Halt trading**, log out.

**Advanced** adds what the agent and a researcher need:
- on the Overview: the risk parameters (overrides beside the `.env` baseline and bounds), the governor in full, the
  brief Claude gets at session start, and the raw JSON behind every card;
- on the Radar: the live reads (`book_plan`, `market_map`, a fresh `scan_shorts`), snapshot and profile on a coin;
- on Results: the edge by tape state, the rulebook and the reports (24 h audit, research report, calibration,
  setups, the full journal);
- **Commands**: every desk command, as a form generated from its `commands.py` declaration, the same one its MCP
  tool and `shortbot` subcommand come from. It runs as actor `web:<command>` under the command's weight class;
- **Docs**: `CLAUDE.md`, `README.md`, `docs/**` and the skills, read-only.

A button on a card (*Close*, *Move stop*, *Cancel*, *Watch*, *Halt trading*) opens that same command form in a sheet.
- A **write** shows what it will send and asks once more, then goes through the gateway as an intent, like an MCP
  call. It is journalled before it runs, and every veto, budget and kill-switch check applies.
- On code older than the code on disk, writes are refused (#68).
- `shortbot audit --actor web` lists what was done here.

Stored research state comes from `/api/research/{candidates,coin,book}` (`web/research.py`): journal reads only
(candidates, predictions, ladder_shadow, the watchlist), so opening the page costs no exchange weight and logs
nothing. Whatever needs the market now is a button that runs its command.

## The front end
React + TypeScript, built with Vite: sources in `ui/`, the bundle in `src/shortbot/web/static/`. The bundle is
**committed**, so the server and the Docker image need no Node.
```bash
cd ui && nvm use        # Node 22 (.nvmrc)
npm ci                  # once
npm run dev             # http://localhost:5173, /api proxied to SHORTBOT_API (default http://127.0.0.1:8080)
npm run build           # typecheck, bundle into web/static, stamp build.json
```
- After any change under `ui/`, run `npm run build` and commit the bundle with it. `tests/test_web.py` compares the
  sources' hash with `static/build.json` and fails on a stale bundle.
- Nothing may be inlined (the CSP is `'self'` only): no CDN, no inline script or style element. React's `style` prop
  writes the CSSOM, which the CSP allows.
- Layout: `api.ts`, `format.ts`, `types.ts`; `components/` (figures, the command form and sheet, the generic result
  renderer); `views/` (one file per tab). On a phone the tabs are a bottom bar and the sheet rises from the bottom.

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
