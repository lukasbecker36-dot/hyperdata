#!/usr/bin/env python3
"""Time-series breakout CONTINUATION (ride breakouts, don't fade) — momentum where the fade fails.

The fade works in MID names; HIGH-liquidity names showed no fade edge (Phase 2). Literature says
the liquid coins trend. So: go WITH the 24h-range breakout (long up-break / short down-break),
hold H, by tier, optionally with the 5x volume-spike gate. Causal, 45d holdout, 11bps rt.
NOTE: for the volume-spike events this is the exact negative of the fade, so it MUST lose on those
by construction; the real question is plain (non-exhaustion) breakouts, esp. in HIGH. Run from analysis/.
"""
import math
from collections import defaultdict
import wide_stop as w

COST=0.0011; HOLDOUT_DAYS=45; NOT=100.0
per=w.per_sym
def moments(xs):
    n=len(xs); m=sum(xs)/n; sd=(sum((x-m)**2 for x in xs)/n)**0.5; return m,sd
def daily_sharpe(rows):
    byd=defaultdict(list)
    for tm,r in rows: byd[tm//86400000].append(r)
    ser=[sum(v)/len(v) for _,v in sorted(byd.items())]
    if len(ser)<2: return 0.0
    m,sd=moments(ser); return m/sd*math.sqrt(365) if sd>0 else 0.0

def run(tiers, hold, vol_gate):
    tr=[]
    for s in per:
        if w.tier(w.uni.get(s,0)) not in tiers: continue
        t,hi,lo,c,v,ret=per[s]
        for i in range(24,len(c)-hold):
            if vol_gate:
                win=sorted(v[i-24:i]); med=win[len(win)//2]
                if med<=0 or v[i]/med<5: continue
            ph=max(hi[i-24:i]); pl=min(lo[i-24:i])
            brk=1 if c[i]>ph else(-1 if c[i]<pl else 0)
            if brk==0: continue
            tr.append((t[i], brk*math.log(c[i+hold]/c[i])-COST))   # WITH the breakout (continuation)
    return tr

def report(lbl, tr):
    if len(tr)<20: print(f"  {lbl:34s} n={len(tr)} (too few)"); return
    rets=[r for _,r in tr]; m,sd=moments(rets)
    tmax=max(t for t,_ in tr); ho=[(t,r) for t,r in tr if t>=tmax-HOLDOUT_DAYS*86400000]
    print(f"  {lbl:34s} n={len(tr):5d} net/t={m*1e4:+6.1f}bps Sh={daily_sharpe(tr):+5.2f} "
          f"hold={daily_sharpe(ho):+5.2f}")

print("TIME-SERIES BREAKOUT CONTINUATION (go WITH the breakout), 24h range, 11bps rt\n")
for hold in (8,24,48):
    print(f"hold {hold}h:")
    report(f"  plain breakouts, HIGH",       run(('HIGH',),      hold, False))
    report(f"  plain breakouts, MID",        run(('MID',),       hold, False))
    report(f"  vol-spike breakouts, HIGH",   run(('HIGH',),      hold, True))
    report(f"  vol-spike breakouts, MID",    run(('MID',),       hold, True))
print("\n(HIGH plain-breakout continuation is the key cell: does momentum pay where the fade doesn't?)")
