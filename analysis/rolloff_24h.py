#!/usr/bin/env python3
"""'24h % display artifact' strategy: the candle exactly 24h ago is about to roll off the 24h
window shown on every exchange. A big GREEN candle rolling off makes the displayed 24h% mechanically
DROP (looks worse -> naive sell -> down); a big RED one makes it RISE (looks better -> naive buy -> up).

Trade it cross-sectionally each hour: LONG the coins whose 24h-ago candle was most RED, SHORT the most
GREEN, hold H hours, market-neutral. Sweep hold; cost sensitivity; 45d holdout. Placebo: run the same
with a NON-24h lag (12h) — if the edge is a real display artifact it should be specific to lag=24.
1h panel (full 8mo). Run from analysis/.
"""
import math
from collections import defaultdict
import wide_stop as w

HOLDOUT_DAYS=45
per=w.per_sym
def moments(xs):
    n=len(xs); m=sum(xs)/n; sd=(sum((x-m)**2 for x in xs)/n)**0.5; return m,sd
liquid=[s for s in per if w.tier(w.uni.get(s,0)) in ('HIGH','MID')]
maps={s:{ms:k for k,ms in enumerate(per[s][0])} for s in liquid}
grid=per['BTC'][0] if 'BTC' in per else max((per[s] for s in liquid),key=lambda x:len(x[0]))[0]

def strat(H, lag=24, dec=0.2):
    ser=[]
    for g in range(lag+1, len(grid)-H, H):
        ms=grid[g]; rows=[]
        for s in liquid:
            k=maps[s].get(ms)
            if k is None or k-lag-1<0 or k+H>=len(per[s][3]): continue
            c=per[s][3]
            r_roll=math.log(c[k-lag]/c[k-lag-1])     # return of the candle 'lag' hours ago
            rows.append((r_roll, math.log(c[k+H]/c[k])))
        if len(rows)<20: continue
        rows.sort(); nd=max(1,int(len(rows)*dec))
        reds=rows[:nd]; greens=rows[-nd:]            # long reds (lowest r_roll), short greens
        ser.append((ms, 0.5*(sum(r[1] for r in reds)/nd - sum(g[1] for g in greens)/nd)))
    return ser

def summ(ser, H, cost):
    if len(ser)<5: return None
    r=[x-cost for _,x in ser]; m,sd=moments(r); ppy=8760.0/H
    ann=m/sd*math.sqrt(ppy) if sd>0 else 0
    tmax=max(t for t,_ in ser); ho=[x-cost for t,x in ser if t>=tmax-HOLDOUT_DAYS*86400000]
    annh=(moments(ho)[0]/moments(ho)[1]*math.sqrt(ppy)) if len(ho)>=3 and moments(ho)[1]>0 else float('nan')
    return len(ser), m*1e4, ann, annh

print("24h ROLL-OFF (long biggest-red-24h-ago / short biggest-green), market-neutral\n")
print(f"  {'hold':>5} {'rebals':>6} {'gross bps':>9} {'annSh@0':>8} {'@5bp':>7} {'@10bp':>7} {'hold@5':>7}")
for H in (1,2,3,6,12,24):
    g=summ(strat(H,24),H,0.0)
    if not g: continue
    n5=summ(strat(H,24),H,0.0005); n10=summ(strat(H,24),H,0.0010)
    print(f"  {H:>4}h {g[0]:>6} {g[1]:>+9.2f} {g[2]:>+8.2f} {n5[2]:>+7.2f} {n10[2]:>+7.2f} {n5[3]:>+7.2f}")

print("\nPLACEBO — same strategy but keying off the candle 12h ago (should be WEAKER if the edge is a real")
print("24h-display artifact rather than generic short-horizon reversal):")
print(f"  {'hold':>5} {'gross bps':>9} {'annSh@0':>8}")
for H in (1,3,6):
    for lag,tag in ((24,'lag=24'),(12,'lag=12')):
        g=summ(strat(H,lag),H,0.0)
        if g: print(f"  {H:>4}h {tag}  gross={g[1]:>+7.2f}  annSh={g[2]:>+6.2f}")
print("\ngross = long-short spread per rebalance (bps). Compare to thread's ~0.08%/day raw claim.")
