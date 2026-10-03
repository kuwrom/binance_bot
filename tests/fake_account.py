"""The account side of the fake (mixed into FakeCcxt, fakes.py): positions as ccxt 4.5.84 parses GET
/fapi/v3/positionRisk, the symbol's margin type, Multi-Assets mode, the ADL quantile and Binance's own closes (ADL,
liquidation).

Per symbol (fake.sym[s], all optional): "margin" ISOLATED (default: the desk sets it before every entry; a symbol's
Binance default is CROSSED), "adl" the ADL quantile 0-4 (default 0), "wallet_adj" margin added to / drained from the
isolated wallet (fake_engine.isolated_wallet). Account: `multi_assets`, `adl_answer` (a raw adlQuantile answer that
replaces the modelled one: shape faults), `force_orders` (Binance's own closes, `force_close`)."""

from __future__ import annotations

from tests import fake_errors as err
from tests.fake_engine import SYM, WEEK_MS, raw


class Account:
    # ccxt 4.5.84 binance.py urls['demo'], what Exchanges' trade client holds after enable_demo_trading(True)
    urls = {"api": {"fapiPrivate": "https://demo-fapi.binance.com/fapi/v1"}}

    async def fapiPublicGetPremiumIndex(self, req=None):
        """GET /fapi/v1/premiumIndex on the demo host (Mark-Price doc): the demo book's mark is its price here."""
        s = self._by_raw((req or {}).get("symbol"))
        return {"symbol": raw(s), "markPrice": str(self.sym[s]["price"]), "indexPrice": str(self.sym[s]["price"]),
                "time": self.now_ms()}

    async def fapiPrivatePostListenKey(self, req=None):
        """POST /fapi/v1/listenKey (weight 1): "If the account has an active listenKey, that listenKey will be returned
        and its validity will be extended for 60 minutes", else a new one (binance-connector-python,
        derivatives_trading_usds_futures rest_api/api/user_data_streams_api.py). One key per account."""
        self.listen_key_calls.append("POST")
        if not self.listen_key:
            n = sum(c == "new" for c in self.listen_key_calls) + 1
            self.listen_key_calls.append("new")
            self.listen_key = ("fakeListenKeyDoNotLog0123456789abcdef" if n == 1
                               else f"fakeListenKey{n}DoNotLog0123456789")
        return {"listenKey": self.listen_key}

    async def fapiPrivatePutListenKey(self, req=None):
        """PUT /fapi/v1/listenKey (weight 1; no key sent: the account's): extends the active key 60 min and answers
        {"listenKey"} (the SDK's KeepaliveUserDataStreamResponse); none active: -1125 (fake_errors)."""
        self.listen_key_calls.append("PUT")
        if not self.listen_key:
            raise err.listen_key_missing()
        return {"listenKey": self.listen_key}

    async def fapiPrivateDeleteListenKey(self, req=None):
        raise AssertionError("the desk never DELETEs the listenKey: one per account, shared by every process (#54)")

    async def fapiPrivateGetIncome(self, req=None):
        """GET /fapi/v1/income (Get-Income-History doc): startTime and endTime inclusive, oldest first, `limit` 100 by
        default and 1000 at most; tranId is unique within an incomeType. Rows are stamped to the whole second
        (probe_result 395, fake_engine._fill), so a startTime at a fill's own ms misses that fill's rows."""
        r = req or {}
        a, b = r.get("startTime"), r.get("endTime")
        rows = sorted((i for i in self.income if (a is None or i["time"] >= a) and (b is None or i["time"] <= b)
                       and r.get("symbol") in (None, i["symbol"]) and r.get("incomeType") in (None, i["incomeType"])),
                      key=lambda i: i["time"])
        return [dict(i) for i in rows[:min(int(r.get("limit") or 100), 1000)]]

    def _position(self, s: str, b: dict) -> dict:
        iso = b.get("margin", "ISOLATED") == "ISOLATED"
        n, upnl = b["qty"] * b["price"], (b["entry"] - b["price"]) * b["qty"]
        mmr, cum = self.bracket(s, n)
        wallet = self.isolated_wallet(s) if iso else 0.0
        liq = self.liq_price(s)
        # Position-Information-V3 fields as Binance's SDK models them (PositionInformationV3ResponseInner); numbers are
        # strings, adl an integer. isolatedMargin = isolatedWallet + unRealizedProfit (ccxt's coin-M sample adds up);
        # maintMargin = notional x MMR - cum (FAQ "Leverage and Margin of USDⓈ-M Futures"); "0" margins when crossed
        info = {"symbol": raw(s), "positionSide": "BOTH", "positionAmt": str(-b["qty"]), "entryPrice": str(b["entry"]),
                "breakEvenPrice": str(b["entry"]), "markPrice": str(b["price"]), "unRealizedProfit": str(upnl),
                "liquidationPrice": str(liq), "isolatedMargin": str(wallet + upnl if iso else 0),
                "notional": str(-n), "marginAsset": "USDT", "isolatedWallet": str(wallet),
                "initialMargin": str(n / b["lev"]), "maintMargin": str(n * mmr - cum),
                "positionInitialMargin": str(n / b["lev"]), "openOrderInitialMargin": "0", "adl": b.get("adl", 0),
                "bidNotional": "0", "askNotional": "0", "updateTime": self.now_ms()}
        # ccxt parse_position_risk: v3 has no marginType, so 'cross' iff isolatedMargin == "0"; notional absolute;
        # maintenanceMargin = its cached bracket's rate x notional (no cum); leverage None (v3 drops it)
        return {"info": info, "symbol": s if s in self.markets else raw(s), "contracts": b["qty"], "side": "short",
                "entryPrice": b["entry"], "markPrice": b["price"], "notional": n, "leverage": None,
                "unrealizedPnl": upnl, "liquidationPrice": liq or None, "marginMode": "isolated" if iso else "cross",
                "collateral": wallet + upnl if iso else 0.0, "maintenanceMargin": n * mmr, "hedged": False}

    async def fetch_positions(self):
        """ccxt 4.5.84 fetch_positions: GET /fapi/v3/positionRisk (no `leverage`: v3 drops the configuration fields,
        change log); a market the client hasn't loaded comes keyed by its raw id (safe_market)."""
        return [self._position(s, b) for s, b in self.sym.items() if b["qty"]]

    async def set_margin_mode(self, m, s):
        """POST /fapi/v1/marginType as ccxt 4.5.84 set_margin_mode sends it. With an open order on the symbol Binance
        answers -4067 before it would say "no need to change" (live demo 2026-10-01, MOVR split entry); -4168 under
        Multi-Assets mode; -4046 no change (ccxt raises MarginModeAlreadySet); -4048 a position exists."""
        b = self._spec(s)
        want = "ISOLATED" if m.lower() == "isolated" else "CROSSED"
        if any(o["symbol"] == s and o["status"] == "open" for o in self.limits.values()):
            raise err.open_orders_block_margin_mode()
        if self.multi_assets and want == "ISOLATED":
            raise err.isolated_under_multi_assets()
        if b.get("margin", "ISOLATED") == want:
            raise err.margin_mode_already_set()
        if b["qty"]:
            raise err.margin_type_position_exists()
        b["margin"] = want
        return {"code": 200, "msg": "success"}

    async def fapiPrivateGetSymbolConfig(self, req=None):
        """GET /fapi/v1/symbolConfig (USER_DATA, weight 5): marginType "ISOLATED" / "CROSSED" per symbol (the doc's
        sample via Binance's SDK and JKorf/Binance.Net's fixture: CROSSED, isAutoAddMargin "false", leverage 21,
        maxNotionalValue "1000000"). A symbol not listed: -1121 (inferred, as leverageBracket)."""
        s = (req or {}).get("symbol")
        rows = [{"symbol": raw(k), "marginType": b.get("margin", "ISOLATED"), "isAutoAddMargin": "false",
                 "leverage": b["lev"], "maxNotionalValue": "1000000"}
                for k, b in self.sym.items() if s in (None, raw(k))]
        if s and not rows:
            raise err.invalid_symbol()
        return rows

    async def fapiPrivateGetMultiAssetsMargin(self, req=None):
        """GET /fapi/v1/multiAssetsMargin (USER_DATA, weight 30): {"multiAssetsMargin": true} = Multi-Assets Mode."""
        return {"multiAssetsMargin": self.multi_assets}

    async def fapiPrivateGetAdlQuantile(self, req=None):
        """GET /fapi/v1/adlQuantile (USER_DATA, weight 5). Doc notes (as Binance's SDK carries them): values update
        every 30 s; 0-4 = the ADL queue position from low to high; One-way Mode answers LONG, SHORT and BOTH (ours:
        BOTH; LONG/SHORT 0 as in ccxt's sample, inferred). Without `symbol` a list of the symbols with positions, []
        when flat (demo, read-only call 2026-10-02); with one, a single object (JKorf/Binance.Net #1107)."""
        if self.adl_answer is not None:
            return self.adl_answer
        rows = [{"symbol": raw(s), "adlQuantile": {"LONG": 0, "SHORT": 0, "BOTH": b.get("adl", 0)}}
                for s, b in self.sym.items() if b["qty"]]
        s = (req or {}).get("symbol")
        if s is None:
            return rows
        return next((r for r in rows if r["symbol"] == s), {"symbol": s, "adlQuantile": {"LONG": 0, "SHORT": 0,
                                                                                            "BOTH": 0}})

    async def fapiPrivateGetForceOrders(self, req=None):
        """GET /fapi/v1/forceOrders (USER_DATA; weight 20 with symbol, 50 without): the user's liquidation and ADL
        orders; autoCloseType "LIQUIDATION" / "ADL" (both when not sent); without startTime, 7 days before endTime.
        Fields: Binance's SDK (UsersForceOrdersResponseInner)."""
        r = req or {}
        end = int(r.get("endTime") or self.now_ms())
        start = int(r.get("startTime") or end - WEEK_MS)
        return [dict(o) for kind, o in self.force_orders if r.get("autoCloseType") in (None, kind)
                and r.get("symbol") in (None, o["symbol"]) and start <= o["time"] <= end]

    def force_close(self, s=SYM, kind="ADL"):
        """Binance closes the whole short itself: ADL (client id adl_autoclose) or a liquidation (autoclose-<n>, plus
        its INSURANCE_CLEAR fee); client ids per the ORDER_TRADE_UPDATE doc (quoted in ccxt pro binance.py ~492).
        Every open order of the symbol is cancelled (ADL FAQ: "for the same token (in isolated margin mode)")."""
        b = self.sym[s]
        for o in self.limits.values():
            if o["symbol"] == s and o["status"] == "open":
                o["status"] = "canceled"
        for i in [i for i in [*self.stops, *self.prearms] if self._algo_sym(i) == s]:
            self._retire(i, "CANCELED")
        cid = "adl_autoclose" if kind == "ADL" else f"autoclose-{self.now_ms()}"
        i = self._record_market(s, "buy", b["qty"], True, cid)
        o = self.market_orders[i]
        if kind == "LIQUIDATION":   # stamped to the second like the trade rows (inferred: 395 measured those only)
            self.income.append({"symbol": raw(s), "incomeType": "INSURANCE_CLEAR", "income": "-0.5", "asset": "USDT",
                                "time": o["ts"] // 1000 * 1000, "tranId": int(i), "tradeId": ""})
        self.force_orders.append((kind, {
            "orderId": int(i), "symbol": raw(s), "status": "FILLED", "clientOrderId": cid, "price": str(o["avg"]),
            "avgPrice": str(o["avg"]), "origQty": str(o["amount"]), "executedQty": str(o["amount"]),
            "cumQuote": str(o["amount"] * o["avg"]), "timeInForce": "IOC", "type": "LIMIT", "reduceOnly": False,
            "closePosition": False, "side": "BUY", "positionSide": "BOTH", "stopPrice": "0",
            "workingType": "CONTRACT_PRICE", "origType": "LIMIT", "time": o["ts"], "updateTime": o["ts"]}))
        return i
