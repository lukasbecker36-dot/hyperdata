#!/usr/bin/env python3
"""ROI per dollar, on four different definitions of "a dollar".

"Return" is meaningless here without saying which denominator, because this strategy's
denominators differ by two orders of magnitude. It turns over its whole book several
times a day, so return-on-turnover is tiny; it holds ~2 positions at a time against a
40-position allowance, so return-on-capital-actually-used is large and return-on-capital-
you-must-keep-parked is small again. All three are true at once.

The denominators, weakest claim to strongest:

  TRADED     total notional pushed through the market. Measures the edge per unit of
             execution, which is the number to compare against fees and slippage.
  AVERAGE    time-weighted mean margin posted. What the strategy actually consumed.
             This is the honest "return on capital employed".
  PEAK       largest margin posted at any instant. What you must never have less than.
  PARKED     collateral that has to sit in the account for the caps to be reachable.
             The real denominator if this is the only thing the money is doing.

The account cannot answer this: it also carries hyperaster's positions. So capital is
reconstructed from the bot's own entry/exit timeline.

  python3 analysis/live_roi.py [trades.csv] [--adjusted]
"""
import csv, sys
from datetime import datetime

args = [a for a in sys.argv[1:] if not a.startswith("--")]
ADJ = "--adjusted" in sys.argv
PATH = args[0] if args else "live_15m_ats/trades_15m.csv"
LEV = 3.0                    # every one of these 177 trades ran at 3x isolated
ADJUST = {("CASHCAT", "2026-08-04 23:16:28"): -3.6482}     # see cashcat_counterfactual.py

rows = []
for r in csv.DictReader(open(PATH)):
    try:
        net = float(r["net_bps"])
        if abs(net) < 1e-9:
            continue
        pnl = float(r["pnl_usd"])
        if ADJ and (r["symbol"], r["entry_time"]) in ADJUST:
            pnl = ADJUST[(r["symbol"], r["entry_time"])]
        rows.append(dict(
            sym=r["symbol"], pnl=pnl, ntl=abs(float(r["pnl_usd"]) / (net / 1e4)),
            fee=float(r["fee_usd"] or 0),
            t0=datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M:%S"),
            t1=datetime.strptime(r["close_time"], "%Y-%m-%d %H:%M:%S")))
    except Exception:
        pass
rows.sort(key=lambda r: r["t0"])
N = len(rows)
PNL = sum(r["pnl"] for r in rows)
FEES = sum(r["fee"] for r in rows)
T0, T1 = rows[0]["t0"], max(r["t1"] for r in rows)
DAYS = (T1 - T0).total_seconds() / 86400
ANN = 365.0 / DAYS

print(f"{'ADJUSTED (strategy view)' if ADJ else 'AS BOOKED (money actually made)'}"
      f" — {N} trades over {DAYS:.2f} days")
print(f"net P&L ${PNL:+.2f}   fees ${FEES:.2f}\n")

# ---------- build the exposure timeline ----------
ev = []
for r in rows:
    ev.append((r["t0"], +r["ntl"]))
    ev.append((r["t1"], -r["ntl"]))
ev.sort(key=lambda x: x[0])
cur = 0.0
prev = T0
area = 0.0          # notional-seconds
peak = 0.0
peak_at = T0
npos = 0
peak_n = 0
open_area = 0.0
for t, dn in ev:
    dt = (t - prev).total_seconds()
    area += cur * dt
    open_area += npos * dt
    prev = t
    cur += dn
    npos += 1 if dn > 0 else -1
    if cur > peak:
        peak, peak_at, peak_n = cur, t, npos
span_s = (T1 - T0).total_seconds()
avg_ntl = area / span_s
avg_pos = open_area / span_s
turn_1way = sum(r["ntl"] for r in rows)
turn_rt = 2 * turn_1way

print("=== capital actually used ===")
print(f"  notional traded, one way      ${turn_1way:>10,.0f}")
print(f"  notional traded, round trip   ${turn_rt:>10,.0f}   "
      f"({turn_rt/DAYS:,.0f}/day)")
print(f"  avg gross exposure            ${avg_ntl:>10,.2f}   "
      f"({avg_pos:.2f} positions open on average)")
print(f"  peak gross exposure           ${peak:>10,.2f}   "
      f"({peak_n} positions, {peak_at:%Y-%m-%d %H:%M})")
print(f"  avg margin posted @ {LEV:.0f}x       ${avg_ntl/LEV:>10,.2f}")
print(f"  peak margin posted @ {LEV:.0f}x      ${peak/LEV:>10,.2f}")
print(f"  book turned over {turn_1way/avg_ntl/DAYS:.1f}x per day\n")


def roi(label, denom, note=""):
    if denom <= 0:
        return
    r = PNL / denom
    print(f"  {label:<34} ${denom:>9,.2f}   {r*100:>+9.3f}%   "
          f"{r*ANN*100:>+11.1f}%   {note}")


print("=== ROI per dollar ===")
print(f"  {'denominator':<34} {'$':>10}   {'over %.1fd' % DAYS:>9}   "
      f"{'annualised':>11}")
roi("per $ TRADED (one way)", turn_1way, "= the edge per unit of execution")
roi("per $ TRADED (round trip)", turn_rt, "comparable to a fee rate")
roi("per $ AVERAGE gross exposure", avg_ntl, "unlevered")
roi("per $ AVERAGE margin (3x)", avg_ntl / LEV, "<-- return on capital employed")
roi("per $ PEAK gross exposure", peak, "unlevered")
roi("per $ PEAK margin (3x)", peak / LEV, "<-- capital you must always have")

print("\n=== ROI per dollar you would have to PARK ===")
print("  the caps only mean something if the collateral to reach them is present:")
for lab, need in (("observed peak margin", peak / LEV),
                  ("20 per-side cap x $35 / 3x", 20 * 35 / LEV),
                  ("40-position cap x $35 / 3x", 40 * 35 / LEV),
                  ("$2,000 gross cap / 3x", 2000 / LEV),
                  ("actual spot USDC in the account", 395.67)):
    roi(lab, need)

print(f"\n=== what the fees cost, in the same units ===")
print(f"  fees ${FEES:.2f} = {FEES/turn_rt*1e4:.2f} bps of round-trip notional, "
      f"{FEES/(PNL+FEES)*100:.0f}% of gross profit")

print(f"\n=== per trade ===")
print(f"  mean notional ${turn_1way/N:.2f}   mean P&L ${PNL/N:+.4f}   "
      f"{PNL/turn_1way*1e4:+.1f} bps")
print(f"  {N/DAYS:.1f} trades/day")

print("\nCaveat that outranks every number above: net P&L over 177 trades carries")
print("t = +0.82 (+0.99 adjusted). None of these returns is yet distinguishable from")
print("zero, so the annualised column is arithmetic, not a forecast. It is also")
print("compounding-free: it assumes the same dollar size, not reinvestment.")
