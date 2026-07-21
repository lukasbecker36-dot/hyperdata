#!/usr/bin/env python3
"""
Hyperliquid volume-breakout FADE — autonomous PAPER-TRADING bot.

Strategy (see README / backtest_report.html):
  ENTRY gates, evaluated on each closed bar, per perp:
    1. volume spike : bar volume >= 5x trailing-24h median
    2. breakout     : close pierces the prior-24h high (up) or low (down)
    3. high vol     : trailing-24h realized vol >= calibrated 60th-pct threshold
    4. crowd-aligned: breakout direction matches funding sign (up-brk & +funding, or down-brk & -funding)
    5. liquidity    : HIGH or MID tier (by 24h notional volume)
  Fade the breakout (short an up-breakout / long a down-breakout), one position per coin.
  EXIT: price closes back INSIDE the prior-24h range (reclaim), OR 8h time backstop. No price stop.

Execution (paper): assume MAKER fill at the current best price on our side of the book
  - short entry -> sell at best ASK ; short exit -> buy at best BID
  - long  entry -> buy  at best BID ; long  exit -> sell at best ASK
P&L logged inclusive of maker fees (both sides).

Run TWO instances to compare timeframes live:
  python paper_bot.py --interval 5m
  python paper_bot.py --interval 15m
"""
import argparse, json, os, sys, time, math, csv
from datetime import datetime, timezone
import urllib.request

API = "https://api.hyperliquid.xyz/info"

# ---- strategy constants (match the backtest) ----
VOL_MULT      = 5.0          # volume-spike multiple
WIN_HOURS     = 24           # lookback for range / vol-median / realized-vol
BACKSTOP_HRS  = 8            # max hold
RV_PCTILE     = 0.60         # keep top 40% realized vol -> threshold = 60th pct of signal rv
MAKER_FEE     = 0.00015      # 1.5 bps per side (Hyperliquid base maker fee)
NOTIONAL      = 100.0        # USD notional per paper trade
LEVERAGE      = 3.0          # isolated-margin leverage; margin posted = NOTIONAL/LEVERAGE. 0 = off
MAINT_MARGIN  = 0.05         # maintenance-margin fraction (approx; Hyperliquid is per-asset)
MAX_POSITIONS = 40           # concurrency cap (risk control; no price stop)
CALIB_DAYS    = 15           # history pulled at startup to calibrate rv threshold
# fallback rv thresholds if calibration fails (computed 2026-07 from historical data)
RV_FALLBACK   = {"5m": 0.00256, "15m": 0.00514}

INTERVAL_MIN  = {"5m": 5, "15m": 15}
POLL_OFFSET_S = 15           # seconds after bar close to poll (let candle settle)


def hl_post(body, tries=5):
    data = json.dumps(body).encode()
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(API, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(1.0 * (a + 1))
    raise last


def now_ms():
    return int(time.time() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Bot:
    def __init__(self, interval, datadir):
        self.interval = interval
        self.bar_min = INTERVAL_MIN[interval]
        self.bar_ms = self.bar_min * 60 * 1000
        self.win = int(WIN_HOURS * 60 / self.bar_min)          # bars in 24h
        self.backstop_ms = BACKSTOP_HRS * 3600 * 1000
        self.datadir = datadir
        os.makedirs(datadir, exist_ok=True)
        self.trade_csv = os.path.join(datadir, f"trades_{interval}.csv")
        self.state_file = os.path.join(datadir, f"state_{interval}.json")
        self.log_file = os.path.join(datadir, f"bot_{interval}.log")
        self.positions = {}     # symbol -> dict(dir, entry_px, entry_ms, prior_high, prior_low)
        self.cum_pnl = 0.0
        self.n_closed = 0
        self.n_win = 0
        self.n_liq = 0
        self.universe = {}      # symbol -> tier
        self.rv_thr = RV_FALLBACK[interval]
        # isolated-margin liquidation distance: the adverse move (fraction of notional)
        # that wipes posted margin down to maintenance -> forced exit. None disables it.
        lm = (1.0 / LEVERAGE - MAINT_MARGIN) if LEVERAGE and LEVERAGE > 0 else None
        self.liq_move = lm if (lm is not None and lm > 0) else None
        self._load_state()
        if not os.path.exists(self.trade_csv):
            with open(self.trade_csv, "w", newline="") as f:
                csv.writer(f).writerow([
                    "close_time","symbol","side","entry_time","entry_px","exit_px","hold_h",
                    "gross_bps","fee_bps","net_bps","pnl_usd","reason",
                    "entry_bid","entry_ask","exit_bid","exit_ask","cum_pnl"])

    # ---------- logging / state ----------
    def log(self, msg):
        line = f"{iso(now_ms())}  {msg}"
        print(line, flush=True)
        with open(self.log_file, "a") as f:
            f.write(line + "\n")

    def _save_state(self):
        with open(self.state_file, "w") as f:
            json.dump({"positions": self.positions, "cum_pnl": self.cum_pnl,
                       "n_closed": self.n_closed, "n_win": self.n_win, "n_liq": self.n_liq}, f)

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                s = json.load(open(self.state_file))
                self.positions = s.get("positions", {})
                self.cum_pnl = s.get("cum_pnl", 0.0)
                self.n_closed = s.get("n_closed", 0)
                self.n_win = s.get("n_win", 0)
                self.n_liq = s.get("n_liq", 0)
            except Exception:
                pass

    # ---------- market data ----------
    def load_universe(self):
        m = hl_post({"type": "metaAndAssetCtxs"})
        uni, ctxs = m[0]["universe"], m[1]
        vols = []
        active = []
        for u, c in zip(uni, ctxs):
            if c.get("midPx") is None:      # delisted
                continue
            v = float(c["dayNtlVlm"])
            active.append((u["name"], v))
            vols.append(v)
        vols.sort()
        q1 = vols[len(vols)//3]; q2 = vols[2*len(vols)//3]
        tier = lambda v: "LOW" if v < q1 else ("MID" if v < q2 else "HIGH")
        self.universe = {n: tier(v) for n, v in active}
        self.log(f"universe: {len(self.universe)} active perps  (tier bounds ${q1:,.0f} / ${q2:,.0f})")

    def funding_signs(self):
        m = hl_post({"type": "metaAndAssetCtxs"})
        uni, ctxs = m[0]["universe"], m[1]
        out = {}
        for u, c in zip(uni, ctxs):
            fr = c.get("funding")
            out[u["name"]] = 0 if fr is None else (1 if float(fr) > 0 else (-1 if float(fr) < 0 else 0))
        return out

    def candles(self, coin, lookback_bars):
        start = now_ms() - int(lookback_bars * self.bar_ms)
        d = hl_post({"type": "candleSnapshot",
                     "req": {"coin": coin, "interval": self.interval, "startTime": start, "endTime": now_ms()}})
        return d or []

    def best_bid_ask(self, coin):
        b = hl_post({"type": "l2Book", "coin": coin})
        lv = b.get("levels")
        if not lv or len(lv) < 2 or not lv[0] or not lv[1]:
            return None
        return float(lv[0][0]["px"]), float(lv[1][0]["px"])   # (bid, ask)

    # ---------- indicators on last closed bar ----------
    def features(self, cs):
        """Return dict for the latest CLOSED bar, or None if insufficient data."""
        closed = [c for c in cs if c["T"] <= now_ms()]
        if len(closed) < self.win + 2:
            return None
        c = [float(x["c"]) for x in closed]
        h = [float(x["h"]) for x in closed]
        l = [float(x["l"]) for x in closed]
        v = [float(x["v"]) for x in closed]
        i = len(closed) - 1
        prior_h = max(h[i-self.win:i])
        prior_l = min(l[i-self.win:i])
        pv = sorted(v[i-self.win:i]); med = pv[len(pv)//2]
        vratio = v[i] / med if med > 0 else 0
        rets = [math.log(c[j]/c[j-1]) for j in range(i-self.win+1, i+1)]
        mean = sum(rets)/len(rets)
        rv = (sum((r-mean)**2 for r in rets)/len(rets))**0.5
        brk = 1 if c[i] > prior_h else (-1 if c[i] < prior_l else 0)
        return {"close": c[i], "close_ms": closed[i]["T"], "prior_h": prior_h, "prior_l": prior_l,
                "vratio": vratio, "rv": rv, "brk": brk}

    # ---------- calibration ----------
    def calibrate(self):
        self.log(f"calibrating rv threshold from last {CALIB_DAYS}d ...")
        rvs = []
        lb = int(CALIB_DAYS * 24 * 60 / self.bar_min)
        syms = [s for s, t in self.universe.items() if t in ("HIGH", "MID")]
        for k, s in enumerate(syms):
            try:
                cs = self.candles(s, lb)
                closed = [c for c in cs if c["T"] <= now_ms()]
                if len(closed) < self.win + 5:
                    continue
                c = [float(x["c"]) for x in closed]; h=[float(x["h"]) for x in closed]
                ll=[float(x["l"]) for x in closed]; v=[float(x["v"]) for x in closed]
                for i in range(self.win, len(closed)):
                    pv = sorted(v[i-self.win:i]); med = pv[len(pv)//2]
                    if med<=0 or v[i]/med < VOL_MULT: continue
                    ph=max(h[i-self.win:i]); pl=min(ll[i-self.win:i])
                    if not (c[i]>ph or c[i]<pl): continue
                    rets=[math.log(c[j]/c[j-1]) for j in range(i-self.win+1,i+1)]
                    mean=sum(rets)/len(rets); rv=(sum((r-mean)**2 for r in rets)/len(rets))**0.5
                    rvs.append(rv)
            except Exception:
                pass
            time.sleep(0.05)
        if len(rvs) > 200:
            rvs.sort()
            self.rv_thr = rvs[int(len(rvs)*RV_PCTILE)]
            self.log(f"calibrated rv threshold = {self.rv_thr:.6f}  (from {len(rvs)} signals)")
        else:
            self.log(f"calibration thin ({len(rvs)}), using fallback rv threshold {self.rv_thr:.6f}")

    # ---------- trade ops ----------
    def open_pos(self, sym, brk, feat):
        ba = self.best_bid_ask(sym)
        if ba is None: return
        bid, ask = ba
        if brk == 1:      # up-breakout -> fade SHORT -> sell at ask (maker)
            d, entry = -1, ask
        else:             # down-breakout -> fade LONG -> buy at bid (maker)
            d, entry = 1, bid
        self.positions[sym] = {"dir": d, "entry_px": entry, "entry_ms": feat["close_ms"],
                               "prior_h": feat["prior_h"], "prior_l": feat["prior_l"],
                               "entry_bid": bid, "entry_ask": ask}
        self.log(f"OPEN  {sym:12s} {'SHORT' if d<0 else 'LONG ':5s} @ {entry:.6g}  "
                 f"(vr={feat['vratio']:.1f}x rv={feat['rv']:.4f})  open={len(self.positions)}")
        self._save_state()

    def adverse_excursion(self, cs, p):
        """Max adverse move (fraction of entry) over intrabar highs/lows since entry.
        Short -> up-moves hurt (highs); long -> down-moves hurt (lows). Models the
        worst point the position passed through between polls, for liquidation checks."""
        d = p["dir"]; entry = p["entry_px"]; worst = 0.0
        for c in cs:
            if c["T"] <= p["entry_ms"]:     # bar at/before entry -> not part of the hold
                continue
            if c["t"] > now_ms():           # unformed future bar
                continue
            adv = (float(c["h"]) - entry) / entry if d < 0 else (entry - float(c["l"])) / entry
            if adv > worst: worst = adv
        return worst

    def close_pos(self, sym, reason, forced_px=None):
        p = self.positions[sym]
        d = p["dir"]
        if forced_px is not None:           # forced exit at a known price (e.g. liquidation)
            bid = ask = exit_px = forced_px
        else:
            ba = self.best_bid_ask(sym)
            if ba is None: return
            bid, ask = ba
            exit_px = bid if d < 0 else ask # short closes by buying at bid; long sells at ask
        gross = d * (exit_px - p["entry_px"]) / p["entry_px"]
        fee = 2 * MAKER_FEE
        net = gross - fee
        pnl = NOTIONAL * net
        self.cum_pnl += pnl; self.n_closed += 1; self.n_win += 1 if net > 0 else 0
        self.n_liq += 1 if reason == "liquidation" else 0
        hold_h = (now_ms() - p["entry_ms"]) / 3600000
        with open(self.trade_csv, "a", newline="") as f:
            csv.writer(f).writerow([iso(now_ms()), sym, "SHORT" if d<0 else "LONG",
                iso(p["entry_ms"]), f"{p['entry_px']:.8g}", f"{exit_px:.8g}", f"{hold_h:.2f}",
                f"{gross*1e4:.1f}", f"{fee*1e4:.1f}", f"{net*1e4:.1f}", f"{pnl:.4f}", reason,
                p["entry_bid"], p["entry_ask"], bid, ask, f"{self.cum_pnl:.4f}"])
        self.log(f"CLOSE {sym:12s} {reason:8s} net={net*1e4:+6.1f}bps pnl=${pnl:+.3f}  "
                 f"cum=${self.cum_pnl:+.2f} trades={self.n_closed} win={self.n_win/max(1,self.n_closed)*100:.0f}%")
        del self.positions[sym]
        self._save_state()

    # ---------- main cycle ----------
    def cycle(self):
        fsign = self.funding_signs()
        # 1) exits first (free up dedup slots)
        for sym in list(self.positions.keys()):
            p = self.positions[sym]
            try:
                cs = self.candles(sym, self.win + 5)
                feat = self.features(cs)
            except Exception as e:
                self.log(f"WARN candles {sym}: {e}"); continue
            # 0) isolated-margin liquidation: forced exit if the intrabar adverse move
            #    since entry crossed the liquidation threshold (1/L - maintenance margin).
            #    Priority over reclaim/backstop since it happens intrabar, first.
            if self.liq_move and self.adverse_excursion(cs, p) >= self.liq_move:
                liq_px = (p["entry_px"] * (1 + self.liq_move) if p["dir"] < 0
                          else p["entry_px"] * (1 - self.liq_move))
                try: self.close_pos(sym, "liquidation", forced_px=liq_px)
                except Exception as e: self.log(f"WARN liq {sym}: {e}")
                continue
            reason = None
            if feat is not None:
                if p["dir"] < 0 and feat["close"] < p["prior_h"]: reason = "reclaim"
                elif p["dir"] > 0 and feat["close"] > p["prior_l"]: reason = "reclaim"
            if reason is None and now_ms() - p["entry_ms"] >= self.backstop_ms:
                reason = "backstop"
            if reason:
                try: self.close_pos(sym, reason)
                except Exception as e: self.log(f"WARN close {sym}: {e}")
        # 2) entries
        n_sig = 0
        for sym, tier in self.universe.items():
            if tier not in ("HIGH", "MID"): continue
            if sym in self.positions: continue
            if len(self.positions) >= MAX_POSITIONS: break
            try:
                feat = self.features(self.candles(sym, self.win + 5))
            except Exception:
                continue
            if feat is None: continue
            if feat["vratio"] < VOL_MULT or feat["brk"] == 0: continue
            if feat["rv"] < self.rv_thr: continue
            if feat["brk"] * fsign.get(sym, 0) != 1: continue      # crowd-aligned
            n_sig += 1
            try: self.open_pos(sym, feat["brk"], feat)
            except Exception as e: self.log(f"WARN open {sym}: {e}")
        self.log(f"cycle done: {n_sig} new signals, {len(self.positions)} open, "
                 f"cum=${self.cum_pnl:+.2f}, {self.n_closed} closed, {self.n_liq} liq")

    def run(self):
        lev_s = (f"lev={LEVERAGE}x liq@{self.liq_move*100:.1f}%" if self.liq_move
                 else "lev=off (no liquidation)")
        self.log(f"=== paper bot [{self.interval}] starting | notional=${NOTIONAL} maker_fee={MAKER_FEE*1e4}bps "
                 f"backstop={BACKSTOP_HRS}h maxpos={MAX_POSITIONS} {lev_s} ===")
        self.load_universe()
        self.calibrate()
        while True:
            # align to next bar close + offset
            nb = (now_ms() // self.bar_ms + 1) * self.bar_ms
            sleep_s = (nb - now_ms()) / 1000 + POLL_OFFSET_S
            time.sleep(max(1, sleep_s))
            t0 = time.time()
            try:
                self.cycle()
            except Exception as e:
                self.log(f"ERROR cycle: {e}")
            # refresh universe/tiers once a day
            if int(time.time()) % 86400 < self.bar_min * 60:
                try: self.load_universe()
                except Exception: pass
            self.log(f"cycle took {time.time()-t0:.1f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", choices=["5m", "15m"], required=True)
    ap.add_argument("--datadir", default=None)
    ap.add_argument("--notional", type=float, default=None)
    ap.add_argument("--leverage", type=float, default=None,
                    help="isolated-margin leverage (default 3x; 0 disables liquidation modelling)")
    ap.add_argument("--maint-margin", type=float, default=None,
                    help="maintenance-margin fraction (default 0.05)")
    a = ap.parse_args()
    if a.notional: NOTIONAL = a.notional
    if a.leverage is not None: LEVERAGE = a.leverage
    if a.maint_margin is not None: MAINT_MARGIN = a.maint_margin
    dd = a.datadir or f"./paper_{a.interval}"
    Bot(a.interval, dd).run()
