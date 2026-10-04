# Deploy to AWS

Binance returns **HTTP 451 to US IPs**, which includes AWS us-east and us-west. Since 2026-10-03 the desk runs in **eu-central-1 (Frankfurt)**: fapi, demo-fapi and the spot API all answered 200 from its Elastic IP. Tokyo (ap-northeast-1) is nearest Binance's matching engine, about 230 ms closer than Frankfurt, which doesn't matter at the desk's cadence. In any region, check from the box before moving anything: `curl -s -o /dev/null -w '%{http_code}' https://fapi.binance.com/fapi/v1/ping` must print 200 (and the same for `demo-fapi`).

**One guardian per Binance account.** Two guardians race each other's auto exits. When the desk moves to a server, stop the laptop's guardian for good: never restart it after a merge. Set the laptop's journal copy to `kill-switch REDUCING`.

## 1. Launch the instance
- **Region:** any non-US region where the ping above returns 200. The desk uses `eu-central-1`.
- **AMI:** Ubuntu Server LTS, arm64.
- **Type:** `t4g.large` (2 vCPU / 8 GB): the guardian, the web console, Claude Code and research runs. Claude Code needs at least 4 GB RAM. If you only run the guardian, `t4g.small` is enough.
- **Disk:** 40 GB gp3, encrypted (`data/` is about 0.8 GB and grows with the research DBs and backups).
- **Security group:** inbound SSH (22), key-only; narrow it to your IP if yours is stable. For the web console (docs/WEB.md): 443 for the page and 80 for the Let's Encrypt challenge, both from anywhere. The bot itself only makes outbound calls.
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
It restarts automatically (`restart: unless-stopped`). After code changes: on the desk server, push to master and it deploys itself (§10); on a server without §10, run `deploy/sync.sh`, then `docker compose up -d --build`.
The same command starts the web console and Caddy (docs/WEB.md). Both need a one-time setup: `shortbot-web passwd` and `data/web.env`.

**One user for every process.** The guardian, every Claude Code session (its MCP server), the CLI and the backup job must run as the same user, the one that owns `data/`, including `data/locks`. Never use `sudo`. docker compose already runs the guardian as your UID and GID. If a lock file was created by another user, the others lock it read-only. That still excludes them (flock(2) works whatever the open mode), but the holder info that a busy symbol reports is lost, and the log warns once per process. On NFS an exclusive flock needs write access, so keep `data/` on a local disk.

**One pid table (applied 2026-10-04: `pid: host` on the guardian, web and gateway services).** Each container has its own pid namespace. A pid read in one namespace names another process, or none, in the next. The guardian's container can't see the host's MCP servers, and the web container runs as pid 1, which in the guardian's namespace is the guardian itself. So every intent row records the pid namespace of the process that received it (`intents.ns`). The guardian's sweep asks after a pid only in its own namespace; a row from any other namespace ends only after `intents.MAX_OPEN_S` (1 h). The desk's containers run in the host's pid namespace (`pid: host` in `docker-compose.yml`), so the 60 s detection holds for every desk process. The process check (`procs.py`) then sees every desk process in one table. It judges code only within its own mount namespace, so the host's brief reports a container's code as unknown, never as stale.

### 4a. The order gateway (default on since #36 steps 5-7)
`shortbot-gateway` takes every order write: the sessions' since step 5, the guardian's since step 6 (docs/GATEWAY.md). `SHORTBOT_GATEWAY` in `.env`:

| value | sessions | guardian |
|---|---|---|
| unset, `1`, `on` (the default) | through the gateway | through the gateway |
| `sessions` | through the gateway | in its own process (step 6's rollback) |
| `0`, `off` | in their own process | in its own process (step 5's rollback) |

Any other value counts as the default: the processes start, the log says so and the guardian sends an urgent `config_invalid`.
- **Where it runs:** one gateway per mode, on the desk host, as the same user as everything else. It holds `data/run/gateway-<mode>.lock` and listens on `data/run/gateway-<mode>.sock`. `data/run` must be mode 0700 and owned by that user, or the gateway refuses to start. Peers with another uid are refused.
- **What goes through it:** the MCP, CLI and web console commands that send orders, the session watchdog's withdrawal and all 13 kinds of guardian intent. Journal-only writes and `probe` stay in their own process. Step 7 enforces it: any other exchange write raises `WriteRefused` before it is sent (`write_guard.py`). The user stream's listen key and the probes are allowed.
- **At start:** before it listens, the gateway reconciles: it sweeps the dead intents, pulls flat targets, reconciles entries and closed trades, restores every stop, runs the HALTED sweep and resolves unknown intents. It holds the same lease as the guardian's positions pass, so the two never reconcile at once.
- **When it's down:** risk-adding commands are refused. Closes, stop tightening, cancels, flatten, halts and every guardian action run locally under the symbol lock as break-glass (actor `breakglass:<caller>`). `gateway_down_breakglass` and `gateway_down` are urgent only after 60 s; a deploy's restart leaves only an alerts row. The guardian's 30 s check stamps kv `gateway_client:guardian`, and it sends `gateway_unreachable` when the gateway beats but the guardian can't reach it.
- **Stale sessions (D5):** an MCP server on old code still sends its order writes, and the gateway runs them on the code on disk. It refuses an intent built for other code (the protocol version or the intent's signature), with the CLI form. Journal-only writes on stale code are refused as before.
- **Compose service (`gateway` in `docker-compose.yml`):** the same image, `command: shortbot-gateway`, the guardian's `user`, `env_file`, `./data` volume and `pid: host`, `restart: unless-stopped`, `stop_grace_period: 45s` (it drains running intents for up to 30 s). The guardian has no `depends_on` on it on purpose: protection never waits for the gateway.
- **Rollout of steps 5-7:**
  1. The deploy restarts guardian, gateway and web together (§10). Its health check waits for the gateway's heartbeat and for the guardian's `gateway_client:guardian` stamp: ok, or not a client.
  2. Reconnect `/mcp` in every session.
  3. Check the brief's gateway line, and that `breakglass:guardian:*` intent rows appear only around restarts.
- **Rollback:** set `SHORTBOT_GATEWAY=sessions` (guardian back in-process) or `0` (everything), then `docker compose up -d --force-recreate guardian web` and reconnect `/mcp`. Leave the gateway service running: it takes the flock per sequence, like any writer.
- **Restarts:** every deploy rebuilds and restarts the gateway. Sessions retry their connection for 5 s, which covers the drain. The guardian tries once, breaks glass, and tries again 10 s later.

## 5. Trading sessions with Claude Code
Run Claude Code **on the server**, so the MCP server and the guardian share `data/shortbot.db`:
```bash
ssh -i ~/.ssh/<key>.pem ubuntu@<elastic-ip>
tmux new -A -s desk          # the session survives disconnects
cd ~/binance_bot && claude   # first run: log in; approve the project's 'shortbot' MCP server
```
Then say "run a trading session". Claude loads the `trading-session` skill.
- **No approval prompts.** The agent trades on its own judgement (owner decision, 2026-10-02): no MCP tool is flagged `requiresUserInteraction`. Money at risk is bounded in code: vetoes, risk budget, account lock, governor, setup ladder, kill switch, stale-code guard, audit trail. Allowlist `mcp__shortbot__*` in `.claude/settings.json` so tools never prompt.
- **The CLI.** `.claude/settings.json` allowlists `.venv/bin/shortbot *`. Every MCP write tool has a CLI form with the same checks and audit trail (#68). An MCP server on stale code refuses writes and names the CLI command to use instead.
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

Each guardian and MCP server records the code it imported in kv `proc:<pid>`. Every hour the guardian lists the live `shortbot-mcp` and `shortbot-guardian` processes. It sends an urgent `stale_process:<pid>` alert, with the pid, the age and the fix, for each one that is unregistered (code from before this check) or whose code differs from the code on disk now, at most once per pid every 6 h. It also removes the rows of dead pids. The session brief shows the count on one line. It is detection only: nothing is signalled or killed. A guardian in a container only sees its own processes, so on Linux the brief on the host is what covers the MCP servers. Under `pid: host` (section 4) they share one process table, and each side judges only the code it can read: the other side's processes are listed as unknown, never stale.

## 6. Going live: the live desk next to demo (docs/DESKS.md)
`.venv/bin/shortbot golive --text` reports READY or NOT READY from the demo evidence (docs/GO_LIVE.md); the gate only reads, and the steps below are the owner's. Live runs alongside demo from this checkout, `.env` and journal: every account state (kill switch, heartbeat, peak, alerts, warm-up, symbol locks) is kept per mode, and the demo guardian runs the market-wide jobs for both.
1. Create the live key (step 2). In the one `.env`, add `BINANCE_LIVE_API_KEY` / `BINANCE_LIVE_API_SECRET`, `LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK` and `COMPOSE_PROFILES=live`. Keep `SHORTBOT_MODE=demo`: the live services set `SHORTBOT_MODE=live` themselves.
2. The `.env` baseline limits are what live trades under (`AI_PARAMS_IN_LIVE=false`; demo's research overrides apply to demo only). Keep them small: `MAX_POSITION_NOTIONAL`, `MAX_TOTAL_NOTIONAL`, `RISK_PER_TRADE_PCT`.
3. `docker compose up -d` starts `guardian-live` and `gateway-live`; `docker compose logs -f guardian-live` should show "Guardian online (live)". Autodeploy then checks both desks' heartbeats: a live desk that can't beat (a bad key, say) rolls every deploy back until it is fixed or the profile is removed.
4. Live session: `SHORTBOT_MODE=live claude` in the same checkout. Its MCP server, CLI and brief are live; the brief header says so.
5. Kill switch per desk: `.venv/bin/shortbot-admin --mode live clear --reason ...` (or `status`, `equity-peak`, `ladder-*`).
6. Start at minimum size. The first 30 live trades are probation: `shortbot golive` in live mode compares their fills with the model and with demo.
7. To stop live: remove `COMPOSE_PROFILES=live` and run `docker compose stop guardian-live gateway-live` once the live book is flat.

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

## 7a. Plumbing drills (demo, #46)
Evidence that the desk's controls work end to end, kept for the go-live report (docs/GO_LIVE.md; each pass counts for 30 days): each run is a `drill_result` decision (the latest per drill counts; `.venv/bin/shortbot drill status` lists them with the uptime summary). The kill-switch and restart drills run in demo only. They refuse off the demo host, unless the kill switch is ACTIVE with a verified guardian, and on a symbol the desk uses (defaults XRPUSDT and ADAUSDT; `--symbol`, `--second`). Each step is the real command's intent (actor `cli:drill:<name>`), through the gateway when it's on. Drill trades use setup `exp_drill`, which no statistic counts, at the minimum size. Each has a full plan with a 3 h auto time stop, carries `veto_override="plumbing drill #46"` (the output says so), and closes with a lesson.
- **Backup**, any morning after the nightly copy: `.venv/bin/shortbot backup drill`. It restores the newest local copy to a temp dir, never under `data/`, with `restore`'s checks. Then it compares the copy with the live journal: no table larger, the same newest trade, under 36 h old. A pass sets kv `backup_drill_last_ok`. For the off-box half, restore a copy from the bucket on another machine (Restore, above), then run `.venv/bin/shortbot drill attest backup-offbox --evidence "<file, from where, what you checked>"`.
- **Kill switch**, with the desk flat (no open trade, resting entry or position): `.venv/bin/shortbot drill kill-switch`. It opens a drill short and a far entry, then checks REDUCING (an open and a widening refused, a tightening allowed, the entry kept), HALTED (the entry withdrawn, the stop kept) and flatten. It leaves the switch HALTED. The owner clears it in their own terminal (`.venv/bin/shortbot-admin clear --reason '...'`), then `.venv/bin/shortbot drill kill-switch --verify` records the pass.
- **Restart**, with no resting entry anywhere (the stale halt withdraws every one), about 30 min, on the desk host in `~/binance_bot`. Don't push meanwhile: a deploy starts the guardian.
  1. `.venv/bin/shortbot drill restart --phase before` opens a drill short with 2 resting targets and a far entry.
  2. `docker compose stop guardian`. Keep it stopped for at least 11 min.
  3. After 10 min, `.venv/bin/shortbot kill-switch` (the watchdog) trips HALTED, cause stale_heartbeat, and withdraws the entry.
  4. `docker compose start guardian`.
  5. `.venv/bin/shortbot drill restart --phase after` waits up to 15 min for the guardian to lift the halt after 2 verified passes, its stream to connect and the gap to be logged. Then it checks the stop, the targets and the alerts, and closes the drill trade. If it answers `pending`, nothing changed: run it again.
- **Uptime.** From kv `uptime_since` (the first verified heartbeat under this code), every gap over 3 min between verified heartbeats is a `guardian_gaps` row. A gap inside a drill's window is tagged `drill`; an untagged one is unexplained downtime.

## 8. Logs
- **Mac:** the guardian writes `nohup ... >> data/logs/guardian.log`. The daily backup job's `--rotate-log data/logs/guardian.log` copies the log to `guardian.log.1.gz` (keeping 5) when it's over 10 MB, then truncates it in place (copytruncate). The guardian keeps writing at the start of the file because `>>` opened it in append mode, so keep the `>>`. Lines written in the instant between the copy and the truncate can be lost. `newsyslog` can't do this: it renames the file and signals a process, and the guardian can't reopen a shell redirect. Alternative: Homebrew `logrotate` with `copytruncate`, `size 10M`, `rotate 5`, `compress`, `missingok`, run by your own launchd job with `-s <state file>`.
- **Linux (docker compose):** the json-file driver already rotates the guardian's output (`max-size: 10m`, `max-file: 5` in `docker-compose.yml`).

## 9. Dependencies (`uv.lock`)
`uv.lock` pins every package, with sha256 hashes. The Dockerfile and `bootstrap.sh` install only from it with `uv sync --locked`, which also fails if the lock is stale against `pyproject.toml`. ccxt matters most: `stops.py` parses its response shapes and `tests/fakes.py` mirrors ccxt 4.5.84, so it only moves on purpose.
- **Laptop venv:** `uv sync --locked --extra dev` (the first step of `scripts/check`, which then runs ruff, mypy and pytest as CI does) makes `.venv` match the lock exactly. It also removes anything not in the lock. The editable install on its own is `uv pip install --python .venv/bin/python -e .` (the venv has no pip). That command doesn't read the lock.
- **Changed a dependency in `pyproject.toml`:** run `uv lock`, which keeps the existing pins, and commit `uv.lock` in the same change.
- **Upgrade deliberately:**
  1. `uv lock --upgrade-package ccxt`, or `ccxt==<version>`.
  2. Read the ccxt changelog for order and stop changes.
  3. `scripts/check`. If live responses differ, fix `tests/fakes.py` first.
  4. Commit the lock diff on its own.
  ruff and mypy move the same way (`uv lock --upgrade-package ruff`): a new release can bring new findings, fixed in that commit.
- **Base image and uv** are pinned by digest in the `Dockerfile`, so security fixes don't arrive on their own. To bump:
  - `docker buildx imagetools inspect python:3.13-slim` and `ghcr.io/astral-sh/uv:<version>` print the new digests.
  - For uv, also update `UV_VERSION` and both sums in `bootstrap.sh` from the release's `uv-<arch>-unknown-linux-gnu.tar.gz.sha256` files, and `version` and the x86_64 `checksum` in `.github/workflows/ci.yml`.
  - `uv lock --check` with the laptop's uv confirms it still reads the lock.
- **The pinned image is untested.** The commit that added these pins didn't build it; only the lock was verified (scratch venv, full suite). After the first `docker compose up -d --build`, check `docker compose run --rm guardian python -c "import ccxt; print(ccxt.__version__)"` prints `4.5.84`.

## 10. Continuous delivery: push to master, the server deploys
The desk server deploys itself. You push to `master`; GitHub CI runs lint, types and tests; on a green run the
`promote` job fast-forwards the `deploy` branch to that commit (`.github/workflows/ci.yml`). On the server, cron runs
`deploy/autodeploy.sh` every 5 minutes. When `deploy` has moved, it:
1. fast-forwards the checkout (`~/binance_bot` is a git checkout on branch `deploy`);
2. runs `uv sync --locked --extra dev`, then the full test suite **on the server** (it catches what only this box
   shows, like a slower disk);
3. runs `docker compose up -d --build`, which rebuilds the guardian and the web console, and prunes dangling images;
4. checks, within 3 minutes, that the guardian beats again, the web console answers and the order gateway beats, and that
   the guardian reaches the gateway (kv `gateway_client:guardian`, §4a).

If a step fails, it rolls back: the commit that was running, its venv and, if a new image was already built, its
images. It sends one urgent `deploy_failed` alert (Telegram and the brief) and doesn't retry that commit; the next
push gets a fresh try. It never deploys over local changes to tracked files, or to a `deploy` branch that was
rewritten. Either case sends `deploy_blocked`, once per commit, and every run checks again.

Expect about 8 minutes from push to running: CI takes ~3 minutes, the poll up to 5, and the tests on the server ~5
while the old guardian keeps running. The restart itself takes seconds, and exchange-side stops protect open positions
through it. After a deploy, Claude sessions on the server reconnect `shortbot` via `/mcp`: on stale code their journal-only
writes are refused (#68), and their order writes reach the gateway only while their protocol matches (D5, §4a).

**Setup on the server**, done once on 2026-10-04:
```bash
ssh-keygen -t ed25519 -N "" -C "shortbot-desk read-only deploy" -f ~/.ssh/binance_bot_deploy
# GitHub's host keys, from its API over HTTPS
curl -fsS https://api.github.com/meta | python3 -c "import json,sys; [print('github.com '+k) for k in json.load(sys.stdin)['ssh_keys']]" >> ~/.ssh/known_hosts
# from the laptop: the public key as a READ-ONLY deploy key (it is tied to that gh login's token)
gh repo deploy-key add binance_bot_deploy.pub --repo kuwrom/binance_bot --title "shortbot-desk EC2 (read-only, autodeploy)"
# in ~/binance_bot: a git checkout of the commit that is running (data/, .env and .venv are gitignored)
git init -b deploy && git remote add origin git@github.com:kuwrom/binance_bot.git
git config core.sshCommand "ssh -i ~/.ssh/binance_bot_deploy -o IdentitiesOnly=yes"
git fetch origin master && git reset <running sha>          # git status must be clean
crontab -e   # */5 * * * *  $HOME/binance_bot/deploy/autodeploy.sh >> $HOME/binance_bot/data/logs/deploy.log 2>&1
```
- **Logs**: `data/logs/deploy.log` (one line per step), `data/logs/deploy-tests.log` (the last test run),
  `data/logs/autodeploy.state` (the last commit deployed, failed or alerted).
- **Deploy by hand** (e.g. after a `deploy_blocked`): `cd ~/binance_bot && deploy/autodeploy.sh`. After a rewrite,
  first `git fetch origin && git reset --hard origin/deploy`.
- **Pause deploys**: comment out the cron line. `deploy/sync.sh` refuses a server that is a git checkout: copying
  files over it would block every later deploy (`--force` to copy anyway).
- The key is read-only. Code written on the server reaches the desk only through GitHub.

## Cost
Roughly $25–35/month for a t4g.medium, gp3 disk and the Elastic IP (public IPv4 is billed). Check current AWS pricing.
