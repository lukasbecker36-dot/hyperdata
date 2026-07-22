#!/usr/bin/env python3
"""Full equity backtest of the 15m-ats arm: breakout/HIGH+MID entries, each trade sized by the
LIVE rule notional = 100 * clip(ats_ratio/2, 0.5, 3.0). Compared head-to-head with the flat-$100
version of the SAME entries, so any difference is purely the whale-vs-crowd sizing.
Causal signal set, 45d holdout, 3x leverage for margin/ROI. Run from analysis/.
"""
import math, bisect, csv
from collections import defaultdict
import wide_stop as w

MAXH=8; COST=0.0011; WARMUP=300; RV_PCT=0.60; NOT=100.0; LEV=3.0
SIZE_REF=2.0; SIZE_MIN=0.5; SIZE_MAX=3.0; HOLD_MS=45*86400000
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
        cands.append((t[i], rv, t[i+MAXH], -brk*math.log(c[i+MAXH]/c[i])-COST, (v[i]/ni)/ma))
cands.sort()
prior=[]; trades=[]
for (tm,rv,et,net,ats) in cands:
    if len(prior)>=WARMUP and rv>=pctile(prior,RV_PCT):
        trades.append({'t':tm,'et':et,'net':net,'mult':min(SIZE_MAX,max(SIZE_MIN,ats/SIZE_REF))})
    bisect.insort(prior,rv)
print(f"causal breakout/HIGH+MID signals with trade-count data: {len(trades)}\n")

tmin=min(x['t'] for x in trades); tmax=max(x['et'] for x in trades); days=(tmax-tmin)/86400000
def run(sized):
    for x in trades: x['notl']=NOT*(x['mult'] if sized else 1.0); x['pnl']=x['notl']*x['net']
    ev=sorted(trades,key=lambda x:x['et']); cum=peak=mdd=0.0
    for x in ev: cum+=x['pnl']; peak=max(peak,cum); mdd=min(mdd,cum-peak)
    byd=defaultdict(float); hd=defaultdict(float)
    for x in trades:
        byd[x['et']//86400000]+=x['pnl']
        if x['et']>=tmax-HOLD_MS: hd[x['et']//86400000]+=x['pnl']
    def sh(d):
        s=list(d.values());
        if len(s)<2: return 0.0
        m=sum(s)/len(s); sd=(sum((z-m)**2 for z in s)/len(s))**0.5; return m/sd*math.sqrt(365) if sd>0 else 0
    evs=[]
    for x in trades: evs.append((x['t'],x['notl'])); evs.append((x['et'],-x['notl']))
    evs.sort(); dep=mx=0.0
    for _,d in evs: dep+=d; mx=max(mx,dep)
    pm=mx/LEV; avg=sum(x['notl'] for x in trades)/len(trades)
    return dict(total=cum,mdd=mdd,sh=sh(byd),sho=sh(hd),avgN=avg,peakN=mx,pm=pm,
                roi=cum/pm*100*365/days, rdd=cum/abs(mdd) if mdd else 0)

f=run(False); a=run(True)
print(f"{'metric':22s} {'FLAT $100':>12s} {'ATS-SIZED':>12s}")
rows=[("trades",f"{len(trades)}",f"{len(trades)}"),
      ("avg notional",f"${f['avgN']:.0f}",f"${a['avgN']:.0f}"),
      ("total P&L",f"${f['total']:+.0f}",f"${a['total']:+.0f}"),
      ("daily $ Sharpe (ann)",f"{f['sh']:+.2f}",f"{a['sh']:+.2f}"),
      ("holdout Sharpe (45d)",f"{f['sho']:+.2f}",f"{a['sho']:+.2f}"),
      ("max drawdown",f"${f['mdd']:.0f}",f"${a['mdd']:.0f}"),
      ("return / |maxDD|",f"{f['rdd']:.2f}",f"{a['rdd']:.2f}"),
      ("peak notional",f"${f['peakN']:.0f}",f"${a['peakN']:.0f}"),
      ("peak margin (3x)",f"${f['pm']:.0f}",f"${a['pm']:.0f}"),
      ("ROI on margin (ann)",f"{f['roi']:+.0f}%",f"{a['roi']:+.0f}%")]
for k,fv,av in rows: print(f"{k:22s} {fv:>12s} {av:>12s}")
print(f"\nverdict: ATS helps only if it lifts Sharpe / return-per-DD, not just raw $ (it deploys more capital).")
