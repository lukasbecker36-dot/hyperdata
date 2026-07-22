#!/usr/bin/env python3
"""Funding CARRY: cross-sectional funding sort, held with funding INCOME counted (never done before).

Every prior test used funding only as a filter and P&L was price-return only. Here we harvest the
funding cashflow: each rebalance, short the top-decile funding coins (crowded longs pay us) and long
the bottom-decile (we receive on negative funding), hold H hours, market-neutral long-short basket.

Per position: pnl = d*price_return  +  (-d)*sum(hourly funding over hold)   (d=+1 long / -1 short)
  funding>0 => longs pay shorts, so a SHORT collects it. We decompose total into PRICE vs FUNDING
  components — the classic carry question is whether funding income survives the price move against
  the crowded side. Causal, 45d holdout, cost per rebalance (2 legs). Run from analysis/.
"""
import math, csv, bisect
from collections import defaultdict
import wide_stop as w

HOLDOUT_DAYS=45; NOT=100.0
per=w.per_sym
def moments(xs):
    n=len(xs); m=sum(xs)/n; sd=(sum((x-m)**2 for x in xs)/n)**0.5; return m,sd

# funding is NOT hour-aligned to candles, so accrue by TIME WINDOW (prefix-sum of funding entries)
fraw=defaultdict(list)
with open('../hyperliquid_funding.csv') as f:
    for row in csv.DictReader(f):
        try: fraw[row['symbol']].append((int(row['time_ms']), float(row['funding_rate'])))
        except Exception: pass
fsort={}
for s,items in fraw.items():
    items.sort(); times=[x[0] for x in items]; pref=[0.0]
    for _,r in items: pref.append(pref[-1]+r)
    fsort[s]=(times, pref)
def facc(s, a, b):                       # funding accrued over [a, b)
    fs=fsort.get(s)
    if not fs: return 0.0
    times,pref=fs
    return pref[bisect.bisect_left(times,b)] - pref[bisect.bisect_left(times,a)]

liquid=[s for s in per if w.tier(w.uni.get(s,0)) in ('HIGH','MID')]
maps={s:{ms:k for k,ms in enumerate(per[s][0])} for s in liquid}
grid=per['BTC'][0] if 'BTC' in per else max((per[s] for s in liquid),key=lambda x:len(x[0]))[0]
print(f"universe {len(liquid)} coins | funding series for {len(fsort)} coins\n")

def carry(H, dec=0.2, cost=0.0):
    ser=[]
    for g in range(0, len(grid)-H, H):
        ms=grid[g]; rows=[]
        for s in liquid:
            k=maps[s].get(ms)
            if k is None or k+H>=len(per[s][3]): continue
            sig=w.fund8_at(s, ms)
            if sig is None: continue
            t=per[s][0]; c=per[s][3]
            fsum=facc(s, t[k], t[k+H])
            rows.append((sig, math.log(c[k+H]/c[k]), fsum))
        if len(rows)<20: continue
        rows.sort(); nd=max(1,int(len(rows)*dec))
        longs=rows[:nd]; shorts=rows[-nd:]; nL=len(longs)+len(shorts)
        pc=fc=0.0
        for sig,pr,fs in longs:  pc+= pr; fc+= -fs   # d=+1
        for sig,pr,fs in shorts: pc+= -pr; fc+= fs   # d=-1
        pc/=nL; fc/=nL
        ser.append((ms, pc+fc-cost, pc, fc))
    return ser

def summ(ser, H, cost):
    if len(ser)<5: return None
    tot=[t-cost for _,t,_,_ in ser]; pc=[p for _,_,p,_ in ser]; fc=[f for _,_,_,f in ser]
    m,sd=moments(tot); ppy=8760.0/H
    ann=m/sd*math.sqrt(ppy) if sd>0 else 0
    tmax=max(t for t,_,_,_ in ser); cut=tmax-HOLDOUT_DAYS*86400000
    ho=[x-cost for t,x,_,_ in ser if t>=cut]
    annh=(moments(ho)[0]/moments(ho)[1]*math.sqrt(ppy)) if len(ho)>=3 and moments(ho)[1]>0 else float('nan')
    return dict(n=len(ser), tot=m*1e4, price=sum(pc)/len(pc)*1e4, fund=sum(fc)/len(fc)*1e4,
                ann=ann, annh=annh)

print(f"CROSS-SECTIONAL FUNDING CARRY (short high-funding / long low-funding, decile 20%)")
print(f"  {'hold':>5} {'rebals':>6} | {'PRICE bps':>9} {'FUND bps':>8} {'TOTAL bps':>9} | {'annSh@0':>7} {'annSh@10bp':>10} {'hold@10':>8}")
for H in (8,24,48,168):
    g=summ(carry(H), H, 0.0)
    if not g: continue
    n10=summ(carry(H), H, 0.0010)
    print(f"  {H:>4}h {g['n']:>6} | {g['price']:>+9.1f} {g['fund']:>+8.1f} {g['tot']:>+9.1f} | "
          f"{g['ann']:>+7.2f} {n10['ann']:>+10.2f} {n10['annh']:>+8.2f}")
print("\nPRICE = price-move component, FUND = funding income collected (per rebalance, bps).")
print("carry pays only if FUND income survives the PRICE move against the crowded side, net of cost.")
