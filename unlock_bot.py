#!/usr/bin/env python3
"""
Pre-unlock short ALERTS -- you trade on MEXC futures by hand, the bot times it.

STRATEGY (scratchpad unl_*.py study, 2026-10-25)
  Tokens bleed into a large cliff unlock and on the unlock day itself. Over 380 one-day
  cliffs of >=5% of circulating supply (DefiLlama schedules, Mar 2024 - Oct 2026), a short
  opened ~5 days before and closed at the end of the unlock day, hedged with an equal BTC
  long, made about +$3 per $100 (median similar, ~66% winners), against ~$0 for the same
  short on random dates of the same tokens. The edge weakened from mid-2025 (+$1-3 vs
  +$5), so expect the low end. Larger and rarer unlocks did better.

  ENTRY   ~5 days before the unlock: short the token, long the same notional of BTC,
          both 1x isolated.
  EXIT    at the end of the unlock day (00:00 UTC on T+1, unlocks are mostly 00:00 on T).
          Holding longer adds little and the day after has recently bounced.
  RISK    the tail is a squeeze: about 1 trade in 100 doubled against the short. 1x
          isolated caps that at the leg's own margin. No stop was tested; the bot warns
          at +25% and +50% against the short.

WHY ALERTS AND NOT ORDERS
  MEXC's contract API has had order placement "under maintenance" since 2022 (the reason
  provision_exec.py trades spot), so nothing here can trade futures. The bot does the part
  that is easy to get wrong by hand -- spotting the unlock, the timing, the close -- and
  you place the orders in the MEXC app. Use Post Only limits at the touch: fees are 0bp
  maker, but these books are 10-75bp wide and crossing four times costs a real slice of a
  ~3% edge.

FLOW
  every --refresh-hours   pull DefiLlama schedules + MEXC contracts into candidates.json;
                          alert on each cliff >= --min-size once it is inside its entry
                          window, with "I shorted it" / "Skip" buttons
  telegram_monitor.py     owns the Telegram long-poll (one consumer per bot token) and turns
                          taps and /unlock_* commands into request files in <datadir>/requests/
  this process            records the trades you report, warns on squeezes, reminds you to
                          close at 00:00 UTC after the unlock day (again 6h later if you have
                          not marked it closed), and keeps state.json for /unlocks

P&L shown is indicative: MEXC last prices when you tapped "shorted" and "closed", not your
fills.

  python3 unlock_bot.py --datadir ./unlocks           # run
  python3 unlock_bot.py --datadir ./unlocks --scan    # one refresh, print candidates, exit
"""
import argparse
import bisect
import json
import os
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import mexc_api
import telegram_notify as tg

DAY = 86400
LLAMA = "https://defillama-datasets.llama.fi/emissions"
GECKO_LIST = "https://api.coingecko.com/api/v3/coins/list"
GECKO_PRICES = "https://api.coingecko.com/api/v3/simple/price"
UA = {"User-Agent": "unlock-bot/1.0"}
HEDGE = "BTC_USDT"
REMIND_AGAIN_H = 6


def now_s():
    return time.time()


def day_str(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _get_json(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def last_price(contract):
    t = mexc_api.get("/ticker", {"symbol": contract}) or {}
    return float(t.get("lastPrice") or 0)


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
        a MEXC crypto perp whose price agrees with CoinGecko's for the scheduled token (a
        ticker shared by two coins is the main way this data lies)."""
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
        the reason for the short is gone. Warn -- a schedule edit can also be DefiLlama
        tidying data, so the call is yours."""
        live = {r["key"] for r in rows}
        for key, t in self.state["trades"].items():
            if t["status"] == "open" and key not in live and not t.get("sched_warned"):
                t["sched_warned"] = True
                self.notify(f"⚠️ <b>{t['sym']}</b>: the {t['unlock']} unlock is no longer in "
                            f"DefiLlama's schedule (delayed or re-dated?). Check it; close "
                            f"the short if the unlock moved.")
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
            n = self.a.notional
            px = r["mexc_px"]
            qty = f" ≈ {n / px:,.0f} {r['sym']}" if px else ""
            rare = "rare unlocker" if r["big_cliffs_past_yr"] <= 6 else "frequent unlocker"
            msg = (f"🔓 <b>Unlock short: {r['sym']}</b> ({r['project']})\n"
                   f"unlock {r['unlock']} 00:00 UTC — in {days:.1f} days\n"
                   f"size <b>{r['size']:.1%}</b> of circulating · {', '.join(r['cats']) or 'n/a'}\n"
                   f"{r['big_cliffs_past_yr']} cliffs ≥2% in past year ({rare})\n"
                   f"24h vol ${r['vol24_usd'] / 1e3:,.0f}k · funding {r['funding'] * 1e4:+.1f}bp\n\n"
                   f"<b>On MEXC futures</b> (Isolated, 1x, Post Only at the touch):\n"
                   f"1. {r['contract']}: open SHORT ${n:.0f}{qty}\n"
                   f"2. {HEDGE}: open LONG ${n:.0f}\n"
                   f"close both {day_str(r['unlock_ts'] + DAY)} 00:00 UTC (I'll remind you)\n"
                   f"<i>verify the date first: tokenomist.ai/{r['gecko']}</i>")
            self.notify(msg, buttons=[[(f"✅ I shorted {r['sym']}", f"unl:go:{key}"),
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
            if sym and not key and r["sym"] == sym.upper() and r["unlock_ts"] + DAY > now and r["tradeable"]:
                return r
        return None

    # ---- trades you report -------------------------------------------------------------
    def record_open(self, key):
        r = self.candidate(key=key)
        if r is None:
            return self.notify(f"❌ {key}: not in the current candidate list; not recorded")
        if key in self.state["trades"]:
            return self.notify(f"{r['sym']}: already recorded")
        try:
            alt_px, btc_px = last_price(r["contract"]), last_price(HEDGE)
        except Exception as e:
            alt_px = btc_px = 0.0
            self.log(f"price snapshot failed: {e}")
        close_at = r["unlock_ts"] + DAY
        self.state["trades"][key] = {
            "key": key, "sym": r["sym"], "contract": r["contract"], "unlock": r["unlock"],
            "unlock_ts": r["unlock_ts"], "size": r["size"], "status": "open",
            "opened_at": now_s(), "close_at": close_at, "alt_px": alt_px, "btc_px": btc_px,
            "notional": self.a.notional, "alerted_adverse": [], "reminders": 0}
        self.save()
        self.notify(f"📝 <b>{r['sym']}</b> short recorded (ref {alt_px:.6g}, BTC {btc_px:,.0f}).\n"
                    f"I'll remind you to close at {day_str(close_at)} 00:00 UTC and warn if it "
                    f"squeezes. /unlock_close {r['sym']} once you've closed.")

    def record_close(self, sym, why="you closed it"):
        keys = [k for k, t in self.state["trades"].items() if t["sym"] == sym and t["status"] == "open"]
        if not keys:
            return self.notify(f"no open unlock trade recorded for {sym}")
        for k in keys:
            t = self.state["trades"][k]
            try:
                alt, btc = last_price(t["contract"]), last_price(HEDGE)
            except Exception:
                alt = btc = 0.0
            pnl = None
            if t["alt_px"] and t["btc_px"] and alt and btc:
                n = t["notional"]
                pnl = n * (-(alt / t["alt_px"] - 1) + (btc / t["btc_px"] - 1))
            t.update(status="closed", closed_at=now_s(), alt_exit=alt, btc_exit=btc,
                     pnl=None if pnl is None else round(pnl, 2), close_reason=why)
            self.save()
            ptxt = f"indicative P&amp;L <b>${pnl:+.2f}</b> on ${t['notional']:.0f} (last prices, before fees/funding)" \
                if pnl is not None else "no price for P&amp;L"
            self.notify(f"🏁 <b>{sym}</b> marked closed: {ptxt}")

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
            if act in ("go", "skip") and not key and sym:
                r = self.candidate(sym=sym)
                key = r["key"] if r else None
                if not key:
                    self.notify(f"❌ no upcoming unlock for {sym} — /unlocks lists them")
                    continue
            if act == "go" and key:
                self.record_open(key)
            elif act == "skip" and key:
                self.state["skipped"][key] = now_s()
                self.save()
            elif act == "close":
                self.record_close(sym)

    def reminders(self):
        for t in self.state["trades"].values():
            if t["status"] != "open" or now_s() < t["close_at"]:
                continue
            due = t["close_at"] + t["reminders"] * REMIND_AGAIN_H * 3600
            if now_s() < due or t["reminders"] >= 4:
                continue
            t["reminders"] += 1
            self.save()
            again = " (reminder)" if t["reminders"] > 1 else ""
            self.notify(f"⏰ <b>Close {t['sym']} now</b>{again} — the {t['unlock']} unlock day is over.\n"
                        f"MEXC futures: close the {t['contract']} SHORT and the {HEDGE} LONG "
                        f"(Post Only at the touch; market if it doesn't fill in ~20 min).\n"
                        f"Then /unlock_close {t['sym']}",
                        buttons=[[(f"✅ Closed {t['sym']}", f"unl:close:{t['sym']}")]])

    def marks(self):
        """Squeeze warnings on the short leg (the tail is the risk)."""
        if now_s() - self.last_marks < 300:
            return
        self.last_marks = now_s()
        for t in self.state["trades"].values():
            if t["status"] != "open" or not t.get("alt_px"):
                continue
            try:
                px = last_price(t["contract"])
            except Exception:
                continue
            if not px:
                continue
            up = px / t["alt_px"] - 1
            t["last_px"] = px
            for lvl in (0.25, 0.50):
                if up >= lvl and lvl not in t["alerted_adverse"]:
                    t["alerted_adverse"].append(lvl)
                    self.notify(f"⚠️ <b>{t['sym']}</b> is {up:+.0%} against the short "
                                f"(ref {t['alt_px']:.6g}, now {px:.6g}). 1x isolated "
                                f"liquidates near +100%.")
        self.save()

    def run(self):
        self.log(f"unlock alerts up | ${self.a.notional:.0f}/leg | min size {self.a.min_size:.0%} "
                 f"| window T-{self.a.entry_days}..T-{self.a.late_days}")
        while True:
            try:
                if now_s() - self.last_refresh > self.a.refresh_hours * 3600:
                    self.alerts(self.refresh())
                self.requests()
                self.reminders()
                self.marks()
            except Exception as e:
                self.log(f"loop error: {e}\n{traceback.format_exc()}")
                if now_s() - self.last_refresh > self.a.refresh_hours * 3600:
                    self.last_refresh = now_s() - (self.a.refresh_hours - 0.25) * 3600  # retry in 15 min
            time.sleep(10)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datadir", default="./unlocks")
    p.add_argument("--notional", type=float, default=100.0, help="USD per leg, for the alert text")
    p.add_argument("--min-size", type=float, default=0.05, help="cliff as a fraction of circulating")
    p.add_argument("--entry-days", type=float, default=5.5, help="alert from this many days before")
    p.add_argument("--late-days", type=float, default=1.5, help="no alert closer than this")
    p.add_argument("--refresh-hours", type=float, default=12)
    p.add_argument("--scan", action="store_true", help="refresh once, print candidates, exit")
    a = p.parse_args()
    bot = UnlockBot(a)
    if a.scan:
        for r in bot.refresh():
            flag = "ALERT" if r["tradeable"] and r["size"] >= a.min_size else ""
            print(f"{r['unlock']}  {r['sym']:8s} {r['size']:6.1%}  past-yr {r['big_cliffs_past_yr']:2d}  "
                  f"{'ok' if r['tradeable'] else r['why_not']:30s} {flag}")
        return
    bot.run()


if __name__ == "__main__":
    main()
