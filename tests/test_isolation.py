"""#80: a test run never sees the desk's keys or Telegram token and never reaches the network. config skips .env when
SHORTBOT_NO_DOTENV is set (conftest sets it before anything imports shortbot), isolated_data clears what the shell
exported, and tests/net_guard.py fails any test that looks up or connects to a non-loopback host or starts a shortbot
process."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from shortbot import app, config, provenance
from shortbot.config import Settings
from tests import net_guard as N

ROOT = Path(__file__).resolve().parents[1]
CANARY = {"BINANCE_DEMO_API_KEY": "canary-demo-key", "BINANCE_DEMO_API_SECRET": "canary-demo-secret",
          "TELEGRAM_BOT_TOKEN": "1:canary-token", "TELEGRAM_CHAT_ID": "canary-chat"}
# a fresh interpreter whose .env lookup (python-dotenv's find_dotenv, which config's import-time load_dotenv() calls)
# finds the canary file instead of the checkout's .env: never the real one
CHILD = ("import json, sys, dotenv.main; dotenv.main.find_dotenv = lambda *a, **k: sys.argv[1]; "
         "from shortbot.config import Settings; s = Settings.from_env(); "
         "print(json.dumps([s.api_key, s.api_secret, s.telegram_token, s.telegram_chat_id]))")
BLACKHOLE = ("203.0.113.1", 9)    # TEST-NET-3 (RFC 5737): never routed, so even an unguarded attempt goes nowhere


def _settings_in_fresh_process(tmp_path: Path, **env: str) -> list[str]:
    dotenv = tmp_path / ".env"
    dotenv.write_text("".join(f"{k}={v}\n" for k, v in CANARY.items()))
    full = {k: v for k, v in os.environ.items() if k not in env or env[k]}
    full.update({k: v for k, v in env.items() if v} | {"PYTHONPATH": str(provenance.PKG.parent)})
    out = subprocess.run([sys.executable, "-c", CHILD, str(dotenv)], capture_output=True, text=True, env=full,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ------------------------------------------------------------ secrets
def test_the_suite_runs_without_dotenv_and_without_exported_secrets():
    assert os.environ.get(config.NO_DOTENV) == "1" and config.load_env() is False
    assert not [k for k in os.environ if k.startswith(("BINANCE_", "TELEGRAM_")) or k == "LIVE_CONFIRM"]
    s = Settings.from_env()
    assert (s.mode, s.api_key, s.api_secret, s.telegram_token, s.telegram_chat_id) == ("demo", "", "", "", "")
    assert app._app is None                       # no keyed app left behind by an earlier test


def test_exported_secrets_are_cleared_but_the_temp_desk_path_stays(isolated_data):
    assert not [k for k in os.environ if k.startswith("SHORTBOT_") and k not in ("SHORTBOT_DB", config.NO_DOTENV)]
    assert Path(os.environ["SHORTBOT_DB"]) == isolated_data / "data" / "shortbot.db"


def test_a_canary_dotenv_never_reaches_settings_in_tests(tmp_path):
    assert _settings_in_fresh_process(tmp_path) == ["", "", "", ""]


def test_a_normal_run_loads_dotenv_at_import(tmp_path):
    """The guardian, the MCP server and the CLI don't set the flag: their import of config loads .env."""
    got = _settings_in_fresh_process(tmp_path, **{config.NO_DOTENV: ""})
    assert got == [CANARY["BINANCE_DEMO_API_KEY"], CANARY["BINANCE_DEMO_API_SECRET"], CANARY["TELEGRAM_BOT_TOKEN"],
                   CANARY["TELEGRAM_CHAT_ID"]]


# ------------------------------------------------------------ network
def test_the_guard_refuses_remote_hosts_and_shortbot_processes_and_records_them():
    g = N.Guard()
    g.test = "t::x"
    v4, v6, unix = (socket.socket(f, socket.SOCK_STREAM) for f in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX))
    refused = [("socket.getaddrinfo", ("api.telegram.org", 443, 0, 0, 0)), ("socket.gethostbyname", ("fapi.binance.com",)),
               ("socket.getaddrinfo", (b"demo-fapi.binance.com", 443, 0, 0, 0)), ("socket.connect", (v4, BLACKHOLE)),
               ("socket.connect", (v6, ("2001:db8::1", 443, 0, 0))), ("socket.sendto", (v4, ("8.8.8.8", 53))),
               ("subprocess.Popen", (sys.executable, [sys.executable, "-m", "shortbot.refresh"], None, None)),
               ("subprocess.Popen", ("/x/.venv/bin/shortbot", ["/x/.venv/bin/shortbot-guardian"], None, None))]
    for event, args in refused:
        with pytest.raises(PermissionError, match="#80"):
            g(event, args)
    assert len(g.seen) == len(refused) and g.seen[0] == "t::x: socket.getaddrinfo 'api.telegram.org'"
    for event, args in [("socket.getaddrinfo", ("localhost", 80, 0, 0, 0)), ("socket.getaddrinfo", (None, 0, 0, 0, 0)),
                        ("socket.connect", (v4, ("127.0.0.1", 9))), ("socket.connect", (v6, ("::1", 9, 0, 0))),
                        ("socket.connect", (unix, "/tmp/x.sock")), ("socket.sendto", (v4, None)),
                        ("subprocess.Popen", ("git", ["git", "-C", "shortbot", "status"], None, None)),
                        ("subprocess.Popen", (sys.executable, [sys.executable, "-c", "import shortbot"], None, None)),
                        ("open", ("shortbot.db", "r", 0))]:
        g(event, args)
    assert len(g.seen) == len(refused)
    g.spawn_ok = True
    g("subprocess.Popen", (sys.executable, [sys.executable, "-m", "shortbot.backtest.run"], None, None))
    assert len(g.seen) == len(refused)
    for s in (v4, v6, unix):
        s.close()


def test_the_session_guard_is_installed():
    """Probed with attempts that can't land even unguarded; the record is dropped so the probe doesn't fail this test."""
    n = len(N.SESSION.seen)
    with pytest.raises(PermissionError, match="#80"):
        socket.getaddrinfo("api.telegram.org", 443)
    with pytest.raises(PermissionError, match="#80"):
        socket.create_connection(BLACKHOLE, timeout=0.2)          # refused at its getaddrinfo, even for an IP
    with socket.socket() as raw, pytest.raises(PermissionError, match="#80"):
        raw.connect(BLACKHOLE)
    with pytest.raises(PermissionError, match="#80"):
        subprocess.run([sys.executable, "-m", "shortbot.refresh"])
    assert len(N.SESSION.seen) == n + 4 and N.SESSION.seen[-2].endswith(f"socket.connect {BLACKHOLE}")
    del N.SESSION.seen[n:]
    with socket.create_server(("127.0.0.1", 0)) as srv:          # loopback stays open (fakes, local servers)
        socket.create_connection(srv.getsockname(), timeout=1).close()


INNER = """
import socket

def test_swallows_its_connection_error():
    try:
        socket.create_connection({addr!r}, timeout=0.2)
    except OSError:
        pass
"""


def test_an_outbound_attempt_fails_the_test_even_when_the_code_swallows_it(tmp_path):
    """A pytest run with this suite's conftest: the inner test passes its body yet fails, naming the attempt."""
    (tmp_path / "test_inner.py").write_text(INNER.format(addr=BLACKHOLE))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(provenance.PKG.parent), str(ROOT)])}
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "tests.conftest", "-p", "no:cacheprovider",
                          "--rootdir", str(tmp_path), str(tmp_path / "test_inner.py")], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "1 passed, 1 error" in out.stdout and f"socket.getaddrinfo '{BLACKHOLE[0]}'" in out.stdout, out.stdout
