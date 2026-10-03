"""The web console's login: one owner password, server-side sessions, a per-IP throttle.

- The password is stored as an scrypt hash in `web_auth.json` beside the journal (SHORTBOT_WEB_AUTH overrides), mode
  0600. `shortbot-web passwd` sets it; the server refuses to start without one.
- Sessions are random tokens held in this process only: a restart logs everyone out, and nothing on disk can be
  replayed into a session.
- After MAX_FAILS wrong passwords from one IP within WINDOW_S, that IP is refused for the rest of the window."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import provenance

N, R, P = 2**15, 8, 1                 # scrypt cost: 32 MiB per check
MAXMEM = 64 * 1024 * 1024
MIN_LEN = 12
SESSION_TTL_S = 12 * 3600
MAX_FAILS, WINDOW_S = 5, 15 * 60


def auth_path() -> Path:
    return Path(os.getenv("SHORTBOT_WEB_AUTH") or provenance.data_dir() / "web_auth.json")


def _hash(password: str, salt: bytes, n: int = N, r: int = R, p: int = P) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=MAXMEM, dklen=32)


def set_password(password: str, path: Path | None = None) -> Path:
    if len(password) < MIN_LEN:
        raise ValueError(f"the password needs at least {MIN_LEN} characters")
    path = path or auth_path()
    salt = secrets.token_bytes(16)
    rec = {"v": 1, "n": N, "r": R, "p": P, "salt": salt.hex(), "hash": _hash(password, salt).hex(),
           "set_at": datetime.now(UTC).isoformat(timespec="seconds")}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, path)
    return path


def load(path: Path | None = None) -> dict[str, Any] | None:
    try:
        rec = json.loads((path or auth_path()).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("hash") and rec.get("salt") else None


def check_password(password: str, rec: dict[str, Any]) -> bool:
    got = _hash(password, bytes.fromhex(rec["salt"]), int(rec["n"]), int(rec["r"]), int(rec["p"]))
    return hmac.compare_digest(got, bytes.fromhex(rec["hash"]))


class Sessions:
    def __init__(self, ttl_s: float = SESSION_TTL_S, clock=time.monotonic):
        self.ttl_s, self.clock = ttl_s, clock
        self._live: dict[str, float] = {}

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        self._live[token] = self.clock() + self.ttl_s
        return token

    def valid(self, token: str | None) -> bool:
        now = self.clock()
        for t in [t for t, exp in self._live.items() if exp <= now]:
            del self._live[t]
        return bool(token) and token in self._live

    def revoke(self, token: str | None) -> None:
        if token:
            self._live.pop(token, None)


class Throttle:
    def __init__(self, max_fails: int = MAX_FAILS, window_s: float = WINDOW_S, clock=time.monotonic):
        self.max_fails, self.window_s, self.clock = max_fails, window_s, clock
        self._fails: dict[str, list[float]] = {}

    def _recent(self, ip: str) -> list[float]:
        cut = self.clock() - self.window_s
        kept = [t for t in self._fails.get(ip, []) if t > cut]
        if kept:
            self._fails[ip] = kept
        else:
            self._fails.pop(ip, None)
        return kept

    def wait_s(self, ip: str) -> float:
        """Seconds until this IP may try again (0: now)."""
        recent = self._recent(ip)
        return 0.0 if len(recent) < self.max_fails else max(0.0, recent[0] + self.window_s - self.clock())

    def fail(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(self.clock())

    def ok(self, ip: str) -> None:
        self._fails.pop(ip, None)
