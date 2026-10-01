# shortbot

An AI-assisted, **short-only** research desk for Binance USDⓈ-M futures. It is not an autonomous bot: Claude Code analyses, you approve, code enforces the risk limits, and every scan and trade becomes data the desk learns from.

```
Claude Code ── skills (playbooks) ── lab (ad-hoc analysis, read-only)
     │
     └─MCP─► shortbot tools ──► Binance (demo | live)
               orders need human approval · risk + veto checks server-side · per-trade plans
               journal: trades (true PnL, MAE/MFE) · candidates + 72h outcomes · learnings
Guardian (Docker, AWS Tokyo): reconcile, restore stops, execute plan rules, Telegram alerts,
                              label research outcomes, record metrics history
```

- **Session protocol and architecture:** [CLAUDE.md](CLAUDE.md)
- **Strategy research:** [docs/PLAYBOOK.md](docs/PLAYBOOK.md)
- **Extending the desk:** [docs/RESEARCH.md](docs/RESEARCH.md)
- **Deploy:** [deploy/DEPLOY.md](deploy/DEPLOY.md)
- **Quick start (local, demo):**
  1. `uv sync --locked --extra dev --python 3.13` (exact versions from `uv.lock`; see [deploy/DEPLOY.md](deploy/DEPLOY.md#9-dependencies-uvlock))
  2. `cp .env.example .env`, then add demo keys from demo.binance.com
  3. `.venv/bin/pytest -q`
  4. `.venv/bin/shortbot brief`
  5. `claude`, then ask it to "run a trading session"
