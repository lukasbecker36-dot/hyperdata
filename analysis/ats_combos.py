#!/usr/bin/env python3
"""ATS sizing x {trigger, universe}: does whale-vs-crowd sizing stack with MID and Bollinger?

For each config (breakout|bollinger x HIGH+MID|MID), backtest the flat-$100 and the ats-sized
(notional=100*clip(ats/2,0.5,3)) versions of the SAME entries. Causal signal set, 45d holdout,
3x leverage for margin/ROI. Uses daily dollar-P&L Sharpe (compare flat-vs-ats within a config).
Run from analysis/.
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
def price_z(c,i,n=20):
    seg=c[i-n+1:i+1]; m=sum(seg)/n; sd=(sum((x-m)**2 for x in seg)/n)**0.5
    return (c[i]-m)/sd if sd>0 else 0.0

nt=defaultdict(dict)
with open('../hyperliquid_1h_history.csv') as f:
    for row in csv.DictReader(f):
        try: nt[row['symbol']][int(row['open_time_ms'])]=float(row['num_trades'])
        except Exception: pass

def build(trigger, tiers):
    cands=[]
    for sym,(t,hi,lo,c,v,ret) in per.items():
        if w.tier(w.uni.get(sym,0)) not in tiers: continue
        nmap=nt.get(sym)
        if not nmap: continue
        for i in range(25,len(c)-MAXH):
            win=sorted(v[i-24:i]); med=win[len(win)//2]
            if med<=0 or v[i]/med<5: continue
            if trigger=='breakout':
                ph=max(hi[i-24:i]); pl=min(lo[i-24:i]); brk=1 if c[i]>ph else(-1 if c[i]<pl else 0)
            else:
                z=price_z(c,i); brk=1 if z>=2.5 else(-1 if z<=-2.5 else 0)
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
    cands.sort(); prior=[]; tr=[]
    for (tm,rv,et,net,ats) in cands:
        if len(prior)>=WARMUP and rv>=pctile(prior,RV_PCT):
            tr.append({'t':tm,'et':et,'net':net,'mult':min(SIZE_MAX,max(SIZE_MIN,ats/SIZE_REF))})
        bisect.insort(prior,rv)
    return tr

def metrics(tr, sized):
    if not tr: return None
    for x in tr: x['notl']=NOT*(x['mult'] if sized else 1.0); x['pnl']=x['notl']*x['net']
    ev=sorted(tr,key=lambda x:x['et']); cum=peak=mdd=0.0
    for x in ev: cum+=x['pnl']; peak=max(peak,cum); mdd=min(mdd,cum-peak)
    tmax=max(x['et'] for x in tr); tmin=min(x['t'] for x in tr); days=(tmax-tmin)/86400000
    byd=defaultdict(float); hd=defaultdict(float)
    for x in tr:
        byd[x['et']//86400000]+=x['pnl']
        if x['et']>=tmax-HOLD_MS: hd[x['et']//86400000]+=x['pnl']
    def sh(d):
        s=list(d.values())
        if len(s)<2: return 0.0
        m=sum(s)/len(s); sd=(sum((z-m)**2 for z in s)/len(s))**0.5; return m/sd*math.sqrt(365) if sd>0 else 0
    evs=[]
    for x in tr: evs.append((x['t'],x['notl'])); evs.append((x['et'],-x['notl']))
    evs.sort(); dep=mx=0.0
    for _,d in evs: dep+=d; mx=max(mx,dep)
    pm=mx/LEV
    return dict(n=len(tr),total=cum,sh=sh(byd),sho=sh(hd),mdd=mdd,rdd=cum/abs(mdd) if mdd else 0,
                pm=pm,roi=cum/pm*100*365/days if pm else 0,avgN=sum(x['notl'] for x in tr)/len(tr))

configs=[("ats (breakout HM)","breakout",('HIGH','MID')),
         ("ats+mid","breakout",('MID',)),
         ("ats+boll","bollinger",('HIGH','MID')),
         ("ats+mid+boll","bollinger",('MID',))]
print(f"{'config':18s} {'n':>4} | {'sizing':>5} {'total$':>7} {'dSh':>5} {'hold':>5} {'maxDD$':>7} {'ret/DD':>6} {'pkMargin':>8} {'ROI/yr':>6}")
for name,trig,tiers in configs:
    tr=build(trig,tiers)
    for lbl,sized in (("flat",False),("ATS",True)):
        m=metrics(tr,sized)
        if not m: continue
        print(f"{name:18s} {m['n']:>4} | {lbl:>5} {m['total']:>+7.0f} {m['sh']:>+5.2f} {m['sho']:>+5.2f} "
              f"{m['mdd']:>7.0f} {m['rdd']:>6.2f} {'$'+format(m['pm'],'.0f'):>8} {m['roi']:>+5.0f}%")
print("\ndSh=daily $ Sharpe; compare ATS vs flat WITHIN a config. return/DD is scale-free (capital-normalized).")
