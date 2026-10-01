#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu LTS EC2 instance (ap-northeast-1). Run as the default 'ubuntu' user
# from inside the project directory:  bash deploy/bootstrap.sh
set -euo pipefail
cd "$(dirname "$0")/.."

sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git tmux sqlite3 curl
sudo usermod -aG docker "$USER"

# Python env for the MCP server (Claude Code launches it on the host)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv venv --python 3.13 .venv
uv pip install -e ".[dev]" --python .venv/bin/python
.venv/bin/pytest -q

# Claude Code (native installer; needs 4GB+ RAM)
curl -fsSL https://claude.ai/install.sh | bash

mkdir -p data
[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo ">> Edit .env (keys, Telegram) then run: docker compose up -d --build"; }
echo ">> Log out and back in (docker group), then: docker compose up -d --build"
