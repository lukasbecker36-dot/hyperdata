#!/usr/bin/env python3
"""Idiosyncratic vs market-driven spikes — does a coin-specific dislocation revert better?

A breakout can be the coin moving on its OWN (news, liquidation, thin-book event) or just riding a
market-wide move. The fade should prefer the former: an idiosyncratic overshoot is more likely to snap
back, a systematic one is a real macro move. Two causal features per fade event:
  idio_ratio  (r_coin - beta*r_btc)/r_coin over the 3-bar move in; beta = trailing 168-bar regression
              on BTC. ~1 = fully idiosyncratic, ~0 = fully explained by the market.
  btc_move    |BTC's 3-bar move| at entry -- was the market itself calm (idiosyncratic env) or moving?
Per-tercile net bps + t, monthly + first/second-half OOS on the strong cells, and if one survives, a
double-sort vs pierce to check it adds independent info. Faithful reclaim/backstop exit. 1h. Run from analysis/.
"""
import math, sys, os
from datetime import datetime, timezone
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

per = w.per_sym; COST = w.COST; MAXH = w.MAXH; WIN = 24; BW = 168
bt, _, _, bc, _, bret = per['BTC']
btc_c = {bt[k]: bc[k] for k in range(len(bt))}
btc_r = {bt[k]: bret[k] for k in range(1, len(bt))}

ev = []
for sym, i, brk in w.signals:
    t, hi, lo, c, v, ret = per[sym]
    if i+MAXH >= len(c) or i < BW+4: continue
    if t[i] not in btc_c or t[i-3] not in btc_c: continue
    d = -brk; e = c[i]; ph = max(hi[i-WIN:i]); pl = min(lo[i-WIN:i])
    xk = MAXH
    for k in range(1, MAXH+1):
        if d < 0 and c[i+k] < ph: xk = k; break
        if d > 0 and c[i+k] > pl: xk = k; break
    net = d*math.log(c[i+xk]/e) - COST
    # trailing beta of coin on BTC (through-origin, causal: bars i-BW..i-1)
    sxy = sxx = 0.0
    for j in range(i-BW, i):
        rb = btc_r.get(t[j])
        if rb is None: continue
        sxy += ret[j]*rb; sxx += rb*rb
    beta = sxy/sxx if sxx > 1e-12 else 1.0
    rc = math.log(c[i]/c[i-3]); rb3 = math.log(btc_c[t[i]]/btc_c[t[i-3]])
    idio_ratio = (rc - beta*rb3)/rc if abs(rc) > 1e-9 else 1.0
    btc_move = abs(rb3)
    ev.append(dict(net=net, idio=idio_ratio, btc=btc_move, beta=beta,
                   ms=t[i], mo=datetime.fromtimestamp(t[i]/1000, timezone.utc).strftime('%m'),
                   pierce=((c[i]-ph)/ph if d < 0 else (pl-c[i])/pl)))
N = len(ev); base = sum(e['net'] for e in ev)/N
print(f"{N} fade events | baseline net {base*1e4:+.1f} bps/trade  (median beta {sorted(e['beta'] for e in ev)[N//2]:.2f})\n")

def terc(k): xs = sorted(e[k] for e in ev); return xs[len(xs)//3], xs[2*len(xs)//3]
def show(name, k):
    a, b = terc(k); g = {'LOW': [], 'MID': [], 'HIGH': []}
    for e in ev: g['LOW' if e[k] < a else 'MID' if e[k] < b else 'HIGH'].append(e['net'])
    print(f"  by {name} (terciles):")
    for lab in ('LOW', 'MID', 'HIGH'):
        r = g[lab]; n = len(r); m = sum(r)/n; sd = (sum((x-m)**2 for x in r)/n)**0.5
        print(f"    {lab:5s} n={n:5d}  net {m*1e4:+7.1f}bps  t={m/sd*math.sqrt(n) if sd>0 else 0:+5.2f}")
    print()
show("idiosyncratic ratio (HIGH=coin-specific)", "idio")
show("BTC move at entry (LOW=calm market)", "btc")

def robust(name, keep):
    sel = [e for e in ev if keep(e)]
    if len(sel) < 40: print(f"  {name}: {len(sel)}, skip"); return
    tm = sorted(e['ms'] for e in sel)[len(sel)//2]
    fh = [e['net'] for e in sel if e['ms'] < tm]; sh = [e['net'] for e in sel if e['ms'] >= tm]
    mo = defaultdict(list)
    for e in sel: mo[e['mo']].append(e['net'])
    tds = [k for k, vv in mo.items() if len(vv) >= 5]; pos = sum(1 for k in tds if sum(mo[k])/len(mo[k]) > 0)
    print(f"  {name:28s} n={len(sel):4d} net {sum(e['net'] for e in sel)/len(sel)*1e4:+6.0f} | "
          f"1st {sum(fh)/len(fh)*1e4:+6.0f}  2nd {sum(sh)/len(sh)*1e4:+6.0f} | mo+ {pos}/{len(tds)}")

ia, ib = terc('idio'); ba, bb = terc('btc')
print("OOS + monthly robustness:")
robust("baseline (all)",          lambda e: True)
robust("idio-HIGH (coin-specific)", lambda e: e['idio'] >= ib)
robust("idio-LOW (market-driven)",  lambda e: e['idio'] < ia)
robust("btc-LOW (calm market)",     lambda e: e['btc'] < ba)
robust("btc-HIGH (market moving)",  lambda e: e['btc'] >= bb)

# does the best idio cell add BEYOND pierce? double-sort
pa, pb = terc('pierce')
print("\nDouble-sort net bps: idio (rows) x pierce (cols) — does idio add beyond the pierce edge?")
print("           pierce-LOW   pierce-MID   pierce-HIGH")
for ik in ('HIGH', 'MID', 'LOW'):
    lo, hi = (ib, 9e9) if ik == 'HIGH' else (ia, ib) if ik == 'MID' else (-9e9, ia)
    row = f"  idio-{ik:4s}:"
    for pk in ('LOW', 'MID', 'HIGH'):
        plo, phi = (-9e9, pa) if pk == 'LOW' else (pa, pb) if pk == 'MID' else (pb, 9e9)
        seg = [e['net'] for e in ev if lo <= e['idio'] < hi and plo <= e['pierce'] < phi]
        row += f"  {sum(seg)/len(seg)*1e4:+6.0f}({len(seg):3d})" if seg else "     n/a   "
    print(row)
