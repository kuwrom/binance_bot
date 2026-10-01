#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu LTS EC2 instance (ap-northeast-1). Run as the default 'ubuntu' user
# from inside the project directory:  bash deploy/bootstrap.sh
set -euo pipefail
cd "$(dirname "$0")/.."

sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git tmux sqlite3 curl
sudo usermod -aG docker "$USER"

# uv: pinned release, sha256-checked before use. Bump version and both sums together (deploy/DEPLOY.md).
UV_VERSION=0.12.21
case "$(uname -m)" in
  aarch64) UV_ARCH=aarch64 UV_SHA256=030b69227b40af8c1981b7301793dc66e71ed3c796ea8688209dd268bd91ec51 ;;
  x86_64)  UV_ARCH=x86_64  UV_SHA256=23f02075b652bb1df64178cfae41b5caf160822e720e2663568f3f5d63bc52c0 ;;
  *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac
UV_TGZ="uv-$UV_ARCH-unknown-linux-gnu.tar.gz"
curl -fsSLo "/tmp/$UV_TGZ" "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_TGZ"
echo "$UV_SHA256  /tmp/$UV_TGZ" | sha256sum -c -
mkdir -p "$HOME/.local/bin"
tar -xzf "/tmp/$UV_TGZ" -C "$HOME/.local/bin" --strip-components=1
export PATH="$HOME/.local/bin:$PATH"

# Python env for the MCP server (Claude Code launches it on the host): exactly uv.lock, plus dev tools.
# --locked fails if uv.lock is stale against pyproject.toml instead of re-resolving.
uv sync --locked --extra dev --python 3.13
.venv/bin/pytest -q

# Claude Code (native installer; needs 4GB+ RAM)
curl -fsSL https://claude.ai/install.sh | bash

mkdir -p data
[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo ">> Edit .env (keys, Telegram) then run: docker compose up -d --build"; }
echo ">> Log out and back in (docker group), then: docker compose up -d --build"
