#!/usr/bin/env bash
# Push code from your laptop to the server (never pushes .env, data or venv).
# usage: deploy/sync.sh ubuntu@<elastic-ip> [~/.ssh/key.pem]
set -euo pipefail
HOST=${1:?usage: deploy/sync.sh ubuntu@<ip> [key.pem]}
KEY=${2:-}
SSH="ssh${KEY:+ -i $KEY}"
rsync -az --delete -e "$SSH" \
  --exclude .venv --exclude .env --exclude data --exclude .git --exclude __pycache__ --exclude .pytest_cache \
  "$(dirname "$0")/../" "$HOST:~/binance_bot/"
echo "synced. On the server: cd ~/binance_bot && docker compose up -d --build"
