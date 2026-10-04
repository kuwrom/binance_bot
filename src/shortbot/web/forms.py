"""Desk commands as web forms: one field per parameter of the body (the MCP tool's parameters for an MCP command),
typed from its annotation, and the posted values turned back into the body's keyword arguments.

Field kinds: str, int, float, bool, choice (an Arg's choices), list (list[str] / list[float]: commas, spaces or
lines), json (dicts and lists of dicts: JSON, or KEY=VALUE lines for a dict). Values are validated with pydantic
against the body's own annotation, in lax mode ("1.5" -> 1.5), as an MCP call's arguments are."""

from __future__ import annotations

import inspect
import json
import re
import types
import typing
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .. import commands
from ..commands import Command, CommandError

GROUPS: dict[str, tuple[str, ...]] = {
    "Desk": ("brief", "trading_state", "account", "positions", "pending_entries", "governor_status", "session_clock",
             "risk_budget", "journal", "audit", "start_session", "log_decision"),
    "Market": ("market_regime", "scan_shorts", "market_map", "coin_snapshot", "coin_news", "latest_announcements",
               "read_coin", "coin_profile", "chart", "probability", "book_plan", "upcoming_unlocks", "coin_memory",
               "update_coin_read", "watch"),
    "Orders": ("open_short", "close_position", "move_stop", "update_plan", "reassess", "widen_stop", "add_to_winner",
               "propose_entries", "cancel_entry_group", "amend_entry", "position_ladder", "ladder"),
    "Research": ("research_report", "calibration_report", "list_setups", "list_signals", "add_learning",
                 "update_learning", "learnings", "study", "sweeps", "registry", "add_unlock", "macro-add", "macro-refresh",
                 "warmup_start", "warmup_check", "warmup", "probe", "drill", "golive_report"),
    "Risk config": ("risk_params", "set_risk_params", "reset_risk_params"),
    "Kill switch": ("kill-switch", "halt_trading", "flatten_all"),
}
_GROUP_OF = {k: g for g, keys in GROUPS.items() for k in keys}


def key(cmd: Command) -> str:
    """The command's name in the web API: its MCP name, else its subcommand."""
    return str(cmd.name or cmd.cli)


def registry() -> dict[str, Command]:
    out: dict[str, Command] = {}
    for cmd in commands.load():
        if cmd.raw:   # backup, hook-session: CLI-only plumbing, no app
            continue
        k = key(cmd)
        if k in out:
            raise ValueError(f"two commands answer to {k!r} in the web API")
        out[k] = cmd
    return out


def params(cmd: Command) -> dict[str, inspect.Parameter]:
    return dict((cmd.mcp_signature() if cmd.name else cmd.signature).parameters)


def _core(ann: Any) -> Any:
    if typing.get_origin(ann) in (typing.Union, types.UnionType):
        core = [t for t in typing.get_args(ann) if t is not type(None)]
        return core[0] if len(core) == 1 else ann
    return ann


def kind_of(ann: Any, choices: tuple[Any, ...] | None) -> str:
    core = _core(ann)
    if choices:
        return "choice"
    if core in (bool, int, float, str):
        return core.__name__
    if typing.get_origin(core) is list and typing.get_args(core) and typing.get_args(core)[0] in (str, float, int):
        return "list"
    return "json"


def _jsonable(v: Any) -> Any:
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


def fields(cmd: Command) -> list[dict[str, Any]]:
    args = {a.name: a for a in cmd.args}
    out = []
    for name, p in params(cmd).items():
        a = args.get(name)
        choices = a.choices if a else None
        required = p.default is inspect.Parameter.empty
        out.append({"name": name, "kind": kind_of(p.annotation, choices), "required": required,
                    "default": None if required else _jsonable(p.default),
                    "choices": list(choices) if choices else None, "help": a.help if a else None,
                    "type": type_name(p.annotation)})
    return out


def type_name(ann: Any) -> str:
    """`str`, `float | None`, `dict[str, Any]`: the annotation as it reads in the source."""
    return ann.__name__ if isinstance(ann, type) else str(ann).replace("typing.", "")


def describe(cmd: Command) -> dict[str, Any]:
    doc = inspect.getdoc(cmd.body) or ""
    return {"key": key(cmd), "group": _GROUP_OF.get(key(cmd), "Other"), "write": cmd.write, "weight": cmd.weight,
            "help": cmd.help or (doc.splitlines()[0] if doc else ""), "doc": doc, "cli": cmd.cli,
            "fields": fields(cmd)}


def _pairs(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in re.split(r"[\n,]", text):
        if not line.strip():
            continue
        if "=" not in line:
            raise CommandError(f"expected JSON or KEY=VALUE lines, got {line.strip()!r}")
        k, v = (s.strip() for s in line.split("=", 1))
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def _raw(kind: str, ann: Any, v: Any) -> Any:
    """A posted value in the shape the annotation validates: text split or decoded where the field asks for it."""
    if not isinstance(v, str):
        return v
    if kind == "list":   # commas or lines when there are any ("trade:1, a note"), else spaces ("BTC ETH")
        return [s.strip() for s in re.split(r"[,\n]" if re.search(r"[,\n]", v) else r"\s+", v) if s.strip()]
    if kind == "json":
        try:
            return json.loads(v)
        except ValueError:
            if typing.get_origin(_core(ann)) is dict:
                return _pairs(v)
            raise CommandError("expected JSON") from None
    return v


def parse(cmd: Command, posted: dict[str, Any]) -> dict[str, Any]:
    """The body's keyword arguments from posted form values. An empty value means "not given" (its default)."""
    ps = params(cmd)
    unknown = sorted(set(posted) - set(ps))
    if unknown:
        raise CommandError(f"{key(cmd)} has no parameter {', '.join(unknown)}")
    choices = {a.name: a.choices for a in cmd.args if a.choices}
    out: dict[str, Any] = {}
    for name, p in ps.items():
        v = posted.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            if p.default is inspect.Parameter.empty:
                raise CommandError(f"{name} is required")
            continue
        kind = kind_of(p.annotation, choices.get(name))
        try:
            val = TypeAdapter(p.annotation).validate_python(_raw(kind, p.annotation, v))
        except ValidationError as e:
            raise CommandError(f"{name}: {e.errors()[0]['msg']}") from e
        if choices.get(name) and val is not None and val not in choices[name]:
            raise CommandError(f"{name} must be one of {', '.join(map(str, choices[name]))}")
        out[name] = val
    return out
