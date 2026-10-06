"""On-chain shadow features from the Binance Skills Hub (docs/SOCIAL_AND_ROBUST_PRICES.md §3b). Owner decision
2026-10-06: logged features only, no points and no vetoes until a research review scores them.

Source: the hub's public, keyless Binance Wallet ("web3") endpoints, called as its skills call them
(github.com/binance/binance-skills-hub @ 9960c67: crypto-market-rank, trading-signal, query-token-info). They are
undocumented web endpoints: no SLA, no published rate limit (business code 100004 = rate-limited), and fields can
change. Numbers only reach the features; no token name, tag or other third-party text is kept.

Which token is the perp's: `search` by the perp's base symbol, EXACT symbol matches on the chains the other endpoints
serve, the deepest pool wins (never the largest market cap: clones report fake caps, e.g. a Solana "MOVR" at $3.2B cap
with $17k liquidity), and only at >= MIN_LIQUIDITY_USD. No confident match = no features, never a guess. A match is
kept MATCH_TTL_S. Holder and supply fields are the matched CHAIN's (a bridged token's top-10 share includes bridges).

Like the social feeds, the guardian snapshots this (job `onchain`, every 10 min, market-wide) into the kv table and a
scan only reads it (MarketContext.onchain): a scan never waits on these calls. Per run: each market-wide list once per
chain a matched coin lives on, then at most CALLS_PER_RUN per-coin calls (search for a new coin, dynamic per coin);
a coin left over keeps its last features, which carry their own time (oc_ts)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from ..untrusted import token
from .sectors import base_of

log = logging.getLogger("shortbot.onchain")
HOST = "https://web3.binance.com/bapi/defi"
SEARCH = "/v5/public/wallet-direct/buw/wallet/market/token/search/ai"
DYNAMIC = "/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai"
RANK = "/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list/ai"
INFLOW = "/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai"
SIGNALS = "/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai"
HEADERS = {"User-Agent": "binance-web3/3.0 (Skill)", "Accept-Encoding": "identity"}   # as the hub's CLIs send
TIMEOUT_S = 10.0
OK, RATE_LIMITED = "000000", "100004"
CHAINS = ("1", "56", "8453", "CT_501")     # Ethereum, BSC, Base, Solana: what token-rank and dynamic serve
INFLOW_CHAINS = ("56", "CT_501", "8453")   # smart-money-inflow
SIGNAL_CHAINS = ("56", "CT_501")           # trading-signal smart-money
RANK_TYPES = {"alpha": 20, "trending": 10}
RANK_DEFAULTS = {"countMin": 10, "launchTimeMin": 15, "liquidityMin": 5000, "uniqueTraderMin": 10, "volumeMin": 10000}
MIN_LIQUIDITY_USD = 100_000.0
MATCH_TTL_S = 86_400.0
CALLS_PER_RUN = 40
PER_COIN_CONCURRENCY = 4
KV_KEY = "onchain"


class RateLimited(Exception):
    """Business code 100004: the run stops and every coin it didn't reach keeps its last snapshot."""


async def fetch_json(method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
    """The one network call (a replay blocks it: backtest/replay_world.offline_feeds)."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT_S) as c:
        r = await (c.post(HOST + path, json=body or {}) if method == "POST" else c.get(HOST + path, params=params))
        r.raise_for_status()
        return r.json()


async def _data(method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
    """The payload's `data`, or None on any failure (logged). Raises RateLimited on 100004."""
    try:
        payload = await fetch_json(method, path, params, body)
    except Exception as e:  # noqa: BLE001 - an unreachable feed is a missing feature, never a broken job
        log.info("onchain %s: %s: %s", path.rsplit("/", 3)[-3], type(e).__name__, e)
        return None
    code = str((payload or {}).get("code"))
    if code == RATE_LIMITED:
        raise RateLimited(path)
    if code != OK:
        log.info("onchain %s: code %s", path.rsplit("/", 3)[-3], code)
        return None
    return payload.get("data")


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def key_of(ca: Any) -> str:
    """Contract key: EVM addresses are case-insensitive, Solana's base58 is not."""
    s = str(ca or "")
    return s.lower() if s.startswith("0x") else s


# ------------------------------------------------------------ parsers (pure; real response shapes in tests/fakes_onchain)
def parse_match(data: Any, base: str, now_s: float) -> dict[str, Any]:
    """The perp's token from a search answer (module doc), or {"matched": False}."""
    rows = [x for x in (data if isinstance(data, list) else [])
            if str(x.get("symbol") or "").upper() == base and str(x.get("chainId")) in CHAINS]
    best = max(rows, key=lambda x: _f(x.get("liquidity")) or 0.0, default=None)
    liq = _f(best.get("liquidity")) if best else None
    if best is None or liq is None or liq < MIN_LIQUIDITY_USD or not best.get("contractAddress"):
        return {"matched": False, "candidates": len(rows), "ts": now_s}
    return {"matched": True, "chain": str(best["chainId"]), "contract": key_of(best["contractAddress"]),
            "liquidity": round(liq, 2), "ts": now_s}


def parse_inflow(data: Any) -> dict[str, dict[str, Any]]:
    """{contract: {inflow, traders, rank}}: the chain's smart-money net inflow ranking (24h), 1 = largest."""
    rows = data if isinstance(data, list) else []
    return {key_of(x.get("ca")): {"inflow": _f(x.get("inflow")), "traders": _f(x.get("traders")), "rank": i + 1}
            for i, x in enumerate(rows) if x.get("ca")}


def parse_signals(data: Any) -> dict[str, dict[str, Any]]:
    """{contract: the latest smart-money signal plus how many the page holds for it}."""
    out: dict[str, dict[str, Any]] = {}
    for x in data if isinstance(data, list) else []:
        k, t = key_of(x.get("contractAddress")), _f(x.get("signalTriggerTime"))
        if not k or t is None:
            continue
        n = out.get(k, {}).get("n", 0) + 1
        if k not in out or t > out[k]["t_ms"]:
            out[k] = {"t_ms": t, "direction": x.get("direction") if x.get("direction") in ("buy", "sell") else None,
                      "status": token(x.get("status")), "count": _f(x.get("smartMoneyCount")),
                      "exit_rate": _f(x.get("exitRate")), "max_gain": _f(x.get("maxGain"))}
        out[k]["n"] = n
    return out


def parse_rank(data: Any) -> dict[str, int]:
    """{contract: rank} of a token-rank list (1 = top)."""
    rows = (data or {}).get("tokens") if isinstance(data, dict) else None
    return {key_of(x.get("contractAddress")): i + 1 for i, x in enumerate(rows or []) if x.get("contractAddress")}


def parse_dynamic(data: Any, now_s: float) -> dict[str, Any]:
    """Holder structure and buy/sell flow of one token (query-token-info `dynamic`). Holder shares are as reported
    (their unit is undocumented: top10HoldersPercentage reads as percent, the others look like fractions)."""
    d = data if isinstance(data, dict) else {}
    buy24, sell24 = _f(d.get("volume24hBuy")), _f(d.get("volume24hSell"))
    buy1, sell1 = _f(d.get("volume1hBuy")), _f(d.get("volume1hSell"))
    launch = _f(d.get("launchTime"))
    return {
        "oc_holders": _f(d.get("holders")), "oc_top10_pct": _f(d.get("top10HoldersPercentage")),
        "oc_kyc_holders": _f(d.get("kycHolderCount")), "oc_smart_money_pct": _f(d.get("smartMoneyHoldingPercent")),
        "oc_dev_pct": _f(d.get("devHoldingPercent")), "oc_insider_pct": _f(d.get("insiderHoldingPercent")),
        "oc_sniper_pct": _f(d.get("sniperHoldingPercent")), "oc_bundler_pct": _f(d.get("bundlerHoldingPercent")),
        "oc_age_d": round((now_s - launch / 1000) / 86_400, 1) if launch else None,
        "oc_net_1h": round(buy1 - sell1, 2) if buy1 is not None and sell1 is not None else None,
        "oc_net_24h": round(buy24 - sell24, 2) if buy24 is not None and sell24 is not None else None,
        "oc_buy_share_24h": (round(buy24 / (buy24 + sell24), 4)
                             if buy24 is not None and sell24 is not None and buy24 + sell24 > 0 else None),
        "oc_net_binance_24h": _f(d.get("volume24hNetBinance"))}


def coin_features(m: dict[str, Any], lists: dict[tuple[str, ...], Any], dyn: dict[str, Any] | None,
                  now_s: float) -> dict[str, Any]:
    """One matched coin's oc_ features from the chain lists (None = that list was unavailable) and its dynamic."""
    chain, ca = m["chain"], m["contract"]
    f: dict[str, Any] = {"oc_status": "matched", "oc_chain": chain, "oc_liquidity": m["liquidity"],
                         "oc_ts": int(now_s * 1000)}
    inflow = lists.get(("inflow", chain))
    if inflow is not None:
        x = inflow.get(ca) or {}
        f.update({"oc_inflow_listed": bool(x), "oc_inflow_24h": x.get("inflow"),
                  "oc_inflow_traders": x.get("traders"), "oc_inflow_rank": x.get("rank")})
    sig = lists.get(("signals", chain))
    if sig is not None:
        s = sig.get(ca)
        f["oc_sm_signals"] = s["n"] if s else 0
        if s:
            f.update({"oc_sm_direction": s["direction"], "oc_sm_status": s["status"], "oc_sm_count": s["count"],
                      "oc_sm_age_h": round((now_s * 1000 - s["t_ms"]) / 3_600_000, 1),
                      "oc_sm_exit_rate": s["exit_rate"], "oc_sm_max_gain": s["max_gain"]})
    for kind in RANK_TYPES:
        ranks = lists.get(("rank", kind, chain))
        if ranks is not None:
            f[f"oc_{kind}_rank"] = ranks.get(ca)
    if dyn:
        f.update(dyn)
    return f


# ------------------------------------------------------------ the guardian's snapshot
async def _list(kind: str, chain: str, rank_kind: str | None = None) -> Any:
    """One chain's list, parsed; None when it couldn't be read (its features are then left out, not zeroed)."""
    if kind == "inflow":
        data = await _data("POST", INFLOW, body={"chainId": chain, "period": "24h", "tagType": 2})
        return None if data is None else parse_inflow(data)
    if kind == "signals":
        data = await _data("POST", SIGNALS, body={"chainId": chain, "page": 1, "pageSize": 100})
        return None if data is None else parse_signals(data)
    rt = RANK_TYPES[rank_kind or "alpha"]
    body = {**RANK_DEFAULTS, **({"tagFilter": [1, 2, 3]} if rt == 10 else {}),
            "rankType": rt, "chainId": chain, "page": 1, "size": 200}
    data = await _data("POST", RANK, body=body)
    return None if data is None else parse_rank(data)


class _Run:
    """One refresh: the per-coin call budget and the rate-limit stop, shared by its concurrent calls."""

    def __init__(self) -> None:
        self.budget, self.limited = CALLS_PER_RUN, False
        self.stats: dict[str, Any] = {"searched": 0, "dynamic": 0, "lists": 0, "rate_limited": False}

    def spend(self) -> bool:
        if self.limited or self.budget <= 0:
            return False
        self.budget -= 1
        return True

    async def call(self, fn, *a: Any) -> Any:
        """fn(*a), or None once the source has said 100004 (then nothing more is sent this run)."""
        if self.limited:
            return None
        try:
            return await fn(*a)
        except RateLimited:
            self.limited = self.stats["rate_limited"] = True
            log.warning("onchain: rate-limited (100004); the snapshot keeps its earlier values")
            return None


def _prune(entries: dict[str, Any], field: str, max_age_s: float, now: float, scale: float = 1.0) -> dict[str, Any]:
    return {k: v for k, v in entries.items() if now - (v.get(field) or 0) / scale < max_age_s}


async def refresh(journal: Any, symbols: list[str], now_s: float | None = None) -> dict[str, Any]:
    """Snapshot the oc_ features of these perps into kv (module doc). Returns a summary for the log."""
    now = time.time() if now_s is None else now_s  # wall-clock: snapshot time and cache TTLs
    raw = journal.get(KV_KEY)
    prev = json.loads(raw) if raw else {}
    matches = _prune(dict(prev.get("matches") or {}), "ts", 7 * MATCH_TTL_S, now)
    coins = _prune(dict(prev.get("coins") or {}), "oc_ts", 2 * MATCH_TTL_S, now, 1000.0)
    bases = sorted({base_of(s).upper() for s in symbols})
    run = _Run()
    for base in bases:   # one search per new or expired coin, in order, within the budget
        m = matches.get(base)
        if (m is None or now - m.get("ts", 0) >= MATCH_TTL_S) and run.spend():
            data = await run.call(_data, "GET", SEARCH, {"keyword": base})
            run.stats["searched"] += 1
            if data is not None:
                matches[base] = parse_match(data, base, now)
    live = {b: matches[b] for b in bases if (matches.get(b) or {}).get("matched")}
    chains = sorted({m["chain"] for m in live.values()})
    wanted: list[tuple[str, str, str | None]] = (
        [("inflow", c, None) for c in chains if c in INFLOW_CHAINS]
        + [("signals", c, None) for c in chains if c in SIGNAL_CHAINS]
        + [("rank", c, k) for c in chains for k in RANK_TYPES])
    got = await asyncio.gather(*(run.call(_list, kind, c, k) for kind, c, k in wanted))
    lists: dict[tuple[str, ...], Any] = {
        (kind, k, c) if k else (kind, c): v for (kind, c, k), v in zip(wanted, got, strict=True)}
    run.stats["lists"] = len(wanted)
    sem = asyncio.Semaphore(PER_COIN_CONCURRENCY)

    async def one(base: str, m: dict[str, Any]) -> None:
        if not run.spend():
            if base not in coins:   # out of budget: a known coin keeps its last snapshot, a new one gets the lists
                coins[base] = coin_features(m, lists, None, now)
            return
        async with sem:
            data = await run.call(_data, "GET", DYNAMIC, {"chainId": m["chain"], "contractAddress": m["contract"]})
        run.stats["dynamic"] += 1
        coins[base] = coin_features(m, lists, parse_dynamic(data, now) if data is not None else None, now)
    await asyncio.gather(*(one(b, m) for b, m in live.items()))
    for b in bases:
        if (matches.get(b) or {}).get("matched") is False:
            coins[b] = {"oc_status": "no_match", "oc_ts": int(now * 1000)}
    v = json.dumps({"ts": now, "matches": matches, "coins": coins})
    await journal.write(lambda j: j.set(KV_KEY, v))
    return {"coins": len(bases), "matched": len(live), **run.stats}
