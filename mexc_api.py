#!/usr/bin/env python3
"""
Stdlib-only MEXC contract (perpetual futures) public API client.

Deliberately mirrors the conventions of this repo's Hyperliquid helpers: no pandas,
no requests, no pip installs, and every call retries with backoff and never returns
half-parsed data. Public endpoints only -- the provision paper bot places no orders,
so no API key is needed or wanted.

Endpoints used:
  /contract/detail                     universe, fees, contract size, apiAllowed
  /contract/kline/{symbol}             hourly OHLCV  (Min60, ~1440 bars/request)
  /contract/depth/{symbol}             order book, for queue-ahead measurement
  /contract/deals/{symbol}             recent trades with aggressor side, for fills
  /contract/funding_rate/history       realised funding settlements

The universe filter is copied deliberately from the research pipeline (mexc.py) so
the live universe is the SAME population the edge was measured on. MEXC's recent
listings are dominated by tokenised equities, index and commodity perps, which have
no token-launch dynamics; including them would silently change the strategy.
"""
import json
import time
import urllib.parse
import urllib.request

BASE = "https://contract.mexc.com/api/v1/contract"
INTERVAL = "Min60"
BAR_SECS = 3600
REQUEST_SLEEP = 0.15          # public contract limit is ~20 req/2s

# --- non-crypto instrument filter (identical to mexc.py) ---------------------
TRADFI_TAGS = {
    "mc-trade-zone-tradfi",
    "mc-trade-zone-Stock",
    "mc-trade-zone-stockindex",
    "mc-trade-zone-ETF",
    "mc-trade-zone-Commodities",
    "mc-trade-zone-metalsfutures",
    "mc-trade-zone-forex",
    "mc-trade-zone-energyfutures",
    "mc-trade-zone-agriculturefutures",
}
NONCRYPTO_SYMBOLS = {
    "SPX500", "NAS100", "US30", "HK50", "JP225", "HK0700", "HK1810", "INDEX",
    "COPPER", "UKOIL", "USOIL", "NGAS", "NICKEL", "LEAD", "ALUMINUM", "ZINC",
    "XAU", "XPT", "XPD", "XAG", "XAUT",
    "SPY", "IWM", "ARKK", "SOXX", "SOXL", "SOXS", "SMH", "SQQQ", "TQQQ",
    "TZA", "DRV", "KORU", "UVXY", "XBI", "XLE", "XLK", "XLU", "WOOD", "URNM",
    "EWY", "EWJ", "EWZ", "EWT", "INDA", "SLX", "SHLD", "USO", "MSTU", "TSLL",
    "NVDL", "GGLL", "NVD", "KSTR",
}


class MexcError(Exception):
    pass


def get(path, params=None, tries=4, timeout=30):
    """GET a public contract endpoint, returning the unwrapped `data` payload."""
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "provision-bot/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.load(r)
            if not payload.get("success", False):
                raise MexcError(f"{path}: code={payload.get('code')} "
                                f"msg={payload.get('message')}")
            return payload.get("data")
        except Exception as e:              # noqa: BLE001 - retry then propagate
            last = e
            time.sleep(0.6 * (a + 1))
    raise MexcError(f"{path} failed after {tries} tries: {last}")


def is_crypto_token(contract):
    """True if this is a crypto token perp, not a stock/index/commodity wrapper."""
    symbol = contract["symbol"].replace("_USDT", "")
    tags = set(contract.get("conceptPlate") or [])
    if tags & TRADFI_TAGS:
        return False, "tradfi_tag"
    if symbol.endswith("STOCK"):
        return False, "stock_symbol"
    if symbol in NONCRYPTO_SYMBOLS:
        return False, "noncrypto_symbol"
    return True, None


def contract_detail():
    """Raw contract list."""
    return get("/detail")


def new_listings(from_days, to_days, detail=None, now=None):
    """
    Crypto USDT perps whose listing age is within [from_days, to_days].

    Launch time is openingTime rounded UP to the next hourly boundary, matching the
    research pipeline so that day counts mean the same thing here as in the study.
    Returns a dict keyed by contract symbol (e.g. "PONS_USDT").
    """
    detail = detail if detail is not None else contract_detail()
    now = now if now is not None else time.time()
    out = {}
    for c in detail:
        if c.get("state") != 0:                      # 0 = live/enabled
            continue
        if c.get("settleCoin") != "USDT" or c.get("quoteCoin") != "USDT":
            continue
        if c.get("preMarket"):
            continue
        ok, _ = is_crypto_token(c)
        if not ok:
            continue
        opening = c.get("openingTime") or c.get("createTime")
        if not opening:
            continue
        opening_ts = opening // 1000
        launch_ts = -(-opening_ts // BAR_SECS) * BAR_SECS     # ceil to next bar
        age = (now - launch_ts) / 86400.0
        if not (from_days <= age <= to_days):
            continue
        out[c["symbol"]] = {
            "contract": c["symbol"],
            "base": c["symbol"].replace("_USDT", ""),
            "launch_ts": launch_ts,
            "age_days": age,
            "contract_size": float(c.get("contractSize") or 1.0),
            "maker_fee": float(c.get("makerFeeRate") or 0.0),
            "taker_fee": float(c.get("takerFeeRate") or 0.0),
            "api_allowed": bool(c.get("apiAllowed")),
            "max_leverage": c.get("maxLeverage"),
            "price_scale": c.get("priceScale"),
            "price_unit": float(c.get("priceUnit") or 0.0),
            "vol_unit": float(c.get("volUnit") or 1.0),
            "tags": "|".join(c.get("conceptPlate") or []),
        }
    return out


def klines(symbol, start_ts, end_ts, interval=INTERVAL):
    """
    Hourly candles as a list of (ts_sec, open, high, low, close, vol_contracts),
    ascending. `vol` is in CONTRACTS -- multiply by contract_size and price for USD.
    """
    d = get(f"/kline/{symbol}", {"interval": interval,
                                 "start": int(start_ts), "end": int(end_ts)})
    times = (d or {}).get("time") or []
    rows = []
    for i, t in enumerate(times):
        try:
            rows.append((int(t), float(d["open"][i]), float(d["high"][i]),
                         float(d["low"][i]), float(d["close"][i]), float(d["vol"][i])))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    rows.sort()
    return rows


def depth(symbol, limit=200):
    """Order book as (bids, asks); each a list of (price, contracts, n_orders)."""
    d = get(f"/depth/{symbol}", {"limit": limit}) or {}
    def side(key):
        out = []
        for lvl in (d.get(key) or []):
            try:
                out.append((float(lvl[0]), float(lvl[1]),
                            int(lvl[2]) if len(lvl) > 2 else 1))
            except (TypeError, ValueError, IndexError):
                continue
        return out
    return side("bids"), side("asks")


def deals(symbol, limit=100):
    """
    Recent trades, newest first: list of dicts with
      t   trade time (ms)
      p   price
      v   size in contracts
      T   aggressor: 1 = buy (lifted an ask), 2 = sell (hit a bid)
      i   trade id (for de-duplication across polls)
    A resting BUY at level L fills only when SELL-aggressor volume prints at px <= L.
    """
    d = get(f"/deals/{symbol}", {"limit": limit}) or []
    out = []
    for x in d:
        try:
            out.append({"t": int(x["t"]), "p": float(x["p"]), "v": float(x["v"]),
                        "T": int(x.get("T", 0)), "i": str(x.get("i", ""))})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def funding_history(symbol, page_size=100, max_pages=3):
    """
    Realised funding settlements, newest first: list of (settle_ts_sec, rate).
    A POSITIVE rate is paid BY longs TO shorts.
    """
    out = []
    for page in range(1, max_pages + 1):
        d = get("/funding_rate/history", {"symbol": symbol, "page_num": page,
                                          "page_size": page_size})
        rows = (d or {}).get("resultList") or []
        if not rows:
            break
        for r in rows:
            try:
                out.append((int(r["settleTime"]) // 1000, float(r["fundingRate"])))
            except (KeyError, TypeError, ValueError):
                continue
        if len(rows) < page_size:
            break
        time.sleep(REQUEST_SLEEP)
    return out


def queue_ahead_usd(bids, level, contract_size):
    """
    USD resting at or better than our bid price -- i.e. what must be consumed before
    a new order at `level` can fill.

    Bids priced ABOVE our level are hit first; bids AT our level were queued before
    us, so they also count. This is a conservative first-order model: it ignores
    cancellations (which help us) and new orders joining ahead (which hurt).
    """
    total = 0.0
    for px, vol, _ in bids:
        if px >= level:
            total += px * vol * contract_size
    return total
