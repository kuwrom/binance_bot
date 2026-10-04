#!/usr/bin/env bash
# Push code from your laptop to the server (never pushes .env, data or venv).
# usage: deploy/sync.sh ubuntu@<elastic-ip> [~/.ssh/key.pem] [--force]
set -euo pipefail
HOST=${1:?usage: deploy/sync.sh ubuntu@<ip> [key.pem]}
KEY=${2:-}
SSH="ssh${KEY:+ -i $KEY}"
# A server that deploys itself from GitHub (deploy/autodeploy.sh, DEPLOY.md §10) is a git checkout: copying files over
# it would leave local changes that block every later deploy. Push to master instead; --force copies anyway.
if [ "${3:-}" != --force ] && $SSH "$HOST" 'test -d ~/binance_bot/.git'; then
  echo "$HOST deploys itself from GitHub: push to master instead (deploy/DEPLOY.md §10). --force to copy anyway." >&2
  exit 1
fi
rsync -az --delete -e "$SSH" \
  --exclude .venv --exclude .env --exclude data --exclude .git --exclude __pycache__ --exclude .pytest_cache \
  --exclude .ruff_cache --exclude node_modules --exclude '.fuse_hidden*' --exclude .claude/settings.local.json --exclude .claude/worktrees \
  "$(dirname "$0")/../" "$HOST:~/binance_bot/"
echo "synced. On the server: cd ~/binance_bot && docker compose up -d --build"
