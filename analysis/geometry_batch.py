#!/usr/bin/env python3
"""Second geometry batch — more entry-shape features, same discipline as entry_geometry.py.

Building on the pierce-depth win (the fade concentrates in the most extreme, cleanest overshoots), test
three more candle-only, causal entry features:
  compression  prior-24h range width / trailing-week range  (LOW = broke out of a tight coil)
  gap_frac     |signal-bar return| / |last-3-bar move|       (HIGH = one-bar jump/dislocation vs a grind)
  pierce_z     pierce / rv  (how many sigma beyond the range) -- a possibly-more-stationary pierce
Per-tercile net bps + t, then monthly + first/second-half OOS on each strong tercile (the tests that
separate real concentrators like pierce from fragile ones). Any winner slots in as a second geometry gate.
Faithful reclaim/backstop exit, 1h panel. Run from analysis/.
"""
import math, sys, os
from datetime import datetime, timezone
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

per = w.per_sym; COST = w.COST; MAXH = w.MAXH; WIN = 24; WK = 168
def sstd(xs): n = len(xs); m = sum(xs)/n; return (sum((x-m)**2 for x in xs)/(n-1))**0.5 if n > 1 else 0.0

ev = []
for sym, i, brk in w.signals:
    t, hi, lo, c, v, ret = per[sym]
    if i+MAXH >= len(c) or i < 48: continue
    d = -brk; e = c[i]; ph = max(hi[i-WIN:i]); pl = min(lo[i-WIN:i])
    xk = MAXH
    for k in range(1, MAXH+1):
        if d < 0 and c[i+k] < ph: xk = k; break
        if d > 0 and c[i+k] > pl: xk = k; break
    net = d*math.log(c[i+xk]/e) - COST
    cw = min(i, WK)
    wkr = max(hi[i-cw:i]) - min(lo[i-cw:i])
    compression = (ph-pl)/wkr if wkr > 0 else 1.0
    move3 = abs(math.log(c[i]/c[i-3]))
    gap_frac = abs(ret[i])/move3 if move3 > 1e-9 else 1.0
    rv = sstd(ret[i-23:i+1])
    pierce = (c[i]-ph)/ph if d < 0 else (pl-c[i])/pl
    pierce_z = pierce/rv if rv > 0 else 0.0
    ev.append(dict(net=net, compression=compression, gap_frac=gap_frac, pierce_z=pierce_z,
                   ms=t[i], mo=datetime.fromtimestamp(t[i]/1000, timezone.utc).strftime('%m')))
N = len(ev); base = sum(e['net'] for e in ev)/N
print(f"{N} fade events | baseline net {base*1e4:+.1f} bps/trade\n")

def terc(k): xs = sorted(e[k] for e in ev); return xs[len(xs)//3], xs[2*len(xs)//3]
def show(name, k):
    a, b = terc(k); g = {'LOW': [], 'MID': [], 'HIGH': []}
    for e in ev: g['LOW' if e[k] < a else 'MID' if e[k] < b else 'HIGH'].append(e['net'])
    print(f"  by {name} (terciles):")
    for lab in ('LOW', 'MID', 'HIGH'):
        r = g[lab]; n = len(r); m = sum(r)/n; sd = (sum((x-m)**2 for x in r)/n)**0.5
        print(f"    {lab:5s} n={n:5d}  net {m*1e4:+7.1f}bps  t={m/sd*math.sqrt(n) if sd>0 else 0:+5.2f}")
    print()
for nm, k in (("range compression (LOW=coil)", "compression"), ("gap fraction (HIGH=jump)", "gap_frac"),
              ("vol-normalized pierce", "pierce_z")):
    show(nm, k)

def robust(name, keep):
    sel = [e for e in ev if keep(e)]
    if len(sel) < 40: print(f"  {name}: {len(sel)}, skip"); return
    tm = sorted(e['ms'] for e in sel)[len(sel)//2]
    fh = [e['net'] for e in sel if e['ms'] < tm]; sh = [e['net'] for e in sel if e['ms'] >= tm]
    mo = defaultdict(list)
    for e in sel: mo[e['mo']].append(e['net'])
    tds = [k for k, vv in mo.items() if len(vv) >= 5]; pos = sum(1 for k in tds if sum(mo[k])/len(mo[k]) > 0)
    print(f"  {name:26s} n={len(sel):4d} net {sum(e['net'] for e in sel)/len(sel)*1e4:+6.0f} | "
          f"1st {sum(fh)/len(fh)*1e4:+6.0f}  2nd {sum(sh)/len(sh)*1e4:+6.0f} | mo+ {pos}/{len(tds)}")

ca, cb = terc('compression'); ga, gb = terc('gap_frac'); za, zb = terc('pierce_z')
print("OOS + monthly robustness (1st-vs-2nd half is the real test — pierce passed it, 16-20h failed):")
robust("baseline (all)",        lambda e: True)
robust("compression-LOW (coil)", lambda e: e['compression'] < ca)
robust("compression-HIGH",       lambda e: e['compression'] >= cb)
robust("gap_frac-HIGH (jump)",   lambda e: e['gap_frac'] >= gb)
robust("pierce_z-HIGH",          lambda e: e['pierce_z'] >= zb)
print("\nWinner must be positive across most months AND hold 2nd-half, then be checked for adding beyond pierce.")
