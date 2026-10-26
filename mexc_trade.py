#!/usr/bin/env python3
"""
Signed MEXC SPOT trading client -- stdlib only, synchronous.

This is a deliberate port of the proven parts of /opt/basis-trade/exchange_client.py
and auth.py, which have been trading MEXC spot live on this box. Reimplementing the
signing from the docs would have meant rediscovering the same failure modes, so the
hard-won details are carried over verbatim in behaviour:

  * CLOCK SKEW (MEXC code 700003). Signed requests carry a millisecond timestamp and
    MEXC rejects anything outside recvWindow. A VPS whose clock drifts, or an ntpd
    restart, otherwise makes EVERY signed call fail -- including the exits. sync_time()
    measures server_time - local_time once at startup and periodically, and every
    signature applies the offset.
  * CONTENT TYPE (MEXC code 700013). Signed endpoints reject anything but
    application/json, and every parameter must ride in the QUERY STRING, never a form
    body. The signature covers the query string exactly as sent.
  * AMBIGUOUS ORDERS. A timeout, a 5xx, or an unparseable 2xx on an ORDER endpoint
    means the order may or may not have landed. Treating that as failure is how bots
    end up double-buying. Those raise AmbiguousOrderError, which the caller must
    resolve by looking the order up by its client id rather than retrying blindly.
  * CLIENT ORDER IDs. Every order carries one, so after a crash or an ambiguous
    response the order can be found again. get_order_by_client_id() returns None
    (not an error) when MEXC has never seen it -- i.e. it definitively never landed.
  * RATE LIMITS. 429/418 back off before raising, so a caller's retry does not walk
    straight into a longer ban.

Two MEXC-specific conveniences this strategy leans on:
  * MARKET BUY by quoteOrderQty -- ask for "$10 of this coin" and MEXC sizes it. That
    removes all base-quantity rounding from the entry, which matters at $2-$10 lots.
  * The buy fee is charged IN THE BASE ASSET, so the credited quantity is slightly
    less than executedQty. Selling executedQty would then fail for insufficient
    balance. sellable_qty() reads the actual free balance and rounds down to the
    symbol's step, which is the only reliable way to size the exit.

Credentials come from the environment and are never logged:
    MEXC_API_KEY, MEXC_API_SECRET
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN

BASE = "https://api.mexc.com"
RECV_WINDOW_MS = 5000
log = logging.getLogger("mexc_trade")


class ExchangeError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class AmbiguousOrderError(ExchangeError):
    """The order may or may not exist on the venue. Resolve, never blind-retry."""


def _dec(v, default="0"):
    if v is None or v == "":
        return Decimal(default)
    try:
        return Decimal(str(v))
    except Exception:                       # noqa: BLE001
        return Decimal(default)


class SymbolInfo:
    __slots__ = ("symbol", "base", "quote", "step", "tick", "min_quote")

    def __init__(self, symbol, base, quote, step, tick, min_quote):
        self.symbol, self.base, self.quote = symbol, base, quote
        self.step, self.tick, self.min_quote = step, tick, min_quote

    def round_qty(self, qty):
        """Round DOWN to the tradeable step -- never round up into a rejection."""
        if self.step <= 0:
            return qty
        return (Decimal(qty) / self.step).to_integral_value(ROUND_DOWN) * self.step

    def round_price(self, px):
        if self.tick <= 0:
            return px
        return (Decimal(px) / self.tick).to_integral_value(ROUND_DOWN) * self.tick


class Order:
    __slots__ = ("symbol", "order_id", "client_id", "side", "status", "price",
                 "orig_qty", "executed_qty", "quote_qty", "avg_price", "raw")

    def __init__(self, p):
        self.raw = p
        self.symbol = p.get("symbol", "")
        self.order_id = str(p.get("orderId", ""))
        self.client_id = str(p.get("clientOrderId", "") or "")
        self.side = p.get("side", "")
        self.status = p.get("status", "NEW")
        self.price = _dec(p.get("price"))
        self.orig_qty = _dec(p.get("origQty"))
        self.executed_qty = _dec(p.get("executedQty"))
        self.quote_qty = _dec(p.get("cummulativeQuoteQty"))
        self.avg_price = (self.quote_qty / self.executed_qty
                          if self.executed_qty > 0 else Decimal(0))

    @property
    def is_open(self):
        return self.status in ("NEW", "PARTIALLY_FILLED")

    @property
    def is_filled(self):
        return self.status == "FILLED"

    def __repr__(self):
        return (f"<Order {self.symbol} {self.side} {self.status} "
                f"exec={self.executed_qty} @ {self.avg_price}>")


class MexcSpot:
    def __init__(self, api_key=None, api_secret=None, base=BASE, timeout=15):
        self.key = api_key if api_key is not None else os.environ.get("MEXC_API_KEY", "")
        self.secret = (api_secret if api_secret is not None
                       else os.environ.get("MEXC_API_SECRET", ""))
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._offset_ms = 0
        self._symbols = {}

    # ---------------- plumbing ----------------
    @property
    def authenticated(self):
        return bool(self.key and self.secret)

    def _sign(self, params):
        if not self.authenticated:
            raise ExchangeError("no MEXC credentials configured (MEXC_API_KEY/SECRET)")
        p = dict(params)
        p["timestamp"] = str(int(time.time() * 1000) + self._offset_ms)
        p["recvWindow"] = str(RECV_WINDOW_MS)
        q = urllib.parse.urlencode(p)
        sig = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        return f"{q}&signature={sig}"

    def _request(self, method, path, params=None, signed=False, order_endpoint=False,
                 tries=3):
        """
        Returns the parsed payload. Raises ExchangeError, or AmbiguousOrderError when
        an ORDER endpoint fails in a way that leaves the order's existence unknown.
        """
        err_cls = AmbiguousOrderError if order_endpoint else ExchangeError
        last = None
        for attempt in range(tries):
            if signed:
                url = f"{self.base}{path}?{self._sign(params or {})}"
                headers = {"X-MEXC-APIKEY": self.key,
                           "Content-Type": "application/json"}
            else:
                url = f"{self.base}{path}"
                if params:
                    url += "?" + urllib.parse.urlencode(params)
                headers = {}
            headers["User-Agent"] = "provision-exec/1.0"
            req = urllib.request.Request(url, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read().decode("utf-8", "replace")
                    status = r.status
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                status = e.code
                if status in (429, 418):
                    delay = float(e.headers.get("Retry-After") or 2.0)
                    log.warning("rate limited HTTP %s, backing off %.1fs", status, delay)
                    time.sleep(min(delay, 30.0))
                    last = ExchangeError(f"rate limited HTTP {status}")
                    continue
                if status >= 500:
                    last = err_cls(f"HTTP {status}: {body[:200]}")
                    if order_endpoint:
                        raise last          # never retry an ambiguous order
                    time.sleep(0.5 * (attempt + 1))
                    continue
            except Exception as e:          # noqa: BLE001 - timeouts, DNS, resets
                # On an order endpoint the request may have been processed.
                raise err_cls(f"transport error: {e!r}") from e

            try:
                payload = json.loads(body) if body else {}
            except ValueError:
                raise err_cls(f"non-JSON response (HTTP {status}): {body[:200]}")

            if status >= 400:
                code = payload.get("code") if isinstance(payload, dict) else None
                msg = payload.get("msg") if isinstance(payload, dict) else body[:200]
                raise ExchangeError(str(msg), code)
            if (isinstance(payload, dict) and "code" in payload and "msg" in payload
                    and payload.get("code") not in (0, 200, "0", "200", None)):
                raise ExchangeError(str(payload["msg"]), payload.get("code"))
            return payload
        raise last or ExchangeError("request failed")

    # ---------------- unsigned ----------------
    def sync_time(self):
        """Align to MEXC's clock. Without this, drift silently kills every exit."""
        try:
            p = self._request("GET", "/api/v3/time")
            self._offset_ms = int(p["serverTime"]) - int(time.time() * 1000)
            if abs(self._offset_ms) > 1000:
                log.warning("MEXC clock skew %dms -- applying offset", self._offset_ms)
        except Exception as e:              # noqa: BLE001
            log.warning("time sync failed, keeping offset %s: %s", self._offset_ms, e)
        return self._offset_ms

    def exchange_info(self, force=False):
        if self._symbols and not force:
            return self._symbols
        p = self._request("GET", "/api/v3/exchangeInfo")
        out = {}
        for s in p.get("symbols", []):
            if not s.get("isSpotTradingAllowed"):
                continue
            if str(s.get("status")) not in ("1", "ENABLED", "TRADING"):
                continue
            qscale = int(s.get("baseAssetPrecision", 8) or 8)
            pscale = int(s.get("quotePrecision", s.get("quoteAssetPrecision", 8)) or 8)
            out[s["symbol"]] = SymbolInfo(
                symbol=s["symbol"], base=s.get("baseAsset", ""),
                quote=s.get("quoteAsset", ""),
                step=Decimal(1).scaleb(-qscale),
                tick=Decimal(1).scaleb(-pscale),
                min_quote=_dec(s.get("quoteAmountPrecision"), "1"))
        self._symbols = out
        return out

    def price(self, symbol):
        p = self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return _dec(p.get("price"))

    def prices(self):
        """All last prices in one call -- one request per polling tick, not N."""
        p = self._request("GET", "/api/v3/ticker/price")
        return {x["symbol"]: _dec(x.get("price")) for x in p if x.get("symbol")}

    def book_ticker(self, symbol=None):
        params = {"symbol": symbol} if symbol else None
        p = self._request("GET", "/api/v3/ticker/bookTicker", params)
        rows = p if isinstance(p, list) else [p]
        return {x["symbol"]: (_dec(x.get("bidPrice")), _dec(x.get("askPrice")))
                for x in rows if x.get("symbol")}

    # ---------------- signed ----------------
    def account(self):
        return self._request("GET", "/api/v3/account", {}, signed=True)

    def balances(self):
        """asset -> (free, locked), only non-zero."""
        out = {}
        for b in self.account().get("balances", []):
            free, locked = _dec(b.get("free")), _dec(b.get("locked"))
            if free > 0 or locked > 0:
                out[b["asset"]] = (free, locked)
        return out

    def free_balance(self, asset):
        return self.balances().get(asset, (Decimal(0), Decimal(0)))[0]

    def sellable_qty(self, symbol, want=None):
        """
        How much we can actually sell: the free base balance rounded DOWN to the step.

        The buy fee is taken in the base asset, so the credited amount is below the
        order's executedQty. Selling executedQty is the classic insufficient-balance
        rejection; this reads reality instead.
        """
        info = self.exchange_info().get(symbol)
        if info is None:
            raise ExchangeError(f"unknown symbol {symbol}")
        free = self.free_balance(info.base)
        qty = min(free, Decimal(want)) if want is not None else free
        return info.round_qty(qty)

    def market_buy_quote(self, symbol, quote_usd, client_id=None):
        """Spend exactly `quote_usd` -- sidesteps base-quantity rounding entirely."""
        params = {"symbol": symbol, "side": "BUY", "type": "MARKET",
                  "quoteOrderQty": format(Decimal(quote_usd), "f")}
        if client_id:
            params["newClientOrderId"] = client_id
        return Order(self._request("POST", "/api/v3/order", params, signed=True,
                                   order_endpoint=True))

    def limit_sell(self, symbol, qty, price, client_id=None):
        info = self.exchange_info().get(symbol)
        if info is not None:
            qty = info.round_qty(qty)
            price = info.round_price(price)
        if qty <= 0:
            raise ExchangeError(f"{symbol}: sell qty rounds to zero")
        params = {"symbol": symbol, "side": "SELL", "type": "LIMIT",
                  "quantity": format(Decimal(qty), "f"),
                  "price": format(Decimal(price), "f")}
        if client_id:
            params["newClientOrderId"] = client_id
        return Order(self._request("POST", "/api/v3/order", params, signed=True,
                                   order_endpoint=True))

    def market_sell(self, symbol, qty, client_id=None):
        info = self.exchange_info().get(symbol)
        if info is not None:
            qty = info.round_qty(qty)
        if qty <= 0:
            raise ExchangeError(f"{symbol}: sell qty rounds to zero")
        params = {"symbol": symbol, "side": "SELL", "type": "MARKET",
                  "quantity": format(Decimal(qty), "f")}
        if client_id:
            params["newClientOrderId"] = client_id
        return Order(self._request("POST", "/api/v3/order", params, signed=True,
                                   order_endpoint=True))

    def get_order(self, symbol, order_id):
        return Order(self._request("GET", "/api/v3/order",
                                   {"symbol": symbol, "orderId": str(order_id)},
                                   signed=True))

    def get_order_by_client_id(self, symbol, client_id):
        """None means MEXC has never seen it -- the order definitively never landed."""
        try:
            p = self._request("GET", "/api/v3/order",
                              {"symbol": symbol, "origClientOrderId": client_id},
                              signed=True)
        except ExchangeError as e:
            if e.code in (-2013, "-2013") or "does not exist" in str(e).lower():
                return None
            raise
        return Order(p)

    def cancel_order(self, symbol, order_id):
        return Order(self._request("DELETE", "/api/v3/order",
                                   {"symbol": symbol, "orderId": str(order_id)},
                                   signed=True, order_endpoint=True))

    def open_orders(self, symbol=None):
        params = {"symbol": symbol} if symbol else {}
        p = self._request("GET", "/api/v3/openOrders", params, signed=True)
        return [Order(o) for o in (p or [])]

    def my_trades(self, symbol, limit=100):
        return self._request("GET", "/api/v3/myTrades",
                             {"symbol": symbol, "limit": str(limit)}, signed=True)


if __name__ == "__main__":
    # Read-only self-check. Signed calls are attempted only if credentials exist.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    c = MexcSpot()
    print("authenticated:", c.authenticated)
    print("clock offset  :", c.sync_time(), "ms")
    info = c.exchange_info()
    print("tradeable spot symbols:", len(info))
    s = info.get("BTCUSDT")
    if s:
        print(f"BTCUSDT step={s.step} tick={s.tick} min_quote={s.min_quote}")
    print("BTCUSDT price :", c.price("BTCUSDT"))
    if c.authenticated:
        try:
            b = c.balances()
            print("balances (non-zero):", {k: str(v[0]) for k, v in list(b.items())[:8]})
            print("open orders:", len(c.open_orders()))
        except ExchangeError as e:
            print("signed call failed:", e)
    else:
        print("set MEXC_API_KEY / MEXC_API_SECRET to test signed endpoints")
