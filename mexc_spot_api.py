#!/usr/bin/env python3
"""
Stdlib-only MEXC SPOT public API client, interface-compatible with mexc_api.py.

Why a spot arm exists
---------------------
MEXC's FUTURES order endpoints have been "Under maintenance" since 2022-07-25 and
return 403 at the edge, so the strategy can never be traded live on the perp no
matter what the paper run shows. Spot is different: POST /api/v3/order answers
"api key required", i.e. it is live. And the strategy is long-only, 1x, cash-bounded
with no liquidation path -- which is simply spot with extra steps. The perp wrapper
was charging funding (~3.6bp/trade) for leverage the strategy never used.

Spot also prices better on the leg that matters: maker 0.00%, taker 0.05%, against
the futures schedule of 4bp maker / 10bp taker on brand-new listings.

What this arm is for
--------------------
Everything measured so far about fills -- 48% queue-confirmed, 0% on deep books --
was measured on PERP order books. Spot books are a different instrument. This arm
re-measures the queue on the books that could actually be traded.

Universe: deliberately seeded from the same FUTURES listing dates as the perp arm, so
the two arms trade the same coins over the same Day 3-45 windows and the fill rates
are directly comparable. That is a controlled comparison, not an oversight -- spot
carries far more new listings than futures, and a spot-native universe would confound
venue differences with universe differences. Coins whose spot history starts later
than their futures listing simply produce no candles and drop out on their own.

Mapping notes:
  * spot symbols have no underscore (JIMOTHYUSDT), futures do (JIMOTHY_USDT)
  * spot quantities are in base units, so contract_size is always 1.0
  * kline timestamps arrive in ms and are converted to seconds
  * /trades gives isBuyerMaker; True means the BUYER was the maker, so the trade was
    initiated by a seller. A resting BUY only fills against sell aggressors, so those
    are exactly the prints that count -- mapped to T=2 to match the futures client.
  * spot has no funding, so funding_history() returns []
"""
import json
import time
import urllib.parse
import urllib.request

import mexc_api as fut          # reused for the crypto/tradfi universe filter

SPOT_BASE = "https://api.mexc.com/api/v3"
INTERVAL = "60m"
BAR_SECS = 3600
REQUEST_SLEEP = 0.15


class SpotError(Exception):
    pass


def get(path, params=None, tries=4, timeout=30):
    url = f"{SPOT_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "provision-bot/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:              # noqa: BLE001
            last = e
            time.sleep(0.6 * (a + 1))
    raise SpotError(f"{path} failed after {tries} tries: {last}")


def exchange_info():
    return get("/exchangeInfo")


def contract_detail():
    """Futures detail -- the source of listing dates and the crypto filter."""
    return fut.contract_detail()


def new_listings(from_days, to_days, detail=None, now=None):
    """
    Crypto coins inside the Day [from_days, to_days] window that are spot-tradeable.

    Listing age comes from the FUTURES openingTime so this arm's universe matches the
    perp arm's exactly. Keyed by spot symbol.
    """
    perp = fut.new_listings(from_days, to_days, detail=detail, now=now)
    try:
        info = exchange_info()
    except SpotError:
        return {}
    spot = {}
    for s in info.get("symbols", []):
        if s.get("quoteAsset") != "USDT":
            continue
        if str(s.get("status")) != "1":             # status is the STRING "1"
            continue
        if not s.get("isSpotTradingAllowed"):
            continue
        spot[s["baseAsset"]] = s

    out = {}
    for _, m in perp.items():
        base = m["base"]
        s = spot.get(base)
        if s is None:
            continue
        out[s["symbol"]] = {
            "contract": s["symbol"],            # e.g. JIMOTHYUSDT
            "base": base,
            "launch_ts": m["launch_ts"],        # from futures, for a like-for-like window
            "age_days": m["age_days"],
            "contract_size": 1.0,               # spot trades base units directly
            "maker_fee": float(s.get("makerCommission") or 0.0),
            "taker_fee": float(s.get("takerCommission") or 0.0005),
            "api_allowed": True,                # spot order endpoint is live for all of these
            "max_leverage": 1,
            "min_quote": float(s.get("quoteAmountPrecision") or 1.0),
            "base_step": s.get("baseSizePrecision"),
            "tags": m.get("tags", ""),
        }
    return out


def klines(symbol, start_ts, end_ts, interval=INTERVAL):
    """(ts_sec, open, high, low, close, base_volume) ascending. Max 1000 bars."""
    d = get("/klines", {"symbol": symbol, "interval": interval,
                        "startTime": int(start_ts) * 1000,
                        "endTime": int(end_ts) * 1000, "limit": 1000})
    rows = []
    if not isinstance(d, list):
        return rows
    for k in d:
        try:
            rows.append((int(k[0]) // 1000, float(k[1]), float(k[2]),
                         float(k[3]), float(k[4]), float(k[5])))
        except (IndexError, TypeError, ValueError):
            continue
    rows.sort()
    return rows


def depth(symbol, limit=200):
    """(bids, asks), each a list of (price, base_qty, n_orders)."""
    lim = min(max(int(limit), 5), 5000)
    d = get("/depth", {"symbol": symbol, "limit": lim}) or {}
    def side(key):
        out = []
        for lvl in (d.get(key) or []):
            try:
                out.append((float(lvl[0]), float(lvl[1]), 1))
            except (TypeError, ValueError, IndexError):
                continue
        return out
    return side("bids"), side("asks")


def deals(symbol, limit=100):
    """
    Recent trades mapped to the futures client's shape:
      t ms, p price, v base qty, T aggressor (2 = sell), i trade id

    isBuyerMaker=True  -> the buyer was resting, the SELLER crossed  -> T=2
    isBuyerMaker=False -> the seller was resting, the BUYER crossed  -> T=1
    Only T=2 can fill our resting bid.
    """
    d = get("/trades", {"symbol": symbol, "limit": min(int(limit), 1000)}) or []
    out = []
    if not isinstance(d, list):
        return out
    for x in d:
        try:
            # MEXC spot returns id=None on this endpoint, so `.get("id", default)`
            # yields None rather than the default and every print would share one
            # key -- de-duplication would then discard all but the first. Fall back
            # to a composite key. Roughly 6% of prints collide (one order sweeping
            # several resting orders at the same ms and price), which UNDERCOUNTS
            # traded volume slightly -- that biases queue_fill conservative, which
            # is the safe direction for the question being asked.
            tid = x.get("id") or f"{x['time']}-{x['price']}-{x['qty']}"
            out.append({"t": int(x["time"]), "p": float(x["price"]),
                        "v": float(x["qty"]),
                        "T": 2 if x.get("isBuyerMaker") else 1,
                        "i": str(tid)})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def funding_history(symbol, page_size=100, max_pages=3):
    """Spot has no funding."""
    return []


def queue_ahead_usd(bids, level, contract_size):
    """USD resting at or better than our bid -- what must clear before we fill."""
    total = 0.0
    for px, qty, _ in bids:
        if px >= level:
            total += px * qty * contract_size
    return total
