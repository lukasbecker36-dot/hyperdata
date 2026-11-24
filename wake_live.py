#!/usr/bin/env python3
"""
Wake-watch trader -- trades the wake-up watch's "liquid" signals on Aster perps.

Signals: wake_watch.py (MEXC offers thinning + Aster funding climbing + price flat, from the
basis-trade engine's scam-screen logs, which read MEXC's PUBLIC API -- no MEXC account needed).
Only alerts tagged liquid (Aster 24h volume >= $50k) are taken; the others lost money.

Rule (backtest on Aster prices and funding, 46 liquid signals 28 Oct - 19 Nov 2026, $100 each:
+$236 after costs and funding, +$42 without the best 3, worst -40%, at most 10 open at once):
    long $size at market, isolated, `lev`x
    sell half if it reaches +30%, the rest if it reaches +100%   (TAKE_PROFIT_MARKET triggers)
    whatever is left is sold at market 72h after entry
No stop: at 2x isolated the liquidation (~-48%) is the stop. Aster rejects LIMIT sells more than
10% above the mark (PERCENT_PRICE), so the take-profits are trigger orders resting on Aster;
they fire even while this service is down.

Every trade is either paper (Aster marks, no orders) or live; the mode is chosen per trade at entry
from the settings, so switching mode never touches open trades. Live trading needs its OWN Aster
account: the basis bot shorts Aster perps in one-way mode and a long in the same account on the
same coin would net against its hedge. A coin that already has a position this trader didn't open
is never traded.

Control: /wl in @Hyperfadebot (telegram_monitor.py) writes settings.json and request files here.
Brake: once realised LIVE net reaches -halt_loss, new live entries stop until /wl resume.

  python3 wake_live.py --datadir ./wakelive               # run
  python3 wake_live.py --datadir ./wakelive --probe       # check keys, balance, positions; no orders
Env: WAKE_ASTER_SIGNER (API agent wallet address), WAKE_ASTER_KEY (its private key).
"""
import argparse
import csv
import json
import os
import threading
import time
import urllib.parse
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

import requests

import telegram_notify as tg

HOUR_MS = 3_600_000
WAKE_DIR = os.environ.get("WAKE_DATADIR", "/opt/hyperdata/wakewatch")
ASTER = "https://fapi.asterdex.com"
LEGS = ((0.5, 0.30), (0.5, 1.00))      # (fraction of the position, take-profit level)
HOLD_H = 72
MAX_SIGNAL_AGE_MIN = 45                 # sweeps are hourly; anything older is a restart backlog
MAX_CHASE = 0.05                        # skip if Aster's ask is > 5% above the signal's mark
PAPER_FEE = 0.0004                      # Aster taker, per side (paper P&L)
LOOP_S = 30
DEFAULTS = {"enabled": True, "mode": "paper", "size": 100.0, "lev": 2, "max_open": 10,
            "halt_loss": 250.0, "trigger": "MARK_PRICE"}
TRADE_COLS = ["signal_ms", "symbol", "mode", "entry", "qty", "notional", "opened_ms", "closed_ms",
              "reason", "legs_hit", "exit_px", "price_pnl", "fees", "funding", "net"]

# ---------------------------------------------------------------- Aster (v3, EIP-712 signed)
_TYPED = {
    "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                               {"name": "chainId", "type": "uint256"},
                               {"name": "verifyingContract", "type": "address"}],
              "Message": [{"name": "msg", "type": "string"}]},
    "primaryType": "Message",
    "domain": {"name": "AsterSignTransaction", "version": "1", "chainId": 1666,
               "verifyingContract": "0x0000000000000000000000000000000000000000"},
}
_nonce_lock = threading.Lock()
_last_nonce = 0


def _nonce():
    """Strictly increasing microsecond nonce (Aster rejects anything below its recent minimum)."""
    global _last_nonce
    with _nonce_lock:
        _last_nonce = max(int(time.time() * 1_000_000), _last_nonce + 1)
        return _last_nonce


class AsterError(Exception):
    def __init__(self, msg, code=None, ambiguous=False):
        super().__init__(f"aster: {msg}" + (f" (code {code})" if code is not None else ""))
        self.code, self.ambiguous = code, ambiguous


def D(v):
    return Decimal(str(v)) if v not in (None, "") else Decimal(0)


class Aster:
    def __init__(self, signer=None, key=None, base=ASTER):
        self.signer, self.key, self.base = signer, key, base
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "wake-live"
        self._specs, self._specs_at = {}, 0

    @property
    def can_trade(self):
        return bool(self.signer and self.key)

    def _sign(self, params):
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        p = dict(params)
        p["nonce"], p["signer"] = str(_nonce()), self.signer
        msg = encode_typed_data(full_message={**_TYPED, "message": {"msg": urllib.parse.urlencode(p)}})
        sig = Account.sign_message(msg, private_key=self.key).signature.hex()
        p["signature"] = sig if sig.startswith("0x") else "0x" + sig
        return p

    def req(self, method, path, params=None, signed=False, order=False):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            if not self.can_trade:
                raise AsterError("no WAKE_ASTER_SIGNER / WAKE_ASTER_KEY configured")
            params = self._sign(params)
        body = urllib.parse.urlencode(params)
        url = self.base + path
        try:
            if method == "GET":
                r = self.s.get(url + ("?" + body if body else ""), timeout=15)
            else:
                r = self.s.request(method, url, data=body, timeout=15,
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        except requests.RequestException as e:
            raise AsterError(f"transport {e!r}", ambiguous=order) from e
        if r.status_code in (418, 429):
            time.sleep(2)
            raise AsterError(f"rate limited HTTP {r.status_code}")
        if r.status_code >= 500:
            raise AsterError(f"HTTP {r.status_code}: {r.text[:200]}", ambiguous=order)
        try:
            p = r.json()
        except ValueError:
            raise AsterError(f"non-JSON HTTP {r.status_code}: {r.text[:200]}", ambiguous=order)
        if isinstance(p, dict) and (r.status_code >= 400 or ("msg" in p and p.get("code") not in (None, 0, 200, "200"))):
            raise AsterError(p.get("msg", r.text[:200]), p.get("code"))
        return p

    # market data
    def specs(self):
        """symbol -> (tick, step, min_notional), refreshed every 6h."""
        if time.time() - self._specs_at > 6 * 3600 or not self._specs:
            out = {}
            for s in self.req("GET", "/fapi/v3/exchangeInfo")["symbols"]:
                if s.get("status") != "TRADING":
                    continue
                f = {x["filterType"]: x for x in s.get("filters", [])}
                tick = D(f.get("PRICE_FILTER", {}).get("tickSize"))
                step = D(f.get("MARKET_LOT_SIZE", {}).get("stepSize") or f.get("LOT_SIZE", {}).get("stepSize"))
                if tick > 0 and step > 0:
                    out[s["symbol"]] = (tick, step, D(f.get("MIN_NOTIONAL", {}).get("notional") or 5))
            self._specs, self._specs_at = out, time.time()
        return self._specs

    def premium(self):
        """symbol -> (mark, predicted funding rate, next funding time ms)."""
        return {r["symbol"]: (float(r["markPrice"]), float(r.get("lastFundingRate") or 0),
                              int(r.get("nextFundingTime") or 0))
                for r in self.req("GET", "/fapi/v3/premiumIndex") if float(r.get("markPrice") or 0) > 0}

    def book(self, sym):
        r = self.req("GET", "/fapi/v3/ticker/bookTicker", {"symbol": sym})
        return float(r["bidPrice"]), float(r["askPrice"])

    # account
    def positions(self):
        return {p["symbol"]: D(p["positionAmt"]) for p in self.req("GET", "/fapi/v3/positionRisk", signed=True)
                if D(p.get("positionAmt")) != 0}

    def available(self):
        for b in self.req("GET", "/fapi/v3/balance", signed=True):
            if b.get("asset") == "USDT":
                return float(b.get("availableBalance") or b.get("maxWithdrawAmount") or 0)
        return 0.0

    def open_orders(self):
        return self.req("GET", "/fapi/v3/openOrders", signed=True)

    def order(self, **p):
        return self.req("POST", "/fapi/v3/order", {"newOrderRespType": "RESULT", **p}, signed=True, order=True)

    def get_order(self, sym, oid=None, cid=None):
        try:
            return self.req("GET", "/fapi/v3/order", {"symbol": sym, "orderId": oid, "origClientOrderId": cid},
                            signed=True)
        except AsterError as e:
            if str(e.code) == "-2013":            # order does not exist
                return None
            raise

    def cancel_all(self, sym):
        return self.req("DELETE", "/fapi/v3/allOpenOrders", {"symbol": sym}, signed=True)

    def setup(self, sym, lev):
        for path, p in (("/fapi/v3/marginType", {"symbol": sym, "marginType": "ISOLATED"}),
                        ("/fapi/v3/leverage", {"symbol": sym, "leverage": str(int(lev))})):
            try:
                self.req("POST", path, p, signed=True)
            except AsterError as e:
                if str(e.code) != "-4046" and "no need" not in str(e).lower():
                    raise

    def income(self, sym, start_ms, end_ms):
        return self.req("GET", "/fapi/v3/income", {"symbol": sym, "startTime": str(int(start_ms)),
                                                   "endTime": str(int(end_ms)), "limit": "1000"}, signed=True)


def floor_to(x, step):
    return (D(x) / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to(x, step):
    return (D(x) / step).to_integral_value(rounding=ROUND_UP) * step


# ---------------------------------------------------------------- files shared with /wl
def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


def load_settings(datadir):
    return {**DEFAULTS, **_read_json(Path(datadir) / "settings.json", {})}


def save_settings(datadir, **changes):
    Path(datadir).mkdir(parents=True, exist_ok=True)
    cur = _read_json(Path(datadir) / "settings.json", {})
    cur.update(changes)
    _write_json(Path(datadir) / "settings.json", cur)
    return load_settings(datadir)


def add_request(datadir, action, sym=None):
    d = Path(datadir) / "requests"
    d.mkdir(parents=True, exist_ok=True)
    _write_json(d / f"{time.time_ns()}.json", {"action": action, "sym": sym})


def closed_trades(datadir):
    try:
        with open(Path(datadir) / "wake_live_trades.csv", newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------- the trader
class Trader:
    def __init__(self, datadir, wake_dir=WAKE_DIR, aster=None, notify=None, clock=time.time):
        self.dir = Path(datadir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.wake_dir = Path(wake_dir)
        self.ax = aster or Aster(os.environ.get("WAKE_ASTER_SIGNER"), os.environ.get("WAKE_ASTER_KEY"))
        self.notify = notify or tg.send
        self.clock = clock
        self.state_file = self.dir / "wake_live.json"
        self.trades_file = self.dir / "wake_live_trades.csv"
        self._warned = set()

    def now_ms(self):
        return int(self.clock() * 1000)

    def load(self):
        st = _read_json(self.state_file, {})
        st.setdefault("trades", {})
        st.setdefault("seen", {})
        st.setdefault("start_ms", self.now_ms())     # never back-fill signals from before the first start
        st.setdefault("halted", None)
        st.setdefault("foreign", [])
        return st

    def save(self, st):
        _write_json(self.state_file, st)

    def warn_once(self, key, msg):
        if key not in self._warned:
            self._warned.add(key)
            self.notify(msg)

    # ---- signals
    def signals(self):
        """(key, sym, signal_ms, mark) for the wake watch's currently-followed liquid alerts."""
        st = _read_json(self.wake_dir / "wake_watch.json", {})
        return [(f"{s}|{int(x['ts'])}", s, int(x["ts"]), float(x["entry"]))
                for s, x in (st.get("active") or {}).items() if x.get("liquid")]

    # ---- one pass
    def step(self):
        st, cfg, now = self.load(), load_settings(self.dir), self.now_ms()
        requests_ = self._take_requests()
        for r in requests_:
            if r.get("action") == "resume":
                st["halted"] = None
                self.notify("▶️ wake live: brake cleared, live entries re-enabled")
        prem = self.ax.premium()
        live = {s: t for s, t in st["trades"].items() if t["mode"] == "live"}
        pos = oo = None
        if live or (cfg["mode"] == "live" and self.ax.can_trade):
            pos = self.ax.positions()
            st["foreign"] = sorted(s for s in pos if s not in live)
        if live:
            oo = {str(o["orderId"]) for o in self.ax.open_orders()}
        close_req = {r.get("sym") for r in requests_ if r.get("action") == "close"}
        close_all = any(r.get("action") == "closeall" for r in requests_)
        for sym, t in list(st["trades"].items()):
            force = close_all or sym in close_req
            try:
                if t["mode"] == "paper":
                    self._paper(st, sym, t, prem, now, force)
                else:
                    self._live(st, sym, t, prem, pos, oo, now, force)
            except AsterError as e:
                self.notify(f"⚠️ wake live {sym}: {e} — retrying next pass")
        for sym, t in st["trades"].items():           # marks for /wl's unrealised P&L (the monitor has no keys)
            if t["mode"] == "live" and t.get("entry") and sym in prem:
                t["last"] = prem[sym][0]
                if pos is not None:
                    t["rem_qty"] = format(pos.get(sym, D(0)), "f")
        self._new_signals(st, cfg, prem, pos, now)
        self.save(st)

    def _take_requests(self):
        out = []
        d = self.dir / "requests"
        for f in sorted(d.glob("*.json")) if d.exists() else []:
            out.append(_read_json(f, {}))
            f.unlink(missing_ok=True)
        return out

    # ---- entries
    def _new_signals(self, st, cfg, prem, pos, now):
        for key, sym, ts, mark in self.signals():
            if key in st["seen"]:
                continue
            st["seen"][key] = now
            if ts < st["start_ms"] - HOUR_MS:
                continue                                   # from before this trader existed
            why = self._skip_reason(st, cfg, sym, ts, mark, prem, pos, now)
            if why:
                self.notify(f"⏭ wake live skipped {sym}: {why}")
                continue
            try:
                if cfg["mode"] == "live":
                    self._open_live(st, cfg, sym, ts, mark, now)
                else:
                    self._open_paper(st, cfg, sym, ts, prem, now)
            except AsterError as e:
                self.notify(f"⚠️ wake live could not open {sym}: {e}")
        cutoff = now - 30 * 24 * HOUR_MS
        st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}

    def _skip_reason(self, st, cfg, sym, ts, mark, prem, pos, now):
        if not cfg["enabled"]:
            return "entries are off (/wl on)"
        if (now - ts) / 60_000 > MAX_SIGNAL_AGE_MIN:
            return f"signal is {(now - ts) / 60_000:.0f} min old"
        if sym in st["trades"]:
            return "already open"
        if len(st["trades"]) >= int(cfg["max_open"]):
            return f"{len(st['trades'])} open, the cap is {int(cfg['max_open'])}"
        if sym not in self.ax.specs() or sym not in prem:
            return "not tradable on Aster"
        if cfg["mode"] == "live":
            if not self.ax.can_trade:
                return "live mode but no Aster key configured"
            if st["halted"]:
                return f"brake on since {st['halted']} (/wl resume)"
            if pos is not None and sym in pos:
                return "this Aster account already holds a position in it that this trader didn't open"
        if prem[sym][0] > mark * (1 + MAX_CHASE):
            return f"Aster mark {prem[sym][0]:.6g} already {prem[sym][0] / mark - 1:+.0%} above the signal"
        return None

    def _open_paper(self, st, cfg, sym, ts, prem, now):
        px = prem[sym][0]
        t = {"mode": "paper", "signal_ms": ts, "opened_ms": now, "entry": px, "notional": float(cfg["size"]),
             "qty": float(cfg["size"]) / px, "left": 1.0, "legs": [{"frac": f, "lv": lv, "hit": False} for f, lv in LEGS],
             "realised": 0.0, "funding": 0.0, "next_fund": prem[sym][2], "rate": prem[sym][1], "last": px, "hi": px}
        st["trades"][sym] = t
        self.notify(f"📝 wake paper BUY {sym} ${t['notional']:.0f} @ {px:.6g} · half off +30% "
                    f"@ {px * 1.3:.6g}, rest +100% @ {px * 2:.6g}, else out at 72h")

    def _open_live(self, st, cfg, sym, ts, mark, now):
        tick, step, min_not = self.ax.specs()[sym]
        size, lev = float(cfg["size"]), int(cfg["lev"])
        avail = self.ax.available()
        if avail < 1.15 * size / lev:
            self.notify(f"⏭ wake live skipped {sym}: ${avail:.0f} available, needs ${1.15 * size / lev:.0f} "
                        f"(${size:.0f} at {lev}x + 15%)")
            return
        _, ask = self.ax.book(sym)
        qty = floor_to(size / ask, step)
        if qty * D(ask) < min_not:
            self.notify(f"⏭ wake live skipped {sym}: ${size:.0f} is below Aster's minimum order")
            return
        self.ax.setup(sym, lev)
        cid = f"wk-{sym}-{ts // 1000}"[:36]
        st["trades"][sym] = {"mode": "live", "status": "opening", "cid": cid, "signal_ms": ts, "opened_ms": now,
                             "lev": lev, "notional": size, "trigger": cfg["trigger"]}
        self.save(st)                                   # a crash from here on is recovered by client id
        try:
            r = self.ax.order(symbol=sym, side="BUY", type="MARKET", quantity=format(qty, "f"), newClientOrderId=cid)
        except AsterError as e:
            if not e.ambiguous:
                del st["trades"][sym]
                raise
            r = self.ax.get_order(sym, cid=cid)
            if r is None:
                del st["trades"][sym]
                raise
        self._filled_entry(st, sym, r)

    def _filled_entry(self, st, sym, r):
        t = st["trades"][sym]
        qty, px = D(r.get("executedQty")), float(r.get("avgPrice") or 0)
        if qty <= 0 or px <= 0:
            if r.get("status") in ("NEW", "PARTIALLY_FILLED"):
                return                                   # still working; checked again next pass
            del st["trades"][sym]
            self.notify(f"⚠️ wake live {sym}: entry order {r.get('status')} with no fill — nothing opened")
            return
        t.update(status="open", entry=px, qty=format(qty, "f"), notional=float(qty) * px,
                 legs=[{"frac": f, "lv": lv, "oid": None, "filled": False} for f, lv in LEGS])
        self._place_legs(sym, t)
        self.notify(f"🟢 <b>wake LIVE BUY {sym}</b> {t['qty']} @ {px:.6g} (${t['notional']:.0f}, {t['lev']}x isolated)\n"
                    f"  take-profits: half @ {px * 1.3:.6g} (+30%), rest @ {px * 2:.6g} (+100%) · "
                    f"out by {time.strftime('%a %H:%M', time.gmtime(t['opened_ms'] / 1000 + HOLD_H * 3600))} UTC")

    def _place_legs(self, sym, t):
        tick, step, _ = self.ax.specs()[sym]
        q_all = D(t["qty"])
        for i, leg in enumerate(t["legs"]):
            if leg["filled"] or leg["oid"]:
                continue
            stop = format(ceil_to(t["entry"] * (1 + leg["lv"]), tick), "f")
            last = i == len(t["legs"]) - 1
            p = {"symbol": sym, "side": "SELL", "type": "TAKE_PROFIT_MARKET", "stopPrice": stop,
                 "workingType": t.get("trigger", "MARK_PRICE")}
            if last:
                p["closePosition"] = "true"             # whatever is left
            else:
                q = floor_to(q_all * D(leg["frac"]), step)
                if q <= 0:
                    leg["filled"] = True                 # too small to split: the last leg sells it all
                    continue
                p.update(quantity=format(q, "f"), reduceOnly="true")
            try:
                leg["oid"] = str(self.ax.order(**p)["orderId"])
            except AsterError as e:
                self.warn_once(f"leg{sym}{i}{t['opened_ms']}", f"⚠️ wake live {sym}: could not place the "
                               f"+{leg['lv']:.0%} take-profit ({e}) — retrying every pass")

    # ---- managing open trades
    def _paper(self, st, sym, t, prem, now, force):
        if sym in prem:
            px, rate, nxt = prem[sym]
            if t["next_fund"] and nxt > t["next_fund"]:          # a funding print passed: the long pays
                t["funding"] -= t["left"] * t["notional"] * t["rate"]
            t.update(last=px, hi=max(t["hi"], px), rate=rate, next_fund=nxt)
            for leg in t["legs"]:
                if not leg["hit"] and t["left"] > 1e-9 and px >= t["entry"] * (1 + leg["lv"]):
                    leg["hit"] = True
                    f = min(leg["frac"], t["left"])
                    t["realised"] += f * t["notional"] * leg["lv"]
                    t["left"] -= f
                    self.notify(f"💰 wake paper {sym}: sold {f:.0%} at +{leg['lv']:.0%}")
        age_h = (now - t["opened_ms"]) / HOUR_MS
        if t["left"] <= 1e-9 or age_h >= HOLD_H or force:
            t["realised"] += t["left"] * t["notional"] * (t["last"] / t["entry"] - 1)
            reason = "take-profits" if t["left"] <= 1e-9 else ("manual" if force and age_h < HOLD_H else "72h")
            fees = 2 * PAPER_FEE * t["notional"]
            self._record(st, sym, t, now, reason, t["last"], t["realised"], -fees, t["funding"])

    def _live(self, st, sym, t, prem, pos, oo, now, force):
        if t.get("status") == "opening":
            r = self.ax.get_order(sym, cid=t["cid"])
            if r is None:
                del st["trades"][sym]
                self.notify(f"⚠️ wake live {sym}: the entry never reached Aster — nothing opened")
            else:
                self._filled_entry(st, sym, r)
            return
        amt = pos.get(sym, D(0)) if pos is not None else None
        for leg in t["legs"]:
            if leg["oid"] and not leg["filled"] and leg["oid"] not in oo:
                r = self.ax.get_order(sym, oid=leg["oid"])
                status = (r or {}).get("status")
                if status == "FILLED":
                    leg.update(filled=True, px=float(r.get("avgPrice") or 0))
                    self.notify(f"💰 wake LIVE {sym}: take-profit +{leg['lv']:.0%} filled @ {leg['px']:.6g}")
                elif status not in ("NEW", "PARTIALLY_FILLED") and amt:
                    leg["oid"] = None                    # cancelled/expired while still holding: re-place
        if amt is not None and amt != 0 and not force and (now - t["opened_ms"]) / HOUR_MS < HOLD_H:
            if any(not lg["filled"] and not lg["oid"] for lg in t["legs"]):
                self._place_legs(sym, t)
            return
        if t.get("status") == "closing" and amt:
            if now - t["closing_ms"] < 5 * 60_000:
                return                                   # market sell sent; wait for the position to show flat
        if amt:                                          # 72h (or /wl close): cancel triggers, sell the rest
            self.ax.cancel_all(sym)
            r = self.ax.order(symbol=sym, side="SELL", type="MARKET", quantity=format(abs(amt), "f"),
                              reduceOnly="true")
            t.update(status="closing", closing_ms=now, exit_px=float(r.get("avgPrice") or 0),
                     reason="manual" if force and (now - t["opened_ms"]) / HOUR_MS < HOLD_H else "72h")
            return
        # flat on the venue: take-profits done, the 72h sell landed, or it was liquidated / closed by hand
        if amt is None:
            return
        self.ax.cancel_all(sym)
        if "reason" not in t:
            t["reason"] = "take-profits" if all(lg["filled"] for lg in t["legs"]) else "closed on Aster (liquidated or by hand)"
        inc = self.ax.income(sym, t["opened_ms"] - 60_000, now + 60_000)
        by = {}
        for x in inc:
            by[x.get("incomeType")] = by.get(x.get("incomeType"), 0.0) + float(x.get("income") or 0)
        price = by.pop("REALIZED_PNL", 0.0)
        fees = by.pop("COMMISSION", 0.0)
        funding = by.pop("FUNDING_FEE", 0.0)
        price += sum(by.values())                        # e.g. INSURANCE_CLEAR on a liquidation
        exit_px = t.get("exit_px") or next((lg.get("px") for lg in reversed(t["legs"]) if lg.get("px")), None) \
            or (prem.get(sym, (0,))[0])
        self._record(st, sym, t, now, t["reason"], exit_px, price, fees, funding)

    def _record(self, st, sym, t, now, reason, exit_px, price, fees, funding):
        net = price + fees + funding
        legs_hit = sum(bool(lg.get("hit") or lg.get("filled") and lg.get("px")) for lg in t["legs"])
        row = {"signal_ms": t["signal_ms"], "symbol": sym, "mode": t["mode"], "entry": f"{t['entry']:.6g}",
               "qty": t["qty"], "notional": f"{t['notional']:.2f}", "opened_ms": t["opened_ms"], "closed_ms": now,
               "reason": reason, "legs_hit": legs_hit, "exit_px": f"{exit_px:.6g}" if exit_px else "",
               "price_pnl": f"{price:.2f}", "fees": f"{fees:.2f}", "funding": f"{funding:.2f}", "net": f"{net:.2f}"}
        new = not self.trades_file.exists()
        with open(self.trades_file, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_COLS)
            if new:
                w.writeheader()
            w.writerow(row)
        del st["trades"][sym]
        tag = "LIVE" if t["mode"] == "live" else "paper"
        self.notify(f"🏁 wake {tag} {sym} closed ({reason}) after {(now - t['opened_ms']) / HOUR_MS:.0f}h: "
                    f"<b>${net:+.2f}</b> (price {price:+.2f}, fees {fees:+.2f}, funding {funding:+.2f})\n"
                    f"{totals_line(self.dir, t['mode'])}")
        if t["mode"] == "live" and not st["halted"]:
            cfg = load_settings(self.dir)
            tot = sum(float(r["net"]) for r in closed_trades(self.dir) if r["mode"] == "live")
            if tot <= -float(cfg["halt_loss"]):
                st["halted"] = time.strftime("%d %b %H:%M UTC", time.gmtime(now / 1000))
                self.notify(f"🛑 wake live brake: live net ${tot:+.0f} reached -${float(cfg['halt_loss']):.0f}. "
                            f"No new live entries; open trades still run their exits. /wl resume to re-enable.")


# ---------------------------------------------------------------- reporting (/wl)
def totals_line(datadir, mode):
    rows = [r for r in closed_trades(datadir) if r["mode"] == mode]
    if not rows:
        return f"{mode}: no closed trades yet"
    net = [float(r["net"]) for r in rows]
    return (f"{mode}: {len(rows)} closed, ${sum(net):+,.2f} net, {sum(n > 0 for n in net)}/{len(rows)} won, "
            f"best ${max(net):+.0f}, worst ${min(net):+.0f}")


def trade_pnl(t):
    """(unrealised $ on the part still held, $ banked by take-profits so far), price only (before fees/funding),
    at the last mark the trader saw."""
    e = t.get("entry")
    if not e:
        return 0.0, 0.0
    last = t.get("last") or e
    if t["mode"] == "paper":
        return t["left"] * t["notional"] * (last / e - 1), t.get("realised", 0.0)
    qty = float(t["qty"])
    banked = sum(lg["frac"] * qty * (lg["px"] - e) for lg in t["legs"][:-1] if lg.get("filled") and lg.get("px"))
    rem = float(t["rem_qty"]) if t.get("rem_qty") is not None else qty
    return rem * (last - e), banked


def report(datadir):
    cfg = load_settings(datadir)
    st = _read_json(Path(datadir) / "wake_live.json", {})
    now = time.time() * 1000
    lines = [f"<b>Wake live</b> — liquid wake-watch signals on Aster: half off +30%, rest +100%, else 72h",
             f"entries {'ON' if cfg['enabled'] else 'OFF'} · new trades <b>{cfg['mode'].upper()}</b> · "
             f"${float(cfg['size']):.0f} at {int(cfg['lev'])}x isolated · max {int(cfg['max_open'])} open · "
             f"brake -${float(cfg['halt_loss']):.0f}" + (f" · <b>BRAKE ON since {st['halted']}</b>" if st.get("halted") else "")]
    trades = (st.get("trades") or {})
    tot = {"live": [0.0, 0.0, 0], "paper": [0.0, 0.0, 0]}
    for sym, t in sorted(trades.items(), key=lambda kv: kv[1]["opened_ms"]):
        e = t.get("entry")
        if not e:
            lines.append(f"• {sym} [{t['mode']}] {t.get('status', '?')}")
            continue
        last = t.get("last") or e
        hit = [f"+{lg['lv']:.0%}" for lg in t.get("legs", []) if lg.get("hit") or lg.get("filled")]
        upnl, banked = trade_pnl(t)
        agg = tot[t["mode"]]
        agg[0] += upnl
        agg[1] += banked
        agg[2] += 1
        lines.append(f"• {sym} [{t['mode']}] {(now - t['opened_ms']) / HOUR_MS:.0f}h in, {e:.6g} → {last:.6g} "
                     f"({last / e - 1:+.0%}) · uPnL <b>${upnl:+.2f}</b>"
                     + (f" · took {', '.join(hit)} (${banked:+.2f} banked)" if hit else ""))
    if not trades:
        lines.append("no open trades")
    for mode, (u, b, n) in tot.items():
        if n:
            lines.append(f"<b>{mode} unrealised: ${u:+,.2f}</b> on {n} open"
                         + (f" (${b:+,.2f} more banked by take-profits)" if b else "") + " · price only, before fees/funding")
    if st.get("foreign"):
        lines.append(f"<i>positions on the Aster account not opened by this trader (never traded): "
                     f"{', '.join(st['foreign'])}</i>")
    lines += [totals_line(datadir, "live"), totals_line(datadir, "paper")]
    return "\n".join(lines)


WL_HELP = ("/wl — status · /wl on|off — entries · /wl paper|live — mode for NEW trades · /wl size 100 · "
           "/wl lev 2 · /wl max 10 · /wl brake 250 · /wl close SYM · /wl closeall · /wl resume")


def command(datadir, args):
    """/wl subcommands (telegram_monitor.py). Returns the reply."""
    if not args or args[0] in ("status", "s"):
        return report(datadir)
    a = args[0]
    if a in ("on", "off"):
        save_settings(datadir, enabled=a == "on")
        return f"wake live entries {a.upper()}"
    if a in ("paper", "live"):
        cfg = save_settings(datadir, mode=a)
        extra = (f"\n<i>REAL ORDERS from the next liquid signal: ${float(cfg['size']):.0f} at {int(cfg['lev'])}x. "
                 f"Open paper trades stay paper.</i>" if a == "live" else "")
        return f"new wake trades will be <b>{a.upper()}</b>{extra}"
    num = {"size": ("size", float, 10, 2000), "lev": ("lev", int, 1, 5), "max": ("max_open", int, 1, 30),
           "brake": ("halt_loss", float, 10, 5000)}
    if a in num and len(args) > 1:
        key, typ, lo, hi = num[a]
        try:
            v = typ(args[1])
        except ValueError:
            return f"usage: /wl {a} N"
        if not lo <= v <= hi:
            return f"{a} must be between {lo} and {hi}"
        save_settings(datadir, **{key: v})
        return f"wake live {a} set to {v} (new trades)"
    if a == "close" and len(args) > 1:
        sym = args[1].upper()
        sym = sym if sym.endswith("USDT") else sym + "USDT"
        add_request(datadir, "close", sym)
        return f"closing {sym} at market within ~30s"
    if a in ("closeall", "resume"):
        add_request(datadir, a)
        return "closing every wake trade at market within ~30s" if a == "closeall" else "clearing the brake"
    return WL_HELP


# ---------------------------------------------------------------- main
def probe(tr):
    ax = tr.ax
    print("signer:", ax.signer or "(none)")
    print("tradable symbols:", len(ax.specs()))
    if not ax.can_trade:
        print("no WAKE_ASTER_SIGNER / WAKE_ASTER_KEY: paper only")
        return
    print(f"USDT available: {ax.available():.2f}")
    pos = ax.positions()
    print("open positions:", {k: str(v) for k, v in pos.items()} or "none")
    if pos:
        print("WARNING: this account already holds positions. Use a separate Aster account from the basis bot.")
    print("open orders:", len(ax.open_orders()))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datadir", default="./wakelive")
    p.add_argument("--wake-dir", default=WAKE_DIR)
    p.add_argument("--probe", action="store_true")
    a = p.parse_args()
    tr = Trader(a.datadir, a.wake_dir)
    if a.probe:
        probe(tr)
        return
    cfg = load_settings(a.datadir)
    print(f"wake live up | mode {cfg['mode']} | key {'yes' if tr.ax.can_trade else 'no'} | {tr.dir}", flush=True)
    st = tr.load()
    tr.save(st)
    fails = 0
    while True:
        try:
            tr.step()
            fails = 0
        except Exception as e:                       # keep running; say so if it persists
            fails += 1
            print(f"step failed: {e!r}", flush=True)
            if fails in (3, 30):
                tg.send(f"⚠️ wake live: {fails} passes in a row failed — {e}")
        time.sleep(LOOP_S)


if __name__ == "__main__":
    main()
