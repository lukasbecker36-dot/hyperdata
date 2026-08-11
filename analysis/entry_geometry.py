#!/usr/bin/env python3
"""Is the exhaustion VISIBLE at entry? Condition the fade on entry geometry (candles only).

The mechanism map says the fade wants forced flow into a reboundable book. This asks whether the
signal bar itself already shows the reversal, using only data available AT entry:
  dir        fade-short (pump) vs fade-long (dump) -- never split; crypto is asymmetric
  reject     did the spike bar close back INSIDE its own range (rejection wick) vs at the extreme?
             directional: short wants a close near the low, long near the high. high = more rejection
  pierce     how far the close pushed BEYOND the prior 24h range (stretched vs marginal)
  prevspike  was the PREVIOUS bar already a spike? (a staircase = participation building = SAGA)
  dollarvol  absolute $ notional of the spike bar (size of the forced flow)
Per-bucket net bps + n + t, with an auto monthly + first/second-half OOS check on the strongest cells
(the tests that have exposed every non-stationary effect here). Faithful reclaim/backstop exit. 1h. Run from analysis/.
"""
import math, sys, os
from datetime import datetime, timezone
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

per = w.per_sym; COST = w.COST; MAXH = w.MAXH; WIN = 24
def med(xs): s = sorted(xs); n = len(s); return s[n//2] if n % 2 else 0.5*(s[n//2-1]+s[n//2])

ev = []
for sym, i, brk in w.signals:
    t, hi, lo, c, v, ret = per[sym]
    if i+MAXH >= len(c) or i < 2: continue
    d = -brk; e = c[i]; ph = max(hi[i-WIN:i]); pl = min(lo[i-WIN:i])
    xk = MAXH
    for k in range(1, MAXH+1):
        if d < 0 and c[i+k] < ph: xk = k; break
        if d > 0 and c[i+k] > pl: xk = k; break
    net = d*math.log(c[i+xk]/e) - COST
    rng = hi[i]-lo[i]
    clv = (c[i]-lo[i])/rng if rng > 0 else 0.5           # 0=closed at low, 1=at high
    reject = (1-clv) if d < 0 else clv                   # high = spike bar already reversed toward entry side
    pierce = ((c[i]-ph)/ph if d < 0 else (pl-c[i])/pl)   # fraction beyond the prior range
    mv = med(v[i-WIN:i]) or 1.0
    prevspike = v[i-1]/mv                                 # was the prior bar already elevated? (staircase)
    dollarvol = v[i]*c[i]
    ev.append(dict(net=net, d=d, reject=reject, pierce=pierce, prevspike=prevspike,
                   dollarvol=dollarvol, ms=t[i], mo=datetime.fromtimestamp(t[i]/1000, timezone.utc).strftime('%m')))
N = len(ev); base = sum(e['net'] for e in ev)/N
print(f"{N} fade events | baseline net {base*1e4:+.1f} bps/trade\n")

def terc(key):
    xs = sorted(e[key] for e in ev); return xs[len(xs)//3], xs[2*len(xs)//3]
def show_terc(name, key):
    a, b = terc(key); g = {'LOW': [], 'MID': [], 'HIGH': []}
    for e in ev: g['LOW' if e[key] < a else 'MID' if e[key] < b else 'HIGH'].append(e['net'])
    print(f"  by {name} (terciles):")
    for k in ('LOW', 'MID', 'HIGH'):
        r = g[k]; n = len(r); m = sum(r)/n; sd = (sum((x-m)**2 for x in r)/n)**0.5
        print(f"    {k:5s} n={n:5d}  net {m*1e4:+7.1f}bps  t={m/sd*math.sqrt(n) if sd>0 else 0:+5.2f}")
    print()

# direction split
g = defaultdict(list)
for e in ev: g['SHORT (fade pump)' if e['d'] < 0 else 'LONG (fade dump)'].append(e['net'])
print("  by direction:")
for k in ('SHORT (fade pump)', 'LONG (fade dump)'):
    r = g[k]; n = len(r); m = sum(r)/n; sd = (sum((x-m)**2 for x in r)/n)**0.5
    print(f"    {k:18s} n={n:5d}  net {m*1e4:+7.1f}bps  t={m/sd*math.sqrt(n) if sd>0 else 0:+5.2f}")
print()
for nm, key in (("rejection (close inside range)", "reject"), ("pierce depth", "pierce"),
                ("prev-bar spike (staircase)", "prevspike"), ("dollar volume", "dollarvol")):
    show_terc(nm, key)

def robust(name, keep):
    sel = [e for e in ev if keep(e)]
    if len(sel) < 40: print(f"  {name}: {len(sel)} events, skip"); return
    tm = sorted(e['ms'] for e in sel)[len(sel)//2]
    fh = [e['net'] for e in sel if e['ms'] < tm]; sh = [e['net'] for e in sel if e['ms'] >= tm]
    mo = defaultdict(list)
    for e in sel: mo[e['mo']].append(e['net'])
    pos = sum(1 for k, vv in mo.items() if len(vv) >= 5 and sum(vv)/len(vv) > 0)
    tot = sum(mo[k].__len__() for k in mo)  # noqa
    ntot = len([k for k, vv in mo.items() if len(vv) >= 5])
    print(f"  {name:24s} n={len(sel):4d} net {sum(e['net'] for e in sel)/len(sel)*1e4:+6.0f} | "
          f"1st-half {sum(fh)/len(fh)*1e4:+6.0f}  2nd-half {sum(sh)/len(sh)*1e4:+6.0f}  | months+ {pos}/{ntot}")

ra, rb = terc('reject'); pa, pb = terc('pierce'); sa, sb = terc('prevspike')
print("OOS + monthly robustness of the strongest cells (1st vs 2nd half is the real test):")
robust("baseline (all)", lambda e: True)
robust("reject-HIGH", lambda e: e['reject'] >= rb)
robust("prevspike-LOW (no staircase)", lambda e: e['prevspike'] < sa)
robust("reject-HIGH & prevspike-LOW", lambda e: e['reject'] >= rb and e['prevspike'] < sa)
print("\nreject-HIGH = spike bar closed back toward the fade side; prevspike-LOW = the prior bar was quiet")
print("(a single isolated dislocation, not a building staircase). Both are the exhaustion signature.")
