#!/usr/bin/env python3
"""Lead-lag: BTC leads the alts. Trade the partial-adjustment gap.

Each bar, an alt with beta b to BTC 'should' have moved b*r_btc; if it moved less it lagged and may
catch up next bar(s). Signal gap = b*r_btc(t) - r_alt(t) (b from a trailing 168-bar regression, causal).
Cross-sectional: long top-decile gap (laggards) / short bottom (over-shooters), market-neutral, hold H
bars, non-overlapping. Reports the long-short spread gross + net of turnover cost + 45d holdout.
1h panel (full 8mo). Run from analysis/.
"""
import math
from collections import defaultdict
import wide_stop as w

W=168; HOLDOUT_DAYS=45
per=w.per_sym
def moments(xs):
    n=len(xs); m=sum(xs)/n; sd=(sum((x-m)**2 for x in xs)/n)**0.5; return m,sd
liquid=[s for s in per if w.tier(w.uni.get(s,0)) in ('HIGH','MID')]
if 'BTC' not in per:
    raise SystemExit("no BTC")
bt,_,_,bc,_,bret=per['BTC']
btc_ret={bt[i]:bret[i] for i in range(1,len(bt)) if bret[i]==bret[i]}
grid=bt

def run(H, dec=0.2):
    by_ms=defaultdict(list)
    for s in liquid:
        if s=='BTC': continue
        t,hi,lo,c,v,ret=per[s]
        ra=[]; rb=[]; cc=[]; tt=[]
        for i in range(1,len(t)):
            rbv=btc_ret.get(t[i])
            if rbv is None or ret[i]!=ret[i]: continue
            ra.append(ret[i]); rb.append(rbv); cc.append(c[i]); tt.append(t[i])
        Sa=Sb=Sab=Sbb=0.0
        for k in range(len(ra)):
            if k>=W and k+H<len(ra):
                varb=Sbb-Sb*Sb/W
                beta=(Sab-Sa*Sb/W)/varb if varb>1e-12 else 0.0
                gap=beta*rb[k]-ra[k]
                by_ms[tt[k]].append((gap, math.log(cc[k+H]/cc[k])))
            Sa+=ra[k]; Sb+=rb[k]; Sab+=ra[k]*rb[k]; Sbb+=rb[k]*rb[k]
            if k>=W:
                j=k-W; Sa-=ra[j]; Sb-=rb[j]; Sab-=ra[j]*rb[j]; Sbb-=rb[j]*rb[j]
    ser=[]
    for g in range(W, len(grid)-H, H):
        ms=grid[g]; rows=by_ms.get(ms)
        if not rows or len(rows)<20: continue
        rows.sort(); nd=max(1,int(len(rows)*dec))
        lag=rows[-nd:]      # highest gap = most lagged -> long (expect catch-up up)
        over=rows[:nd]      # lowest gap = over-shot -> short
        r=0.5*(sum(x[1] for x in lag)/nd - sum(x[1] for x in over)/nd)
        ser.append((ms, r))
    return ser

def summ(ser, H, cost):
    if len(ser)<5: return None
    r=[x-cost for _,x in ser]; m,sd=moments(r); ppy=8760.0/H
    ann=m/sd*math.sqrt(ppy) if sd>0 else 0
    tmax=max(t for t,_ in ser); ho=[x-cost for t,x in ser if t>=tmax-HOLDOUT_DAYS*86400000]
    annh=(moments(ho)[0]/moments(ho)[1]*math.sqrt(ppy)) if len(ho)>=3 and moments(ho)[1]>0 else float('nan')
    return len(ser), m*1e4, ann, annh

print("LEAD-LAG (BTC leads): long laggards / short over-shooters, market-neutral\n")
print(f"  {'hold':>5} {'rebals':>6} {'gross bps':>9} {'annSh@0':>8} {'annSh@5bp':>9} {'annSh@15bp':>10} {'hold@5':>7}")
for H in (1,2,3,6):
    g=summ(run(H),H,0.0)
    if not g: continue
    n5=summ(run(H),H,0.0005); n15=summ(run(H),H,0.0015)
    print(f"  {H:>4}h {g[0]:>6} {g[1]:>+9.1f} {g[2]:>+8.2f} {n5[2]:>+9.2f} {n15[2]:>+10.2f} {n5[3]:>+7.2f}")
print("\ngross = long-short spread per rebalance (bps); high turnover so cost matters. positive gross = laggards catch up.")
