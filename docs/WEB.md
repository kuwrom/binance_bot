# Web console

The owner's browser view of the desk: `https://<elastic-ip>` (code: `src/shortbot/web/`).

## What it shows and does
- **Overview**: the kill switch and guardian heartbeat, equity and day/week P&L, open shorts, pending entries, the
  governor and the effective risk parameters, plus the same brief Claude gets at session start. It refreshes every
  60 s. Like the SessionStart hook, the brief runs the guardian watchdog, so a dead guardian trips HALTED.
- **Alerts**: the alerts table, newest first, with filters for urgent and unacknowledged.
- **Commands**: every desk command, as a form generated from its `commands.py` declaration, the same one its MCP
  tool and `shortbot` subcommand come from. It runs as actor `web:<command>` under the command's weight class.
  - A **write** asks for confirmation, then goes through the gateway as an intent, like an MCP call. It is
    journalled before it runs, and every veto, budget and kill-switch check applies.
  - On code older than the code on disk, writes are refused (#68).
  - `shortbot audit --actor web` lists what was done here.
- **Journal**: presets for trades and learnings, the 24 h audit, the research report, calibration, setups and the
  governor.
- **Docs**: `CLAUDE.md`, `README.md`, `docs/**` and the skills, read-only.

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
