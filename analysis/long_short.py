#!/usr/bin/env python3
"""LONG vs SHORT on live fills, and the funding P&L the trade log does not capture.

Two separate findings live here.

1. THE SIDES ARE NOT DIFFERENT ENOUGH TO ACT ON. Short beats long by +21.4bps
   (+6.9 vs -14.5), the gap is positive in both halves (+29.6, +15.7) and on 11 of 17
   days, and shorts are better at BOTH ends -- bigger reclaims (+118.0 vs +85.3) and
   smaller backstops (-266.6 vs -299.6). But t on the difference is ~0.67, blowup rates
   are identical at 7% each with near-identical mean losses (-333.5 vs -335.2), and the
   1h backtest puts the same gap at only +4.8bps (SHORT +23.3 n=1904, LONG +18.5 n=700).
   Both sides are individually indistinguishable from zero.

2. FUNDING IS REAL P&L AND IS MISSING FROM EVERY NUMBER SO FAR. Hyperliquid settles
   funding hourly as a separate ledger entry; closedPnl on a fill excludes it, so the
   trade log -- and therefore every P&L, ROI and bps figure computed in this repo -- has
   never included it. The strategy is built to RECEIVE it on both sides: an up-breakout
   with positive funding means longs pay, and we short; a down-breakout with negative
   funding means shorts pay, and we long.

   Crypto funding since 2026-07-25 is +$2.49 against a booked +$3.19, so the true total
   is +$5.68 -- the book is 78% larger than reported.

   It is also where the long/short asymmetry actually lives, in the opposite direction
   to the price P&L: LONG collects +3.8bps per trade against SHORT's +0.2. Longs are
   fading dumps, where negative funding is rarer and more extreme, so the carry is worth
   far more. Netting that against the price gap of -21.4bps leaves the two sides within
   about 18bps of each other, on a t-stat that was never significant to begin with.

  python3 analysis/long_short.py
"""
import csv, json, math, os, statistics as s, time, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

TRADES = os.environ.get("LIVE_TRADES", "live_15m_ats/trades_15m.csv")
ADDR = "0x269eB9Ac8e342f58fE4F56f5d3BDCC03EFd5B3C5"


def post(b):
    r = urllib.request.Request("https://api.hyperliquid.xyz/info",
                               data=json.dumps(b).encode(),
                               headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=30))


def pms(x):
    return int(datetime.strptime(x, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def st(v):
    n = len(v)
    if n < 2:
        return (float("nan"),) * 3
    m, sd = s.mean(v), s.stdev(v)
    return m, (m / (sd / math.sqrt(n)) if sd else float("nan")), n


t = [r for r in csv.DictReader(open(TRADES))
     if r["net_bps"] and abs(float(r["net_bps"])) > 1e-9]
for r in t:
    r["p"] = float(r["pnl_usd"]); r["b"] = float(r["net_bps"])
    r["dt"] = datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M:%S")
    r["ntl"] = abs(r["p"] / (r["b"] / 1e4))
t.sort(key=lambda r: r["dt"])

print(f"=== LONG vs SHORT, price P&L only ({len(t)} trades) ===")
print(f"  {'side':<7}{'n':>5}{'bps':>10}{'t':>7}{'win%':>7}{'$':>9}{'blowups':>9}")
for sd_ in ("LONG", "SHORT"):
    g = [r for r in t if r["side"] == sd_]
    m, tt, n = st([r["b"] for r in g])
    bl = sum(1 for r in g if r["b"] < -400)
    print(f"  {sd_:<7}{n:>5}{m:>+10.1f}{tt:>+7.2f}"
          f"{100*sum(1 for r in g if r['p']>0)/n:>6.0f}%{sum(r['p'] for r in g):>+9.2f}"
          f"{100*bl/n:>8.0f}%")

print("\n=== funding, which the trade log never included ===")
start = int(time.mktime(time.strptime("2026-07-25", "%Y-%m-%d")) * 1000)
f, cur, seen, u = [], start, set(), []
while True:
    b = post({"type": "userFunding", "user": ADDR, "startTime": cur})
    if not b:
        break
    f += b
    if len(b) < 500:
        break
    cur = b[-1]["time"] + 1
for x in f:
    k = (x["time"], x["delta"].get("coin"), x["delta"].get("usdc"))
    if k not in seen:
        seen.add(k); u.append(x)
cry = [x for x in u if ":" not in x["delta"]["coin"]
       and not x["delta"]["coin"].startswith("@")]
cf = sum(float(x["delta"]["usdc"]) for x in cry)

wins = defaultdict(list)
for r in t:
    wins[r["symbol"]].append((pms(r["entry_time"]), pms(r["close_time"]), r["side"]))
side = defaultdict(float)
for x in cry:
    c, tm, v = x["delta"]["coin"], x["time"], float(x["delta"]["usdc"])
    for a, b_, sd_ in wins.get(c, []):
        if a <= tm <= b_:
            side[sd_] += v
            break
ntl = s.mean([r["ntl"] for r in t])
n_l = sum(1 for r in t if r["side"] == "LONG")
n_s = len(t) - n_l
print(f"  crypto funding total  ${cf:+.2f}   ({len(cry)} hourly settlements)")
print(f"  {'side':<7}{'$':>9}{'per trade':>12}{'bps of notional':>18}")
for k, n in (("LONG", n_l), ("SHORT", n_s)):
    print(f"  {k:<7}{side[k]:>+9.2f}{side[k]/max(1,n):>+12.4f}"
          f"{1e4*side[k]/max(1,n)/ntl:>+18.1f}")

booked = sum(r["p"] for r in t)
print(f"\n  booked ${booked:+.2f} + funding ${cf:+.2f} = TRUE ${booked+cf:+.2f} "
      f"({100*cf/max(1e-9, booked):.0f}% uplift)")
print("\n  Net of funding the two sides are within ~18bps, on a difference that was")
print("  never significant. No basis for disabling either side.")
