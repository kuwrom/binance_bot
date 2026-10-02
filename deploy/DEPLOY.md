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
>
> flock doesn't cross that VM boundary either: the per-symbol locks in `data/locks` would not serialise a containerised guardian with the MCP servers on the Mac. At startup the guardian sends an urgent `guardian_docker_vm` alert when it sees Docker Desktop's VM (a `linuxkit` kernel) or a data dir on a VM file share (virtiofs, fakeowner, grpcfuse, osxfs, 9p, sshfs).

```bash
cd ~/binance_bot
docker compose up -d --build
docker compose logs -f guardian     # expect "Guardian online (demo)" and a Telegram ping
```
It restarts automatically (`restart: unless-stopped`). After code changes, run `deploy/sync.sh`, then `docker compose up -d --build`.

**One user for every process.** The guardian, every Claude Code session (its MCP server), the CLI and the backup job must run as the same user, the one that owns `data/`, including `data/locks`. Never use `sudo`. docker compose already runs the guardian as your UID and GID. If a lock file was created by another user, the others lock it read-only. That still excludes them (flock(2) works whatever the open mode), but the holder info that a busy symbol reports is lost, and the log warns once per process. On NFS an exclusive flock needs write access, so keep `data/` on a local disk.

## 5. Trading sessions with Claude Code
Run Claude Code **on the server**, so the MCP server and the guardian share `data/shortbot.db`:
```bash
ssh -i ~/.ssh/<key>.pem ubuntu@<elastic-ip>
tmux new -A -s desk          # the session survives disconnects
cd ~/binance_bot && claude   # first run: log in; approve the project's 'shortbot' MCP server
```
Then say "run a trading session". Claude loads the `trading-session` skill.
- **Approval prompts.** Nine MCP tools that change money at risk are flagged `requiresUserInteraction`: `open_short`, `propose_entries`, `close_position`, `add_to_winner`, `widen_stop`, `amend_entry`, `update_plan`, `set_risk_params` and `flatten_all`. Claude Code asks you on **every** call, even in auto or bypass mode, and dontAsk mode denies them. That needs Claude Code v2.1.199 or later; check with `claude --version`.
- **The CLI.** `.claude/settings.json` allowlists `.venv/bin/shortbot *`. The CLI runs the same actions with the same checks and audit trail, and it doesn't ask. Only `update_plan` and `set_risk_params` have no CLI form.
- **Secrets.** Claude is denied read and write access to `.env` (`.claude/settings.json`). Shell deny rules are best-effort, so don't give Claude a shell on a box with live keys if you don't trust the session.
- **Kill switch.** Claude may trip it (`halt_trading`, `shortbot kill-switch`), and the guardian trips it on its loss limits. Only you clear it, from your own terminal: `.venv/bin/shortbot-admin clear --reason ...` (docs/RISK_GOVERNANCE.md).

**After a code change** (a merge in the checkout the editable install points at, or `deploy/sync.sh`), the long-lived processes keep running the code they imported at startup, without any fix merged since:
- restart the guardian (Mac: `pkill -f shortbot-guardian`, then the `nohup` line in section 4; Linux: `docker compose up -d --build`);
- in every open Claude Code session, reconnect shortbot via `/mcp`.

**Audit trail.** Every decision and alert row records who acted (`mcp:<tool>`, `cli:<cmd>`, `guardian:<job>`, `admin:<cmd>`), the Claude Code session (`CLAUDE_CODE_SESSION_ID`), the code version (the hash shown in `procs`) and the mode. Tool and CLI calls that change state, and refusals on the order paths, get rows too. Decisions are append-only. To review it, run `.venv/bin/shortbot audit --since 24h [--actor mcp]`. The model is visible only to Claude Code's SessionStart hook. To record it, add this entry to the `SessionStart` hooks in `.claude/settings.json`, next to `shortbot brief`. That file is the owner's to edit:
```json
{"type": "command", "command": "cd \"$CLAUDE_PROJECT_DIR\" && .venv/bin/shortbot hook-session", "timeout": 30}
```
Until it's added, `model` stays unknown. The hook prints nothing and always exits 0.

Each guardian and MCP server records the code it imported in kv `proc:<pid>`. Every hour the guardian lists the live `shortbot-mcp` and `shortbot-guardian` processes. It sends an urgent `stale_process:<pid>` alert, with the pid, the age and the fix, for each one that is unregistered (code from before this check) or whose code differs from the code on disk now, at most once per pid every 6 h. It also removes the rows of dead pids. The session brief shows the count on one line. It is detection only: nothing is signalled or killed. A guardian in a container only sees its own processes, so on Linux the brief on the host is what covers the MCP servers.

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
- On success it writes kv `backup_last_ok` (time, copy name, `offbox_ok`), retrying if the journal is busy; if that still fails, the run counts as ok and raises a `backup_warning` alert. On any failure it exits 1 and raises a `backup_failed` alert: in the journal (so `shortbot brief` shows it) and on Telegram. A verified copy is listed in the output even when rotation or the off-box step failed.

```bash
.venv/bin/shortbot backup             # daily: the journal (same as: .venv/bin/python -m shortbot.backup)
.venv/bin/shortbot backup --db data/hist.db --db data/hist5m.db --db data/lifecycle.db --keep-daily 0 --keep-weekly 2
```
The first line also copies the trial register, `data/research/trials.db` (#31: every trial behind the Deflated Sharpe's N; not rebuildable), once a study has created it, with the same verify step. The AWS guardian has no register and needs none. The second line is the weekly job for the research DBs. They can only partly be rebuilt: the REST tail and `exchangeInfo` tags are as of the build, and `lifecycle.db` holds a CoinGecko supply snapshot. Backtest write-ups also cite specific builds; to keep one, rename its copy (e.g. `hist-build-0929.db.gz`). Each stamped result names its build by fingerprint (`provenance.json`), and `refresh.py` records hist.db before and after every rebuild; a backup right before the weekly rebuild would keep every cited build restorable (a proposal: the disk is the owner's call, docs/BACKTEST.md "Provenance").

**Off-box** (`SHORTBOT_BACKUP_OFFBOX` in `.env`). Copies on the same disk only protect against corruption and mistakes, so set one of these. The command runs once per new file (the copy, then its manifest) without a shell: `{file}` is the absolute path and `{name}` the file name. Credentials stay in the tool's own config, never in the template.
- **S3:** `aws s3 cp {file} s3://<bucket>/shortbot/{name}`. On EC2, attach an instance role whose policy allows only `s3:PutObject` on `arn:aws:s3:::<bucket>/shortbot/*`, so a compromised box can't delete backups. Use a bucket in another region, turn on versioning, and add a lifecycle rule that expires old copies.
- **rclone** (Backblaze B2, Google Drive...): `rclone copyto {file} <remote>:shortbot/{name}`.
- **Another disk:** `cp {file} /Volumes/<disk>/shortbot/{name}`. Reach external or network disks this way, and keep `SHORTBOT_BACKUP_DIR` on the local disk. The copy is written while the journal's read snapshot is open, so a sleeping or stalled disk there would keep the snapshot open and the WAL growing. The off-box command only runs after the journal is closed.

**Schedule.** Nothing is installed for you. Run the job as the same user as the guardian, never with `sudo`: a root process that opens the journal first can leave root-owned `-wal`/`-shm` files the guardian can't write.

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

## 9. Dependencies (`uv.lock`)
`uv.lock` pins every package, with sha256 hashes. The Dockerfile and `bootstrap.sh` install only from it with `uv sync --locked`, which also fails if the lock is stale against `pyproject.toml`. ccxt matters most: `stops.py` parses its response shapes and `tests/fakes.py` mirrors ccxt 4.5.84, so it only moves on purpose.
- **Laptop venv:** `uv sync --locked --extra dev` makes `.venv` match the lock exactly. It also removes anything not in the lock. The editable install on its own is `uv pip install --python .venv/bin/python -e .` (the venv has no pip). That command doesn't read the lock.
- **Changed a dependency in `pyproject.toml`:** run `uv lock`, which keeps the existing pins, and commit `uv.lock` in the same change.
- **Upgrade deliberately:**
  1. `uv lock --upgrade-package ccxt`, or `ccxt==<version>`.
  2. Read the ccxt changelog for order and stop changes.
  3. `uv sync --locked --extra dev`, then `.venv/bin/pytest -q`. If live responses differ, fix `tests/fakes.py` first.
  4. Commit the lock diff on its own.
- **Base image and uv** are pinned by digest in the `Dockerfile`, so security fixes don't arrive on their own. To bump:
  - `docker buildx imagetools inspect python:3.13-slim` and `ghcr.io/astral-sh/uv:<version>` print the new digests.
  - For uv, also update `UV_VERSION` and both sums in `bootstrap.sh` from the release's `uv-<arch>-unknown-linux-gnu.tar.gz.sha256` files.
  - `uv lock --check` with the laptop's uv confirms it still reads the lock.
- **The pinned image is untested.** The commit that added these pins didn't build it; only the lock was verified (scratch venv, full suite). After the first `docker compose up -d --build`, check `docker compose run --rm guardian python -c "import ccxt; print(ccxt.__version__)"` prints `4.5.84`.

## Cost
Roughly $25–35/month for a t4g.medium, gp3 disk and the Elastic IP (public IPv4 is billed). Check current AWS pricing.
