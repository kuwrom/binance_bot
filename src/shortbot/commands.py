"""One declaration per command (#36 step 1): the MCP tool (mcp_server.tool) and the `shortbot` subcommand
(build_parser, cli.py) are both generated from it, so the two surfaces can't drift apart.

A command is an async body plus:
- its MCP tool name (the body's name; mcp=False: CLI only) and its subcommand (cli=None: MCP only);
- args: the CLI form of each argument (Arg). Types, defaults and required-ness come from the body's signature, which is
  also the MCP tool's (minus cli_only args), so a type lives in one place;
- write: it changes state, so it leaves a #24 `call` row and is refused on stale code (#68) unless stale_exempt. A
  read changes nothing at risk, or journals its own row (log_decision);
- weight: its #70 class, weight.PROTECTIVE or weight.RESEARCH (None: a desk read);
- help: the CLI one-liner (the body's docstring is the MCP description); hint: the CLI form a stale MCP server names for
  a write whose subcommand has another shape (cli_form() generates the rest);
- raw: a subcommand that runs without the app, the audit stamp or a weight class; its body takes the parsed namespace
  and returns the exit code (backup, hook-session).
The declarations live in mcp_trade, mcp_research, mcp_book, mcp_desk, mcp_kill and cli_cmds; load() imports them in
tools/list order. Nothing here imports the MCP SDK: the CLI builds its parser from these declarations, and importing the
SDK costs ~0.3 s per CLI call (its MCPServer also reconfigures the root logger, #66). Bodies raise CommandError; the MCP
wrapper turns it into a ToolError with the same message."""

from __future__ import annotations

import argparse
import importlib
import inspect
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Callable

MODULES = ("mcp_trade", "mcp_research", "mcp_book", "mcp_desk", "mcp_kill", "cli_cmds")
_EMPTY = inspect.Parameter.empty
_UNSET: Any = object()


class CommandError(Exception):
    """A command refused with a readable reason (an MCP ToolError; a CLI traceback, as before)."""


@dataclass(frozen=True)
class Arg:
    """The CLI form of one argument. `name` is the body's keyword (and the MCP parameter unless cli_only); `flag` is the
    option ("--stop"), None for a positional. `dest` keeps the CLI's own spelling of a positional (`price` for
    new_stop): the CLI's call rows record the parsed namespace. The rest is argparse's, derived from the body where
    left out: type (float / int), default, required (a flag whose parameter has no default), store_true (a bool
    defaulting to False). `convert` turns the parsed value into the body's (JSON text, KEY=VALUE pairs)."""
    name: str
    flag: str | None = None
    help: str | None = None
    dest: str | None = None
    metavar: str | None = None
    choices: tuple | None = None
    nargs: str | None = None
    action: str | None = None
    type: Callable | None = None
    default: Any = _UNSET
    required: bool | None = None
    convert: Callable[[Any], Any] | None = None
    cli_only: bool = False

    @property
    def cli_dest(self) -> str:
        if self.dest:
            return self.dest
        return self.flag.lstrip("-").replace("-", "_") if self.flag else self.name


@dataclass(frozen=True)
class Command:
    body: Callable
    name: str | None = None
    cli: str | None = None
    args: tuple[Arg, ...] = ()
    write: bool = False
    weight: str | None = None
    stale_exempt: bool = False
    help: str | None = None
    hint: str | None = None
    raw: bool = False
    sig: inspect.Signature | None = field(default=None, compare=False, repr=False)

    def mcp_signature(self) -> inspect.Signature:
        """The MCP tool's signature: the body's, annotations evaluated, without the CLI-only parameters."""
        cli_only = {a.name for a in self.args if a.cli_only}
        return self.sig.replace(parameters=[p for n, p in self.sig.parameters.items() if n not in cli_only])

    def cli_kwargs(self, ns: argparse.Namespace) -> dict[str, Any]:
        """The body's keyword arguments from a parsed command line."""
        out = {}
        for a in self.args:
            v = getattr(ns, a.cli_dest)
            out[a.name] = a.convert(v) if a.convert is not None and v is not None else v
        return out

    def cli_form(self) -> str | None:
        """How to run this command through the CLI, for a stale MCP server's refusal (#68): positionals and flags in
        declaration order, optional ones in brackets."""
        if self.hint or not self.cli:
            return self.hint
        parts = [self.cli]
        for a in self.args:
            kw = self._spec(a)
            mv = a.metavar or a.cli_dest.upper()
            value = "" if kw.get("action") == "store_true" else mv
            if kw.get("nargs") == "+":
                value = f"{mv} [{mv} ...]"
            elif kw.get("nargs") == "*":
                value = f"[{mv} ...]"
            if not a.flag:
                parts.append(f"[{value}]" if kw.get("nargs") == "?" else value)
                continue
            token = f"{a.flag} {value}".strip()
            if kw.get("action") == "append":
                token = f"{token} [{token} ...]" if kw.get("required") else f"[{token} ...]"
            parts.append(token if kw.get("required") else f"[{token}]")
        return " ".join(parts)

    def _spec(self, a: Arg) -> dict[str, Any]:
        """argparse keyword arguments for `a`: what it declares, the rest from the body's parameter."""
        p = None if a.cli_only or self.raw else self.sig.parameters[a.name]
        kw: dict[str, Any] = {k: v for k, v in (("help", a.help), ("metavar", a.metavar), ("choices", a.choices),
                                                ("nargs", a.nargs), ("action", a.action), ("type", a.type)) if v}
        default = a.default if a.default is not _UNSET else p.default if p is not None else _EMPTY
        if p is not None and "action" not in kw and _core(p.annotation) is bool and default is False:
            kw["action"] = "store_true"
        if p is not None and "type" not in kw and kw.get("action") not in ("store_true", "store_false"):
            t = _core(p.annotation)
            if t in (int, float):
                kw["type"] = t
        if default is not _EMPTY:
            kw["default"] = default
        if a.flag:
            if a.dest:
                kw["dest"] = a.dest
            required = a.required if a.required is not None else p is not None and p.default is _EMPTY
            if required:
                kw["required"] = True
        return kw


def _core(ann: Any) -> Any:
    """`float` for float and float | None; anything else as is."""
    if typing.get_origin(ann) in (typing.Union, types.UnionType):
        core = [t for t in typing.get_args(ann) if t is not type(None)]
        return core[0] if len(core) == 1 else ann
    return ann


REGISTRY: list[Command] = []
_BY_NAME: dict[str, Command] = {}
_BY_CLI: dict[str, Command] = {}


def command(*, cli: str | None = None, mcp: bool = True, args: tuple[Arg, ...] = (), write: bool = False,
            weight: str | None = None, stale_exempt: bool = False, help: str | None = None, hint: str | None = None,
            raw: bool = False) -> Callable[[Callable], Callable]:
    """Declare a command; the body comes back unchanged (the MCP tool and the subcommand are generated from it)."""
    def register(body: Callable) -> Callable:
        name = body.__name__ if mcp else None
        sig = None if raw else inspect.signature(body, eval_str=True)
        cmd = Command(body, name, cli, tuple(args), write, weight, stale_exempt, help, hint, raw, sig)
        _check(cmd)
        REGISTRY.append(cmd)
        if name:
            _BY_NAME[name] = cmd
        if cli:
            _BY_CLI[cli] = cmd
        return body
    return register


def _check(cmd: Command) -> None:
    """A declaration that can't generate both surfaces fails at import, not on the first call."""
    label = cmd.name or cmd.cli
    if not (cmd.name or cmd.cli) or (cmd.name in _BY_NAME) or (cmd.cli in _BY_CLI):
        raise ValueError(f"{label}: needs a unique MCP name or subcommand")
    if cmd.raw:
        if cmd.name or not cmd.cli:
            raise ValueError(f"{label}: a raw command is CLI only")
        return
    params = cmd.sig.parameters
    for a in cmd.args:
        if a.name not in params:
            raise ValueError(f"{label}: {a.name} is not a parameter of the body")
        if a.cli_only and params[a.name].default is _EMPTY:
            raise ValueError(f"{label}: CLI-only {a.name} needs a default in the body")
    if cmd.name and cmd.cli:   # parity: the subcommand reaches every parameter of the tool
        missing = set(cmd.mcp_signature().parameters) - {a.name for a in cmd.args if not a.cli_only}
        if missing:
            raise ValueError(f"{label}: the CLI form lacks {sorted(missing)}")


def load() -> list[Command]:
    """Import every declaration module (tools/list order) and return the registry."""
    for m in MODULES:
        importlib.import_module(f"{__package__}.{m}")
    return REGISTRY


def by_cli(name: str) -> Command:
    return _BY_CLI[name]


def by_name(name: str) -> Command:
    return _BY_NAME[name]


def add_arguments(p: argparse.ArgumentParser, cmd: Command) -> None:
    for a in cmd.args:
        p.add_argument(*([a.flag] if a.flag else [a.cli_dest]), **cmd._spec(a))


def build_parser() -> argparse.ArgumentParser:
    """The `shortbot` argument tree: one subcommand per command with a CLI form."""
    p = argparse.ArgumentParser(prog="shortbot")
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in load():
        if cmd.cli:
            add_arguments(sub.add_parser(cmd.cli, **({"help": cmd.help} if cmd.help else {})), cmd)
    return p
