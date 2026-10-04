"""The web console's HTTP side: a JSON API over the command registry plus the static page (docs/WEB.md).

It listens on 127.0.0.1 only; Caddy in front terminates HTTPS on the public IP (deploy/Caddyfile). Every /api route
but login needs the session cookie (HttpOnly, SameSite=Strict, Secure), and every POST the X-Shortbot header, which a
cross-site form can't send. The page loads nothing from other origins (CSP 'self')."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import alerts_view, audit, procs, provenance
from ..app import close_app, get_app
from ..commands import CommandError
from ..redact import install_log_redaction, redact
from . import auth, forms, research, runner

log = logging.getLogger("shortbot.web")

STATIC = Path(__file__).parent / "static"
COOKIE = "sb_session"
HEADER = "x-shortbot"
DOC_GLOBS = ("CLAUDE.md", "README.md", "docs/**/*.md", ".claude/skills/**/*.md", "deploy/*.md")
HEADERS = {
    "content-security-policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "x-frame-options": "DENY",
    "cache-control": "no-store",
}
Handler = Callable[[Request], Awaitable[Response]]


def client_ip(request: Request) -> str:
    """The browser's IP: the peer, or Caddy's X-Forwarded-For when the peer is the local proxy."""
    peer = request.client.host if request.client else "?"
    xff = request.headers.get("x-forwarded-for")
    if xff and peer in ("127.0.0.1", "::1"):
        return xff.split(",")[-1].strip()
    return peer


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


class Web:
    def __init__(self, auth_rec: dict[str, Any], docs_root: Path, secure_cookie: bool = True):
        self.rec, self.docs_root, self.secure = auth_rec, docs_root, secure_cookie
        self.sessions, self.throttle = auth.Sessions(), auth.Throttle()
        self.cmds = forms.registry()

    # ------------------------------------------------------------ guards
    def guarded(self, fn: Handler) -> Handler:
        async def wrapper(request: Request) -> Response:
            if request.method == "POST" and request.headers.get(HEADER) != "1":
                return _err(403, "missing X-Shortbot header")
            if not self.sessions.valid(request.cookies.get(COOKIE)):
                return _err(401, "log in first")
            return await fn(request)
        return wrapper

    # ------------------------------------------------------------ session
    async def login(self, request: Request) -> Response:
        if request.headers.get(HEADER) != "1":
            return _err(403, "missing X-Shortbot header")
        ip = client_ip(request)
        if wait := self.throttle.wait_s(ip):
            return _err(429, f"too many failed logins: try again in {int(wait) + 1} s")
        try:
            password = str((await request.json()).get("password") or "")
        except ValueError:
            return _err(400, "expected JSON")
        if not await asyncio.to_thread(auth.check_password, password, self.rec):
            self.throttle.fail(ip)
            log.warning("web login failed from %s", ip)
            await asyncio.sleep(0.5)
            return _err(401, "wrong password")
        self.throttle.ok(ip)
        log.warning("web login from %s", ip)
        res = JSONResponse({"ok": True})
        res.set_cookie(COOKIE, self.sessions.issue(), max_age=int(auth.SESSION_TTL_S), httponly=True,
                       secure=self.secure, samesite="strict", path="/")
        return res

    async def logout(self, request: Request) -> Response:
        self.sessions.revoke(request.cookies.get(COOKIE))
        res = JSONResponse({"ok": True})
        res.delete_cookie(COOKIE, path="/")
        return res

    async def me(self, request: Request) -> Response:
        app = get_app()
        return JSONResponse({"mode": app.settings.mode, "experiment": app.ex.experiment or "baseline",
                             "code": procs.code_version()[:12]})

    # ------------------------------------------------------------ commands
    async def commands(self, request: Request) -> Response:
        return JSONResponse([forms.describe(c) for c in self.cmds.values()])

    async def run(self, request: Request) -> Response:
        name = request.path_params["name"]
        cmd = self.cmds.get(name)
        if cmd is None:
            return _err(404, f"no command {name}")
        try:
            body = await request.json()
        except ValueError:
            return _err(400, "expected JSON")
        try:
            kwargs = forms.parse(cmd, dict(body.get("args") or {}))
            return JSONResponse(await runner.run(name, cmd, kwargs))
        except CommandError as e:
            return _err(400, str(e))
        except Exception as e:  # noqa: BLE001 - shown to the owner who ran it, and logged with its traceback
            log.exception("web command %s failed", name)
            return _err(500, redact(f"{type(e).__name__}: {e}"))

    # ------------------------------------------------------------ journal reads
    async def alerts(self, request: Request) -> Response:
        try:
            limit = max(1, min(500, int(request.query_params.get("limit", "100"))))
        except ValueError:
            return _err(400, "limit must be a number")
        j = get_app().journal
        rows = [dict(r) for r in j.db.execute("SELECT id, ts, symbol, kind, message FROM alerts ORDER BY id DESC "
                                              "LIMIT ?", (limit,)).fetchall()]
        ack = alerts_view.ack_id(j)
        for r in rows:
            r["urgent"], r["acked"] = alerts_view.is_urgent(r["kind"], r["symbol"]), r["id"] <= ack
        return JSONResponse({"ack_id": ack, "alerts": rows})

    async def research(self, request: Request) -> Response:
        """Stored research state (web/research.py): candidates | coin?symbol= | book. Journal reads only."""
        j, now_ms, q = get_app().journal, int(time.time() * 1000), request.query_params
        view = request.path_params["view"]
        try:
            hours = max(0.25, min(72.0, float(q.get("hours", "6"))))
        except ValueError:
            return _err(400, "hours must be a number")
        if view == "candidates":
            return JSONResponse(runner.jsonable(research.candidates(j, now_ms, hours)))
        if view == "book":
            return JSONResponse(runner.jsonable(research.book(j, now_ms)))
        if view == "coin":
            symbol = q.get("symbol", "").strip()
            if not re.fullmatch(r"[\w/:]{1,30}", symbol):   # \w: Binance lists non-ASCII tickers too
                return _err(400, "symbol: letters and digits, e.g. SOL or SOL/USDT:USDT")
            floor = getattr(getattr(getattr(get_app(), "ex", None), "limits", None), "min_quote_volume_24h", None)
            return JSONResponse(runner.jsonable(research.coin(j, symbol, now_ms, hours, floor)))
        return _err(404, f"no research view {view}")

    # ------------------------------------------------------------ files
    def doc_paths(self) -> list[str]:
        root = self.docs_root.resolve()
        found = {p.resolve() for g in DOC_GLOBS for p in root.glob(g) if p.is_file()}
        return sorted(str(p.relative_to(root)) for p in found if p.is_relative_to(root))

    async def docs(self, request: Request) -> Response:
        return JSONResponse(self.doc_paths())

    async def doc(self, request: Request) -> Response:
        rel = request.query_params.get("path", "")
        if rel not in self.doc_paths():
            return _err(404, "no such document")
        return PlainTextResponse((self.docs_root / rel).read_text(encoding="utf-8", errors="replace"))

    async def png(self, request: Request) -> Response:
        """A chart the chart command rendered: a .png under the desk's data dir, nothing else."""
        data = provenance.data_dir().resolve()
        raw = Path(request.query_params.get("path", ""))
        p = (raw if raw.is_absolute() else Path.cwd() / raw).resolve()
        if p.suffix != ".png" or not p.is_relative_to(data) or not p.is_file():
            return _err(404, "no such chart")
        return FileResponse(p, media_type="image/png")

    async def index(self, request: Request) -> Response:
        return FileResponse(STATIC / "index.html", media_type="text/html")

    # ------------------------------------------------------------ app
    def app(self) -> Starlette:
        @asynccontextmanager
        async def lifespan(_app: Starlette):
            procs.register_app("web")
            try:
                yield
            finally:
                await close_app()

        g = self.guarded
        routes = [
            Route("/", self.index),
            Route("/api/login", self.login, methods=["POST"]),
            Route("/api/logout", g(self.logout), methods=["POST"]),
            Route("/api/me", g(self.me)),
            Route("/api/commands", g(self.commands)),
            Route("/api/run/{name}", g(self.run), methods=["POST"]),
            Route("/api/alerts", g(self.alerts)),
            Route("/api/research/{view}", g(self.research)),
            Route("/api/docs", g(self.docs)),
            Route("/api/doc", g(self.doc)),
            Route("/api/png", g(self.png)),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ]
        inner = Starlette(routes=routes, lifespan=lifespan, max_body_size=1_000_000)
        return SecurityHeaders(inner)   # type: ignore[return-value]  # an ASGI app around the Starlette app


class SecurityHeaders:
    """Adds HEADERS to every response."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                msg.setdefault("headers", [])
                have = {k.lower() for k, _ in msg["headers"]}
                msg["headers"] += [(k.encode(), v.encode()) for k, v in HEADERS.items() if k.encode() not in have]
            await send(msg)
        await self.app(scope, receive, with_headers)


# ------------------------------------------------------------ entry point
def passwd(generate: bool) -> int:
    if generate:
        pw = secrets.token_urlsafe(18)
    else:
        pw = getpass.getpass("new web password: ")
        if pw != getpass.getpass("again: "):
            print("the two passwords differ; nothing changed")
            return 1
    try:
        path = auth.set_password(pw)
    except ValueError as e:
        print(e)
        return 1
    print(f"password stored (scrypt hash) in {path}" + (f"\npassword: {pw}" if generate else ""))
    print("restart the web server to apply it (docker compose restart web)")
    return 0


def serve(host: str, port: int, wait_s: float = 30.0) -> int:
    """Run the console. Without a password it waits for one (checked every wait_s) instead of exiting, so a
    container with `restart: unless-stopped` doesn't crash-loop on a fresh box."""
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    while (rec := auth.load()) is None:
        log.warning("no web password in %s: run `shortbot-web passwd` (checking again in %.0f s)",
                    auth.auth_path(), wait_s)
        time.sleep(wait_s)
    audit.set_process("web", claude=False)
    install_log_redaction()
    docs_root = Path(os.getenv("SHORTBOT_WEB_DOCS") or Path.cwd())
    web = Web(rec, docs_root, secure_cookie=os.getenv("SHORTBOT_WEB_INSECURE_COOKIE") != "1")
    uvicorn.run(web.app(), host=host, port=port, proxy_headers=False, server_header=False)
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="shortbot-web", description="the desk's web console (docs/WEB.md)")
    sub = p.add_subparsers(dest="cmd")
    pw = sub.add_parser("passwd", help="set the owner password (prompted, or --generate prints a random one)")
    pw.add_argument("--generate", action="store_true")
    sv = sub.add_parser("serve", help="run the console (the default)")
    for sp in (p, sv):
        sp.add_argument("--host", default=os.getenv("SHORTBOT_WEB_HOST", "127.0.0.1"))
        sp.add_argument("--port", type=int, default=int(os.getenv("SHORTBOT_WEB_PORT", "8080")))
    args = p.parse_args(argv)
    if args.cmd == "passwd":
        raise SystemExit(passwd(args.generate))
    raise SystemExit(serve(args.host, args.port))


if __name__ == "__main__":
    main()
