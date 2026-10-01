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

## 7. Backups
`data/shortbot.db` is the only copy of the desk's research: trades, scan candidates, decisions, predictions, learnings and alerts. It is gitignored and never synced, so it needs its own backups, off the box.

**What `shortbot backup` does** (`src/shortbot/backup.py`):
- Copies the live journal with SQLite's online backup API, in one step inside the read transaction that counts every table's rows. The guardian and the MCP server keep writing meanwhile (in WAL mode readers don't block writers), and the copy is exactly the counted snapshot. It never copies the raw file, which would miss the `-wal`.
- Checks the copy: `PRAGMA integrity_check`, and every table's row count must equal the snapshot's. Then it gzips it, checks that the gzip decompresses to the same bytes, and writes `shortbot-<UTC>.db.gz` plus a `shortbot-<UTC>.json` manifest (counts, sha256s, `offbox_ok`) to `SHORTBOT_BACKUP_DIR` (default `data/backups`).
- Runs the off-box command if one is set, then rotates: it keeps the newest copy of each of the last 7 days and of the last 4 ISO weeks (`SHORTBOT_BACKUP_KEEP_DAILY` / `_WEEKLY`). Rotation only deletes names it generated, so renaming a copy pins it. No journal table is ever pruned.
- On success it writes kv `backup_last_ok` (time, copy name, `offbox_ok`). On any failure it exits 1 and raises a `backup_failed` alert: in the journal (so `shortbot brief` shows it) and on Telegram.

```bash
.venv/bin/shortbot backup             # daily: the journal (same as: .venv/bin/python -m shortbot.backup)
.venv/bin/shortbot backup --db data/hist.db --db data/hist5m.db --db data/lifecycle.db --keep-daily 0 --keep-weekly 2
```
The second line is the weekly job for the research DBs. They can only partly be rebuilt: the REST tail and `exchangeInfo` tags are as of the build, and `lifecycle.db` holds a CoinGecko supply snapshot. Backtest write-ups also cite specific builds; to keep one, rename its copy (e.g. `hist-build-0929.db.gz`).

**Off-box** (`SHORTBOT_BACKUP_OFFBOX` in `.env`). Copies on the same disk only protect against corruption and mistakes, so set one of these. The command runs once per new file (the copy, then its manifest) without a shell: `{file}` is the absolute path and `{name}` the file name. Credentials stay in the tool's own config, never in the template.
- **S3:** `aws s3 cp {file} s3://<bucket>/shortbot/{name}`. On EC2, attach an instance role whose policy allows only `s3:PutObject` on `arn:aws:s3:::<bucket>/shortbot/*`, so a compromised box can't delete backups. Use a bucket in another region, turn on versioning, and add a lifecycle rule that expires old copies.
- **rclone** (Backblaze B2, Google Drive...): `rclone copyto {file} <remote>:shortbot/{name}`.
- **Another disk:** `cp {file} /Volumes/<disk>/shortbot/{name}`.

**Schedule.** Nothing is installed for you.

*Mac (launchd).* Run this from the repo directory; `$PWD` fills in the absolute paths. launchd runs a job that was missed during sleep when the Mac wakes.
```bash
cat > ~/Library/LaunchAgents/com.shortbot.backup.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.shortbot.backup</string>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>ProgramArguments</key><array>
    <string>$PWD/.venv/bin/python</string><string>-m</string><string>shortbot.backup</string>
    <string>--rotate-log</string><string>data/logs/guardian.log</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>17</integer></dict>
  <key>StandardOutPath</key><string>$PWD/data/logs/backup.log</string>
  <key>StandardErrorPath</key><string>$PWD/data/logs/backup.log</string>
</dict></plist>
EOF
cat > ~/Library/LaunchAgents/com.shortbot.backup-research.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.shortbot.backup-research</string>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>ProgramArguments</key><array>
    <string>$PWD/.venv/bin/python</string><string>-m</string><string>shortbot.backup</string>
    <string>--db</string><string>data/hist.db</string><string>--db</string><string>data/hist5m.db</string>
    <string>--db</string><string>data/lifecycle.db</string>
    <string>--keep-daily</string><string>0</string><string>--keep-weekly</string><string>2</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>4</integer>
    <key>Minute</key><integer>17</integer></dict>
  <key>StandardOutPath</key><string>$PWD/data/logs/backup.log</string>
  <key>StandardErrorPath</key><string>$PWD/data/logs/backup.log</string>
</dict></plist>
EOF
for j in backup backup-research; do
  plutil -lint ~/Library/LaunchAgents/com.shortbot.$j.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.shortbot.$j.plist
done
launchctl kickstart gui/$(id -u)/com.shortbot.backup      # one run now, then read data/logs/backup.log
launchctl print gui/$(id -u)/com.shortbot.backup | grep -i "last exit"
```
`launchctl bootout gui/$(id -u)/com.shortbot.backup` removes a job.

*Mac privacy (Full Disk Access).* The repo is under `~/Documents`, which macOS protects, and a launchd job doesn't inherit Terminal's access. If `data/logs/backup.log` stays empty or says "Operation not permitted", pick one:
- Grant Full Disk Access (System Settings → Privacy & Security) to the interpreter the venv points at: `readlink -f .venv/bin/python`, here `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13`. Every script run by that interpreter gets the access.
- Or move the repo out of `~/Documents` (e.g. to `~/binance_bot`), recreate the venv, restart the guardian from there, and regenerate the plists.

*Linux host (cron).* Run `crontab -e` as `ubuntu`. EC2 cron runs in UTC.
```
17 3 * * *  cd $HOME/binance_bot && .venv/bin/python -m shortbot.backup >> data/logs/backup.log 2>&1
17 4 * * 0  cd $HOME/binance_bot && .venv/bin/python -m shortbot.backup --db data/hist.db --db data/hist5m.db --db data/lifecycle.db --keep-daily 0 --keep-weekly 2 >> data/logs/backup.log 2>&1
```

*docker compose.* `./data` is a bind mount, so the host cron above backs up the same files the guardian container writes. On Linux, host and container share the files and the WAL memory, as the host MCP server already does. Cron uses the host venv from `bootstrap.sh`, which is also where `aws` or `rclone` live. To run inside the image instead: `docker compose run --rm guardian python -m shortbot.backup --no-offbox` (same mount and `.env`; local copies only, since the image has no upload tools).

**Restore.** `restore` only ever writes a new path. With the `.json` manifest beside the `.db.gz`, it checks the sha256 of both the file and the database, `integrity_check`, and every row count.
- Drill, any time:
  ```bash
  .venv/bin/shortbot backup restore data/backups/shortbot-<UTC>.db.gz --to /tmp/drill.db
  sqlite3 /tmp/drill.db "select count(*) from trades; select count(*) from candidates;"
  ```
- Replacing a lost or corrupt journal:
  1. Stop every writer: the guardian (`pkill -f shortbot-guardian` on the Mac, `docker compose stop guardian` on Linux) and Claude Code (it runs the MCP server).
  2. Move all of the journal's files aside: `mkdir data/broken-$(date +%F) && mv data/shortbot.db* data/broken-$(date +%F)/`.
  3. `.venv/bin/shortbot backup restore data/backups/shortbot-<UTC>.db.gz --to data/shortbot.db`. For an off-box copy, download the `.db.gz` and its `.json` into one folder first.
  4. Start the guardian; it switches the file back to WAL. Journal rows written after the snapshot are gone. The exchange is the truth for positions and orders, so check `.venv/bin/shortbot positions` and the guardian's reconcile alerts.

## 8. Logs
- **Mac:** the guardian writes `nohup ... >> data/logs/guardian.log`. The daily backup job's `--rotate-log data/logs/guardian.log` copies the log to `guardian.log.1.gz` (keeping 5) when it's over 10 MB, then truncates it in place (copytruncate). The guardian keeps writing at the start of the file because `>>` opened it in append mode, so keep the `>>`. Lines written in the instant between the copy and the truncate can be lost. `newsyslog` can't do this: it renames the file and signals a process, and the guardian can't reopen a shell redirect. Alternative: Homebrew `logrotate` with `copytruncate`, `size 10M`, `rotate 5`, `compress`, `missingok`, run by your own launchd job with `-s <state file>`.
- **Linux (docker compose):** the json-file driver already rotates the guardian's output (`max-size: 10m`, `max-file: 5` in `docker-compose.yml`).

## Cost
Roughly $25–35/month for a t4g.medium, gp3 disk and the Elastic IP (public IPv4 is billed). Check current AWS pricing.
