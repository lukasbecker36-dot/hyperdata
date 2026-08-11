#!/usr/bin/env python3
"""Capstone equity test: LIVE (ats) + deep-pierce filter + market-vol gate, stacked.

Two validated conditioners stacked onto the live arm, with CAUSAL trailing thresholds (deployable, no
lookahead):
  deep-pierce gate:  keep if pierce >= trailing-median pierce           (fade only the deep overshoots)
  market-vol gate:   keep if BTC's 3-bar move >= trailing 33rd pct btc_move  (stand down in calm markets)
Same signal set / metrics as pierce_equity.py: flat comparison, ats sizing, 45d holdout, 3x leverage.
Shows each gate's incremental effect and the full stack vs the live baseline. Run from analysis/.
"""
import math, bisect, csv, sys, os
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

MAXH=8; COST=0.0011; WARMUP=300; RV_PCT=0.60; NOT=100.0; LEV=3.0
SIZE_REF=2.0; MN=0.5; MX=3.0; HOLD=45*86400000; per=w.per_sym
def median(xs): s=sorted(xs); n=len(s); return s[n//2] if n%2 else .5*(s[n//2-1]+s[n//2])
def pctile(s,q):
    n=len(s)
    if n<2: return s[0] if s else 0
    p=q*(n-1); lo=int(p); hi=min(lo+1,n-1); return s[lo]+(s[hi]-s[lo])*(p-lo)
bt=per['BTC'][0]; bc=per['BTC'][3]; btc_c={bt[k]:bc[k] for k in range(len(bt))}

nt=defaultdict(dict)
with open('../hyperliquid_1h_history.csv') as f:
    for r in csv.DictReader(f):
        try: nt[r['symbol']][int(r['open_time_ms'])]=float(r['num_trades'])
        except: pass

cands=[]
for sym,(t,hi,lo,c,v,ret) in per.items():
    if w.tier(w.uni.get(sym,0)) not in ('HIGH','MID'): continue
    nm=nt.get(sym)
    if not nm: continue
    for i in range(25,len(c)-MAXH):
        win=sorted(v[i-24:i]); md=win[len(win)//2]
        if md<=0 or v[i]/md<5: continue
        ph=max(hi[i-24:i]); pl=min(lo[i-24:i]); brk=1 if c[i]>ph else(-1 if c[i]<pl else 0)
        if brk==0: continue
        rv=w.sample_std(ret[i-23:i+1])
        if math.isnan(rv): continue
        f8=w.fund8_at(sym,t[i])
        if f8 is None or brk*(1 if f8>0 else -1)!=1: continue
        ni=nm.get(t[i])
        if not ni or ni<=0: continue
        pa=[v[j]/nm[t[j]] for j in range(i-24,i) if nm.get(t[j],0)>0]
        if len(pa)<12: continue
        ma=median(pa)
        if ma<=0 or t[i] not in btc_c or t[i-3] not in btc_c: continue
        pierce=(c[i]-ph)/ph if brk>0 else (pl-c[i])/pl
        btcmv=abs(math.log(btc_c[t[i]]/btc_c[t[i-3]]))
        cands.append((t[i], rv, t[i+MAXH], -brk*math.log(c[i+MAXH]/c[i])-COST, (v[i]/ni)/ma, pierce, btcmv))
cands.sort()

prior=[]; pp=[]; bm=[]; trades=[]
for (tm,rv,et,net,ats,pierce,btcmv) in cands:
    if len(prior)>=WARMUP and rv>=pctile(prior,RV_PCT):
        deep = pierce >= median(pp) if len(pp)>=WARMUP else True
        active = btcmv >= pctile(sorted(bm),0.33) if len(bm)>=WARMUP else True
        trades.append({'t':tm,'et':et,'net':net,'mult':min(MX,max(MN,ats/SIZE_REF)),
                       'deep':deep,'active':active})
        bisect.insort(pp,pierce); bisect.insort(bm,btcmv)
    bisect.insort(prior,rv)
tmax=max(x['et'] for x in trades); tmin=min(x['t'] for x in trades); days=(tmax-tmin)/86400000
print(f"{len(trades)} causal signals\n")

def run(keep):
    sel=[dict(x) for x in trades if keep(x)]
    for x in sel: x['notl']=NOT*x['mult']; x['pnl']=x['notl']*x['net']
    ev=sorted(sel,key=lambda x:x['et']); cum=pk=mdd=0.0
    for x in ev: cum+=x['pnl']; pk=max(pk,cum); mdd=min(mdd,cum-pk)
    hd=defaultdict(float)
    for x in sel:
        if x['et']>=tmax-HOLD: hd[x['et']//86400000]+=x['pnl']
    s=list(hd.values()); m=sum(s)/len(s); sd=(sum((z-m)**2 for z in s)/len(s))**0.5
    sho=m/sd*math.sqrt(365) if sd>0 else 0
    byd=defaultdict(float)
    for x in sel: byd[x['et']//86400000]+=x['pnl']
    s2=list(byd.values()); m2=sum(s2)/len(s2); sd2=(sum((z-m2)**2 for z in s2)/len(s2))**0.5
    sh=m2/sd2*math.sqrt(365) if sd2>0 else 0
    ev2=[]
    for x in sel: ev2+=[(x['t'],x['notl']),(x['et'],-x['notl'])]
    ev2.sort(); dep=mp=0.0
    for _,z in ev2: dep+=z; mp=max(mp,dep)
    return dict(n=len(sel),total=cum,mdd=mdd,sh=sh,sho=sho,rdd=cum/abs(mdd) if mdd else 0,
                pm=mp/LEV, roi=cum/(mp/LEV)*100*365/days if mp else 0)

variants=[("LIVE (all + ats)", lambda x:True),
          ("+ deep-pierce", lambda x:x['deep']),
          ("+ mkt-vol gate", lambda x:x['active']),
          ("+ BOTH gates", lambda x:x['deep'] and x['active'])]
res=[(nm,run(k)) for nm,k in variants]
print(f"{'variant':20s}"+"".join(f"{nm:>15s}" for nm,_ in res))
def line(lbl,key,fmt):
    print(f"{lbl:20s}"+"".join(f"{fmt(r[key]):>15s}" for _,r in res))
line("trades","n",lambda x:f"{x}")
line("total P&L","total",lambda x:f"${x:+.0f}")
line("daily Sharpe","sh",lambda x:f"{x:+.2f}")
line("holdout Sharpe 45d","sho",lambda x:f"{x:+.2f}")
line("max drawdown","mdd",lambda x:f"${x:.0f}")
line("return / |maxDD|","rdd",lambda x:f"{x:.2f}")
line("peak margin (3x)","pm",lambda x:f"${x:.0f}")
line("ROI on margin","roi",lambda x:f"{x:+.0f}%")
print("\ngates are causal (trailing median pierce / trailing 33pct btc_move). Judge on holdout Sharpe & return/|DD|.")
