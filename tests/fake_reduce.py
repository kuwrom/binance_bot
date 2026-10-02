"""Reduce-only LIMIT orders on the fake (mixed into FakeCcxt via Engine): the BUY limits a short's resting plan targets
are (#65). Sources:
- binance.com FAQ "Summary of Failed Orders in Binance Futures" (support/faq/360039707291): reduce-only orders compete.
  "Higher priority reduce-only limit orders will cause low-priority reduce-only orders to be canceled"; priority =
  the more aggressive price (a BUY: the higher one) with a quantity greater than the position's remaining quantity;
  its example: long 100, reduce-only SELL 50 @ 1000 then 60 @ 999 -> the first is canceled. "Market Reduce-only order
  with quantity equal to or greater than the open position and stop market Reduce-only order will have the highest
  priority". A reduce-only order is rejected when "the order size exceeds the open position".
- dev.binance.vision/t/802 (Binance staff, 2020-10-18), why a futures LIMIT order is EXPIRED: "The reduceOnly open
  order is expired due to current position has been closed"; "replaced by another reduceOnly open order with better
  price".
Inferred (the docs don't say): a partial market close (below the position) outranks every limit (its price is the
most aggressive); equal prices rank by time; a newcomer that doesn't fit is rejected with -2022; a reduce-only fill
is capped at the position, and the rest expires once flat; an order left resting at flat (`ro_expire_on_flat` off,
the open question the desk is safe against) never fills without a position. Untriggered conditional orders (Algo
service) take no part: nothing documents that they do."""

from __future__ import annotations

from tests import fake_errors as err


class ReduceOnly:
    ro_expire_on_flat = True   # dev.binance.vision/t/802; a test turns it off to check the desk cancels on flat

    def _ro_open(self, s: str) -> list[tuple[str, dict]]:
        """Open reduce-only BUY limits on s, highest priority first: the higher price, then the older order."""
        rows = [(i, o) for i, o in self.limits.items()
                if o["symbol"] == s and o["status"] == "open" and o["reduce"] and o["side"] == "buy"]
        return sorted(rows, key=lambda r: (-r[1]["price"], int(r[0])))

    def _ro_compete(self, s: str, qty: float, price: float | None) -> None:
        """A new reduce-only BUY of `qty` (price None: a market order) against the resting ones: whatever no longer
        fits the position, lowest priority first, expires; a newcomer that doesn't fit itself is rejected."""
        pos = self.sym[s]["qty"]
        if price is None and qty >= pos - 1e-12:
            return   # a closing market order: highest priority, and the position's close expires the rest
        newest = float("inf")   # at its own price the newcomer ranks after the older orders
        ranked = [(float("inf") if price is None else price, newest, None, qty)]
        ranked += [(o["price"], int(i), i, o["amount"] - o["filled"]) for i, o in self._ro_open(s)]
        ranked.sort(key=lambda r: (-r[0], r[1]))
        used, drop, fits_new = 0.0, [], True
        for _, _, i, q in ranked:
            if used + q <= pos + 1e-12:
                used += q
            elif i is None:
                fits_new = False
            else:
                drop.append(i)
        if not fits_new:
            raise err.reduce_only_rejected()
        for i in drop:
            self.limits[i]["status"] = "expired"

    def _ro_flat(self, s: str) -> None:
        """The position closed: its reduce-only limits expire."""
        if self.ro_expire_on_flat:
            for _, o in self._ro_open(s):
                o["status"] = "expired"

    def _walk_down(self, s: str, old: float, p: float) -> None:
        """Price falls from old to p: resting BUY limits at or above p fill at their own price, highest first. A
        reduce-only one fills at most the position; with none left it expires unfilled."""
        b = self.sym[s]
        for i, o in [(i, o) for i, o in self._ro_open(s) if o["price"] >= p]:
            if o["status"] != "open":
                continue   # expired meanwhile: an earlier fill closed the position
            b["price"] = min(o["price"], old)
            self._ro_exec(i)

    def _ro_exec(self, i: str) -> None:
        o = self.limits[i]
        q = min(o["amount"] - o["filled"], self.sym[o["symbol"]]["qty"])
        if q <= 0:
            o["status"] = "expired"
            return
        self._exec_limit(i, q)
