#!/usr/bin/env python3
"""Phase 1 — re-adjudicate the three risk levers under the honest (causal) harness.

Levers (from claudeStudy.md Tier-2/3):
  1. Vol-scaled sizing        notional ~ 1/realized-vol (cap 4x), constant risk/trade
  2. Same-direction cap       limit concurrent same-side positions (correlation control)
  3. ATR catastrophe stop     stop at k x ATR (k=3,4,5) — wider on high-vol names, unlike
                              the fixed-% stops already shown to fail

Judged on the causal signal series (no lookahead) by: annualized daily Sharpe, untouched
45d holdout Sharpe, and the dollar risk metrics (maxDD, worst trade, return/|DD|).
Adopt only if it improves risk-adjusted return without worsening the tail. Run from analysis/.
"""
import math, bisect
import wide_stop as w

MAXH=w.MAXH; COST=0.0011; RV_PCT=0.60; WARMUP=300; HOLDOUT_DAYS=45; NOT=100.0

def pctile(s,q):
    n=len(s)
    if n<2: return s[0] if s else None
    pos=q*(n-1); lo=int(pos); hi=min(lo+1,n-1); return s[lo]+(s[hi]-s[lo])*(pos-lo)
def moments(xs):
    n=len(xs); m=sum(xs)/n; sd=(sum((x-m)**2 for x in xs)/n)**0.5; return m,sd

# ---- causal signal set (matches wf_harness) ----
cands=[]
for sym,(t,hi,lo,c,v,ret) in w.per_sym.items():
    for i in range(24,len(c)-MAXH):
        win=sorted(v[i-24:i]); med=win[len(win)//2] if len(win)%2 else (win[len(win)//2-1]+win[len(win)//2])/2
        if med<=0 or v[i]/med<5: continue
        ph=max(hi[i-24:i]); pl=min(lo[i-24:i]); brk=1 if c[i]>ph else(-1 if c[i]<pl else 0)
        if brk==0 or w.tier(w.uni.get(sym,0)) not in('HIGH','MID'): continue
        rv=w.sample_std(ret[i-23:i+1])
        if math.isnan(rv): continue
        f8=w.fund8_at(sym,t[i])
        if f8 is None or brk*(1 if f8>0 else -1)!=1: continue
        cands.append((t[i],rv,sym,i,brk))
cands.sort()
prior=[]; T=[]
for (tm,rv,sym,i,brk) in cands:
    if len(prior)>=WARMUP:
        thr=pctile(prior,RV_PCT)
        if rv>=thr:
            cc=w.per_sym[sym]; e=cc[3][i]
            T.append({'t':tm,'texit':cc[0][i+MAXH],'sym':sym,'i':i,'brk':brk,'rv':rv,'e':e,
                      'ret':-brk*math.log(cc[3][i+MAXH]/e)-COST})
    bisect.insort(prior,rv)
tmax=max(x['t'] for x in T); cutoff=tmax-HOLDOUT_DAYS*86400000

def atr(sym,i,n=24):
    _,hi,lo,c,_,_=w.per_sym[sym]; s=0.0; k=0
    for j in range(max(1,i-n+1),i+1):
        s+=max(hi[j]-lo[j],abs(hi[j]-c[j-1]),abs(lo[j]-c[j-1])); k+=1
    return s/k if k else 0.0
def stopped(sym,i,brk,sf):
    _,hi,lo,c,_,_=w.per_sym[sym]; d=-brk; e=c[i]
    for k in range(1,MAXH+1):
        if d==-1 and hi[i+k]>=e*(1+sf): return -sf-COST
        if d==1 and lo[i+k]<=e*(1-sf): return -sf-COST
    return d*math.log(c[i+MAXH]/e)-COST

def daily_sharpe(rows):   # rows=(t_ms, contribution)
    byd={}
    for tm,r in rows: byd.setdefault(tm//86400000,[]).append(r)
    ser=[sum(v)/len(v) for _,v in sorted(byd.items())]
    m,sd=moments(ser); return (m/sd*math.sqrt(365)) if sd>0 else 0.0

def metrics(trades, weighted=False):
    # per-trade contribution (equal or vol-scaled) for Sharpe
    if weighted:
        inv=[1.0/x['rv'] for x in trades]; mi=sum(inv)/len(inv)
        for x,q in zip(trades,inv): x['w']=min(q/mi,4.0)
    else:
        for x in trades: x['w']=1.0
    rows=[(x['t'], x['w']*x['ret']) for x in trades]
    hold=[(x['t'],x['w']*x['ret']) for x in trades if x['t']>=cutoff]
    sh=daily_sharpe(rows); sho=daily_sharpe(hold)
    # dollar, concurrency-aware equity
    ev=sorted(trades,key=lambda x:x['texit'])
    pnls=[NOT*x['w']*x['ret'] for x in ev]; exits=[x['texit'] for x in ev]
    cum=peak=mdd=0.0
    for p in pnls: cum+=p; peak=max(peak,cum); mdd=min(mdd,cum-peak)
    pre=[0.0]
    for p in pnls: pre.append(pre[-1]+p)
    W=48*3600*1000; w48=0.0
    for a in range(len(exits)):
        b=bisect.bisect_right(exits,exits[a]+W); w48=min(w48,pre[b]-pre[a])
    worst=min(pnls)
    return dict(n=len(trades),sh=sh,sho=sho,tot=cum,mdd=mdd,w48=w48,worst=worst,
                rdd=cum/abs(mdd) if mdd<0 else float('inf'))

def cap_series(cap, side):   # same-direction concurrency cap, causal by entry
    order=sorted(T,key=lambda x:x['t']); openp=[]; kept=[]
    for x in order:
        openp=[p for p in openp if p[0]>x['t']]
        same=sum(1 for p in openp if side=='BOTH' or p[1]==x['brk'])
        gate=(side=='BOTH') or (x['brk']==(1 if side=='SHORT' else -1))
        if gate and same>=cap: continue
        kept.append(x); openp.append((x['texit'],x['brk']))
    return kept

def atr_series(k):
    out=[]
    for x in T:
        a=atr(x['sym'],x['i']); sf=k*a/x['e'] if x['e']>0 else 0
        y=dict(x); y['ret']=stopped(x['sym'],x['i'],x['brk'],sf) if sf>0 else x['ret']
        out.append(y)
    return out

def row(lbl,m):
    print(f"  {lbl:26s} n={m['n']:4d} Sh={m['sh']:+5.2f} hold={m['sho']:+5.2f} | "
          f"tot=${m['tot']:+7.0f} maxDD=${m['mdd']:7.0f} worst=${m['worst']:6.0f} "
          f"r/DD={m['rdd']:5.2f}")

print(f"causal trades={len(T)}  holdout={HOLDOUT_DAYS}d\n")
base=metrics(T); print("BASELINE (flat $100, no cap, no stop):"); row("baseline",base)
print("\n1) VOL-SCALED SIZING (notional ~ 1/rv, cap 4x):")
row("vol-scaled", metrics([dict(x) for x in T], weighted=True))
print("\n2) SAME-DIRECTION CONCURRENCY CAP:")
for side in ('SHORT','BOTH'):
    for cap in (3,5,8):
        row(f"cap {cap} {side}", metrics(cap_series(cap,side)))
print("\n3) ATR CATASTROPHE STOP (k x ATR24):")
for k in (3,4,5):
    row(f"stop {k}xATR", metrics(atr_series(k)))
print(f"\nbaseline Sharpe {base['sh']:+.2f} / holdout {base['sho']:+.2f} / r-DD {base['rdd']:.2f}"
      f" — adopt a lever only if it beats these OOS without worsening the tail")
