"""Run a desk command for the web console, the way mcp_server.tool() runs it for Claude: actor web:<command>, its
weight class, the stale-code check (#68), and a write submitted to the gateway as an intent, journalled before it
runs (with SHORTBOT_GATEWAY on, one that sends orders runs in shortbot-gateway: gateway_client.py). Writes from this
process run one at a time."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pydantic_core

from .. import audit, gateway_client, procs, stale_guard, weight
from ..app import get_app
from ..commands import Command

_writes = asyncio.Lock()


def jsonable(res: Any) -> Any:
    return json.loads(pydantic_core.to_json(res, fallback=str))


def refusal(name: str, v: stale_guard.Verdict, form: str | None) -> dict[str, Any]:
    """A write on stale or unverifiable code: refused, nothing sent (the intent row says so)."""
    why = (f"could not compare this web server's code with the code on disk ({v.error})" if v.error else
           f"this web server started on {procs.code_version()[:12]}, the code on disk is "
           f"{(v.disk or 'gone')[:12]}")
    cli = f", or run `.venv/bin/shortbot {form}`" if form else ""
    return {"ok": False, "stale_code": not v.error, "code_check_failed": bool(v.error),
            "error": f"{why}: {name} refused, nothing was sent. Restart the web server "
                     f"(docker compose up -d --build web){cli}."}


async def run(name: str, cmd: Command, kwargs: dict[str, Any]) -> dict[str, Any]:
    """{result, ms, stale_code?}. A CommandError (a refusal with a reason) propagates to the caller."""
    t0 = time.monotonic()
    with audit.acting(f"web:{name}"), weight.tagged(cmd.weight, wait_s=weight.ENTRY_WAIT_S):
        code = await stale_guard.verdict()
        if not cmd.write:
            res = await cmd.body(**kwargs)
        else:
            async with _writes:
                gw = gateway_client.for_command(cmd, lambda: get_app().journal, lambda: get_app().settings)
                intent = cmd.intent(kwargs)
                if not code.ok and not cmd.stale_exempt:
                    res = await gw.submit(intent, refusal(name, code, cmd.cli_form()))
                else:
                    res = await gw.submit(intent)
    out: dict[str, Any] = {"result": jsonable(res), "ms": round((time.monotonic() - t0) * 1000)}
    if code.stale:
        out["stale_code"] = (f"this web server runs code {procs.code_version()[:12]}, the code on disk is "
                             f"{(code.disk or 'gone')[:12]}: restart it")
    return out
