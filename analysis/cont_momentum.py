#!/usr/bin/env python3
"""Continuation as a strategy: cross-sectional MOMENTUM (long winners / short losers).

The direct flip of the Phase-3 cross-sectional reversal test (which was negative -> momentum
implied). Literature (Liu-Tsyvinski-Wu, Dobrynskaya) puts crypto momentum at daily-to-weekly
horizons, so we sweep longer lookbacks/holds than the 8h fade. Market-neutral, non-overlapping
rebalances, HIGH+MID universe. Reports gross and net at two turnover-cost levels + 45d holdout.
Run from analysis/.
"""
import math
from collections import defaultdict
import wide_stop as w

HOLDOUT_DAYS=45; NOT=100.0
per=w.per_sym
def moments(xs):
    n=len(xs); m=sum(xs)/n; sd=(sum((x-m)**2 for x in xs)/n)**0.5; return m,sd
liquid=[s for s in per if w.tier(w.uni.get(s,0)) in ('HIGH','MID')]
maps={s:{ms:k for k,ms in enumerate(per[s][0])} for s in liquid}
grid=per['BTC'][0] if 'BTC' in per else max((per[s] for s in liquid),key=lambda x:len(x[0]))[0]

def momentum(lookback, hold, dec=0.2):
    """long top-`dec` past-return coins, short bottom-`dec`; hold `hold` bars, non-overlapping."""
    ser=[]
    for g in range(lookback, len(grid)-hold, hold):
        ms=grid[g]; rows=[]
        for s in liquid:
            k=maps[s].get(ms)
            if k is None or k<lookback or k+hold>=len(per[s][3]): continue
            c=per[s][3]
            rows.append((math.log(c[k]/c[k-lookback]), math.log(c[k+hold]/c[k])))
        if len(rows)<20: continue
        rows.sort(); nd=max(1,int(len(rows)*dec))
        losers=rows[:nd]; winners=rows[-nd:]
        long_fut=sum(r[1] for r in winners)/nd            # long winners (momentum)
        short_fut=sum(r[1] for r in losers)/nd            # short losers
        ser.append((ms, 0.5*(long_fut-short_fut)))        # dollar-neutral, gross exposure 1
    return ser

def stats(ser, hold, cost):
    if len(ser)<5: return None
    rets=[r-cost for _,r in ser]                          # cost per rebalance (round-trip, 2 legs)
    m,sd=moments(rets); ppy=8760.0/hold
    ann=m/sd*math.sqrt(ppy) if sd>0 else 0
    tmax=max(t for t,_ in ser); cut=tmax-HOLDOUT_DAYS*86400000
    ho=[r-cost for t,r in ser if t>=cut]
    if len(ho)>=3:
        mh,sh=moments(ho); annh=mh/sh*math.sqrt(ppy) if sh>0 else 0
    else: annh=float('nan')
    return len(ser), m*1e4, ann, annh

print(f"universe {len(liquid)} coins | CROSS-SECTIONAL MOMENTUM (long winners/short losers)\n")
print(f"  {'lookbk':>6} {'hold':>5} {'rebals':>6} {'gross bps':>9} {'Sh@0':>6} {'Sh@10bp':>8} {'hold@10':>8} {'Sh@20bp':>8}")
for lb in (24,48,168,336):
    for hd in (24,48,168):
        if hd>lb: continue
        ser=momentum(lb,hd)
        g=stats(ser,hd,0.0); n10=stats(ser,hd,0.0010); n20=stats(ser,hd,0.0020)
        if not g: continue
        print(f"  {lb:>5}h {hd:>4}h {g[0]:>6} {g[1]:>+9.1f} {g[2]:>+6.2f} {n10[2]:>+8.2f} {n10[3]:>+8.2f} {n20[2]:>+8.2f}")
print("\ngross bps = per-rebalance before cost; Sh@Nbp = annualized Sharpe after N bps/rebalance turnover cost")
print("(momentum = continuation; compare to the fade's edge and check if it SURVIVES cost + holdout)")
