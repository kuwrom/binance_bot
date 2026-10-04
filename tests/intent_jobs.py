"""Helpers for the #36 step 2b tests: run a guardian job as its lane does and read the intents it submitted, checked
against the writes the `sent` fixture (conftest.py) saw."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from shortbot import audit, gateway


def rows(j) -> list[dict[str, Any]]:
    """The intents table, oldest first, request and result decoded."""
    out = j._rows("SELECT * FROM intents ORDER BY rowid")
    for r in out:
        r["args"], r["result"] = json.loads(r["args"]), r["result"] and json.loads(r["result"])
    return out


async def job(g, sent: list[tuple[str, str | None]], run: Callable[[], Awaitable[Any]],
              actor: str = "guardian:entries") -> list[dict[str, Any]]:
    """Run one guardian job under its actor (audit.acting, as guardian_loop.Lane does) and return the intents it
    submitted. Every write it made belongs to one of them; each is the actor's, and its stored request binds its
    handler again (what step 6's transport replays)."""
    before, n = len(rows(g.j)), len(sent)
    with audit.acting(actor):
        await run()
    new = rows(g.j)[before:]
    ids = {r["id"] for r in new}
    assert all(i in ids for _, i in sent[n:]), (sent[n:], new)
    for r in new:
        assert r["actor"] == actor and r["side"] == "short"
        inspect.signature(gateway.HANDLERS[r["kind"]]).bind(None, **r["args"])
    return new


def kinds(new: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(r["kind"], r["status"]) for r in new]
