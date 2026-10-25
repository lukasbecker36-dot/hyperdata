#!/usr/bin/env python3
"""
Stdlib-only SIGNED client for MEXC contract (perpetual futures) trading.

mexc_api.py is the public, read-only half; this is the half that can place orders, and
it is deliberately separate so nothing that only needs market data ever imports a key.

Auth (MEXC contract API): every private request carries
    ApiKey        the access key
    Request-Time  ms timestamp
    Signature     hex HMAC-SHA256(secret, access_key + request_time + param_string)
where param_string is, for GET, the query parameters sorted by key and joined as
k=v&k=v (url-encoded), and for POST the exact JSON body string sent.

Keys come from the environment (MEXC_FUTURES_KEY / MEXC_FUTURES_SECRET), never from the
repo. Create them with futures-trading permission only (no withdrawal) and bind them to
the server's IP.

Order vocabulary (MEXC's own codes, kept as named constants so call sites read):
    side       1 open long, 2 close short, 3 open short, 4 close long
    type       1 limit, 2 post-only maker, 3 IOC, 4 FOK, 5 market
    openType   1 isolated, 2 cross
    order state (get_order) 1 uninformed, 2 open, 3 filled, 4 cancelled, 5 invalid
"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

BASE = "https://contract.mexc.com"

OPEN_LONG, CLOSE_SHORT, OPEN_SHORT, CLOSE_LONG = 1, 2, 3, 4
LIMIT, POST_ONLY, IOC, FOK, MARKET = 1, 2, 3, 4, 5
ISOLATED, CROSS = 1, 2
ST_OPEN, ST_FILLED, ST_CANCELLED, ST_INVALID = 2, 3, 4, 5


class MexcFuturesError(Exception):
    pass


class MexcFutures:
    def __init__(self, key=None, secret=None, timeout=15):
        self.key = (key or os.environ.get("MEXC_FUTURES_KEY", "")).strip()
        self.secret = (secret or os.environ.get("MEXC_FUTURES_SECRET", "")).strip()
        self.timeout = timeout

    def enabled(self):
        return bool(self.key and self.secret)

    # ---- transport -------------------------------------------------------------------
    def _sign(self, ts, param_string):
        msg = f"{self.key}{ts}{param_string}".encode()
        return hmac.new(self.secret.encode(), msg, hashlib.sha256).hexdigest()

    def _request(self, method, path, params=None, body=None, private=True, tries=1):
        params = params or {}
        url = BASE + path
        data = None
        param_string = ""
        if method == "GET" and params:
            param_string = "&".join(f"{k}={urllib.parse.quote(str(params[k]), safe='')}"
                                    for k in sorted(params))
            url += "?" + param_string
        if method == "POST":
            param_string = json.dumps(body if body is not None else {}, separators=(",", ":"))
            data = param_string.encode()
        headers = {"Content-Type": "application/json", "User-Agent": "unlock-bot/1.0"}
        if private:
            if not self.enabled():
                raise MexcFuturesError("MEXC_FUTURES_KEY / MEXC_FUTURES_SECRET not set")
            ts = str(int(time.time() * 1000))
            headers.update({"ApiKey": self.key, "Request-Time": ts,
                            "Signature": self._sign(ts, param_string)})
        last = None
        for a in range(tries):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.load(r)
                if not payload.get("success", False):
                    raise MexcFuturesError(f"{path}: code={payload.get('code')} "
                                           f"msg={payload.get('message')}")
                return payload.get("data")
            except MexcFuturesError:
                raise                       # an API rejection is an answer, not a blip
            except Exception as e:          # noqa: BLE001 - network: retry
                last = e
                time.sleep(0.5 * (a + 1))
        raise MexcFuturesError(f"{method} {path} failed after {tries} tries: {last}")

    # ---- public ----------------------------------------------------------------------
    def ticker(self, symbol):
        """bid1 / ask1 / lastPrice / fairPrice / fundingRate / volume24 etc."""
        return self._request("GET", "/api/v1/contract/ticker", {"symbol": symbol},
                             private=False, tries=3)

    def contract(self, symbol):
        return self._request("GET", "/api/v1/contract/detail", {"symbol": symbol},
                             private=False, tries=3)

    # ---- private: reads (safe to retry) ----------------------------------------------
    def assets(self):
        return self._request("GET", "/api/v1/private/account/assets", tries=3)

    def usdt_available(self):
        for a in self.assets() or []:
            if a.get("currency") == "USDT":
                return float(a.get("availableBalance") or 0)
        return 0.0

    def open_positions(self, symbol=None):
        return self._request("GET", "/api/v1/private/position/open_positions",
                             {"symbol": symbol} if symbol else None, tries=3) or []

    def get_order(self, order_id):
        return self._request("GET", f"/api/v1/private/order/get/{order_id}", tries=3)

    # ---- private: writes (NOT retried -- a timeout may still have placed the order) --
    def submit(self, symbol, side, vol, price=None, order_type=LIMIT, leverage=1,
               open_type=ISOLATED, position_id=None, external_oid=None):
        body = {"symbol": symbol, "side": side, "vol": vol, "type": order_type,
                "openType": open_type, "leverage": leverage}
        if price is not None:
            body["price"] = price
        if position_id is not None:
            body["positionId"] = position_id
        if external_oid:
            body["externalOid"] = external_oid
        return self._request("POST", "/api/v1/private/order/submit", body=body)

    def cancel(self, order_ids):
        return self._request("POST", "/api/v1/private/order/cancel", body=list(order_ids))


if __name__ == "__main__":
    # Read-only key check: `python3 mexc_futures.py` prints the USDT futures balance and
    # any open positions. Places nothing.
    c = MexcFutures()
    if not c.enabled():
        raise SystemExit("Set MEXC_FUTURES_KEY and MEXC_FUTURES_SECRET first.")
    print("USDT available:", c.usdt_available())
    for p in c.open_positions():
        print(p.get("symbol"), "type", p.get("positionType"), "vol", p.get("holdVol"),
              "avg", p.get("holdAvgPrice"), "liq", p.get("liquidatePrice"))
