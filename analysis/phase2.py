#!/usr/bin/env python3
"""Phase 2 — signal-parameter changes, judged on the causal series + 45d holdout.

Disciplined 1-D sweeps around the baseline (multiple-testing hygiene per Phase 0):
  A) Funding-extremity gate   sign-match -> require |funding z| >= thr  (HEADLINE)
  B) Realized-vol cutoff      trailing percentile {50,60,70,80}
  C) Hold / backstop          {4,6,8,12,16}h  + pooled OU half-life estimate
  D) Volume-spike multiple    {3,4,5,7,10}x median
  E) Liquidity buckets        HIGH vs MID; notional-volume quintiles

Adopt only if OOS holdout Sharpe (and return/|DD|) beats baseline. Run from analysis/.
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

def funding_z(sym,ms):
    fs=w.fund_series.get(sym)
    if not fs: return None
    times,f8=fs; j=bisect.bisect_right(times,ms)-1
    if j<30: return None
    win=f8[max(0,j-720):j+1]; m,sd=moments(win)
    return (f8[j]-m)/sd if sd>0 else 0.0

def build(vol_mult=5, rv_pct=0.60, hold=8, want_fz=False):
    cn=[]
    for sym,(t,hi,lo,c,v,ret) in per.items():
        for i in range(24,len(c)-hold):
            win=sorted(v[i-24:i]); med=win[len(win)//2] if len(win)%2 else (win[len(win)//2-1]+win[len(win)//2])/2
            if med<=0 or v[i]/med<vol_mult: continue
            ph=max(hi[i-24:i]); pl=min(lo[i-24:i]); brk=1 if c[i]>ph else(-1 if c[i]<pl else 0)
            if brk==0 or w.tier(w.uni.get(sym,0)) not in('HIGH','MID'): continue
            rv=w.sample_std(ret[i-23:i+1])
            if math.isnan(rv): continue
            f8=w.fund8_at(sym,t[i])
            if f8 is None or brk*(1 if f8>0 else -1)!=1: continue
            cn.append((t[i],rv,sym,i,brk))
    cn.sort(); prior=[]; out=[]
    for (tm,rv,sym,i,brk) in cn:
        if len(prior)>=WARMUP and rv>=pctile(prior,rv_pct):
            cc=per[sym]; e=cc[3][i]
            d={'t':tm,'texit':cc[0][i+hold],'sym':sym,'i':i,'brk':brk,'rv':rv,'e':e,
               'nvol':w.uni.get(sym,0),'ret':-brk*math.log(cc[3][i+hold]/e)-COST}
            if want_fz: d['fz']=funding_z(sym,tm)
            out.append(d)
        bisect.insort(prior,rv)
    return out

def daily_sharpe(rows):
    byd={}
    for tm,r in rows: byd.setdefault(tm//86400000,[]).append(r)
    ser=[sum(v)/len(v) for _,v in sorted(byd.items())]
    if len(ser)<2: return 0.0
    m,sd=moments(ser); return (m/sd*math.sqrt(365)) if sd>0 else 0.0

def metrics(trades):
    if not trades: return dict(n=0,sh=0,sho=0,tot=0,mdd=0,rdd=0,bps=0)
    tmax=max(x['t'] for x in trades); cut=tmax-HOLDOUT_DAYS*86400000
    sh=daily_sharpe([(x['t'],x['ret']) for x in trades])
    sho=daily_sharpe([(x['t'],x['ret']) for x in trades if x['t']>=cut])
    ev=sorted(trades,key=lambda x:x['texit']); pnls=[NOT*x['ret'] for x in ev]
    cum=peak=mdd=0.0
    for p in pnls: cum+=p; peak=max(peak,cum); mdd=min(mdd,cum-peak)
    bps=sum(x['ret'] for x in trades)/len(trades)*1e4
    return dict(n=len(trades),sh=sh,sho=sho,tot=cum,mdd=mdd,
                rdd=cum/abs(mdd) if mdd<0 else float('inf'),bps=bps)
def row(lbl,m):
    print(f"  {lbl:22s} n={m['n']:4d} bps/t={m['bps']:+5.1f} Sh={m['sh']:+5.2f} hold={m['sho']:+5.2f} "
          f"tot=${m['tot']:+6.0f} maxDD=${m['mdd']:6.0f} r/DD={m['rdd']:5.2f}")

base=build(want_fz=True)
bm=metrics(base)
print(f"causal baseline (5x, rv60, 8h): "); row("baseline",bm); print()

print("A) FUNDING-EXTREMITY GATE  (require breakout-aligned |funding z| >= thr):")
zs=[x for x in base if x.get('fz') is not None]
print(f"   ({len(zs)}/{len(base)} trades have a funding z; rest lack 30d history)")
for thr in (0.0,0.5,1.0,1.5,2.0):
    sub=[x for x in zs if x['brk']*x['fz']>=thr]
    row(f"|z|>= {thr}", metrics(sub))

print("\nB) REALIZED-VOL CUTOFF (trailing percentile):")
for p in (0.50,0.60,0.70,0.80):
    row(f"rv pct {int(p*100)}", metrics(build(rv_pct=p)))

print("\nC) HOLD / BACKSTOP:")
for h in (4,6,8,12,16):
    row(f"hold {h}h", metrics(build(hold=h)))
# pooled OU half-life on the breakout-excursion path (x_k = brk*log(c[i+k]/c[i]) -> reverts to 0)
dx=[]; xl=[]
for x in base:
    cc=per[x['sym']]; c=cc[3]; i=x['i']
    prevx=0.0
    for k in range(1,MAXH+1):
        xk=x['brk']*math.log(c[i+k]/x['e']); dx.append(xk-prevx); xl.append(prevx); prevx=xk
mx=sum(xl)/len(xl); mdx=sum(dx)/len(dx)
cov=sum((a-mx)*(b-mdx) for a,b in zip(xl,dx)); var=sum((a-mx)**2 for a in xl)
lam=cov/var if var>0 else 0
hl=(-math.log(2)/lam) if lam<0 else float('inf')
print(f"   pooled OU: lambda={lam:+.4f}/h  ->  half-life={hl:.1f}h  (guides the backstop)")

print("\nD) VOLUME-SPIKE MULTIPLE (x median):")
for m in (3,4,5,7,10):
    row(f"vol {m}x", metrics(build(vol_mult=m)))

print("\nE) LIQUIDITY BUCKETS:")
for tl in ('HIGH','MID'):
    row(f"tier {tl}", metrics([x for x in base if w.tier(x['nvol'])==tl]))
nv=sorted(x['nvol'] for x in base); qs=[pctile(nv,q) for q in (0.2,0.4,0.6,0.8)]
def qbucket(v): return sum(1 for q in qs if v>=q)  # 0..4
for b in range(5):
    row(f"nvol quintile {b+1}", metrics([x for x in base if qbucket(x['nvol'])==b]))

print(f"\nADOPT only if holdout Sharpe > {bm['sho']:+.2f} AND r/DD >= {bm['rdd']:.2f} (baseline)")
