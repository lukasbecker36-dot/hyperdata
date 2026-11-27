#!/usr/bin/env python3
"""
Dividend gap watch: alerts @Hyperfadebot when the same stock's perp on Aster and on HL's xyz dex trade apart
around an ex-dividend date. Alert only; it places nothing.

WHY (24-27 Nov 2026, scratchpad div_venues.py)
  HL's xyz oracle is the raw stock price, so the perp falls by the dividend at the ex-date open, and HL's book
  starts pricing that in days before (funding turns negative for longs). Aster's books don't always do the same
  (its market makers seem to hedge on venues that treat dividends differently). SK Hynix, 1.89% dividend, ex
  27 Nov: Aster traded 1.1-1.5% above HL from 25 Nov and 2.0-2.3% above for the 11 hours before the Korean
  open, then 0.15% after it. Short Aster / long HL over that window: ~1.7% net. Samsung (Sep), IBM and BX
  showed nothing, so it's occasional; Aster's depth (~$70-150k in 10 levels on SKHYNIX) caps the size.

RULES
  Every minute: tradable gap per pair = Aster bid / HL ask - 1 (short Aster) or HL bid / Aster ask - 1
  (short HL), on HL's impact prices and Aster's top of book.
  - A name inside an ex-dividend window (hyperaster's exdiv.json, from 2 days before to 1 day after) alerts
    at >= 1.0% held for 3 checks.
  - Any other name needs >= 2.0% held for 30 checks (catches dividends missing from the calendar; stale
    off-hours quotes on thin Aster books are what the wait and the depth floor filter out).
  - Either way Aster must show >= $5k within 0.5% of the top on the side you'd trade.
  - Re-alerts when the gap grows another 0.5%; says when it closes (< 0.3%).
"""
import argparse
import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import telegram_notify as tg

HL = "https://api.hyperliquid.xyz/info"
ASTER = "https://fapi.asterdex.com"
EXDIV = Path(os.environ.get("DIVGAP_EXDIV", "/opt/hyperaster/exdiv.json"))
ALIAS = {"SKHX": "SKHYNIXUSDT", "SMSN": "SAMSUNGUSDT"}     # xyz name -> Aster symbol where they differ
ASIA_OPEN = {"SKHX", "SMSN", "HYUNDAI", "SKHY", "KR200", "SOFTBANK", "JP225", "KIOXIA"}   # opens 00:00 UTC
CAL_MIN, CAL_N = 0.010, 3
ANY_MIN, ANY_N = 0.020, 30
STEP, CLOSE = 0.005, 0.003
DEPTH_MIN, DEPTH_BAND = 5_000.0, 0.005
PERIOD_S = 60

log = logging.getLogger("divgap")


def get(url, body=None, timeout=20):
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None,
                                 {"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------- pure helpers
def gap(h_bid, h_ask, a_bid, a_ask):
    """(gap, side): the better of short-Aster (Aster bid over HL ask) and short-HL (HL bid over Aster ask)."""
    g_a = a_bid / h_ask - 1 if h_ask > 0 else 0.0
    g_h = h_bid / a_ask - 1 if a_ask > 0 else 0.0
    return (g_a, "short Aster / long HL") if g_a >= g_h else (g_h, "short HL / long Aster")


def windows(cal, today):
    """{name: entry} whose ex-dividend window, widened 2 days before and 1 after, contains `today` (ISO)."""
    d = datetime.fromisoformat(today).date()
    out = {}
    for n, ents in (cal.get("names") or {}).items():
        for e in ents:
            lo = datetime.fromisoformat(e["from"]).date()
            hi = datetime.fromisoformat(e["to"]).date()
            if (lo - d).days <= 2 and (d - hi).days <= 1:
                out[n.upper()] = e
                break
    return out


def step(st, name, g, in_cal):
    """Alert state machine for one pair. Returns 'open', 'grow', 'close' or None, and updates st."""
    s = st.setdefault(name, {"n": 0, "alerted": 0.0})
    need, lo = (CAL_N, CAL_MIN) if in_cal else (ANY_N, ANY_MIN)
    s["n"] = s["n"] + 1 if g >= lo else 0
    if s["alerted"]:
        if g < CLOSE:
            s.update(n=0, alerted=0.0)
            return "close"
        if g >= s["alerted"] + STEP:
            s["alerted"] = g
            return "grow"
        return None
    if s["n"] >= need:
        s["alerted"] = g
        return "open"
    return None


def depth_usd(levels, top, band, side):
    """$ resting within `band` of the top price on one side of a book [[px, qty], ...]."""
    tot = 0.0
    for p, q in levels:
        p, q = float(p), float(q)
        if (side == "bid" and p < top * (1 - band)) or (side == "ask" and p > top * (1 + band)):
            break
        tot += p * q
    return tot


# ---------------------------------------------------------------- the watcher
class Watch:
    def __init__(self, datadir: Path, notify=tg.send, fetch=get):
        self.dir = Path(datadir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.notify, self.get = notify, fetch
        self.pairs: dict = {}
        self.pairs_at = 0.0
        self.st = self._load()

    def _load(self):
        try:
            return json.loads((self.dir / "state.json").read_text())
        except Exception:
            return {}

    def _save(self):
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(self.st))
        tmp.replace(self.dir / "state.json")

    def refresh_pairs(self):
        meta = self.get(HL, {"type": "meta", "dex": "xyz"})
        xyz = {u["name"].split(":")[-1] for u in meta["universe"] if not u.get("isDelisted")}
        info = self.get(f"{ASTER}/fapi/v1/exchangeInfo")
        # stocks and ETFs only: Aster's BBUSDT / QNTUSDT are crypto coins, not BlackBerry / the xyz QNT
        live = {s["symbol"] for s in info["symbols"] if s.get("status") == "TRADING"
                and {"STOCK", "ETF"} & set(s.get("underlyingSubType") or [])}
        self.pairs = {n: ALIAS.get(n, n + "USDT") for n in xyz if ALIAS.get(n, n + "USDT") in live}
        self.pairs_at = time.time()

    def check(self, now=None):
        now = now or time.time()
        if not self.pairs or now - self.pairs_at > 3600:
            self.refresh_pairs()
        meta, ctxs = self.get(HL, {"type": "metaAndAssetCtxs", "dex": "xyz"})
        hl = {}
        for u, c in zip(meta["universe"], ctxs):
            imp = c.get("impactPxs") or []
            if len(imp) == 2:
                hl[u["name"].split(":")[-1]] = (float(imp[0]), float(imp[1]), float(c.get("funding") or 0))
        book = {b["symbol"]: (float(b["bidPrice"] or 0), float(b["askPrice"] or 0))
                for b in self.get(f"{ASTER}/fapi/v1/ticker/bookTicker")}
        try:
            cal = json.loads(EXDIV.read_text())
        except Exception:
            cal = {}
        today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        win = windows(cal, today)
        pst = self.st.setdefault("pairs", {})
        for n, sym in sorted(self.pairs.items()):
            if n not in hl or sym not in book or min(book[sym]) <= 0:
                continue
            hb, ha, hf = hl[n]
            ab, aa = book[sym]
            g, side = gap(hb, ha, ab, aa)
            if g > 0.25:
                continue                                  # not the same thing (a different listing or units)
            ev = step(pst, n, g, n in win)
            if ev == "open" or ev == "grow":
                dep = self.depth(sym, ab if side.startswith("short Aster") else aa,
                                 "bid" if side.startswith("short Aster") else "ask")
                if dep is not None and dep < DEPTH_MIN:
                    pst[n].update(alerted=0.0, n=0)       # too thin to trade: forget it, look again
                    continue
                self.notify(self.msg(n, sym, g, side, win.get(n), dep, hf, ev, (hb, ha, ab, aa)))
            elif ev == "close":
                self.notify(f"✅ div gap {n} closed: Aster vs HL now {g:+.2%}")
        self._save()

    def depth(self, sym, top, side):
        try:
            d = self.get(f"{ASTER}/fapi/v1/depth?symbol={sym}&limit=50")
            return depth_usd(d["bids" if side == "bid" else "asks"], top, DEPTH_BAND, side)
        except Exception as e:
            log.warning(f"{sym} depth: {e}")
            return None

    @staticmethod
    def msg(n, sym, g, side, ent, dep, hf, ev, px):
        hb, ha, ab, aa = px
        legs = (f"Aster bid {ab:.6g} vs HL ask {ha:.6g}" if side.startswith("short Aster")
                else f"HL bid {hb:.6g} vs Aster ask {aa:.6g}")
        ex = (f"ex-div {ent['from']}{'' if ent['to'] == ent['from'] else '..' + ent['to']} (~{ent['pct']}%)" if ent
              else "<i>no ex-date in the calendar: check for a dividend or a stale quote</i>")
        opn = "00:00 UTC (Asian open)" if n in ASIA_OPEN else "~14:30 UTC (US open)"
        return (f"{'💱' if ev == 'open' else '📈'} <b>div gap {n}</b> {g:+.2%} — {side}\n"
                f"{legs} · {ex}\n"
                f"Aster {sym} depth within 0.5%: ${dep or 0:,.0f} · HL funding {hf * 100:+.4f}%/h\n"
                f"Usually closes at the {opn} after the ex-date. Alert only, nothing traded.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--datadir", default="/opt/hyperdata/divgap")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    w = Watch(Path(a.datadir))
    while True:
        try:
            w.check()
        except Exception as e:
            log.warning(f"check failed: {e}")
        if a.once:
            break
        time.sleep(PERIOD_S)


if __name__ == "__main__":
    main()
