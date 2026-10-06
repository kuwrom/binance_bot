"""Binance Skills Hub web3 endpoints (market/onchain.py) as they answered on 2026-10-06 00:3x UTC, trimmed to the
fields the desk reads (search KITE / MOVR, smart-money inflow and signals on BSC, token-rank Alpha on BSC, dynamic of
KITE on BSC). Numbers arrive as long decimal strings, the inflow as a JSON number. KITE_ROW and the like are the real
row shapes with KITE's BSC contract put in, so a list can name a matched coin."""

from __future__ import annotations

from typing import Any

import httpx

from shortbot.market import onchain as OC

KITE_BSC = "0x904567252d8f48555b7447c67dca23f0372e16be"
OK = {"code": "000000", "success": True}
SEARCH = {
    "KITE": [
        {"symbol": "KITE", "chainId": "56", "contractAddress": KITE_BSC,
         "liquidity": "1685904.60343066126983101101329924384",
         "marketCap": "33962496.235027161910028711540877680996414740418819546987682496", "holders": "9664"},
        {"symbol": "KITE", "chainId": "1", "contractAddress": "0x118b70df4f06fa5678e7d543e6066e028c8ea0c0",
         "liquidity": "836738.54214315525588191486604862172",
         "marketCap": "1373843983.108364677472577520796773526257559993942657", "holders": "94230"},
        {"symbol": "KITE", "chainId": "CT_501", "contractAddress": "HMwqgCc2xiFT3ygfCXhzTnbVzxauMSkuYpwvHZMrbonk",
         "liquidity": "152.20519478615678013", "marketCap": "4538.684781162000000000", "holders": "0"}],
    "MOVR": [   # a clone's "market cap" in the billions on a $17k pool, the real token's pool on Moonriver not served
        {"symbol": "MOVR", "chainId": "CT_501", "contractAddress": "3DrmQ144kzEx5ntNWbjGfH3D8NhGw2KeCNUCeUGUHM9C",
         "liquidity": "16756.4436654533371417598848",
         "marketCap": "3199498334.88498087017627506999408490174516082"},
        {"symbol": "MOVR", "chainId": "8453", "contractAddress": "0x43feb74608334dda8c1a6500d185cfc3ea962b83",
         "liquidity": "3894.49829032343441653287140417672909",
         "marketCap": "21788623.82889534485364559161522190979319426"}],
}
INFLOW_56 = [
    {"ca": "0x55a1188e929741d9b65bac2028f9d23334357777",
     "inflow": 607.515248165433656000000000000000000000000000000000000000000000, "traders": 7, "tokenName": "Yajucoin"},
    {"ca": KITE_BSC.upper().replace("0X", "0x"), "inflow": -19294.5145007405870343, "traders": 13, "tokenName": "Kite"}]
SIGNALS_56 = [
    {"ticker": "BAI", "contractAddress": "0xdbe748db52f88052fcf7173d1ae33afe9e3d7777", "direction": "buy",
     "status": "exitRate", "smartMoneyCount": 3, "signalCount": 1, "exitRate": 99, "maxGain": "0.2166",
     "signalTriggerTime": 1791216972000},
    {"ticker": "KITE", "contractAddress": KITE_BSC, "direction": "sell", "status": "timeout", "smartMoneyCount": 4,
     "signalCount": 1, "exitRate": 89, "maxGain": "1.9583", "signalTriggerTime": 1791209783000},
    {"ticker": "KITE", "contractAddress": KITE_BSC, "direction": "buy", "status": "active", "smartMoneyCount": 6,
     "signalCount": 2, "exitRate": 12, "maxGain": "0.0412", "signalTriggerTime": 1791216000000}]
ALPHA_56 = {"total": 197, "page": 1, "size": 200, "tokens": [
    {"symbol": "ELIZAOS", "chainId": "56", "contractAddress": "0xea17df5cf6d172224892b5477a16acb111182478",
     "holdersTop10Percent": "64.88325733600877", "kycHolders": "1106", "launchTime": "1762446488000",
     "liquidity": "263234.5497156608"},
    {"symbol": "KITE", "chainId": "56", "contractAddress": KITE_BSC, "holdersTop10Percent": "87.08",
     "kycHolders": "555", "launchTime": "1762010345000", "liquidity": "1685987.2"}]}
DYNAMIC_KITE = {
    "holders": "9665", "top10HoldersPercentage": "87.0848263214924582", "kycHolderCount": "555",
    "smartMoneyHoldingPercent": "0.000001", "devHoldingPercent": None, "insiderHoldingPercent": None,
    "sniperHoldingPercent": None, "bundlerHoldingPercent": "0.037771", "launchTime": 1762010345000,
    "volume1hBuy": "6248.1780216271473893007", "volume1hSell": "7489.13175260636200718955",
    "volume24hBuy": "392941.83645624782455243009", "volume24hSell": "417526.24578045198955071693",
    "volume24hNetBinance": "-55.70611271626101117341613215706", "liquidity": "1685987.24248502649467098711527371434",
    "circulatingSupply": "240900461.204047315559720396"}
NOW_S = 1791218000.0   # 2026-10-06 ~00:33 UTC, after every fixture's times


class FakeWeb3:
    """Stands in for onchain.fetch_json: answers by endpoint and request, records every call. `limit_at` (an endpoint
    name) answers business code 100004 from then on; `fail_at` raises an HTTP 503 for that endpoint."""

    NAMES = {OC.SEARCH: "search", OC.DYNAMIC: "dynamic", OC.RANK: "rank", OC.INFLOW: "inflow", OC.SIGNALS: "signals"}

    def __init__(self, search: dict[str, list] | None = None, limit_at: str | None = None,
                 fail_at: str | None = None) -> None:
        self.search = SEARCH if search is None else search
        self.limit_at, self.fail_at = limit_at, fail_at
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        name, req = self.NAMES[path], {**(params or {}), **(body or {})}
        self.calls.append((name, str(req.get("keyword") or req.get("contractAddress") or req.get("chainId"))))
        if name == self.fail_at:
            raise httpx.HTTPStatusError("503", request=httpx.Request(method, OC.HOST + path),
                                        response=httpx.Response(503))
        if name == self.limit_at:
            return {"code": "100004", "message": "rate limited", "data": None, "success": False}
        if name == "search":
            return {**OK, "data": self.search.get(req["keyword"], [])}
        if name == "dynamic":
            return {**OK, "data": DYNAMIC_KITE if req["contractAddress"] == KITE_BSC else {}}
        chain = req["chainId"]
        if name == "inflow":
            return {**OK, "data": INFLOW_56 if chain == "56" else []}
        if name == "signals":
            return {**OK, "data": SIGNALS_56 if chain == "56" else []}
        if req["rankType"] == 20 and chain == "56":
            return {**OK, "data": ALPHA_56}
        return {**OK, "data": {"total": 0, "page": 1, "size": 200, "tokens": []}}

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)
