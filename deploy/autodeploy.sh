#!/usr/bin/env bash
# Continuous delivery on the desk server (deploy/DEPLOY.md §10). Cron runs it every 5 minutes:
#   */5 * * * *  $HOME/binance_bot/deploy/autodeploy.sh >> $HOME/binance_bot/data/logs/deploy.log 2>&1
#
# CI moves the `deploy` branch only to master commits whose lint, types and tests passed (.github/workflows/ci.yml, job
# promote). When it has moved, this fast-forwards the checkout, syncs the venv, runs the test suite on this box,
# rebuilds the guardian and the web console, and checks that both came back. A failed step puts the code that was
# running back (same steps, old commit) and sends an urgent `deploy_failed` alert; that commit is not retried, the
# next one is. Nothing deploys over local changes to tracked files, or to a branch that isn't a fast-forward
# (`deploy_blocked`, alerted once per commit, retried every run). Each step's command can be swapped through
# AUTODEPLOY_* (tests/test_autodeploy.py runs it against a local origin with stub steps).
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/snap/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

BRANCH=${AUTODEPLOY_BRANCH:-deploy}
STATE=${AUTODEPLOY_STATE:-data/logs/autodeploy.state}
LOCK=${AUTODEPLOY_LOCK:-/tmp/shortbot-autodeploy.lock}
INSTALL=${AUTODEPLOY_INSTALL:-uv sync --locked --extra dev}
TESTS=${AUTODEPLOY_TEST:-.venv/bin/pytest -q -p no:cacheprovider}
BUILD=${AUTODEPLOY_BUILD:-docker compose up -d --build && docker image prune -f}
HEALTH=${AUTODEPLOY_HEALTH:-health}
HEALTH_S=${AUTODEPLOY_HEALTH_S:-180}
ALERT=${AUTODEPLOY_ALERT:-.venv/bin/python -m shortbot.deploy_alert}
TEST_LOG=${AUTODEPLOY_TEST_LOG:-data/logs/deploy-tests.log}
FETCH_ALERT_AFTER=12   # failed fetches in a row (an hour) before deploy_blocked: a network blip stays quiet

log() { echo "$(date -u +%FT%TZ) autodeploy: $*"; }

# ------------------------------------------------------------ one run at a time
if command -v flock >/dev/null; then
  exec 9>"$LOCK"
  flock -n 9 || exit 0                    # the previous run is still deploying
else                                      # macOS has no flock: a lock directory holding the owner's pid
  if ! mkdir "$LOCK.d" 2>/dev/null; then
    kill -0 "$(cat "$LOCK.d/pid" 2>/dev/null)" 2>/dev/null && exit 0
    rm -rf "$LOCK.d" && mkdir "$LOCK.d" || exit 0
  fi
  echo $$ > "$LOCK.d/pid"
  trap 'rm -rf "$LOCK.d"' EXIT
fi

# ------------------------------------------------------------ state: key=value lines
mkdir -p "$(dirname "$STATE")" "$(dirname "$TEST_LOG")"
touch "$STATE"
state_get() { sed -n "s/^$1=//p" "$STATE" | tail -1; }
state_set() { { grep -v "^$1=" "$STATE" || true; echo "$1=$2"; } > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"; }

alert() {   # kind message: once per (kind, commit) when $3 is a commit
  if [ -n "${3:-}" ] && [ "$(state_get "alerted_$1")" = "$3" ]; then return 0; fi
  log "$1: $2"
  bash -c "$ALERT \"\$0\" \"\$1\" --urgent" "$1" "$2" || log "could not send the $1 alert"
  [ -n "${3:-}" ] && state_set "alerted_$1" "$3"
  return 0
}

beat() {    # kv key [json]: its stamp as epoch seconds (empty when missing)
  local v="CAST(v AS INTEGER)"
  [ -n "${2:-}" ] && v="CAST(json_extract(v, '\$.at') AS INTEGER)"
  sqlite3 -readonly data/shortbot.db "SELECT $v FROM kv WHERE k='$1'" 2>/dev/null || echo 0
}

reached() { # kv key: the guardian's gateway check stamped ok, or that it isn't the gateway's client
  sqlite3 -readonly data/shortbot.db "SELECT CASE WHEN json_extract(v, '\$.client') = 0 OR \
    json_extract(v, '\$.ok') = 1 THEN CAST(json_extract(v, '\$.at') AS INTEGER) ELSE 0 END FROM kv WHERE k='$1'" \
    2>/dev/null || echo 0
}

desk_ok() { # suffix ("" demo, "@live") gateway-service: that desk's guardian beats and, with a gateway, the gateway
            # beats and the guardian reaches it (kv gateway_client:guardian: its 30 s check)
  [ "$(beat "guardian_heartbeat$1")" -ge "$BUILT_AT" ] 2>/dev/null || return 1
  grep -qx "$2" <<<"$SVCS" || return 0
  [ "$(beat "gateway_heartbeat$1" json)" -ge "$BUILT_AT" ] 2>/dev/null &&
    [ "$(reached "gateway_client:guardian$1")" -ge "$BUILT_AT" ] 2>/dev/null
}

health() {  # within HEALTH_S of the restart: every desk's guardian (and gateway) is up and the web console answers.
            # The live desk's services run only under the compose profile `live` (docs/DESKS.md)
  local deadline=$(( $(date +%s) + HEALTH_S ))
  SVCS=$(docker compose config --services 2>/dev/null)
  while :; do
    if desk_ok "" gateway && { ! grep -qx guardian-live <<<"$SVCS" || desk_ok "@live" gateway-live; } &&
       { [ ! -f data/web_auth.json ] || curl -fsS -o /dev/null --max-time 5 http://127.0.0.1:8080/; }; then
      return 0
    fi
    [ "$(date +%s)" -ge "$deadline" ] && return 1
    sleep 10
  done
}

run_health() { if [ "$HEALTH" = health ]; then health; else bash -c "$HEALTH"; fi; }

# ------------------------------------------------------------ is there something to deploy?
if ! err=$(git fetch -q origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" 2>&1); then
  n=$(( $(state_get fetch_fails || true) + 1 ))
  state_set fetch_fails "$n"
  [ "$n" -eq "$FETCH_ALERT_AFTER" ] &&
    alert deploy_blocked "git fetch of origin/$BRANCH failed $n times in a row: ${err:0:200}"
  exit 1
fi
state_set fetch_fails 0
new=$(git rev-parse "origin/$BRANCH")
old=$(git rev-parse HEAD)
[ "$new" = "$old" ] && exit 0
[ "$new" = "$(state_get failed)" ] && exit 0      # failed and rolled back: wait for the next commit

if ! git merge-base --is-ancestor "$old" "$new"; then
  alert deploy_blocked "origin/$BRANCH ${new:0:12} is not a fast-forward of the running ${old:0:12}: deploy it by hand \
(deploy/DEPLOY.md §10)" "$new"
  exit 1
fi
dirty=$(git status --porcelain --untracked-files=no)
if [ -n "$dirty" ]; then
  alert deploy_blocked "local changes to $(echo "$dirty" | wc -l | tr -d ' ') tracked files on the server, so \
${new:0:12} was not deployed. Push them through GitHub or discard them (git status); the next run deploys." "$new"
  exit 1
fi

# ------------------------------------------------------------ deploy
log "deploying ${old:0:12} -> ${new:0:12}: $(git log -1 --format=%s "$new" | cut -c1-100)"
failed=""
built=0
if ! git merge -q --ff-only "$new"; then
  failed="git merge"
elif ! bash -c "$INSTALL"; then
  failed="venv sync"
elif ! bash -c "$TESTS" > "$TEST_LOG" 2>&1; then
  failed="tests on the server ($(tail -1 "$TEST_LOG" | cut -c1-120); $TEST_LOG)"
else
  BUILT_AT=$(date +%s)
  built=1
  if ! bash -c "$BUILD"; then
    failed="docker build"
  elif ! run_health; then
    failed="health check (within 3 minutes: no guardian heartbeat, no gateway heartbeat, the guardian not reaching \
the gateway, or no web console)"
  fi
fi
if [ -z "$failed" ]; then
  state_set deployed "$new"
  log "deployed ${new:0:12}"
  exit 0
fi

# ------------------------------------------------------------ roll back to what was running
state_set failed "$new"
log "rolling back to ${old:0:12} after: $failed"
rollback="ok"
git reset -q --hard "$old" || rollback="git reset failed"
bash -c "$INSTALL" || rollback="venv sync failed"
if [ "$built" = 1 ]; then
  bash -c "$BUILD" || rollback="rebuild of ${old:0:12} failed: the desk may be down"
fi
alert deploy_failed "${new:0:12} was not deployed: $failed. Rolled back to ${old:0:12} (rollback: $rollback). \
Details: data/logs/deploy.log"
exit 1
