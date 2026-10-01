"""Strategy backtester for the short-only desk (docs/BACKTEST.md). Read-only research: hourly event-driven
simulator (sim), no-look-ahead entry features (features), pre-registered strategies with small parameter grids
(strategies), and a report with walk-forward, clustering and a deflated-Sharpe verdict (report, stats).

    .venv/bin/python -m shortbot.backtest.run [--days60] [--out PATH]
"""
