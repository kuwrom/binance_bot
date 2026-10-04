"""Provenance for research outputs: which code, data, window and config produced a results file, so a number cited
in a learning can be traced and rerun (docs/BACKTEST.md "Provenance").

    prov = stamp(config=config("shortbot.backtest.run", argv), inputs={"hist_db": fingerprint(path)}, window=...)
    save(run_dir("bt60_r3"), prov, {"results.json": ...})   # + provenance.json next to the files
    .venv/bin/python -m shortbot.provenance verify DIR       # what differs now: code, each input's fingerprint
    .venv/bin/python -m shortbot.provenance rerun DIR        # the command that reruns it (window pinned)

Outputs default to <data dir>/research/<UTC date>-<sha7>[-dirty]/<kind>/, never overwriting a non-empty dir. The data
dir is the desk journal's directory (SHORTBOT_DB, as Settings.db_path), so an installed copy (Docker:
SHORTBOT_DB=/app/data/shortbot.db) writes to /app/data/research. Inputs are only ever opened read-only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from importlib import import_module, metadata
from pathlib import Path
from typing import Any

SCHEMA = 1
PKG = Path(__file__).resolve().parent
DEFAULT_DB = "data/shortbot.db"
CODE_PATHS = ("src", "pyproject.toml")          # what can change a result (docs and tests can't)
CHUNK = 1 << 20


# ------------------------------------------------------------ where outputs go
def desk_db() -> Path:
    import_module(f"{__package__}.config")      # loads .env, so SHORTBOT_DB means what Settings.db_path means
    return Path(os.getenv("SHORTBOT_DB", DEFAULT_DB)).expanduser().resolve()


def data_dir() -> Path:
    return desk_db().parent


def run_dir(kind: str, git: dict[str, Any] | None = None, now: datetime | None = None,
            base: Path | None = None) -> Path:
    """<base>/<UTC date>-<sha7>[-dirty]/<kind>, suffixed -2, -3... rather than overwrite (not created here)."""
    g = git or git_state()
    tag = g["sha7"] if g.get("sha") else f"nogit-{code_sha()[:7]}"
    day = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    return unique((base or data_dir() / "research") / f"{day}-{tag}{'-dirty' if g.get('dirty') else ''}" / kind)


def unique(p: Path) -> Path:
    out, k = p, 1
    while out.exists() and any(out.iterdir()):
        k += 1
        out = p.with_name(f"{p.name}-{k}")
    return out


# ------------------------------------------------------------ code + environment
def _git(cwd: Path | str, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def git_state(pkg: Path = PKG) -> dict[str, Any]:
    """HEAD of the checkout the imported package lives in (sha None for an installed copy, e.g. Docker or
    `uv sync --no-editable`); dirty = uncommitted or untracked changes under src/ or pyproject.toml."""
    top = _git(pkg, "rev-parse", "--show-toplevel")
    if not top or (Path(top).resolve() / "src" / pkg.name) != pkg.resolve():
        return {"sha": None, "sha7": "nogit", "dirty": None, "why": "package is not in a git checkout (installed)"}
    sha = _git(top, "rev-parse", "HEAD")
    st = _git(top, "status", "--porcelain", "--untracked-files=all", "--", *CODE_PATHS)
    paths = [ln[3:] for ln in (st or "").splitlines()]
    return {"sha": sha, "sha7": (sha or "nogit")[:7], "branch": _git(top, "rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(paths) if st is not None else None, "dirty_paths": paths[:20]}


def code_sha(pkg: Path = PKG) -> str:
    """sha256 over every module of the imported package: the exact code, with or without git."""
    h = hashlib.sha256()
    for f in sorted(pkg.rglob("*.py")):
        h.update(f.relative_to(pkg).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def environment() -> dict[str, Any]:
    out: dict[str, Any] = {"python": platform.python_version(), "platform": platform.platform()}
    for dist in ("ccxt", "numpy", "shortbot"):
        try:
            out[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            out[dist] = None
    return out


# ------------------------------------------------------------ inputs
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _time_col(cols: list[str]) -> str | None:
    if "ts" in cols:
        return "ts"
    return next((c for c in cols if c.endswith("_ms")), None) or next((c for c in cols if c.endswith("_at")), None)


def fingerprint(path: Path | str, tables: list[str] | None = None, file_hash: bool = True) -> dict[str, Any]:
    """Cheap identity of a SQLite input, read in one snapshot: per table its rows, min/max of its time column and
    distinct symbols. `id` hashes only these, so it survives VACUUM and WAL checkpoints. `file_sha256` (whole-DB
    inputs) also catches in-place value overwrites with the same counts, but not data still in the -wal file
    (wal_bytes > 0). hist.db (235 MB): ~0.3 s for the stats, ~0.1 s for the hash."""
    p = Path(path).expanduser().resolve()
    out: dict[str, Any] = {"path": str(p)}
    if tables:
        out["only"] = list(tables)
    if not p.is_file():
        return {**out, "missing": True, "id": None}
    db = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None)
    try:
        db.execute("BEGIN")                     # one read snapshot across every table
        names = tables or [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        stats: dict[str, Any] = {}
        for t in names:
            cols = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
            if not cols:
                stats[t] = None
                continue
            tc = _time_col(cols)
            sel = ["COUNT(*)"] + ([f'MIN("{tc}")', f'MAX("{tc}")'] if tc else []) + (
                ['COUNT(DISTINCT "symbol")'] if "symbol" in cols else [])
            row = db.execute(f'SELECT {", ".join(sel)} FROM "{t}"').fetchone()
            s: dict[str, Any] = {"rows": row[0]}
            if tc:
                s.update(time_col=tc, min=row[1], max=row[2])
            if "symbol" in cols:
                s["symbols"] = row[-1]
            stats[t] = s
        db.execute("COMMIT")
    finally:
        db.close()
    wal = p.with_name(p.name + "-wal")
    out.update(id=_digest(stats)[:16], tables=stats, bytes=p.stat().st_size,
               wal_bytes=wal.stat().st_size if wal.exists() else 0)
    if file_hash and not tables:
        out["file_sha256"] = _sha256(p)
    return out


def file_digest(path: Path | str) -> dict[str, Any]:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return {"path": str(p), "missing": True, "sha256": None}
    return {"path": str(p), "bytes": p.stat().st_size, "sha256": _sha256(p)}


# ------------------------------------------------------------ the record
def rerun_args(argv: list[str], drop: tuple[str, ...] = ("--out",), flags: tuple[str, ...] = ("--no-save",)
               ) -> list[str]:
    """argv without the output options: what to pass again to reproduce the run."""
    out, skip = [], False
    for x in argv:
        if skip:
            skip = False
        elif x in drop:
            skip = True
        elif x not in flags and not any(x.startswith(d + "=") for d in drop):
            out.append(x)
    return out


def ensure_arg(argv: list[str], flag: str, path: Path | str) -> list[str]:
    """argv + [flag, absolute path] unless the flag was given: a rerun reads the same file, whatever its CWD."""
    if any(x == flag or x.startswith(flag + "=") for x in argv):
        return list(argv)
    return [*argv, flag, str(Path(path).expanduser().resolve())]


def config(command: str, argv: list[str] | None, **extra: Any) -> dict[str, Any]:
    return {"command": command, "argv": rerun_args(list(sys.argv[1:] if argv is None else argv)), **extra}


def stamp(config: dict[str, Any], inputs: dict[str, dict], window: dict | None = None, seeds: dict | None = None,
          upstream: dict | None = None, git: dict | None = None, now: datetime | None = None) -> dict[str, Any]:
    return {"schema": SCHEMA, "created_utc": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
            "git": git or git_state(), "code_sha": code_sha(), "env": environment(), "config": config,
            "inputs": inputs, "window": window, "seeds": seeds, "upstream": upstream}


def footer(prov: dict[str, Any]) -> str:
    """One markdown line, identical on a rerun of the same code + data (no timestamp)."""
    g, w = prov["git"], prov.get("window") or {}
    ins = ", ".join(f"{k} {v.get('id') or (v.get('sha256') or '')[:16] or 'missing'}"
                    for k, v in prov["inputs"].items())
    win = f"; window {w['t0']} → {w['t1']}" if w.get("t0") else ""
    return (f"_Provenance: code {g.get('sha7')}{' (dirty)' if g.get('dirty') else ''} / {prov['code_sha'][:12]}; "
            f"inputs {ins}{win}; `{' '.join([prov['config']['command'], *prov['config']['argv']])}`. "
            f"Full record: provenance.json._")


def save(out: Path, prov: dict[str, Any], files: dict[str, str]) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "provenance.json").write_text(json.dumps(prov, indent=1, default=str) + "\n")
    for name, text in files.items():
        (out / name).write_text(text)
    return out


def write_json(kind: str, prov: dict[str, Any], res: dict[str, Any], out: Path | None = None) -> Path:
    """A one-file study's JSON with its provenance embedded: at `out`, else <run_dir(kind)>/<kind>.json beside
    provenance.json."""
    text = json.dumps({**res, "provenance": prov}, indent=1, default=str)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        return out
    return save(run_dir(kind, prov["git"]), prov, {f"{kind}.json": text}) / f"{kind}.json"


# ------------------------------------------------------------ checking a past run
def load(path: Path | str) -> dict[str, Any]:
    """A run dir (provenance.json), or a results file carrying a "provenance" key."""
    p = Path(path)
    p = p / "provenance.json" if p.is_dir() else p
    d = json.loads(p.read_text())
    return d.get("provenance", d) if isinstance(d, dict) else d


def _table_diff(a: dict, b: dict) -> str:
    keys = sorted(set(a.get("tables") or {}) | set(b.get("tables") or {}))
    ch = [f"{t} {(a['tables'] or {}).get(t)} -> {(b['tables'] or {}).get(t)}" for t in keys
          if (a.get("tables") or {}).get(t) != (b.get("tables") or {}).get(t)]
    return "; ".join(ch[:4])


def verify(prov: dict[str, Any]) -> list[str]:
    """What differs between that run and now: [] means same code and same inputs (a rerun should reproduce it)."""
    diffs = []
    g = git_state()
    if prov["git"].get("sha") != g.get("sha"):
        diffs.append(f"git: run at {prov['git'].get('sha7')}, now {g.get('sha7')}")
    if prov.get("code_sha") != code_sha():
        diffs.append("code: the imported package differs from the run's (code_sha)")
    for name, was in (prov.get("inputs") or {}).items():
        if "sha256" in was:
            now = file_digest(was["path"])
            if now["sha256"] != was["sha256"]:
                diffs.append(f"{name}: {was['path']} changed (sha256)")
            continue
        now = fingerprint(was["path"], was.get("only"), file_hash="file_sha256" in was)
        if now.get("id") != was.get("id"):
            diffs.append(f"{name}: {was['path']} fingerprint {was.get('id')} -> {now.get('id')} "
                         f"({_table_diff(was, now)})")
        elif was.get("file_sha256") and now.get("file_sha256") != was["file_sha256"]:
            diffs.append(f"{name}: same row counts and time range, different bytes (values overwritten in place?)")
    return diffs


# a default that changed: runs recorded before the flag existed rerun with the old value
LEGACY_DEFAULTS = {"shortbot.backtest.run": (("--min-vol24", "min_vol24", "1000000"),)}   # rounds 0-4 floor (#73)


def rerun_cmd(prov: dict[str, Any], out: Path | str | None = None, pin: bool = True) -> list[str]:
    """The command that reruns it; pin=True adds the recorded window (config["pin"]) so a grown hist.db replays it."""
    c = prov["config"]
    argv = list(c["argv"])
    if pin and c.get("pin") and not any(x in argv for x in c["pin"][::2]):
        argv += c["pin"]
    for flag, key, value in LEGACY_DEFAULTS.get(c["command"], ()):
        if key not in (c.get("args") or {}) and flag not in argv:   # recorded before the flag existed
            argv += [flag, value]
    return [sys.executable, "-m", c["command"], *argv, *(["--out", str(out)] if out else [])]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="check or rerun a research output (docs/BACKTEST.md)")
    ap.add_argument("cmd", choices=("verify", "rerun"))
    ap.add_argument("path", type=Path, help="run dir (provenance.json) or a results file with a provenance key")
    a = ap.parse_args(argv)
    prov = load(a.path)
    if a.cmd == "rerun":
        print(" ".join(rerun_cmd(prov)))
        return 0
    diffs = verify(prov)
    print(json.dumps({"same_code_and_inputs": not diffs, "differences": diffs}, indent=1))
    return 1 if diffs else 0


if __name__ == "__main__":
    raise SystemExit(main())
