"""The web console (shortbot.web): login and throttle, typed forms from the command registry, the API's guards, and a
write run through the gateway as actor web:<command>, as the MCP server runs it."""

import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shortbot import cmd_research
from shortbot.commands import CommandError
from shortbot.journal import Journal
from shortbot.web import auth, forms, research, runner, server

PW = "correct horse battery staple"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ------------------------------------------------------------ auth
def test_password_is_stored_as_a_private_scrypt_hash(tmp_path):
    p = auth.set_password(PW, tmp_path / "web_auth.json")
    rec = auth.load(p)
    assert PW not in p.read_text() and rec["n"] == auth.N
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert auth.check_password(PW, rec) and not auth.check_password(PW + "x", rec)
    with pytest.raises(ValueError, match="12"):
        auth.set_password("short", tmp_path / "other.json")
    assert auth.load(tmp_path / "missing.json") is None


def test_sessions_expire_and_revoke():
    clock = Clock()
    s = auth.Sessions(ttl_s=60, clock=clock)
    a, b = s.issue(), s.issue()
    assert s.valid(a) and s.valid(b) and not s.valid("forged") and not s.valid(None)
    s.revoke(a)
    assert not s.valid(a) and s.valid(b)
    clock.t += 61
    assert not s.valid(b)


def test_throttle_blocks_an_ip_after_max_fails_for_the_window():
    clock = Clock()
    t = auth.Throttle(max_fails=3, window_s=100, clock=clock)
    for _ in range(3):
        assert t.wait_s("1.2.3.4") == 0
        t.fail("1.2.3.4")
    assert t.wait_s("1.2.3.4") == pytest.approx(100) and t.wait_s("5.6.7.8") == 0
    clock.t += 101
    assert t.wait_s("1.2.3.4") == 0


# ------------------------------------------------------------ forms
def test_every_command_gets_a_describable_form():
    reg = forms.registry()
    assert {"set_risk_params", "open_short", "brief", "kill-switch", "audit"} <= set(reg)
    assert "backup" not in reg   # raw: CLI plumbing
    for cmd in reg.values():
        d = forms.describe(cmd)
        assert d["fields"] is not None and d["group"]
    groups = {forms.describe(c)["group"] for c in reg.values()}
    assert "Other" not in groups, [k for k, c in reg.items() if forms.describe(c)["group"] == "Other"]


def test_posted_values_become_the_bodys_typed_arguments():
    reg = forms.registry()
    scan = forms.parse(reg["scan_shorts"], {"min_change_24h": "12.5", "top_n": "7", "direction": "losers"})
    assert scan == {"min_change_24h": 12.5, "top_n": 7, "direction": "losers"}
    assert "log" not in {f["name"] for f in forms.fields(reg["scan_shorts"])}   # CLI-only: not an MCP parameter
    assert forms.parse(reg["market_map"], {"fresh": "true"}) == {"fresh": True}
    params = forms.parse(reg["set_risk_params"], {"changes": "risk_per_trade_pct=1.5\nmax_leverage=5",
                                                  "reasoning": "r", "experiment": "x"})
    assert params["changes"] == {"risk_per_trade_pct": 1.5, "max_leverage": 5.0}
    assert forms.parse(reg["set_risk_params"], {"changes": '{"max_positions": 3}', "reasoning": "r",
                                                "experiment": "x"})["changes"] == {"max_positions": 3.0}
    learn = forms.parse(reg["add_learning"], {"statement": "s", "evidence": "e", "kind": "process",
                                              "provenance": "trade:1, research_report"})
    assert learn["provenance"] == ["trade:1", "research_report"]
    assert forms.parse(reg["warmup_start"], {"symbols": "BTC ETH"}) == {"symbols": ["BTC", "ETH"]}
    assert forms.parse(reg["probability"], {"symbol": "X", "stop": "2", "targets": "1.2 1.1"})["targets"] == \
        [1.2, 1.1]


@pytest.mark.parametrize("name, posted, err", [
    ("scan_shorts", {"direction": "sideways"}, "one of"),
    ("scan_shorts", {"min_change_24h": "lots"}, "min_change_24h"),
    ("scan_shorts", {"nope": "1"}, "no parameter nope"),
    ("set_risk_params", {"changes": "a=1", "experiment": "x"}, "reasoning is required"),
    ("set_risk_params", {"changes": "not pairs", "reasoning": "r", "experiment": "x"}, "KEY=VALUE"),
])
def test_bad_values_are_refused_with_the_field_named(name, posted, err):
    with pytest.raises(CommandError, match=err):
        forms.parse(forms.registry()[name], posted)


# ------------------------------------------------------------ HTTP
@pytest.fixture
def web(tmp_path, monkeypatch):
    j = Journal(":memory:")
    fake = SimpleNamespace(journal=j)
    for mod in (cmd_research, runner, server):
        monkeypatch.setattr(mod, "get_app", lambda *a, **k: fake)
    docs = tmp_path / "repo"
    (docs / "docs").mkdir(parents=True)
    (docs / "docs" / "A.md").write_text("# A\nhello")
    (docs / "secret.txt").write_text("no")
    rec = auth.load(auth.set_password(PW, tmp_path / "auth.json"))
    w = server.Web(rec, docs, secure_cookie=False)
    return SimpleNamespace(client=TestClient(w.app()), journal=j, web=w)


def login(c, pw=PW, header=True):
    return c.post("/api/login", json={"password": pw}, headers={"X-Shortbot": "1"} if header else {})


def test_the_page_is_public_but_every_api_needs_a_session(web):
    c = web.client
    page = c.get("/")
    assert page.status_code == 200 and "Shortbot" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', page.text)
    assert len(assets) >= 2 and all(c.get(a).status_code == 200 for a in assets), assets
    assert " style=" not in page.text and "<script>" not in page.text   # the CSP allows neither inline
    assert c.get("/api/commands").status_code == 401
    assert c.post("/api/run/journal", json={}, headers={"X-Shortbot": "1"}).status_code == 401


def test_login_needs_the_header_and_the_password_and_is_throttled(web):
    c = web.client
    assert login(c, header=False).status_code == 403
    for _ in range(auth.MAX_FAILS):
        assert login(c, "wrong password!").status_code == 401
    assert login(c).status_code == 429   # even the right one, until the window passes
    web.web.throttle.ok("testclient")
    r = login(c)
    assert r.status_code == 200 and "samesite=strict" in r.headers["set-cookie"].lower()
    assert "httponly" in r.headers["set-cookie"].lower()
    assert any(x["key"] == "set_risk_params" for x in c.get("/api/commands").json())
    assert c.post("/api/logout", json={}, headers={"X-Shortbot": "1"}).status_code == 200
    assert c.get("/api/commands").status_code == 401


def test_a_write_runs_through_the_gateway_as_the_web_actor(web):
    c = web.client
    login(c)
    args = {"statement": "fades work in Asia", "evidence": "research_report", "provenance": "research_report",
            "kind": "process"}
    assert c.post("/api/run/add_learning", json={"args": args}).status_code == 403   # no header: CSRF guard
    r = c.post("/api/run/add_learning", json={"args": args}, headers={"X-Shortbot": "1"})
    assert r.status_code == 200, r.text
    assert web.journal.learnings()[0]["statement"] == "fades work in Asia"
    row = web.journal.db.execute("SELECT kind, actor, status FROM intents").fetchone()
    assert tuple(row) == ("add_learning", "web:add_learning", "done")
    bad = c.post("/api/run/add_learning", json={"args": {**args, "kind": "vibes"}}, headers={"X-Shortbot": "1"})
    assert bad.status_code == 400 and "kind" in bad.json()["error"]
    assert c.post("/api/run/nope", json={}, headers={"X-Shortbot": "1"}).status_code == 404


def test_alerts_carry_urgency_and_acknowledgement(web):
    c = web.client
    login(c)
    web.journal.db.execute("INSERT INTO alerts (ts, symbol, kind, message) VALUES ('2026-10-03T10:00:00', 'X', "
                           "'guardian_error:scan', 'boom')")
    web.journal.db.commit()
    got = c.get("/api/alerts?limit=5").json()
    assert got["alerts"][0]["message"] == "boom" and "urgent" in got["alerts"][0]
    assert c.get("/api/alerts?limit=x").status_code == 400


def test_docs_and_charts_serve_only_their_own_files(web, tmp_path):
    c = web.client
    login(c)
    assert c.get("/api/docs").json() == ["docs/A.md"]
    assert c.get("/api/doc", params={"path": "docs/A.md"}).text.startswith("# A")
    for bad in ("secret.txt", "../auth.json", "/etc/passwd", "docs/../secret.txt"):
        assert c.get("/api/doc", params={"path": bad}).status_code == 404
    charts = Path(os.environ["SHORTBOT_DB"]).parent / "charts"
    charts.mkdir()
    (charts / "X_15m_1.png").write_bytes(b"\x89PNG\r\n")
    assert c.get("/api/png", params={"path": "data/charts/X_15m_1.png"}).status_code == 200
    (tmp_path / "out.png").write_bytes(b"\x89PNG\r\n")
    for bad in (str(tmp_path / "out.png"), "data/charts/../../out.png", "data/shortbot.db"):
        assert c.get("/api/png", params={"path": bad}).status_code == 404


def test_the_committed_bundle_was_built_from_the_ui_sources():
    """web/static is `npm run build` output (ui/, docs/WEB.md), committed so the server needs no Node. ui/stamp.mjs
    writes a hash of the sources beside it: a bundle older than ui/ fails here."""
    ui = Path(__file__).parent.parent / "ui"
    files = sorted([p.relative_to(ui).as_posix() for p in (ui / "src").rglob("*") if p.is_file()]
                   + ["index.html", "package.json", "vite.config.ts"])
    h = hashlib.sha256()
    for f in files:
        h.update(f.encode() + b"\0" + (ui / f).read_bytes() + b"\0")
    stamp = json.loads((server.STATIC / "build.json").read_text())
    assert stamp["src_hash"] == h.hexdigest(), "ui/ changed since the bundle was built: cd ui && npm run build"


def test_every_command_the_page_calls_is_a_desk_command_with_those_arguments():
    """The page names commands as strings: a typo, or a command renamed in commands.py, would only fail in the browser.
    Every call site in ui/src (run, settle, a sheet's `cmd`, the Overview's reads, the reports) must be in the registry,
    and every argument or prefill it sends must be one of the command's parameters."""
    text = "\n".join(p.read_text() for p in sorted((Path(__file__).parent.parent / "ui" / "src").rglob("*.ts*")))
    reg = forms.registry()
    sent: dict[str, set[str]] = {}
    for key, args in re.findall(r"(?:run|settle|ask|runReport)(?:<[^>]*>)?\(\s*'([\w-]+)'(?:,\s*\{([^}]*)\})?", text):
        sent.setdefault(key, set()).update(re.findall(r"(\w+):", args))
    for key, args in re.findall(r"\['([\w-]+)', '[^']+', \{([^}]*)\}\]", text):                 # REPORTS
        sent.setdefault(key, set()).update(re.findall(r"(\w+):", args))
    for key, rest in re.findall(r"cmd: '([\w-]+)'(.{0,260})", text, re.S):                       # sheets
        prefill = re.search(r"prefill:\s*(?:[^{}\n]*\?\s*)?\{([^}]*)\}", rest.split("openSheet")[0])
        sent.setdefault(key, set()).update(re.findall(r"(\w+):", prefill.group(1)) if prefill else [])
    for block in re.findall(r"_READS = \[([^\]]+)\]", text):
        for key in re.findall(r"'([\w-]+)'", block):
            sent.setdefault(key, set())
    assert len(sent) >= 25, sorted(sent)   # the patterns still find the call sites
    assert set(sent) <= set(reg), sorted(set(sent) - set(reg))
    for key, names in sent.items():
        assert names <= {f["name"] for f in forms.fields(reg[key])}, (key, names)


# ------------------------------------------------------------ research reads
def test_research_shows_the_latest_scan_a_coins_standing_and_the_books_odds(web):
    c, j = web.client, web.journal
    assert c.get("/api/research/candidates").status_code == 401
    login(c)
    now = int(time.time() * 1000)
    old, new = now - 30 * 60_000, now - 5 * 60_000
    j.log_candidates("2026-10-03T10:00:00+00:00", old, [{"symbol": "AAA/USDT:USDT", "score": 10, "price": 1.0}], "caution")
    j.log_candidates("2026-10-03T10:25:00+00:00", new, [
        {"symbol": "AAA/USDT:USDT", "score": 31, "price": 1.1, "setups": ["pump_fade"], "signals": ["+10 lower high"],
         "features": {"rsi_1h": 71.0, "secret_feature": 1}},
        {"symbol": "BBB/USDT:USDT", "score": 44, "price": 2.0, "vetoes": ["too thin"]}], "caution")
    j.log_prediction("scan", "AAA/USDT:USDT", 1.1, 1.2, 0.9, 24.0, "bracket", 0.31, context={"tape_state": "hype_fade"},
                     ts_ms=new)
    got = c.get("/api/research/candidates").json()
    assert (got["scored"], got["n_tradable"], got["n_vetoed"]) == (2, 1, 1)   # AAA once: its newest row
    a = got["tradable"][0]
    assert (a["symbol"], a["score"], a["setups"], a["tape"], a["p_target"]) == \
        ("AAA/USDT:USDT", 31, ["pump_fade"], "hype_fade", 0.31)
    assert a["features"] == {"rsi_1h": 71.0} and got["vetoed"][0]["vetoes"] == ["too thin"]
    assert got["symbols"] == ["AAA/USDT:USDT", "BBB/USDT:USDT"]
    assert c.get("/api/research/candidates?hours=0.25").json()["scored"] == 2
    assert c.get("/api/research/candidates?hours=x").status_code == 400

    state = lambda s: c.get("/api/research/coin", params={"symbol": s}).json()["standing"]["state"]  # noqa: E731
    listed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    j.db.executemany("INSERT INTO metrics (ts, symbol, price, quote_volume_24h) VALUES (?, ?, 1.0, 9e6)",
                     [(listed_at, "QQQ/USDT:USDT"), (listed_at, "币安/USDT:USDT")])
    j.db.commit()
    assert [state(s) for s in ("aaa", "BBBUSDT", "QQQ", "币安", "ZZZ")] == \
        ["candidate", "vetoed", "unscored", "unscored", "unknown"]   # a listed coin, a non-ASCII ticker, a typo
    assert "QQQ/USDT:USDT" in c.get("/api/research/candidates").json()["symbols"]   # the search box knows it
    assert "too thin" in research.coin(j, "QQQ", now, min_volume=2e7)["standing"]["why"]
    assert len(c.get("/api/research/coin", params={"symbol": "AAA"}).json()["history"]) == 2
    assert c.get("/api/research/coin", params={"symbol": "AAA; DROP"}).status_code == 400
    tid = j.create_trade("AAA/USDT:USDT", "pump_fade", "lower high", stop_price=1.2, leverage=2, plan={})
    j.activate_trade(tid, 1.1, 50, 55)
    assert state("AAA") == "position"   # the most binding standing wins

    j.db.execute("INSERT INTO ladder_shadow (ts, trade_id, symbol, price, score, parts, levels, x_star, x_actual, "
                 "x_virtual, action, variant, entry, init_r) VALUES ('2026-10-03T10:26:00+00:00', ?, 'AAA/USDT:USDT', "
                 "1.05, 1.0, '{\"tape_with\": 1}', '[{\"target\": 1.0, \"p0\": 0.4, \"p_model\": 0.5}]', 1.0, 1.0, 1.0, "
                 "'hold', 'base', 1.1, 0.1)", (tid,))
    j.db.commit()
    lad = c.get("/api/research/book").json()["ladders"]["AAA/USDT:USDT"]
    assert lad["levels"][0]["p_model"] == 0.5 and lad["action"] == "hold" and lad["parts"] == {"tape_with": 1}
    assert c.get("/api/research/nope").status_code == 404
