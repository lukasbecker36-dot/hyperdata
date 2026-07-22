#!/usr/bin/env python3
"""Whale-vs-crowd: does the fade edge depend on WHO made the volume spike?

Every 5x volume spike decomposes into a trade-COUNT spike (many small trades = crowd/retail
capitulation) and an avg-trade-SIZE spike (few big trades = whale/informed). Hypothesis: crowd
spikes are exhaustion -> fade works; whale spikes are informed -> more likely to continue -> skip.

Uses num_trades (n) from the candles, which no prior test touched. For each causal fade signal:
  ats_ratio = (v/n at signal) / trailing-24h median(v/n)   -> >1 = unusually large trades (whale)
  n_ratio   = n / trailing-24h median(n)                    -> high = trade-count spike (crowd)
Buckets the fade P&L by each, then tests a crowd-only gate. Causal + 45d holdout. Run from analysis/.
"""
import math, bisect, csv
from collections import defaultdict
import wide_stop as w

MAXH=8; COST=0.0011; WARMUP=300; RV_PCT=0.60; HOLDOUT_DAYS=45; NOT=100.0
per=w.per_sym
def moments(xs):
    n=len(xs); m=sum(xs)/n; sd=(sum((x-m)**2 for x in xs)/n)**0.5; return m,sd
def median(xs):
    s=sorted(xs); n=len(s); return s[n//2] if n%2 else 0.5*(s[n//2-1]+s[n//2])
def pctile(s,q):
    n=len(s)
    if n<2: return s[0] if s else None
    pos=q*(n-1); lo=int(pos); hi=min(lo+1,n-1); return s[lo]+(s[hi]-s[lo])*(pos-lo)

# num_trades per (sym, ms)
nt=defaultdict(dict)
with open('../hyperliquid_1h_history.csv') as f:
    for row in csv.DictReader(f):
        try: nt[row['symbol']][int(row['open_time_ms'])]=float(row['num_trades'])
        except Exception: pass

cands=[]
for sym,(t,hi,lo,c,v,ret) in per.items():
    if w.tier(w.uni.get(sym,0)) not in ('HIGH','MID'): continue
    nmap=nt.get(sym)
    if not nmap: continue
    for i in range(25,len(c)-MAXH):
        win=sorted(v[i-24:i]); med=win[len(win)//2]
        if med<=0 or v[i]/med<5: continue
        ph=max(hi[i-24:i]); pl=min(lo[i-24:i]); brk=1 if c[i]>ph else(-1 if c[i]<pl else 0)
        if brk==0: continue
        rv=w.sample_std(ret[i-23:i+1])
        if math.isnan(rv): continue
        f8=w.fund8_at(sym,t[i])
        if f8 is None or brk*(1 if f8>0 else -1)!=1: continue
        ni=nmap.get(t[i])
        if not ni or ni<=0: continue
        ns=[]; atss=[]; ok=True
        for j in range(i-24,i):
            nj=nmap.get(t[j])
            if not nj or nj<=0: ok=False; break
            ns.append(nj); atss.append(v[j]/nj)
        if not ok: continue
        med_n=median(ns); med_ats=median(atss)
        if med_n<=0 or med_ats<=0: continue
        cands.append((t[i], rv, -brk*math.log(c[i+MAXH]/c[i])-COST,
                      (v[i]/ni)/med_ats, ni/med_n))
cands.sort()
prior=[]; sig=[]
for (tm,rv,fade,ar,nr) in cands:
    if len(prior)>=WARMUP and rv>=pctile(prior,RV_PCT):
        sig.append({'t':tm,'fade':fade,'ats':ar,'nr':nr})
    bisect.insort(prior,rv)
print(f"causal fade signals with trade-count data: {len(sig)}")

def daily_sharpe(rows):
    byd=defaultdict(list)
    for tm,r in rows: byd[tm//86400000].append(r)
    ser=[sum(v)/len(v) for _,v in sorted(byd.items())]
    if len(ser)<2: return 0.0
    m,sd=moments(ser); return m/sd*math.sqrt(365) if sd>0 else 0.0
tmax=max(s['t'] for s in sig); cut=tmax-HOLDOUT_DAYS*86400000
def stat(rows):
    if len(rows)<20: return f"n={len(rows)} (thin)"
    r=[x['fade'] for x in rows]; m,_=moments(r); wins=sum(1 for x in r if x>0)/len(r)*100
    sh=daily_sharpe([(x['t'],x['fade']) for x in rows])
    sho=daily_sharpe([(x['t'],x['fade']) for x in rows if x['t']>=cut])
    return f"n={len(rows):5d} net/t={m*1e4:+6.1f}bps win={wins:4.1f}% Sh={sh:+5.2f} hold={sho:+5.2f}"

def buckets(key, label):
    s=sorted(sig,key=lambda x:x[key]); q=len(s)//4
    print(f"\n{label} quartiles (Q1=lowest):")
    for b,(lo,hi) in enumerate([(0,q),(q,2*q),(2*q,3*q),(3*q,len(s))]):
        seg=s[lo:hi]; rng=f"[{seg[0][key]:.2f},{seg[-1][key]:.2f}]"
        print(f"  Q{b+1} {rng:>14}  {stat(seg)}")
print(f"\nBASELINE (all): {stat(sig)}")
buckets('ats', "AVG-TRADE-SIZE ratio  (high = whale/few-big-trades, low = crowd/many-small)")
buckets('nr',  "TRADE-COUNT ratio     (high = crowd spike, low = few trades)")

# gate test: crowd-only (drop the whale quartile = highest avg-trade-size)
thr=pctile(sorted(x['ats'] for x in sig),0.75)
crowd=[x for x in sig if x['ats']<thr]
print(f"\nGATE — drop top-quartile avg-trade-size (keep crowd spikes, ats<{thr:.2f}):")
print(f"  KEPT   {stat(crowd)}")
print(f"  DROPPED{stat([x for x in sig if x['ats']>=thr])}   <- want this weak/negative to justify the gate")
