#!/usr/bin/env python3
"""Is the market-vol gate a REAL second lever, or just the rv gate / beta in disguise?

idiosyncratic.py found the fade earns most when BTC is moving (btc-HIGH +49) and the spike is
market-driven (idio-LOW +56). Two things must be ruled out before that is a usable gate:
  1. rv-independence -- btc_move correlates with the coin's own rv (already gated). Double-sort
     btc_move x coin_rv: does btc_move still sort WITHIN each rv column?
  2. beta -- "fade harder when BTC moves" could be market-timing, not reversion. Hedge BTC out of each
     trade's P&L over its actual hold (net_mn = d*(logret - beta*btc_ret_hold) - cost). If the btc-HIGH
     edge survives market-neutralization it is real; if it collapses it was beta (like the MA-pullback).
Faithful reclaim/backstop exit, causal trailing beta. 1h panel. Run from analysis/.
"""
import math, sys, os
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

per = w.per_sym; COST = w.COST; MAXH = w.MAXH; WIN = 24; BW = 168
def sstd(xs): n = len(xs); m = sum(xs)/n; return (sum((x-m)**2 for x in xs)/(n-1))**0.5 if n > 1 else 0.0
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
    if t[i+xk] not in btc_c: continue
    sxy = sxx = 0.0
    for j in range(i-BW, i):
        rb = btc_r.get(t[j])
        if rb is None: continue
        sxy += ret[j]*rb; sxx += rb*rb
    beta = sxy/sxx if sxx > 1e-12 else 1.0
    logret = math.log(c[i+xk]/e)
    btc_hold = math.log(btc_c[t[i+xk]]/btc_c[t[i]])
    net = d*logret - COST
    net_mn = d*(logret - beta*btc_hold) - COST                 # beta-hedged
    ev.append(dict(net=net, net_mn=net_mn, btc=abs(math.log(btc_c[t[i]]/btc_c[t[i-3]])),
                   rv=sstd(ret[i-23:i+1]), beta=beta,
                   idio=((math.log(c[i]/c[i-3]) - beta*math.log(btc_c[t[i]]/btc_c[t[i-3]]))
                         / (math.log(c[i]/c[i-3]) or 1e-9))))
N = len(ev)
print(f"{N} fade events | baseline: RAW {sum(e['net'] for e in ev)/N*1e4:+.1f}bps  "
      f"beta-hedged {sum(e['net_mn'] for e in ev)/N*1e4:+.1f}bps\n")

def terc(k): xs = sorted(e[k] for e in ev); return xs[len(xs)//3], xs[2*len(xs)//3]
ba, bb = terc('btc'); ra, rb = terc('rv')
def bt_(x): return 'L' if x < ba else 'M' if x < bb else 'H'
def rt_(x): return 'L' if x < ra else 'M' if x < rb else 'H'

print("(1) rv-INDEPENDENCE — net bps by btc_move (rows) x coin_rv (cols). Does btc_move sort within rv?")
print("            rv-LOW      rv-MID      rv-HIGH")
for bk in ('H', 'M', 'L'):
    row = f"  btc-{bk}: "
    for rk in ('L', 'M', 'H'):
        seg = [e['net'] for e in ev if bt_(e['btc']) == bk and rt_(e['rv']) == rk]
        row += f"{sum(seg)/len(seg)*1e4:+6.0f}({len(seg):3d}) " if seg else "    n/a     "
    print(row)
# correlation btc_move vs rv
xs = [e['btc'] for e in ev]; ys = [e['rv'] for e in ev]; mx = sum(xs)/N; my = sum(ys)/N
cor = sum((xs[k]-mx)*(ys[k]-my) for k in range(N))/((sum((x-mx)**2 for x in xs)*sum((y-my)**2 for y in ys))**0.5)
print(f"  btc_move-rv correlation: {cor:+.2f}\n")

print("(2) BETA CHECK — btc-HIGH and idio-LOW edges, RAW vs beta-hedged:")
def stat(sel, key):
    v = [e[key] for e in sel]; n = len(v); m = sum(v)/n; sd = (sum((x-m)**2 for x in v)/n)**0.5
    return m*1e4, (m/sd*math.sqrt(n) if sd > 0 else 0)
ia, ib = terc('idio')
for name, sel in (("baseline (all)", ev),
                  ("btc-HIGH (market moving)", [e for e in ev if e['btc'] >= bb]),
                  ("idio-LOW (market-driven)", [e for e in ev if e['idio'] < ia])):
    r, rt = stat(sel, 'net'); m, mt = stat(sel, 'net_mn')
    print(f"  {name:26s} n={len(sel):4d}  RAW {r:+6.1f} (t{rt:+.1f})  beta-hedged {m:+6.1f} (t{mt:+.1f})")
print("\nadds beyond rv only if btc_move sorts within rv columns; is real reversion only if the beta-hedged")
print("edge stays strong. If hedging guts btc-HIGH but not idio-LOW, market TIMING is beta, market SELECTION isn't.")
