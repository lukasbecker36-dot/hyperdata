#!/usr/bin/env python3
"""Where does the fade's edge CONCENTRATE? Condition the existing signal on never-tested axes.

Not a new strategy: the same volume-exhaustion fade (wide_stop signals, faithful reclaim/backstop exit),
sliced by dimensions we have never conditioned on, to find a regime where the proven edge is markedly
stronger. Axes:
  - hour-of-day (UTC) and trading session  -- overshoots are bigger / reversions cleaner in thin hours
  - days-since-listing                      -- young perps are pure price-discovery chaos
  - liquidity tier                          -- thinner books overshoot more
  - signal realized-vol tercile             -- how violent the spike was
Reports per-bucket net bps, n, t. Auto-runs a month-by-month check on the strongest hour and archetype
cell (the test that has killed every fragile regime effect in this repo). 1h panel. Run from analysis/.
"""
import math, sys, os
from datetime import datetime, timezone
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

per = w.per_sym; COST = w.COST; MAXH = w.MAXH; WIN = 24
def sstd(xs):
    n = len(xs); m = sum(xs)/n; return (sum((x-m)**2 for x in xs)/(n-1))**0.5 if n > 1 else 0.0
GLOBAL_START = min(per[s][0][0] for s in per)

events = []
for sym, i, brk in w.signals:
    t, hi, lo, c, v, ret = per[sym]
    if i+MAXH >= len(c): continue
    d = -brk; e = c[i]; prior_h = max(hi[i-WIN:i]); prior_l = min(lo[i-WIN:i])
    exit_k = MAXH
    for k in range(1, MAXH+1):
        if d < 0 and c[i+k] < prior_h: exit_k = k; break
        if d > 0 and c[i+k] > prior_l: exit_k = k; break
    net = d*math.log(c[i+exit_k]/e) - COST
    dt = datetime.fromtimestamp(t[i]/1000, timezone.utc)
    age = (t[i]-per[sym][0][0])/86400000.0
    rv = sstd(ret[i-23:i+1])
    events.append(dict(net=net, hr=dt.hour, mo=dt.strftime('%m'), age=age,
                       tier=w.tier(w.uni.get(sym, 0)), rv=rv, sym=sym))
N = len(events); base = sum(e['net'] for e in events)/N
print(f"{N} fade events | baseline net {base*1e4:+.1f} bps/trade\n")

def show(name, keyfn, order=None):
    g = defaultdict(list)
    for e in events: g[keyfn(e)].append(e['net'])
    keys = order or sorted(g)
    print(f"  {name}:")
    for k in keys:
        r = g.get(k, [])
        if len(r) < 20: continue
        n = len(r); m = sum(r)/n; sd = (sum((x-m)**2 for x in r)/n)**0.5
        t = m/sd*math.sqrt(n) if sd > 0 else 0
        print(f"    {str(k):16s} n={n:5d}  net {m*1e4:+7.1f}bps  t={t:+5.2f}")
    print()

def hourblock(e): return f"{e['hr']//4*4:02d}-{e['hr']//4*4+4:02d}h"
def session(e):
    h = e['hr']
    return ("Asia 00-07" if h < 7 else "Europe 07-13" if h < 13 else "US 13-21" if h < 21 else "Late 21-24")
def agebucket(e):
    a = e['age']
    return "new <21d" if a < 21 else "21-60d" if a < 60 else "60-120d" if a < 120 else "120d+"
rvs = sorted(e['rv'] for e in events); q1 = rvs[len(rvs)//3]; q2 = rvs[2*len(rvs)//3]
def rvbucket(e): return "rv-LOW" if e['rv'] < q1 else "rv-MID" if e['rv'] < q2 else "rv-HIGH"

show("by 4h block (UTC)", hourblock)
show("by session (UTC)", session, ["Asia 00-07", "Europe 07-13", "US 13-21", "Late 21-24"])
show("by days-since-listing", agebucket, ["new <21d", "21-60d", "60-120d", "120d+"])
show("by liquidity tier", lambda e: e['tier'], ["HIGH", "MID"])
show("by signal realized-vol", rvbucket, ["rv-LOW", "rv-MID", "rv-HIGH"])

# ---- monthly robustness of the strongest session and archetype cells ----
def monthly(name, keep):
    sel = [e for e in events if keep(e)]
    if len(sel) < 40: print(f"  {name}: only {len(sel)} events, skip"); return
    mo = defaultdict(list)
    for e in sel: mo[e['mo']].append(e['net'])
    tot = sum(e['net'] for e in sel)/len(sel)*1e4
    line = "  ".join(f"{k}:{sum(v)/len(v)*1e4:+.0f}" for k, v in sorted(mo.items()) if len(v) >= 5)
    print(f"  {name} (n={len(sel)}, net {tot:+.0f}bps): {line}")

print("MONTHLY robustness of the promising cells (is it one month, like every fragile effect before?):")
monthly("Late 21-24h",         lambda e: 21 <= e['hr'] < 24)
monthly("new <21d listings",   lambda e: e['age'] < 21)
monthly("new<21d & rv-HIGH",   lambda e: e['age'] < 21 and e['rv'] >= q2)
print("\nnet = per-trade after cost; t>2 ~ nominal (clustering inflates it, so the monthly line is the real test).")
