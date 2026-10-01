# Deploy to AWS Tokyo

Binance returns **HTTP 451 to US IPs**, which includes AWS us-east and us-west. Its matching engine is close to AWS Tokyo, so use **ap-northeast-1**.

## 1. Launch the instance
- **Region:** Asia Pacific (Tokyo), `ap-northeast-1`.
- **AMI:** Ubuntu Server LTS, arm64.
- **Type:** `t4g.medium` (2 vCPU / 4 GB). Claude Code needs at least 4 GB RAM. If you only run the guardian and use Claude Code from your laptop, `t4g.small` is enough.
- **Disk:** 20 GB gp3.
- **Security group:** inbound **SSH (22) from your IP only**. No other inbound ports. The bot only makes outbound calls.
- **Elastic IP:** allocate one and associate it, so the IP never changes. That IP is what you whitelist on Binance.

## 2. Binance API key (live, when you're ready)
- The Futures account must be activated **before** you create the key.
- Permissions: Enable Reading + Enable Futures. **Never enable withdrawals.**
- Restrict access to trusted IPs: your **Elastic IP** only. Add your home IP too if you trade from your laptop.
- Demo keys come from https://demo.binance.com (Demo Trading → API Management).

## 3. Install
```bash
# from your laptop
deploy/sync.sh ubuntu@<elastic-ip> ~/.ssh/<key>.pem
ssh -i ~/.ssh/<key>.pem ubuntu@<elastic-ip>
cd ~/binance_bot && bash deploy/bootstrap.sh
nano .env            # keys, Telegram token and chat id; keep SHORTBOT_MODE=demo at first
exit                 # re-login so the docker group applies
```

## 4. Run the guardian (always on)
> **macOS local runs:** don't use Docker. SQLite's shared-memory (WAL) mode doesn't work across the Mac↔Docker file share, so the guardian's writes get lost. Run `nohup .venv/bin/shortbot-guardian >> data/logs/guardian.log 2>&1 &` instead. Docker is fine on Linux, for example EC2.

```bash
cd ~/binance_bot
docker compose up -d --build
docker compose logs -f guardian     # expect "Guardian online (demo)" and a Telegram ping
```
It restarts automatically (`restart: unless-stopped`). After code changes, run `deploy/sync.sh`, then `docker compose up -d --build`.

## 5. Trading sessions with Claude Code
Run Claude Code **on the server**, so the MCP server and the guardian share `data/shortbot.db`:
```bash
ssh -i ~/.ssh/<key>.pem ubuntu@<elastic-ip>
tmux new -A -s desk          # the session survives disconnects
cd ~/binance_bot && claude   # first run: log in; approve the project's 'shortbot' MCP server
```
Then say "run a trading session". Claude loads the `trading-session` skill. `open_short`, `close_position`, `update_plan` and `set_risk_params` are flagged `requiresUserInteraction`, so Claude Code asks you on **every** call, even in auto or bypass mode. That needs Claude Code v2.1.199 or later; check with `claude --version`. Claude is also denied read and write access to `.env` (`.claude/settings.json`). Shell deny rules are best-effort, so don't give Claude a shell on a box with live keys if you don't trust the session.

## 6. Going live (only after demo results in the journal)
1. Create the live key (step 2) and add it to `.env` as `BINANCE_LIVE_API_KEY` / `BINANCE_LIVE_API_SECRET`.
2. Set `SHORTBOT_MODE=live` and `LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK`.
3. Keep the limits small: `MAX_POSITION_NOTIONAL`, `MAX_TOTAL_NOTIONAL`, `RISK_PER_TRADE_PCT`.
4. Run `docker compose up -d` to restart the guardian, and restart Claude Code.

## Backups and ops
- The journal is `data/shortbot.db`, and it holds your trading history. Back it up now and then: `sqlite3 data/shortbot.db ".backup data/backup-$(date +%F).db"`.
- Cost is roughly $25–35/month for a t4g.medium, gp3 disk and the Elastic IP (public IPv4 is billed). Check current AWS pricing.
