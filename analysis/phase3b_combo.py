#!/usr/bin/env python3
"""Does combining the two OOS-validated levers help? Bollinger x MID-tier.

MID-only (Phase 2) and Bollinger |z|>=2.5 (Phase 3) each beat baseline independently,
tested vs the breakout+HIGH+MID baseline. This checks whether they STACK or interact,
on the causal series + 45d holdout. Run from analysis/.
"""
import math, bisect
import wide_stop as w

MAXH=8; COST=0.0011; WARMUP=300; HOLDOUT_DAYS=45; NOT=100.0
per=w.per_sym
def pctile(s,q):
    n=len(s)
    if n<2: return s[0] if s else None
    pos=q*(n-1); lo=int(pos); hi=min(lo+1,n-1); return s[lo]+(s[hi]-s[lo])*(pos-lo)
def moments(xs):
    n=len(xs); m=sum(xs)/n; sd=(sum((x-m)**2 for x in xs)/n)**0.5; return m,sd
def price_z(c,i,n=20):
    seg=c[i-n+1:i+1]; m=sum(seg)/n; sd=(sum((x-m)**2 for x in seg)/n)**0.5
    return (c[i]-m)/sd if sd>0 else 0.0

def build(trigger, tthr, tiers, hold=8):
    cn=[]
    for sym,(t,hi,lo,c,v,ret) in per.items():
        if w.tier(w.uni.get(sym,0)) not in tiers: continue
        for i in range(24,len(c)-hold):
            win=sorted(v[i-24:i]); med=win[len(win)//2] if len(win)%2 else (win[len(win)//2-1]+win[len(win)//2])/2
            if med<=0 or v[i]/med<5: continue
            if trigger=='breakout':
                ph=max(hi[i-24:i]); pl=min(lo[i-24:i]); bexp=1 if c[i]>ph else(-1 if c[i]<pl else 0)
            else:
                z=price_z(c,i); bexp=1 if z>=tthr else(-1 if z<=-tthr else 0)
            if bexp==0: continue
            rv=w.sample_std(ret[i-23:i+1])
            if math.isnan(rv): continue
            f8=w.fund8_at(sym,t[i])
            if f8 is None or bexp*(1 if f8>0 else -1)!=1: continue
            cn.append((t[i],rv,sym,i,bexp,hold))
    cn.sort(); prior=[]; out=[]
    for (tm,rv,sym,i,bexp,h) in cn:
        if len(prior)>=WARMUP and rv>=pctile(prior,0.60):
            cc=per[sym]; e=cc[3][i]
            out.append({'t':tm,'texit':cc[0][i+h],'ret':-bexp*math.log(cc[3][i+h]/e)-COST})
        bisect.insort(prior,rv)
    return out
def daily_sharpe(rows):
    byd={}
    for tm,r in rows: byd.setdefault(tm//86400000,[]).append(r)
    ser=[sum(v)/len(v) for _,v in sorted(byd.items())]
    if len(ser)<2: return 0.0
    m,sd=moments(ser); return (m/sd*math.sqrt(365)) if sd>0 else 0.0
def metrics(trades):
    if not trades: return dict(n=0,sh=0,sho=0,tot=0,mdd=0,rdd=0)
    tmax=max(x['t'] for x in trades); cut=tmax-HOLDOUT_DAYS*86400000
    sh=daily_sharpe([(x['t'],x['ret']) for x in trades])
    sho=daily_sharpe([(x['t'],x['ret']) for x in trades if x['t']>=cut])
    ev=sorted(trades,key=lambda x:x['texit']); pnls=[NOT*x['ret'] for x in ev]
    cum=peak=mdd=0.0
    for p in pnls: cum+=p; peak=max(peak,cum); mdd=min(mdd,cum-peak)
    return dict(n=len(trades),sh=sh,sho=sho,tot=cum,mdd=mdd,rdd=cum/abs(mdd) if mdd<0 else 0)
def row(lbl,m):
    print(f"  {lbl:34s} n={m['n']:5d} Sh={m['sh']:+5.2f} hold={m['sho']:+6.2f} tot=${m['tot']:+6.0f} "
          f"maxDD=${m['mdd']:6.0f} r/DD={m['rdd']:5.2f}")

HM=('HIGH','MID'); MID=('MID',)
print("Do the two validated levers stack? (causal series, 45d holdout)\n")
row("baseline (breakout, HIGH+MID)",   metrics(build('breakout',None,HM)))
row("MID only (breakout)",             metrics(build('breakout',None,MID)))
row("Bollinger z>=2.5 (HIGH+MID)",     metrics(build('bollinger',2.5,HM)))
row("Bollinger z>=2.5 + MID  <<combo", metrics(build('bollinger',2.5,MID)))
print("\nread: if combo holdout & r/DD beat BOTH singles, they stack; if not, they overlap/interact")
