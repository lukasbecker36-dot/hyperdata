#!/usr/bin/env python3
"""Can a CHOP filter rescue the MA-pullback trend-follower?

ma_pullback.py found the rule is ~86% BTC beta and gets whipsawed in choppy months (May -122 bps).
Trend-following should only be run when the market actually trends. Test causal trend/chop filters on
the best config (slow=400h, fast=34, hold=72h):
  - efficiency ratio ER_N = |p[i]-p[i-N]| / sum|dp|  (1=clean trend, 0=chop), per-COIN and on BTC (market)
  - filter the entry on ER >= threshold, using ONLY bars up to entry (causal)
For each filter report: trades kept, RAW net bps + t, MARKET-NEUTRAL (minus BTC/hold) net bps + t, and
the monthly RAW series (did the May whipsaw go away?). The neutral column is the honesty check: if the
filter only lifts RAW, it is improved market-timing (beta), not new alpha. Run from analysis/.
"""
import math, sys, os
from datetime import datetime, timezone
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

per = w.per_sym; COST = 0.0011
liquid = [s for s in per if w.tier(w.uni.get(s, 0)) in ('HIGH', 'MID')]
SLOW, FAST, H, ERN = 400, 34, 72, 48   # ER over 48h: BTC median 0.14, p75 0.23, p90 0.32

def ema(xs, n):
    a = 2.0/(n+1); o = [xs[0]]*len(xs)
    for i in range(1, len(xs)): o[i] = a*xs[i] + (1-a)*o[i-1]
    return o

def er_series(c):
    """causal efficiency ratio over trailing ERN bars, via prefix sum of |dc|."""
    pre = [0.0]*(len(c))
    for i in range(1, len(c)): pre[i] = pre[i-1] + abs(c[i]-c[i-1])
    out = [0.0]*len(c)
    for i in range(len(c)):
        if i >= ERN:
            denom = pre[i]-pre[i-ERN]
            out[i] = abs(c[i]-c[i-ERN])/denom if denom > 1e-12 else 0.0
    return out

bt = per['BTC'][0]; bc = per['BTC'][3]
btc_px = {bt[i]: bc[i] for i in range(len(bt))}
btc_er_arr = er_series(bc); btc_er = {bt[i]: btc_er_arr[i] for i in range(len(bt))}

def trades():
    """all with-trend pullback entries, tagged with coin ER and BTC ER at entry (causal)."""
    out = []
    for s in liquid:
        t, hi, lo, c, v, ret = per[s]
        if len(c) < SLOW+H+5: continue
        es = ema(c, SLOW); ef = ema(c, FAST); erc = er_series(c); i = SLOW+1; cool = 0
        while i < len(c)-H:
            if cool > 0: cool -= 1; i += 1; continue
            up = ef[i] > es[i]
            buy = c[i-1] > es[i-1] and c[i] <= es[i]; sell = c[i-1] < es[i-1] and c[i] >= es[i]
            d = 1 if (up and buy) else (-1 if ((not up) and sell) else 0)
            if d != 0:
                bf = (math.log(btc_px[t[i+H]]/btc_px[t[i]]) if (t[i] in btc_px and t[i+H] in btc_px) else None)
                out.append((t[i], d, math.log(c[i+H]/c[i]), bf, erc[i], btc_er.get(t[i], 0.0)))
                cool = H
            i += 1
    return out

ALL = trades()

def report(name, keep):
    rs = [x for x in ALL if keep(x)]
    if len(rs) < 30:
        print(f"  {name:28s} n={len(rs):5d}  (too few)"); return
    raw = [d*cf - COST for _, d, cf, bf, ec, be in rs]
    neu = [d*(cf-bf) - COST for _, d, cf, bf, ec, be in rs if bf is not None]
    def st(v):
        n = len(v); m = sum(v)/n; sd = (sum((x-m)**2 for x in v)/n)**0.5
        return m*1e4, (m/sd*math.sqrt(n) if sd > 0 else 0)
    rb, rt = st(raw); nb, nt = st(neu)
    mo = defaultdict(list)
    for ms, d, cf, bf, ec, be in rs: mo[datetime.fromtimestamp(ms/1000, timezone.utc).strftime('%m')].append(d*cf-COST)
    may = sum(mo['05'])/len(mo['05'])*1e4 if mo.get('05') else float('nan')
    print(f"  {name:28s} n={len(rs):5d}  RAW {rb:+6.1f} (t{rt:+.1f})  NEUTRAL {nb:+6.1f} (t{nt:+.1f})  May {may:+6.0f}")

print(f"MA-pullback (slow={SLOW}h fast={FAST} hold={H}h) with causal chop filters (ER over {ERN} bars)\n")
print(f"  {'filter':28s} {'trades':>6}  {'raw net':>19}  {'market-neutral':>21}  {'May':>6}")
report("none (baseline)", lambda x: True)
for thr in (0.15, 0.20, 0.25, 0.30):
    report(f"coin ER >= {thr}", lambda x, thr=thr: x[4] >= thr)
for thr in (0.15, 0.20, 0.25, 0.30):
    report(f"BTC(market) ER >= {thr}", lambda x, thr=thr: x[5] >= thr)
report("both coin&BTC ER >= 0.20", lambda x: x[4] >= 0.20 and x[5] >= 0.20)
print("\nRAW = directional (beta+alpha); NEUTRAL = minus BTC over the hold (alpha only). A filter that lifts")
print("RAW but not NEUTRAL is better market-timing, not new alpha. May = that month's raw bps (the whipsaw).")
