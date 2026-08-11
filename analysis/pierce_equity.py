#!/usr/bin/env python3
"""Equity backtest of PIERCE-weighted sizing vs flat / ats, on identical entries.

entry_geometry.py found pierce depth (how far the breakout closed beyond the prior 24h range) is a
robust, OOS-stable, monthly-consistent edge concentrator, independent of the rv gate. Here we test it as
a live SIZING lever, exactly like ats: notional = NOT * clip(pierce_ratio/REF, MIN, MAX), where
pierce_ratio = pierce / trailing-median-pierce (causal). Same signal set as ats_equity.py, same metrics.
Deep-pierce trades are bigger bets, so the question is risk-ADJUSTED: does it lift Sharpe / return-per-DD,
not just raw $? Also reports a deep-only FILTER (drop the shallow half) since those are ~break-even.
Causal signals, 45d holdout, 3x leverage. Run from analysis/.
"""
import math, bisect, csv, sys, os
from collections import defaultdict
_o=sys.stdout; sys.stdout=open(os.devnull,"w")
import wide_stop as w
sys.stdout.close(); sys.stdout=_o

MAXH=8; COST=0.0011; WARMUP=300; RV_PCT=0.60; NOT=100.0; LEV=3.0
SIZE_REF=2.0; SIZE_MIN=0.5; SIZE_MAX=3.0; REF_P=1.0; HOLD_MS=45*86400000
per=w.per_sym
def median(xs): s=sorted(xs); n=len(s); return s[n//2] if n%2 else 0.5*(s[n//2-1]+s[n//2])
def pctile(s,q):
    n=len(s)
    if n<2: return s[0] if s else 0
    p=q*(n-1); lo=int(p); hi=min(lo+1,n-1); return s[lo]+(s[hi]-s[lo])*(p-lo)

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
        pa=[v[j]/nmap[t[j]] for j in range(i-24,i) if nmap.get(t[j],0)>0]
        if len(pa)<12: continue
        ma=median(pa)
        if ma<=0: continue
        pierce=(c[i]-ph)/ph if brk>0 else (pl-c[i])/pl
        cands.append((t[i], rv, t[i+MAXH], -brk*math.log(c[i+MAXH]/c[i])-COST, (v[i]/ni)/ma, pierce))
cands.sort()

prior=[]; pplist=[]; trades=[]
for (tm,rv,et,net,ats,pierce) in cands:
    if len(prior)>=WARMUP and rv>=pctile(prior,RV_PCT):
        pmed=median(pplist) if len(pplist)>=WARMUP else pierce   # causal trailing median pierce
        pratio=pierce/pmed if pmed>0 else 1.0
        trades.append({'t':tm,'et':et,'net':net,
            'mult_ats':min(SIZE_MAX,max(SIZE_MIN,ats/SIZE_REF)),
            'mult_p':min(SIZE_MAX,max(SIZE_MIN,pratio/REF_P)),
            'pratio':pratio})
        bisect.insort(pplist,pierce)
    bisect.insort(prior,rv)
print(f"causal breakout/HIGH+MID signals: {len(trades)}\n")

tmin=min(x['t'] for x in trades); tmax=max(x['et'] for x in trades); days=(tmax-tmin)/86400000
def run(multkey, keep=None):
    sel=[x for x in trades if (keep is None or keep(x))]
    for x in sel:
        m = 1.0 if multkey=='flat' else x[multkey]
        x['notl']=NOT*m; x['pnl']=x['notl']*x['net']
    ev=sorted(sel,key=lambda x:x['et']); cum=peak=mdd=0.0
    for x in ev: cum+=x['pnl']; peak=max(peak,cum); mdd=min(mdd,cum-peak)
    byd=defaultdict(float); hd=defaultdict(float)
    for x in sel:
        byd[x['et']//86400000]+=x['pnl']
        if x['et']>=tmax-HOLD_MS: hd[x['et']//86400000]+=x['pnl']
    def sh(d):
        s=list(d.values())
        if len(s)<2: return 0.0
        m=sum(s)/len(s); sd=(sum((z-m)**2 for z in s)/len(s))**0.5; return m/sd*math.sqrt(365) if sd>0 else 0
    evs=[]
    for x in sel: evs.append((x['t'],x['notl'])); evs.append((x['et'],-x['notl']))
    evs.sort(); dep=mx=0.0
    for _,d in evs: dep+=d; mx=max(mx,dep)
    pm=mx/LEV; avg=sum(x['notl'] for x in sel)/len(sel)
    return dict(n=len(sel),total=cum,mdd=mdd,sh=sh(byd),sho=sh(hd),avgN=avg,peakN=mx,pm=pm,
                roi=cum/pm*100*365/days if pm else 0, rdd=cum/abs(mdd) if mdd else 0)

# combined ats*pierce multiplier
for x in trades: x['mult_c']=min(SIZE_MAX,max(SIZE_MIN,x['mult_ats']*x['mult_p']/1.0))
cols=[("FLAT",run('flat')), ("ATS",run('mult_ats')), ("PIERCE",run('mult_p')),
      ("ATS*PIERCE",run('mult_c')), ("DEEP-only flat",run('flat',keep=lambda x:x['pratio']>=1.0))]
labels=[c[0] for c in cols]
print(f"{'metric':22s}"+"".join(f"{l:>13s}" for l in labels))
def line(name,key,fmt):
    print(f"{name:22s}"+"".join(f"{fmt(c[1][key]):>13s}" for c in cols))
line("trades","n",lambda x:f"{x}")
line("avg notional","avgN",lambda x:f"${x:.0f}")
line("total P&L","total",lambda x:f"${x:+.0f}")
line("daily Sharpe (ann)","sh",lambda x:f"{x:+.2f}")
line("holdout Sharpe 45d","sho",lambda x:f"{x:+.2f}")
line("max drawdown","mdd",lambda x:f"${x:.0f}")
line("return / |maxDD|","rdd",lambda x:f"{x:.2f}")
line("peak margin (3x)","pm",lambda x:f"${x:.0f}")
line("ROI on margin (ann)","roi",lambda x:f"{x:+.0f}%")
print("\nPIERCE wins only if daily/holdout Sharpe or return/|DD| beats FLAT and ATS -- not just raw $ or ROI")
print("(deeper pierces are bigger bets). DEEP-only shows the filter version (drop shallow half, flat size).")
