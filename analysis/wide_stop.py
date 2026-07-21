#!/usr/bin/env python3
"""Catastrophic-WIDE-stop sweep — stdlib reimplementation of stop_target.py.

Question: is there a stop wide enough to clip an ACE/HEMI -25% blowup WITHOUT
killing the reversion edge (which needs room to breathe)? The repo only swept
down to 3% and found stops hurt; live blowups were ~25%. Test the 5-12% band.

Replicates stop_target.py exactly:
  - 1h candles, per-symbol features (24h windows), rv_thr = global 60th pct of rv24
  - signal = 5x vol spike + 24h breakout + rv24>=thr + HIGH/MID tier + crowd-aligned funding
  - fade the breakout; walk intrabar highs/lows for stop/target; else time-exit at 8h
  - P&L per trade net of 11 bps round-trip cost
"""
import csv, math, bisect

H1 = '../hyperliquid_1h_history.csv'
FUND = '../hyperliquid_funding.csv'
UNI = '../perp_universe.csv'
COST = 0.0011
MAXH = 8

def pctile(sorted_vals, q):
    n=len(sorted_vals);
    if n==1: return sorted_vals[0]
    pos=q*(n-1); lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    if lo==hi: return sorted_vals[lo]
    return sorted_vals[lo]+(sorted_vals[hi]-sorted_vals[lo])*(pos-lo)

def sample_std(xs):
    n=len(xs)
    if n<2: return float('nan')
    m=sum(xs)/n
    return (sum((x-m)**2 for x in xs)/(n-1))**0.5

# ---- tiers ----
vols=[]; uni={}
with open(UNI) as f:
    for r in csv.DictReader(f):
        v=float(r['day_notional_vol']); uni[r['name']]=v; vols.append(v)
vols.sort(); q1=pctile(vols,1/3); q2=pctile(vols,2/3)
tier=lambda v:'LOW' if v<q1 else ('MID' if v<q2 else 'HIGH')

# ---- funding: per-symbol fund8 = rolling(8,min1).mean, times sorted ----
fund_raw={}
with open(FUND) as f:
    for r in csv.DictReader(f):
        fund_raw.setdefault(r['symbol'],[]).append((int(r['time_ms']),float(r['funding_rate'])))
fund_series={}
for sym,arr in fund_raw.items():
    arr.sort()
    times=[a[0] for a in arr]; rates=[a[1] for a in arr]
    f8=[];
    for i in range(len(rates)):
        w=rates[max(0,i-7):i+1]; f8.append(sum(w)/len(w))
    fund_series[sym]=(times,f8)

def fund8_at(sym, ms, tol=3*3600*1000):
    fs=fund_series.get(sym)
    if not fs: return None
    times,f8=fs
    j=bisect.bisect_right(times,ms)-1        # latest funding time <= ms
    if j<0 or ms-times[j]>tol: return None
    return f8[j]

# ---- load candles per symbol ----
cand={}
with open(H1) as f:
    for r in csv.DictReader(f):
        cand.setdefault(r['symbol'],[]).append(
            (int(r['open_time_ms']),float(r['open']),float(r['high']),float(r['low']),float(r['close']),float(r['volume'])))

# ---- build signals + collect all rv24 for global threshold ----
all_rv=[]; per_sym={}
for sym,rows in cand.items():
    if len(rows)<600: continue
    rows.sort()
    t=[x[0] for x in rows]; hi=[x[2] for x in rows]; lo=[x[3] for x in rows]; c=[x[4] for x in rows]; v=[x[5] for x in rows]
    ret=[float('nan')]+[math.log(c[i]/c[i-1]) for i in range(1,len(c))]
    per_sym[sym]=(t,hi,lo,c,v,ret)
    for i in range(len(c)):
        if i>=24:
            rv=sample_std(ret[i-23:i+1])
            if not math.isnan(rv): all_rv.append(rv)
all_rv.sort(); rv_thr=pctile(all_rv,0.60)

signals=[]  # (sym, i, brk)
for sym,(t,hi,lo,c,v,ret) in per_sym.items():
    for i in range(24,len(c)-MAXH):
        # vratio: v[i] / median(v[i-24:i])
        w=sorted(v[i-24:i]); med=w[len(w)//2] if len(w)%2 else (w[len(w)//2-1]+w[len(w)//2])/2
        if med<=0 or v[i]/med<5: continue
        ph=max(hi[i-24:i]); pl=min(lo[i-24:i])
        brk=1 if c[i]>ph else (-1 if c[i]<pl else 0)
        if brk==0: continue
        if tier(uni.get(sym,0)) not in ('HIGH','MID'): continue
        rv=sample_std(ret[i-23:i+1])
        if math.isnan(rv) or rv<rv_thr: continue
        f8=fund8_at(sym,t[i])
        if f8 is None: continue
        if brk*(1 if f8>0 else (-1 if f8<0 else 0))!=1: continue
        signals.append((sym,i,brk))
print(f"stacked-filter signals: {len(signals)}   (rv_thr={rv_thr:.5f})\n")

def simulate(stop, target, maxh=MAXH):
    rets=[]; exits=[]
    for sym,i,brk in signals:
        t,hi,lo,c,v,ret=per_sym[sym]; d=-brk; e=c[i]; outcome=None
        for k in range(1,maxh+1):
            H=hi[i+k]; L=lo[i+k]
            if d==-1:   # fade short: hurt by up moves
                if stop and H>=e*(1+stop): outcome=(-stop,'stop'); break
                if target and L<=e*(1-target): outcome=(target,'tgt'); break
            else:       # fade long: hurt by down moves
                if stop and L<=e*(1-stop): outcome=(-stop,'stop'); break
                if target and H>=e*(1+target): outcome=(target,'tgt'); break
        if outcome is None:
            outcome=(d*math.log(c[i+maxh]/e),'time')
        rets.append(outcome[0]-COST); exits.append(outcome[1])
    return rets, exits

def stats(rets):
    n=len(rets); m=sum(rets)/n; sd=(sum((x-m)**2 for x in rets)/n)**0.5
    sr=sorted(rets); worst=sr[0]; p5=pctile(sr,0.05)
    win=sum(1 for x in rets if x>0)/n*100
    return dict(n=n,net=m*1e4,win=win,worst=worst*100,p5=p5*100,sharpe=m/sd,cum=sum(rets)*100)

print(f"{'stop':>6s} {'tgt':>5s} | {'net bps':>7s} {'win%':>5s} {'worst%':>7s} {'p5%':>6s} {'PTsharpe':>8s} {'cum%':>7s} {'stops':>6s}")
configs=[(None,None),(0.03,None),(0.05,None),(0.06,None),(0.08,None),(0.10,None),(0.12,None),
         (0.06,0.06),(0.08,0.08),(0.10,0.08),(0.08,0.10)]
for stop,tgt in configs:
    rets,exits=simulate(stop,tgt); s=stats(rets)
    nstop=sum(1 for e in exits if e=='stop')
    ls='none' if not stop else f'{stop*100:.0f}%'; lt='hold' if not tgt else f'{tgt*100:.0f}%'
    print(f"{ls:>6s} {lt:>5s} | {s['net']:+7.1f} {s['win']:5.1f} {s['worst']:+7.1f} {s['p5']:+6.1f} {s['sharpe']:+8.3f} {s['cum']:+7.1f} {nstop:6d}")

# how much of baseline loss is in the far tail, and what wide stops recover
base,_=simulate(None,None)
sb=sorted(base)
print(f"\nbaseline: {sum(1 for x in base if x< -0.06)} trades worse than -6%  (sum {sum(x for x in base if x<-0.06)*100:+.1f}%)")
print(f"          {sum(1 for x in base if x< -0.10)} trades worse than -10% (sum {sum(x for x in base if x<-0.10)*100:+.1f}%)")
for stp in (0.06,0.08,0.10):
    r,_=simulate(stp,None)
    print(f"stop {int(stp*100)}%: cum {sum(r)*100:+.1f}%  vs baseline {sum(base)*100:+.1f}%  "
          f"(delta {(sum(r)-sum(base))*100:+.1f}%)  worst trade {min(r)*100:+.1f}%")
