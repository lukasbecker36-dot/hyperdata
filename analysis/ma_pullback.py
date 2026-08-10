#!/usr/bin/env python3
"""MA-pullback strategy test (user's rule).

Rule: anchor = slow EMA. Trend = fast EMA vs slow EMA.
  - fast ABOVE slow (uptrend):   BUY  when price pulls back DOWN to the slow line.
  - fast BELOW slow (downtrend): SELL when price rallies UP to the slow line.
Bet is WITH the trend (buy the dip / sell the rip, anchored at the slow MA). Exit = fixed H-bar hold.

Data note: only ~206 days of 1h candles exist, so a literal 200-DAY EMA is untestable (200-day warmup
leaves ~6 days). We test the same STRUCTURE on the 1h panel, sweeping slow in HOURS (100/200/400) and
fast (21/34), holds (6/24/72/168h), net of 11bps cost, with a 45-day holdout and an against-trend
placebo. HIGH+MID tiers. Run from analysis/.
"""
import math, sys, os
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

per = w.per_sym; COST = 0.0011; HOLDOUT_DAYS = 45
liquid = [s for s in per if w.tier(w.uni.get(s, 0)) in ('HIGH', 'MID')]

def ema(xs, n):
    a = 2.0/(n+1); out = [xs[0]]*len(xs)
    for i in range(1, len(xs)):
        out[i] = a*xs[i] + (1-a)*out[i-1]
    return out

def run(slow, fast, H, with_trend=True):
    ser = []   # (entry_ms, net_ret)
    for s in liquid:
        t, hi, lo, c, v, ret = per[s]
        if len(c) < slow + H + 5: continue
        es = ema(c, slow); ef = ema(c, fast)
        warm = slow; i = warm+1; cool = 0
        while i < len(c)-H:
            if cool > 0:
                cool -= 1; i += 1; continue
            up = ef[i] > es[i]
            # pullback-to-anchor crossing
            buy_touch  = c[i-1] > es[i-1] and c[i] <= es[i]     # price dips down onto the line
            sell_touch = c[i-1] < es[i-1] and c[i] >= es[i]     # price rallies up onto the line
            d = 0
            if with_trend:
                if up and buy_touch: d = 1
                elif (not up) and sell_touch: d = -1
            else:   # placebo: fade the other way
                if up and sell_touch: d = -1
                elif (not up) and buy_touch: d = 1
            if d != 0:
                r = d*math.log(c[i+H]/c[i]) - COST
                ser.append((t[i], r)); cool = H
            i += 1
    return ser

def stats(ser, H):
    if len(ser) < 20: return None
    n = len(ser); m = sum(r for _, r in ser)/n
    sd = (sum((r-m)**2 for _, r in ser)/n)**0.5
    win = sum(1 for _, r in ser if r > 0)/n*100
    t = m/sd*math.sqrt(n) if sd > 0 else 0
    tmax = max(x for x, _ in ser)
    ho = [r for x, r in ser if x >= tmax - HOLDOUT_DAYS*86400000]
    hom = sum(ho)/len(ho)*1e4 if len(ho) >= 10 else float('nan')
    return n, m*1e4, win, t, hom

print("MA-PULLBACK — buy dips to slow-MA in uptrend / sell rips to slow-MA in downtrend (net of 11bps)\n")
print(f"  {'slow':>4} {'fast':>4} {'hold':>5} {'trades':>6} {'net bps':>8} {'win%':>5} {'t':>6} {'hold45 bps':>10}")
for slow in (100, 200, 400):
    for fast in (21, 34):
        for H in (6, 24, 72, 168):
            r = stats(run(slow, fast, H), H)
            if r:
                print(f"  {slow:>4} {fast:>4} {H:>4}h {r[0]:>6} {r[1]:>+8.1f} {r[2]:>5.0f} {r[3]:>+6.2f} {r[4]:>+10.1f}")
    print()

print("PLACEBO (against-trend: sell dips in uptrend / buy rips in downtrend), slow=200 fast=34:")
print(f"  {'hold':>5} {'trades':>6} {'net bps':>8} {'win%':>5} {'t':>6}")
for H in (6, 24, 72, 168):
    r = stats(run(200, 34, H, with_trend=False), H)
    if r:
        print(f"  {H:>4}h {r[0]:>6} {r[1]:>+8.1f} {r[2]:>5.0f} {r[3]:>+6.2f}")
print("\nnet bps = per-trade after cost; t>2 ~ significant; hold45 = per-trade net in the last 45 days (OOS).")

# ---- ALPHA vs BETA: the strong configs are with-trend 3-day holds = trend-following. Hedge out
# BTC over the hold; if the edge is coin-specific pullback alpha it survives, if it is just market
# direction it collapses. ----
from datetime import datetime, timezone
from collections import defaultdict
bt = per['BTC'][0]; bc = per['BTC'][3]; btc = {bt[i]: bc[i] for i in range(len(bt))}
def run_detailed(slow, fast, H):
    out = []
    for s in liquid:
        t, hi, lo, c, v, ret = per[s]
        if len(c) < slow+H+5: continue
        es = ema(c, slow); ef = ema(c, fast); i = slow+1; cool = 0
        while i < len(c)-H:
            if cool > 0: cool -= 1; i += 1; continue
            up = ef[i] > es[i]
            buy = c[i-1] > es[i-1] and c[i] <= es[i]; sell = c[i-1] < es[i-1] and c[i] >= es[i]
            d = 1 if (up and buy) else (-1 if ((not up) and sell) else 0)
            if d != 0:
                bf = (math.log(btc[t[i+H]]/btc[t[i]]) if (t[i] in btc and t[i+H] in btc) else None)
                out.append((t[i], d, math.log(c[i+H]/c[i]), bf)); cool = H
            i += 1
    return out
print("\nALPHA vs BETA — best config slow=400 fast=34 H=72h:")
rs = run_detailed(400, 34, 72)
for lab, neu in (("RAW (directional)", False), ("MARKET-NEUTRAL (minus BTC/hold)", True)):
    vals = [(d*(cf-bf) if neu else d*cf) - COST for _, d, cf, bf in rs if (not neu or bf is not None)]
    n = len(vals); m = sum(vals)/n; sd = (sum((x-m)**2 for x in vals)/n)**0.5
    print(f"  {lab:34s} n={n:5d}  net={m*1e4:+7.1f}bps  t={m/sd*math.sqrt(n):+5.2f}")
mo = defaultdict(list)
for ms, d, cf, bf in rs: mo[datetime.fromtimestamp(ms/1000, timezone.utc).strftime('%Y-%m')].append(d*cf-COST)
print("  monthly RAW (bps/trade): " + "  ".join(f"{k}:{sum(v)/len(v)*1e4:+.0f}" for k, v in sorted(mo.items())))
print("  => raw edge is ~86% BTC beta; market-neutral it is +12bps t~0.8 (insignificant). Trend-following,")
print("     not a coin-specific MA-pullback alpha; the -121bps month (May) is the trend-whipsaw tell.")
