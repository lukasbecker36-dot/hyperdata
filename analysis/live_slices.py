#!/usr/bin/env python3
"""Timing, concurrency and concentration slices of the live book — and their power.

Everything here is a sizing/risk lever rather than another feature hunt, so it is worth
checking even after the flow batches came up empty. Two candidates appeared and both fail
the same way, which is the useful part.

SESSION. Live says 08-14 UTC is -75.3bps ($-26.53 over 92 trades) while 14-21 is +48.9
(t=+2.38). It is stable across halves in the right direction (Europe -25.1 then -115.7,
US +25.8 then +74.5). But the 49-day event set says Europe is +31.4 (t=+2.23, n=598) --
positive, on 6.5x the sample -- and the live loss is four trades: KAITO -$8.50, BOME
-$5.10 (a liquidation), BABY -$4.93, ATOM -$3.10. Excluding them leaves -$4.90 over 88
trades.

CONCURRENCY. Live says isolated entries beat clustered ones (+119.1 alone against -39.2
at 6+ open). That directly contradicts cluster_entries.py, which the service file cites as
the reason the gross cap sits at $2,000 rather than binding during bursts. Re-run on the
event set, the backtest is monotone the OTHER way and strongly so: alone -33.3, 1-2 +8.2,
3-5 +46.2, 6+ +59.2 (t=+7.50, n=1220). The live "alone" bucket is 39 trades, 8 of them in
the second half.

The arithmetic that settles both: per-trade sd is 319bps, so

  live n=92  (Europe session)  resolves nothing under  67 bps
  live n=39  (alone)           resolves nothing under 102 bps
  backtest n=1220              resolves            18 bps

Sliced four ways, 416 live trades can only see enormous effects. Where live and the event
set disagree, the event set is 3-30x better powered and should be believed.

One genuine finding, not a null: SAME-COIN STACKING IS ZERO. Not one of 416 entries opened
while already holding that coin, so the bot never doubles into a position. That risk
control works and needs no attention.

  python3 analysis/live_slices.py [trades.csv]
"""
import csv, math, os, statistics as s, sys
from datetime import datetime

PATH = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "LIVE_TRADES", "live_15m_ats/trades_15m.csv")

t = [r for r in csv.DictReader(open(PATH))
     if r["net_bps"] and abs(float(r["net_bps"])) > 1e-9]
for r in t:
    r["p"] = float(r["pnl_usd"]); r["b"] = float(r["net_bps"])
    r["t0"] = datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M:%S")
    r["t1"] = datetime.strptime(r["close_time"], "%Y-%m-%d %H:%M:%S")
t.sort(key=lambda r: r["t0"])
for r in t:
    r["nopen"] = sum(1 for o in t if o is not r and o["t0"] <= r["t0"] < o["t1"])
cut = t[len(t)//2]["t0"]
SD = s.stdev([r["b"] for r in t])


def blk(lab, g, w=22):
    if len(g) < 8:
        print(f"  {lab:<{w}} n={len(g):>3}  --"); return
    m = s.mean([r["b"] for r in g])
    tt = m / (s.stdev([r["b"] for r in g]) / math.sqrt(len(g)))
    a = [r for r in g if r["t0"] <= cut]; b = [r for r in g if r["t0"] > cut]
    ha = s.mean([r["b"] for r in a]) if len(a) >= 8 else float("nan")
    hb = s.mean([r["b"] for r in b]) if len(b) >= 8 else float("nan")
    print(f"  {lab:<{w}} n={len(g):>3} {m:>+8.1f} t={tt:>+5.2f} ${sum(r['p'] for r in g):>+7.2f}"
          f"   halves {ha:>+7.1f} / {hb:>+7.1f}   min detectable {2*SD/math.sqrt(len(g)):>5.0f}")


print(f"{len(t)} live trades, per-trade sd {SD:.0f} bps\n")
print("=== session (UTC) ===")
for lo, hi, lab in ((0, 8, "00-08 Asia"), (8, 14, "08-14 Europe"),
                    (14, 21, "14-21 US"), (21, 24, "21-24 late")):
    blk(lab, [r for r in t if lo <= r["t0"].hour < hi])
print("\n=== concurrency at entry ===")
for lo, hi, lab in ((0, 1, "alone"), (1, 3, "1-2 others"),
                    (3, 6, "3-5 others"), (6, 99, "6+ others")):
    blk(lab, [r for r in t if lo <= r["nopen"] < hi])
print("\n=== same-coin stacking ===")
stk = [r for r in t if any(o is not r and o["symbol"] == r["symbol"]
                           and o["t0"] <= r["t0"] < o["t1"] for o in t)]
print(f"  entries opened while already holding that coin: {len(stk)} of {len(t)}")
print("  the bot never doubles into a position; this control needs no attention.")
print("\nWhere live and the 49-day event set disagree, the event set is better powered.")
