#!/usr/bin/env python3
"""
Pre-unlock short bot -- MEXC futures, approve-by-Telegram.

STRATEGY (scratchpad unl_*.py study, 2026-10-25)
  Tokens bleed into a large cliff unlock and on the unlock day itself. Over 380 one-day
  cliffs of >=5% of circulating supply (DefiLlama schedules, Mar 2024 - Oct 2026), a short
  opened ~5 days before and closed at the end of the unlock day, hedged with an equal BTC
  long, made about +$3 per $100 (median similar, ~66% winners), against ~$0 for the same
  short on random dates of the same tokens. The edge weakened from mid-2025 (+$1-3 vs
  +$5), so expect the low end. Larger and rarer unlocks did better.

  ENTRY   ~5 days before the unlock: short the token, long the same notional of BTC,
          both 1x isolated. Only after a human taps "Short" in Telegram -- the schedule
          data is good but not perfect (delays, wrong ticker), so a person checks it.
  EXIT    at the end of the unlock day (00:00 UTC on T+1, unlocks are mostly 00:00 on T).
          Holding longer adds little and the day after has recently bounced.
  RISK    the tail is a squeeze: about 1 trade in 100 doubled against the short. 1x
          isolated caps that at the leg's own margin. There is no stop (none was tested);
          the bot alerts at +25% and +50% adverse and /unlock_close closes on demand.

FLOW
  every --refresh-hours   pull DefiLlama unlock schedules + MEXC contracts, build the
                          candidate list (candidates.json), alert on any cliff >= --min-size
                          now inside its entry window, with Short / Skip buttons
  telegram_monitor.py     owns the Telegram long-poll (one consumer per bot token), so it
                          turns a button tap or /unlock_* command into a request file in
                          <datadir>/requests/. It never holds the MEXC key.
  this process            consumes requests, works the orders, closes on schedule, and
                          keeps state.json (read by /unlocks)

EXECUTION -- maker first, taker fallback
  Each leg rests a post-only order at the touch and re-pegs it every --repeg-sec while
  the market moves, for up to --maker-minutes; whatever is still unfilled then crosses at
  market. MEXC charges these contracts 0bp maker / 0-2bp taker, so the fee is not the
  point -- the SPREAD is: these books run 10-75bp wide, and four crossings on a 30-75bp
  book cost 0.6-1.5% against a ~3% edge. The edge accrues over days, so waiting minutes
  for a passive fill costs nothing measurable. The exit gets the same treatment, starting
  at the scheduled time.

  python3 unlock_bot.py --datadir ./unlocks             # dry run: alerts + paper fills
  python3 unlock_bot.py --datadir ./unlocks --live      # real orders (needs MEXC_FUTURES_*)
  python3 unlock_bot.py --datadir ./unlocks --scan      # one refresh, print candidates, exit
"""
import argparse
import bisect
import gzip
import json
import math
import os
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import mexc_api
import mexc_futures as mf
import telegram_notify as tg

DAY = 86400
LLAMA = "https://defillama-datasets.llama.fi/emissions"
GECKO_LIST = "https://api.coingecko.com/api/v3/coins/list"
GECKO_PRICES = "https://api.coingecko.com/api/v3/simple/price"
UA = {"User-Agent": "unlock-bot/1.0"}
HEDGE = "BTC_USDT"


def now_s():
    return time.time()


def day_str(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _get_json(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------------------- schedules
def _compact(p):
    """One protocol's schedule, reduced to what the study used: cliff events summed per
    UTC day, and the cumulative documented unlocked supply by day (the denominator)."""
    try:
        d = _get_json(f"{LLAMA}/{p}")
    except Exception:
        return None
    cum = {}
    for s in (d.get("documentedData") or {}).get("data", []):
        for r in s.get("data", []):
            cum[r["timestamp"]] = cum.get(r["timestamp"], 0) + (r.get("unlocked") or 0)
    cliffs = {}
    for e in (d.get("metadata") or {}).get("events", []) or []:
        if e.get("unlockType") != "cliff":
            continue
        try:
            t = int(float(e["timestamp"]))
            n = sum(float(x) for x in e.get("noOfTokens", []) if x)
        except (TypeError, ValueError, KeyError):
            continue
        if n <= 0:
            continue
        k = t // DAY * DAY
        c = cliffs.setdefault(k, {"tokens": 0.0, "cats": set()})
        c["tokens"] += n
        if e.get("category"):
            c["cats"].add(e["category"])
    return {"gecko": d.get("gecko_id"), "name": d.get("name"),
            "cum": sorted(cum.items()),
            "cliffs": {str(k): {"tokens": v["tokens"], "cats": sorted(v["cats"])}
                       for k, v in cliffs.items()}}


def fetch_schedules(workers=8):
    protos = _get_json(LLAMA + "ProtocolsList")
    with ThreadPoolExecutor(workers) as ex:
        out = [s for s in ex.map(_compact, protos) if s and s["gecko"] and s["cum"]]
    return out


def cliff_table(sched, now):
    """[(day_ts, size, tokens, cats)] for every cliff, size = tokens / documented unlocked
    supply the day before -- the study's definition -- plus how many >=2% cliffs the token
    had in the year before `now` (rare unlockers did ~2x better than monthly ones)."""
    ts = [t for t, _ in sched["cum"]]
    cum = [v for _, v in sched["cum"]]
    rows, past = [], 0
    for k, c in sched["cliffs"].items():
        k = int(k)
        i = bisect.bisect_left(ts, k) - 1
        if i < 0 or cum[i] <= 0:
            continue
        size = c["tokens"] / cum[i]
        rows.append((k, size, c["tokens"], c["cats"]))
        if size >= 0.02 and now - 365 * DAY <= k < now:
            past += 1
    return rows, past


class UnlockBot:
    def __init__(self, a):
        self.a = a
        self.dir = a.datadir
        os.makedirs(os.path.join(self.dir, "requests"), exist_ok=True)
        self.live = a.live
        self.ex = mf.MexcFutures()
        if self.live and not self.ex.enabled():
            raise SystemExit("--live needs MEXC_FUTURES_KEY and MEXC_FUTURES_SECRET")
        self.state_path = os.path.join(self.dir, "state.json")
        self.state = self._load()
        self.last_refresh = 0
        self.last_marks = 0

    # ---- persistence -------------------------------------------------------------------
    def _load(self):
        try:
            with open(self.state_path) as f:
                st = json.load(f)
        except Exception:
            st = {}
        st.setdefault("alerted", {})
        st.setdefault("skipped", {})
        st.setdefault("trades", {})
        return st

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_path)

    def log(self, msg):
        line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}"
        print(line, flush=True)
        with open(os.path.join(self.dir, "unlock_bot.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def notify(self, msg, buttons=None):
        self.log("TG " + msg.replace("\n", " | "))
        tg.send(msg, buttons=buttons)

    # ---- candidates --------------------------------------------------------------------
    def _gecko_symbols(self):
        """CoinGecko id -> ticker, cached for a week (the list is ~15k coins and the free
        endpoint rate-limits)."""
        path = os.path.join(self.dir, "gecko_list.json")
        try:
            if now_s() - os.path.getmtime(path) < 7 * DAY:
                with open(path) as f:
                    return json.load(f)
        except OSError:
            pass
        try:
            m = {c["id"]: c["symbol"].upper() for c in _get_json(GECKO_LIST)}
            with open(path, "w") as f:
                json.dump(m, f)
            return m
        except Exception as e:
            self.log(f"coingecko list failed ({e}); using cache")
            with open(path) as f:
                return json.load(f)

    def _gecko_prices(self, ids):
        out = {}
        ids = sorted(set(ids))
        for i in range(0, len(ids), 100):
            q = ",".join(ids[i:i + 100])
            try:
                d = _get_json(f"{GECKO_PRICES}?ids={q}&vs_currencies=usd")
                out.update({k: v.get("usd") for k, v in d.items()})
            except Exception as e:
                self.log(f"coingecko prices failed: {e}")
        return out

    def refresh(self):
        """Rebuild candidates.json: every cliff >= 2% in the next 30 days on a token with
        an API-tradeable MEXC crypto perp whose price agrees with CoinGecko's for the
        scheduled token (a ticker shared by two coins is the main way this data lies)."""
        t0 = now_s()
        scheds = fetch_schedules()
        gsym = self._gecko_symbols()
        detail = {c["symbol"]: c for c in mexc_api.contract_detail()
                  if c.get("quoteCoin") == "USDT" and c.get("state") == 0}
        tickers = {t["symbol"]: t for t in mexc_api.get("/ticker") or []}
        rows = []
        for s in scheds:
            sym = gsym.get(s["gecko"])
            if not sym:
                continue
            table, past = cliff_table(s, t0)
            for k, size, tokens, cats in table:
                if size < 0.02 or not (t0 - DAY < k <= t0 + 30 * DAY):
                    continue
                cs = f"{sym}_USDT"
                c = detail.get(cs)
                ok, why = (mexc_api.is_crypto_token(c) if c else (False, "no MEXC perp"))
                if c and ok and not c.get("apiAllowed"):
                    ok, why = False, "MEXC API not allowed"
                tk = tickers.get(cs) or {}
                rows.append({"key": f"{sym}_{day_str(k).replace('-', '')}", "sym": sym,
                             "contract": cs, "project": s["name"], "gecko": s["gecko"],
                             "unlock_ts": k, "unlock": day_str(k), "size": round(size, 4),
                             "tokens": tokens, "cats": cats, "big_cliffs_past_yr": past,
                             "tradeable": bool(c and ok), "why_not": None if ok else why,
                             "mexc_px": float(tk.get("lastPrice") or 0),
                             "funding": float(tk.get("fundingRate") or 0),
                             "vol24_usd": float(tk.get("amount24") or 0)})
        gp = self._gecko_prices([r["gecko"] for r in rows if r["tradeable"]])
        for r in rows:
            g = gp.get(r["gecko"])
            if r["tradeable"] and g and r["mexc_px"] and abs(r["mexc_px"] / g - 1) > 0.15:
                r["tradeable"], r["why_not"] = False, f"ticker clash (MEXC {r['mexc_px']:.4g} vs CG {g:.4g})"
        rows.sort(key=lambda r: r["unlock_ts"])
        with open(os.path.join(self.dir, "candidates.json"), "w") as f:
            json.dump({"refreshed": t0, "rows": rows}, f, indent=1)
        self.last_refresh = t0
        n = sum(1 for r in rows if r["tradeable"] and r["size"] >= self.a.min_size)
        self.log(f"refresh: {len(scheds)} schedules, {len(rows)} cliffs >=2% in 30d, "
                 f"{n} tradeable >= {self.a.min_size:.0%} ({now_s() - t0:.0f}s)")
        self._check_schedule_changes(rows)
        return rows

    def _check_schedule_changes(self, rows):
        """An open trade whose unlock vanished or moved is the 'project delayed it' case:
        the reason for the short is gone. Alert, don't act -- a schedule edit can also be
        DefiLlama tidying data."""
        live = {r["key"] for r in rows}
        for key, t in self.state["trades"].items():
            if t["status"] in ("open", "opening") and key not in live and not t.get("sched_warned"):
                t["sched_warned"] = True
                self.notify(f"⚠️ <b>{t['sym']}</b>: the {t['unlock']} unlock is no longer in "
                            f"DefiLlama's schedule (delayed or re-dated?). Check it; "
                            f"/unlock_close {t['sym']} closes the trade.")
        self.save()

    def alerts(self, rows):
        now = now_s()
        for r in rows:
            if not r["tradeable"] or r["size"] < self.a.min_size:
                continue
            key = r["key"]
            if key in self.state["alerted"] or key in self.state["trades"] or key in self.state["skipped"]:
                continue
            days = (r["unlock_ts"] - now) / DAY
            if not (self.a.late_days <= days <= self.a.entry_days):
                continue
            self.state["alerted"][key] = now
            self.save()
            rare = "rare unlocker" if r["big_cliffs_past_yr"] <= 6 else "frequent unlocker"
            msg = (f"🔓 <b>Unlock short: {r['sym']}</b> ({r['project']})\n"
                   f"unlock {r['unlock']} 00:00 UTC — in {days:.1f} days\n"
                   f"size <b>{r['size']:.1%}</b> of circulating · {', '.join(r['cats']) or 'n/a'}\n"
                   f"{r['big_cliffs_past_yr']} cliffs ≥2% in past year ({rare})\n"
                   f"MEXC {r['contract']} · 24h vol ${r['vol24_usd'] / 1e3:,.0f}k · "
                   f"funding {r['funding'] * 1e4:+.1f}bp\n"
                   f"plan: short ${self.a.notional:.0f} + long ${self.a.notional:.0f} BTC, 1x "
                   f"isolated, close {day_str(r['unlock_ts'] + DAY)} 00:00 UTC\n"
                   f"<i>verify the date: tokenomist.ai/{r['gecko']}</i>")
            self.notify(msg, buttons=[[(f"Short {r['sym']}", f"unl:go:{key}"),
                                       ("Skip", f"unl:skip:{key}")]])

    def candidate(self, key=None, sym=None):
        try:
            with open(os.path.join(self.dir, "candidates.json")) as f:
                rows = json.load(f)["rows"]
        except Exception:
            return None
        now = now_s()
        for r in rows:
            if key and r["key"] == key:
                return r
            if sym and not key and r["sym"] == sym.upper() and r["unlock_ts"] > now and r["tradeable"]:
                return r
        return None

    # ---- execution ---------------------------------------------------------------------
    @staticmethod
    def _round_px(px, unit):
        dec = max(0, -int(math.floor(math.log10(unit)))) if unit < 1 else 0
        return round(round(px / unit) * unit, dec)

    def work(self, contract, side, vol, position_id=None, tag=""):
        """Fill `vol` contracts maker-first, then taker. Returns (filled, avg_px)."""
        is_buy = side in (mf.OPEN_LONG, mf.CLOSE_SHORT)
        info = self.ex.contract(contract)
        unit = float(info["priceUnit"])
        if not self.live:
            tk = self.ex.ticker(contract)
            px = float(tk["bid1"] if is_buy else tk["ask1"])
            self.log(f"PAPER {tag} {contract} side={side} vol={vol} @ {px} (resting at touch)")
            return vol, px
        filled, notional = 0, 0.0
        deadline = now_s() + self.a.maker_minutes * 60
        errors = 0
        while filled < vol and now_s() < deadline and errors < 5:
            tk = self.ex.ticker(contract)
            px = self._round_px(float(tk["bid1"] if is_buy else tk["ask1"]), unit)
            try:
                oid = self.ex.submit(contract, side, vol - filled, price=px,
                                     order_type=mf.POST_ONLY, position_id=position_id)
            except mf.MexcFuturesError as e:
                errors += 1
                self.log(f"{tag} post-only rejected ({e}); retry")
                time.sleep(3)
                continue
            rest_until = min(deadline, now_s() + self.a.repeg_sec)
            o = None
            while now_s() < rest_until:
                time.sleep(4)
                o = self.ex.get_order(oid)
                if int(o.get("state", 0)) in (mf.ST_FILLED, mf.ST_CANCELLED, mf.ST_INVALID):
                    break
                touch = float(self.ex.ticker(contract)["bid1" if is_buy else "ask1"])
                if abs(touch - px) >= unit / 2:
                    break                           # market moved: re-peg
            if o is None or int(o.get("state", 0)) == mf.ST_OPEN:
                try:
                    self.ex.cancel([oid])
                except mf.MexcFuturesError as e:
                    self.log(f"{tag} cancel {oid}: {e}")      # usually: filled meanwhile
                time.sleep(1)
                o = self.ex.get_order(oid)
            dv = float(o.get("dealVol") or 0)
            if dv:
                filled += dv
                notional += dv * float(o.get("dealAvgPrice") or px)
                self.log(f"{tag} maker fill {dv} @ {o.get('dealAvgPrice')} ({filled}/{vol})")
        if filled < vol:
            rest = vol - filled
            oid = self.ex.submit(contract, side, rest, order_type=mf.MARKET, position_id=position_id)
            for _ in range(15):
                time.sleep(2)
                o = self.ex.get_order(oid)
                if int(o.get("state", 0)) in (mf.ST_FILLED, mf.ST_CANCELLED, mf.ST_INVALID):
                    break
            dv = float(o.get("dealVol") or 0)
            filled += dv
            notional += dv * float(o.get("dealAvgPrice") or 0)
            self.log(f"{tag} taker fill {dv} @ {o.get('dealAvgPrice')} ({filled}/{vol})")
        return filled, (notional / filled if filled else 0.0)

    def _vol_for(self, contract, usd):
        info = self.ex.contract(contract)
        px = float(self.ex.ticker(contract)["lastPrice"])
        vol = int(usd / (px * float(info["contractSize"])))
        return max(vol, int(info.get("minVol") or 1)), px, float(info["contractSize"])

    def open_trade(self, key):
        r = self.candidate(key=key)
        if r is None:
            return self.notify(f"❌ {key}: not in the current candidate list (refreshing daily); not traded")
        if key in self.state["trades"]:
            return self.notify(f"{r['sym']}: already {self.state['trades'][key]['status']}")
        if r["unlock_ts"] + DAY <= now_s():
            return self.notify(f"❌ {r['sym']}: the unlock day is over; not traded")
        n_open = sum(1 for t in self.state["trades"].values() if t["status"] in ("open", "opening"))
        if n_open >= self.a.max_open:
            return self.notify(f"❌ {r['sym']}: {n_open} trades already open (cap {self.a.max_open})")
        if self.live:
            need = 2 * self.a.notional * 1.1
            avail = self.ex.usdt_available()
            if avail < need:
                return self.notify(f"❌ {r['sym']}: futures wallet has ${avail:,.0f} available, "
                                   f"needs ${need:,.0f}. Transfer USDT spot→futures first.")
        t = {"key": key, "sym": r["sym"], "contract": r["contract"], "unlock": r["unlock"],
             "unlock_ts": r["unlock_ts"], "size": r["size"], "status": "opening",
             "paper": not self.live, "opened_at": now_s(),
             "close_at": r["unlock_ts"] + DAY + 300, "alerted_adverse": []}
        self.state["trades"][key] = t
        self.save()
        self.notify(f"⏳ opening <b>{r['sym']}</b> short + BTC long, maker first "
                    f"(up to {self.a.maker_minutes} min per leg)…")
        try:
            vol, _, csize = self._vol_for(r["contract"], self.a.notional)
            f_alt, px_alt = self.work(r["contract"], mf.OPEN_SHORT, vol, tag=f"{r['sym']} open")
            t["alt"] = {"vol": f_alt, "px": px_alt, "csize": csize}
            self.save()
            if f_alt <= 0:
                t["status"] = "failed"
                self.save()
                return self.notify(f"❌ {r['sym']}: short did not fill; nothing open")
            hv, _, hcs = self._vol_for(HEDGE, f_alt * csize * px_alt)
            f_btc, px_btc = self.work(HEDGE, mf.OPEN_LONG, hv, tag=f"{r['sym']} hedge")
            t["btc"] = {"vol": f_btc, "px": px_btc, "csize": hcs}
            t["status"] = "open"
            self.save()
            self.notify(f"✅ <b>{r['sym']}</b> {'PAPER ' if t['paper'] else ''}open: short "
                        f"{f_alt:g} ct @ {px_alt:.6g} (${f_alt * csize * px_alt:,.0f}), long BTC "
                        f"{f_btc:g} ct @ {px_btc:,.1f} (${f_btc * hcs * px_btc:,.0f}).\n"
                        f"auto-close {day_str(t['close_at'])} 00:05 UTC · /unlock_close {r['sym']} to close early")
        except Exception as e:
            t["status"] = "check"
            t["error"] = str(e)
            self.save()
            self.log(traceback.format_exc())
            self.notify(f"🆘 <b>{r['sym']}</b> open failed part-way: {e}\n"
                        f"Check MEXC futures positions/orders by hand.")

    def close_trade(self, key, why):
        t = self.state["trades"][key]
        if t["status"] not in ("open", "check"):
            return
        t["status"] = "closing"
        self.save()
        try:
            pnl = 0.0
            for leg, contract, side in (("alt", t["contract"], mf.CLOSE_SHORT),
                                        ("btc", HEDGE, mf.CLOSE_LONG)):
                L = t.get(leg)
                if not L or not L.get("vol"):
                    continue
                pid, vol = None, L["vol"]
                if self.live:
                    want = 2 if side == mf.CLOSE_SHORT else 1      # positionType 1 long, 2 short
                    pos = [p for p in self.ex.open_positions(contract)
                           if int(p.get("positionType", 0)) == want]
                    if not pos:
                        self.log(f"{t['sym']} {leg}: no {contract} position on MEXC; skipping")
                        continue
                    pid = pos[0].get("positionId")
                    vol = min(vol, float(pos[0].get("holdVol") or vol))
                f, px = self.work(contract, side, vol, position_id=pid, tag=f"{t['sym']} close {leg}")
                L["exit_px"] = px
                sign = -1 if leg == "alt" else 1
                pnl += sign * (px - L["px"]) * f * L["csize"]
            t["pnl"] = round(pnl, 2)
            t["status"] = "closed"
            t["closed_at"] = now_s()
            t["close_reason"] = why
            self.save()
            a = t["alt"]
            self.notify(f"🏁 <b>{t['sym']}</b> {'PAPER ' if t['paper'] else ''}closed ({why}): "
                        f"short {a['px']:.6g} → {a.get('exit_px', 0):.6g}, "
                        f"P&amp;L incl. BTC hedge <b>${pnl:+.2f}</b> (before funding)")
        except Exception as e:
            t["status"] = "check"
            t["error"] = str(e)
            self.save()
            self.log(traceback.format_exc())
            self.notify(f"🆘 <b>{t['sym']}</b> close failed part-way: {e}\nCheck MEXC by hand.")

    # ---- loop pieces -------------------------------------------------------------------
    def requests(self):
        rq = os.path.join(self.dir, "requests")
        for name in sorted(os.listdir(rq)):
            path = os.path.join(rq, name)
            try:
                with open(path) as f:
                    req = json.load(f)
            except Exception:
                req = None
            os.remove(path)
            if not req:
                continue
            act, key, sym = req.get("action"), req.get("key"), (req.get("sym") or "").upper()
            self.log(f"request {req}")
            if act == "go":
                if not key and sym:
                    r = self.candidate(sym=sym)
                    key = r["key"] if r else None
                if not key:
                    self.notify(f"❌ no upcoming tradeable unlock for {sym or '?'} — /unlocks lists them")
                    continue
                self.open_trade(key)
            elif act == "skip":
                if not key and sym:
                    r = self.candidate(sym=sym)
                    key = r["key"] if r else None
                if key:
                    self.state["skipped"][key] = now_s()
                    self.save()
                    self.notify(f"skipped {key}")
            elif act == "close":
                keys = [k for k, t in self.state["trades"].items()
                        if t["sym"] == sym and t["status"] in ("open", "check")]
                if not keys:
                    self.notify(f"no open unlock trade for {sym}")
                for k in keys:
                    self.close_trade(k, "manual")

    def schedule_exits(self):
        for key, t in list(self.state["trades"].items()):
            if t["status"] == "open" and now_s() >= t["close_at"]:
                self.close_trade(key, "end of unlock day")

    def marks(self):
        """Adverse-move alerts on the short leg (the squeeze tail is the risk)."""
        if now_s() - self.last_marks < 300:
            return
        self.last_marks = now_s()
        for t in self.state["trades"].values():
            if t["status"] != "open" or not t.get("alt"):
                continue
            try:
                px = float(self.ex.ticker(t["contract"])["lastPrice"])
            except Exception:
                continue
            up = px / t["alt"]["px"] - 1
            t["last_px"] = px
            for lvl in (0.25, 0.50):
                if up >= lvl and lvl not in t["alerted_adverse"]:
                    t["alerted_adverse"].append(lvl)
                    self.notify(f"⚠️ <b>{t['sym']}</b> is {up:+.0%} against the short "
                                f"(entry {t['alt']['px']:.6g}, now {px:.6g}). 1x isolated "
                                f"liquidates near +100%. /unlock_close {t['sym']} to close.")
        self.save()

    def startup_check(self):
        for t in self.state["trades"].values():
            if t["status"] in ("opening", "closing"):
                was = "open" if t["status"] == "opening" else "close"
                t["status"] = "check"
                self.notify(f"🆘 <b>{t['sym']}</b> was mid-{was} when the bot restarted. "
                            f"Check MEXC by hand; /unlock_close {t['sym']} closes whatever "
                            f"the state file recorded.")
        self.save()

    def run(self):
        self.log(f"unlock bot up | {'LIVE' if self.live else 'DRY RUN'} | notional "
                 f"${self.a.notional:.0f} | min size {self.a.min_size:.0%} | window "
                 f"T-{self.a.entry_days}..T-{self.a.late_days}")
        self.startup_check()
        while True:
            try:
                if now_s() - self.last_refresh > self.a.refresh_hours * 3600:
                    self.alerts(self.refresh())
                self.requests()
                self.schedule_exits()
                self.marks()
            except Exception as e:
                self.log(f"loop error: {e}\n{traceback.format_exc()}")
                if now_s() - self.last_refresh > self.a.refresh_hours * 3600:
                    self.last_refresh = now_s() - (self.a.refresh_hours - 0.25) * 3600  # retry in 15 min
            time.sleep(10)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datadir", default="./unlocks")
    p.add_argument("--live", action="store_true", help="place real orders")
    p.add_argument("--notional", type=float, default=100.0, help="USD per leg")
    p.add_argument("--min-size", type=float, default=0.05, help="cliff as a fraction of circulating")
    p.add_argument("--entry-days", type=float, default=5.5, help="alert from this many days before")
    p.add_argument("--late-days", type=float, default=1.5, help="no alert closer than this")
    p.add_argument("--max-open", type=int, default=6)
    p.add_argument("--maker-minutes", type=float, default=20)
    p.add_argument("--repeg-sec", type=float, default=30)
    p.add_argument("--refresh-hours", type=float, default=12)
    p.add_argument("--scan", action="store_true", help="refresh once, print candidates, exit")
    a = p.parse_args()
    bot = UnlockBot(a)
    if a.scan:
        for r in bot.refresh():
            flag = "TRADE" if r["tradeable"] and r["size"] >= a.min_size else ""
            print(f"{r['unlock']}  {r['sym']:8s} {r['size']:6.1%}  past-yr {r['big_cliffs_past_yr']:2d}  "
                  f"{'ok' if r['tradeable'] else r['why_not']:30s} {flag}")
        return
    bot.run()


if __name__ == "__main__":
    main()
