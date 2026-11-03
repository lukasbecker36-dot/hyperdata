#!/usr/bin/env python3
"""
Stock-perp paper books -- simulate (never trade) two HL xyz strategies from the Nov 2026 study and keep score.

FUNDING CARRY ("carry")
  Every day at 00:00 UTC rank the xyz perps (no FX / commodities) with >= $200k 24h volume and >= 14 days
  of history by their mean funding over the last 24h. Short the highest-funding 20%, buy the lowest 20%,
  each side $1,000, inverse-volatility weighted (10d hourly vol). Each daily cohort is held 72h: the book
  is the average of the last three cohorts, rebalanced once a day. Shorts collect the funding longs pay.
  Backtest Apr-Nov 2026 at 2.5bp/side: Sharpe 1.1 dev (Apr-mid Aug), 3.9 holdout, maxDD -10%; negative in
  most variants before August, so the regime matters.

US INTRADAY MOMENTUM ("intraday")
  On full US trading days at 11:00 New York time (open + 90 min) take each US single-stock perp's move
  since the previous US close (16:00 NY), less the median move. Buy the top 10%, short the bottom 10%,
  $1,000 a side, inverse-vol weighted, needs >= $500k 24h volume. Close everything at 14:00 New York time.
  Backtest: Sharpe 2.8 dev / 2.5 holdout on the perps; 1.0 on 2-3 years of the underlying shares (2024 flat).

ACCOUNTING (per book): positions are $ notionals marked to the HL mid every hour; P&L = price + funding
(longs pay the hourly rate, shorts receive it) - costs (2.5bp of every $ traded: ~0.9bp growth-mode taker
fee + half spread). Results are on $1,000 a side; returns are quoted on that $1,000.

REQUESTS: one metaAndAssetCtxs per hour; at a rebalance one candleSnapshot (+ one fundingHistory for the
carry book) per eligible coin, paced to stay well inside HL's rate limit (a few minutes, once a day).

OUTPUT (datadir): state.json, trades.csv, equity.csv (hourly, cumulative per book), service.out.
/stocks in @Hyperfadebot shows the report; a weekly summary goes out Saturdays 12:00 UTC.

  python3 stock_paper.py --datadir ./stockpaper           # run
  python3 stock_paper.py --datadir ./stockpaper --report  # print the report
  python3 stock_paper.py --datadir ./stockpaper --dry     # compute today's carry + intraday picks, print, exit
"""
import argparse
import csv
import json
import math
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import telegram_notify as tg

HL_INFO = "https://api.hyperliquid.xyz/info"
COST = 0.00025                    # per $ traded
SIDE_USD = 1000.0
HOUR = 3600

FX_COMM = {"GOLD", "SILVER", "CL", "COPPER", "NATGAS", "PLATINUM", "PALLADIUM", "BRENTOIL", "HO", "JPY", "EUR", "GBP"}
INDEX = {"XYZ100", "SP500", "KR200", "JP225", "NIFTY", "EWY", "EWJ", "EWZ", "EWT", "SMH", "SOXL", "XLE", "XBI", "TLT",
         "MAGS", "KORU", "DRAM", "URNM"}
ASIA = {"SKHX", "SMSN", "SKHY", "HYUNDAI", "MINIMAX", "ZHIPU", "UNITREE", "CXMT", "KIOXIA", "SOFTBANK", "GIGADEV",
        "CAMBRICON", "RELIANCE", "JIO", "BABA", "UMC", "KSTR"}
PREIPO = {"SPCX", "ANT", "OURA", "SHEIN", "CBRS", "NCLD", "BIRD", "SHAZ", "LYTE", "QNT", "TWST", "PURRDAT", "STRC",
          "SNXX", "BOT"}
NOT_US_STOCK = FX_COMM | INDEX | ASIA | PREIPO

CARRY = dict(look_h=24, hold_d=3, q=0.2, min_vol=2e5, min_age_d=14)
INTRA = dict(q=0.1, min_vol=5e5, min_names=3, entry=(11, 0), exit=(14, 0))   # New York wall clock
# NYSE full closures and early closes (13:00 NY, before our 14:00 exit) -- skipped. Extend each year.
NYSE_CLOSED = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
               "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
NYSE_HALF = {"2026-11-27", "2026-12-24", "2027-11-26"}
EQ_COLS = ["ts", "book", "price", "funding", "cost", "total", "gross", "n"]
TR_COLS = ["ts", "book", "coin", "usd", "px", "reason"]


# ------------------------------------------------------------------ time
def _nth_sunday(year, month, n):
    d = datetime(year, month, 1, tzinfo=timezone.utc)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def ny_offset(ts):
    """UTC offset of New York (hours) at unix time ts: -4 in DST (2nd Sun Mar 07:00 UTC -> 1st Sun Nov 06:00 UTC)."""
    t = datetime.fromtimestamp(ts, timezone.utc)
    start = _nth_sunday(t.year, 3, 2) + timedelta(hours=7)
    end = _nth_sunday(t.year, 11, 1) + timedelta(hours=6)
    return -4 if start <= t < end else -5


def ny_time(day, hh, mm=0):
    """Unix time of New York wall clock hh:mm on date `day` (YYYY-MM-DD)."""
    naive = datetime.strptime(day, "%Y-%m-%d").replace(hour=hh, minute=mm, tzinfo=timezone.utc).timestamp()
    return int(naive - ny_offset(naive + 5 * HOUR) * HOUR)   # offset at that wall time (+5h ~ its UTC moment)


def ny_day(ts):
    return datetime.fromtimestamp(ts + ny_offset(ts) * HOUR, timezone.utc).strftime("%Y-%m-%d")


def is_full_trading_day(day):
    return (datetime.strptime(day, "%Y-%m-%d").weekday() < 5 and day not in NYSE_CLOSED and day not in NYSE_HALF)


def prev_trading_day(day):
    d = datetime.strptime(day, "%Y-%m-%d")
    while True:
        d -= timedelta(days=1)
        s = d.strftime("%Y-%m-%d")
        if d.weekday() < 5 and s not in NYSE_CLOSED:
            return s


# ------------------------------------------------------------------ data
_last_req = [0.0]


def _post(body, pace=0.0, tries=6):
    for k in range(tries):
        wait = _last_req[0] + pace - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.time()
        try:
            req = urllib.request.Request(HL_INFO, json.dumps(body).encode(),
                                         {"Content-Type": "application/json", "User-Agent": "stock-paper"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or k == tries - 1:
                raise
            time.sleep(10 * (k + 1))


def snapshot():
    """{coin: {"mid", "funding" (hourly), "vol" ($ 24h)}} for every listed xyz perp."""
    meta, ctxs = _post({"type": "metaAndAssetCtxs", "dex": "xyz"})
    out = {}
    for u, c in zip(meta["universe"], ctxs):
        if u.get("isDelisted"):
            continue
        mid = _f(c.get("midPx")) or _f(c.get("markPx"))
        if mid:
            out[u["name"].split(":", 1)[-1]] = {"mid": mid, "funding": _f(c.get("funding")) or 0.0,
                                                 "vol": _f(c.get("dayNtlVlm")) or 0.0}
    return out


def candles(coin, start_ts, end_ts):
    """[(open_ts, close)] hourly."""
    d = _post({"type": "candleSnapshot", "req": {"coin": f"xyz:{coin}", "interval": "1h",
                                                 "startTime": int(start_ts * 1000), "endTime": int(end_ts * 1000)}},
              pace=1.5)
    return [(int(x["t"]) // 1000, float(x["c"])) for x in d]


def funding_mean(coin, start_ts):
    d = _post({"type": "fundingHistory", "coin": f"xyz:{coin}", "startTime": int(start_ts * 1000)}, pace=1.5)
    v = [float(x["fundingRate"]) for x in d]
    return statistics.mean(v) if v else None


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def hourly_vol(closes):
    r = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    return statistics.pstdev(r) if len(r) >= 48 else None


# ------------------------------------------------------------------ portfolio construction
def long_short(signal, vol, q, side_usd=SIDE_USD, min_n=3):
    """Long the top q of `signal`, short the bottom q, each side `side_usd` split by inverse vol. {coin: usd}."""
    s = sorted((v, c) for c, v in signal.items() if v is not None and vol.get(c))
    n = int(len(s) * q)
    if n < min_n:
        return {}
    out = {}
    for names, sign in (([c for _, c in s[-n:]], 1), ([c for _, c in s[:n]], -1)):
        iv = {c: 1 / vol[c] for c in names}
        tot = sum(iv.values())
        for c in names:
            out[c] = sign * side_usd * iv[c] / tot
    return out


def carry_target(cohorts, hold_d=CARRY["hold_d"]):
    """Average of the last `hold_d` daily cohorts ({coin: usd})."""
    cs = cohorts[-hold_d:]
    out = {}
    for w in cs:
        for c, usd in w.items():
            out[c] = out.get(c, 0.0) + usd / hold_d
    return out


def intraday_signal(now_px, prev_close):
    """Move since the previous US close, less the median move."""
    mv = {c: math.log(now_px[c] / prev_close[c]) for c in now_px if prev_close.get(c) and now_px[c] > 0}
    if not mv:
        return {}
    med = statistics.median(mv.values())
    return {c: v - med for c, v in mv.items()}


# ------------------------------------------------------------------ book accounting
def new_book():
    return {"pos": {}, "price": 0.0, "funding": 0.0, "cost": 0.0, "t_mark": None}


def mark(book, snap, now):
    """Mark positions to `snap` mids; accrue funding at the current hourly rate for the time since the last mark."""
    hours = (now - book["t_mark"]) / HOUR if book["t_mark"] is not None else 0.0
    for c, p in book["pos"].items():
        s = snap.get(c)
        if not s:
            continue                                  # no quote: carry the last mark
        book["price"] += p["usd"] * (s["mid"] / p["px"] - 1)
        p["usd"] *= s["mid"] / p["px"]
        p["px"] = s["mid"]
        book["funding"] -= p["usd"] * s["funding"] * hours
    book["t_mark"] = now


def rebalance(book, target, snap, now, name, reason, trades):
    """Trade to `target` ({coin: usd}) at the mids; 2.5bp of every $ traded."""
    for c in set(book["pos"]) | set(target):
        cur = book["pos"].get(c, {}).get("usd", 0.0)
        want = target.get(c, 0.0)
        s = snap.get(c)
        if abs(want - cur) < 1e-9:
            continue
        if not s:                                     # can't trade without a quote: keep what we have
            continue
        book["cost"] += abs(want - cur) * COST
        trades.append({"ts": int(now), "book": name, "coin": c, "usd": f"{want - cur:.2f}",
                       "px": f"{s['mid']:.6g}", "reason": reason})
        if abs(want) < 1e-9:
            book["pos"].pop(c, None)
        else:
            book["pos"][c] = {"usd": want, "px": s["mid"]}


def equity_row(book, name, now):
    tot = book["price"] + book["funding"] - book["cost"]
    return {"ts": int(now), "book": name, "price": f"{book['price']:.4f}", "funding": f"{book['funding']:.4f}",
            "cost": f"{book['cost']:.4f}", "total": f"{tot:.4f}",
            "gross": f"{sum(abs(p['usd']) for p in book['pos'].values()):.2f}", "n": len(book["pos"])}


# ------------------------------------------------------------------ the two strategies
def eligible(snap, min_vol, us_only=False):
    return [c for c, s in snap.items() if s["vol"] >= min_vol and c not in FX_COMM
            and (not us_only or c not in NOT_US_STOCK)]


def build_carry(snap, now, log=print):
    coins = eligible(snap, CARRY["min_vol"])
    sig, vol = {}, {}
    for c in coins:
        try:
            cl = candles(c, now - CARRY["min_age_d"] * 86400 - HOUR, now)
            if not cl or cl[0][0] > now - CARRY["min_age_d"] * 86400 + 6 * HOUR:
                continue                              # listed < 14 days
            vol[c] = hourly_vol([x[1] for x in cl[-241:]])
            fm = funding_mean(c, now - CARRY["look_h"] * HOUR)
            if fm is not None:
                sig[c] = -fm                          # high funding -> short
        except Exception as e:
            log(f"carry: {c} skipped ({e})")
    return long_short(sig, vol, CARRY["q"]), {c: -v * 8760 for c, v in sig.items()}


def build_intraday(snap, now, day, log=print):
    pday = prev_trading_day(day)
    t_close = ny_time(pday, 16)
    sig_px, prev, vol = {}, {}, {}
    for c in eligible(snap, INTRA["min_vol"], us_only=True):
        try:
            cl = candles(c, now - 11 * 86400, now)
            closes = dict(cl)
            pc = closes.get(t_close - HOUR)           # the hourly candle ending at the US close
            if pc:
                prev[c], sig_px[c] = pc, snap[c]["mid"]
                vol[c] = hourly_vol([x[1] for x in cl[-241:]])
        except Exception as e:
            log(f"intraday: {c} skipped ({e})")
    sig = intraday_signal(sig_px, prev)
    return long_short(sig, vol, INTRA["q"], min_n=INTRA["min_names"]), sig


# ------------------------------------------------------------------ storage
def load_state(d):
    try:
        return json.loads((d / "state.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"carry": new_book(), "intraday": new_book(), "cohorts": [], "carry_day": None, "intra_day": None,
                "intra_open": False, "started": int(time.time()), "last_hour": None, "last_week_report": None}


def save_state(d, st):
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(st))
    tmp.replace(d / "state.json")


def append(path, cols, rows):
    if not rows:
        return
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------ one hourly tick
def tick(st, snap, now, d, log=print, builders=None):
    """Mark, then run whatever decision falls due at `now`. Returns the trades made."""
    b = builders or {"carry": build_carry, "intraday": build_intraday}
    trades = []
    for name in ("carry", "intraday"):
        mark(st[name], snap, now)
    utc_day = time.strftime("%Y-%m-%d", time.gmtime(now))
    hour = time.gmtime(now).tm_hour
    if hour == 0 and st["carry_day"] != utc_day:
        st["carry_day"] = utc_day
        w, fund = b["carry"](snap, now, log)
        if w:
            st["cohorts"] = (st["cohorts"] + [w])[-CARRY["hold_d"]:]
            rebalance(st["carry"], carry_target(st["cohorts"]), snap, now, "carry", "daily rebalance", trades)
            longs = sorted((c for c in w if w[c] > 0), key=lambda c: fund.get(c, 0))
            shorts = sorted((c for c in w if w[c] < 0), key=lambda c: -fund.get(c, 0))
            log(f"carry cohort {utc_day}: long {', '.join(longs)} | short {', '.join(shorts)}")
    day = ny_day(now)
    t_in = ny_time(day, *INTRA["entry"])
    if st["intra_open"] and now >= ny_time(st["intra_open"], *INTRA["exit"]):
        rebalance(st["intraday"], {}, snap, now, "intraday", "exit 14:00 NY", trades)
        st["intra_open"] = False
    if (is_full_trading_day(day) and st["intra_day"] != day and t_in <= now < t_in + 1800
            and not st["intra_open"]):
        st["intra_day"] = day
        w, sig = b["intraday"](snap, now, day, log)
        if w:
            rebalance(st["intraday"], w, snap, now, "intraday", "entry 11:00 NY", trades)
            st["intra_open"] = day
            log(f"intraday {day}: long {', '.join(c for c in w if w[c] > 0)} | short "
                f"{', '.join(c for c in w if w[c] < 0)}")
        else:
            log(f"intraday {day}: too few names, no trade")
    append(d / "trades.csv", TR_COLS, trades)
    append(d / "equity.csv", EQ_COLS, [equity_row(st[n], n, now) for n in ("carry", "intraday")])
    return trades


# ------------------------------------------------------------------ report
def _daily(rows, book):
    """Cumulative total at each UTC day's last mark -> list of (day, total)."""
    last = {}
    for r in rows:
        if r["book"] == book:
            last[time.strftime("%Y-%m-%d", time.gmtime(int(r["ts"])))] = float(r["total"])
    return sorted(last.items())


def report(datadir, now=None):
    d = Path(datadir)
    st = load_state(d)
    try:
        with open(d / "equity.csv", newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        rows = []
    now = now or time.time()
    lines = [f"<b>Stock-perp paper books</b> (HL xyz, $1,000 a side, 2.5bp/side costs) · since "
             f"{time.strftime('%d %b', time.gmtime(st['started']))}"]
    desc = {"carry": "Funding carry — short top-20% 24h funding, long bottom-20%, 3-day cohorts, rebalanced 00:00 UTC",
            "intraday": "US intraday momentum — 11:00 NY buy top-10% / short bottom-10% move since last close, out 14:00 NY"}
    bt = {"carry": "backtest SR 1.1 dev / 3.9 holdout", "intraday": "backtest SR 2.8 dev / 2.5 holdout (1.0 on 2y of shares)"}
    combo = {}
    for name in ("carry", "intraday"):
        b = st[name]
        tot = b["price"] + b["funding"] - b["cost"]
        days = _daily(rows, name)
        rets = [(y - x) / SIDE_USD for (_, x), (_, y) in zip(days, days[1:])]
        for (dy, _), r in zip(days[1:], rets):
            combo[dy] = combo.get(dy, 0.0) + r
        sr = (statistics.mean(rets) / statistics.pstdev(rets) * math.sqrt(365)
              if len(rets) >= 5 and statistics.pstdev(rets) > 0 else None)
        wk = [r for (dy, _), r in zip(days[1:], rets) if dy >= time.strftime("%Y-%m-%d", time.gmtime(now - 7 * 86400))]
        lines += [f"\n<b>{desc[name]}</b>",
                  f"P&amp;L <b>${tot:+,.2f}</b> ({tot / SIDE_USD:+.1%}) · price ${b['price']:+,.2f} · funding "
                  f"${b['funding']:+,.2f} · costs −${b['cost']:,.2f}",
                  f"{len(rets)} days · last 7d {sum(wk):+.1%} · Sharpe {'%+.1f' % sr if sr is not None else 'n/a (under 5 days)'}"
                  f" · <i>{bt[name]}</i>"]
        if b["pos"]:
            pos = sorted(b["pos"].items(), key=lambda kv: -kv[1]["usd"])
            lines.append("long " + ", ".join(f"{c} ${p['usd']:,.0f}" for c, p in pos if p["usd"] > 0)
                         + " | short " + ", ".join(f"{c} ${-p['usd']:,.0f}" for c, p in pos[::-1] if p["usd"] < 0))
        elif name == "intraday":
            lines.append("flat (trades 11:00-14:00 New York on full US trading days)")
    if len(combo) >= 5:
        v = [combo[k] for k in sorted(combo)]
        sd = statistics.pstdev(v)
        lines.append(f"\n<b>Both books</b>: {sum(v):+.1%} on $1,000 a side · Sharpe "
                     f"{statistics.mean(v) / sd * math.sqrt(365) if sd else 0:+.1f} over {len(v)} days")
    lines.append("\n<i>Judge after 4-6 weeks: target live Sharpe ≥ 1 on both before risking money.</i>")
    return "\n".join(lines)


# ------------------------------------------------------------------ main loop
def run(datadir):
    d = Path(datadir)
    d.mkdir(parents=True, exist_ok=True)
    st = load_state(d)
    print(f"stock paper up | {d}", flush=True)
    while True:
        now = time.time()
        hour_ts = int(now // HOUR * HOUR)
        if st["last_hour"] != hour_ts and now - hour_ts >= 20:     # once per hour, 20s after the boundary
            try:
                snap = snapshot()
                tick(st, snap, hour_ts, d, log=lambda m: print(m, flush=True))
                st["last_hour"] = hour_ts
                save_state(d, st)
                t = time.gmtime(now)
                week = time.strftime("%G-%V", t)
                if t.tm_wday == 5 and t.tm_hour == 12 and st.get("last_week_report") != week:
                    st["last_week_report"] = week
                    save_state(d, st)
                    tg.send(report(d))
            except Exception as e:                                  # never die on a bad response
                print(f"tick failed: {e}", flush=True)
        time.sleep(30)


def dry():
    now = time.time()
    snap = snapshot()
    w, fund = build_carry(snap, now)
    print("CARRY cohort (usd):", {c: round(v) for c, v in sorted(w.items(), key=lambda kv: kv[1])})
    day = ny_day(now)
    if not is_full_trading_day(day):
        day = prev_trading_day(day)
    w, sig = build_intraday(snap, now, day)
    print("INTRADAY picks if entered now (usd):", {c: round(v) for c, v in sorted(w.items(), key=lambda kv: kv[1])})


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datadir", default="./stockpaper")
    p.add_argument("--report", action="store_true")
    p.add_argument("--dry", action="store_true")
    a = p.parse_args()
    if a.report:
        print(report(a.datadir).replace("&amp;", "&"))
    elif a.dry:
        dry()
    else:
        run(a.datadir)


if __name__ == "__main__":
    main()
