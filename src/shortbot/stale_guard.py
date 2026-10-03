"""A stale MCP server refuses state-changing tools (#68). An MCP server imports the code once and lives for days; after a
merge it holds the old modules in memory but lazily imports new ones from disk (binance-bot-d5: old trading.py, new
risk_budget.py -> KeyError: 'equity_peak' in governor_status). A process mixing two versions can misbehave anywhere,
the order path included, so mcp_server.tool() asks `verdict()` before every call:

- the code this process imported (procs.code_version) vs the code in its own package dir now, hashed by
  procs.disk_codes as the #58 audit does: a worktree server compares with its worktree, a dir that is gone is stale;
- on stale code a write is refused before anything runs (`refuse`: ok false, stale_code true, its CLI form to run
  instead, from its commands.py declaration; its call row says refused). Reads and the stale-exempt writes
  (halt_trading: a halt only removes risk) run with a `stale_code` warning (`annotate`); a crash while stale says so
  (`failed`);
- hashing takes ~7 ms (168 files, 1.4 MB), so it runs in a thread and is cached TTL_S. A failed hash is not cached and
  fails closed for writes (code_check_failed), open for reads.
The CLI (a fresh process per call) and the guardian (restarted after merges) don't come through here."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import pydantic_core
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from . import procs, provenance

log = logging.getLogger("shortbot.stale_guard")

TTL_S = 5.0
RECONNECT = "ask the human to reconnect shortbot via /mcp"
SHORTBOT = ".venv/bin/shortbot"
_clock = time.monotonic
_cache: tuple[float, str | None] | None = None


@dataclass(frozen=True)
class Verdict:
    disk: str | None = None    # the code on disk now (None: the package dir is gone)
    error: str | None = None   # the hash failed

    @property
    def stale(self) -> bool:
        return self.error is None and self.disk != procs.code_version()

    @property
    def ok(self) -> bool:
        return self.error is None and not self.stale


def _pkg() -> str:
    return str(provenance.PKG)   # what procs.register records as this process's package dir


def _disk() -> str | None:
    pkg = _pkg()
    return procs.disk_codes([pkg])[pkg]


def reset() -> None:
    global _cache
    _cache = None


async def verdict() -> Verdict:
    """This process's code vs the code on disk (cached TTL_S). Never raises."""
    global _cache
    now = _clock()
    if _cache is not None and now - _cache[0] < TTL_S:
        return Verdict(_cache[1])
    try:
        disk = await asyncio.to_thread(_disk)
    except Exception as e:  # noqa: BLE001
        log.warning("could not hash the code on disk in %s: %s", _pkg(), e)
        return Verdict(error=f"{type(e).__name__}: {e}")
    _cache = (now, disk)
    return Verdict(disk)


def why(v: Verdict) -> str:
    started = f"this MCP server started on {procs.code_version()[:12]}"
    return f"{started}, its code directory {_pkg()} is gone" if v.disk is None else \
        f"{started}, the code on disk is {v.disk[:12]}"


async def refuse(name: str, v: Verdict, form: str | None) -> dict[str, Any]:
    """What a write gets instead of running, on stale or unverifiable code: the way out, its CLI `form` included."""
    out = RECONNECT + (f", or run the same action through the CLI (a fresh process on the code on disk): "
                       f"`{SHORTBOT} {form}`" if form else "") + ". Nothing was sent."
    if v.error:
        return {"ok": False, "code_check_failed": True,
                "error": f"could not compare this MCP server's code with the code on disk ({v.error}), so {name} was "
                         f"refused to be safe; retry, and if it keeps failing {out}"}
    return {"ok": False, "stale_code": True, "error": f"stale code: {why(v)}; {out}"}


def annotate(res: Any, v: Verdict) -> Any:
    """A result produced on stale code, with a top-level `stale_code` warning. A non-dict result becomes
    {result, stale_code}: the shape its output schema already has on the wire ({"result": ...}), plus the warning."""
    if not v.stale:
        return res
    w = (f"stale code: {why(v)}. This result may be wrong (the server mixes two code versions) and state-changing "
         f"tools are refused; {RECONNECT}.")
    if isinstance(res, dict):
        return {**res, "stale_code": w}   # a copy: the tool's own dict may be shared
    body = json.loads(pydantic_core.to_json({"result": res, "stale_code": w}, fallback=str))
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(body, indent=2, ensure_ascii=False))],
                          structured_content=body)


def failed(name: str, e: BaseException, v: Verdict) -> ToolError:
    """A tool that raised on stale code: the mismatch is the likely cause, so it leads the message."""
    detail = str(e) if isinstance(e, ToolError) else f"{type(e).__name__}: {e}"
    return ToolError(f"stale code: {why(v)}, and {name} failed ({detail}), most likely because this server mixes the "
                     f"two code versions; {RECONNECT}.")
