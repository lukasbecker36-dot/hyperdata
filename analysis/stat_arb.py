#!/usr/bin/env python3
"""Market-neutral statistical arbitrage (Avellaneda-Lee style) on Hyperliquid perps.

For each coin, at each bar (rolling W-bar estimation window, causal):
  1. regress the coin's returns on a market factor (equal-weight mean return) -> beta
  2. residual return = coin - beta*market  (the idiosyncratic component; removes BTC/market beta)
  3. cumulative residual X_t, fit an OU / AR(1): X_t = a + b*X_{t-1}
     -> equilibrium m = a/(1-b), sigma_eq, s-score = (X_t - m)/sigma_eq, half-life = ln2/(-ln b)
  4. trade the s-score (AL bands): open long if s < -1.25 (buy the cheap residual), close at -0.5;
     open short if s > +1.25, close at +0.75. Only enter if the residual is mean-reverting
     (0<b<1) with a sane half-life. Position = long coin / short beta*market (market-neutral).

Per-trade net return = dir * sum(hedged bar returns over the hold) - cost.  Judged on the
causal series + 45d holdout, same bar as the fade phases. Run from analysis/ (imports wide_stop).
"""
import math, bisect
from collections import defaultdict
import wide_stop as w

W=100; STEP=3; COST=0.0012; S_OPEN=1.25; S_CL_L=-0.5; S_CL_S=0.75
HL_MIN=3.0; HL_MAX=60.0; HOLDOUT_DAYS=45; NOT=100.0
per=w.per_sym
def moments(xs):
    n=len(xs); m=sum(xs)/n; sd=(sum((x-m)**2 for x in xs)/n)**0.5; return m,sd

liquid=[s for s in per if w.tier(w.uni.get(s,0)) in ('HIGH','MID')]
# aligned return-by-timestamp per coin; market factor = equal-weight mean return per ms
ret_at={}
for s in liquid:
    t,hi,lo,c,v,ret=per[s]
    ret_at[s]={t[i]:ret[i] for i in range(1,len(t)) if ret[i]==ret[i]}
sm=defaultdict(float); cn=defaultdict(int)
for s in liquid:
    for ms,r in ret_at[s].items(): sm[ms]+=r; cn[ms]+=1
mkt={ms:sm[ms]/cn[ms] for ms in sm if cn[ms]>=10}
print(f"universe: {len(liquid)} liquid coins  |  market factor over {len(mkt)} bars")

def sscore(xw,yw):
    """rolling OLS beta + OU s-score on the cumulative residual. Returns (beta,s,hl) or None."""
    n=len(xw); Sx=sum(xw); Sy=sum(yw)
    Sxx=sum(a*a for a in xw); Sxy=sum(xw[j]*yw[j] for j in range(n))
    den=n*Sxx-Sx*Sx
    if den==0: return None
    beta=(n*Sxy-Sx*Sy)/den; alpha=(Sy-beta*Sx)/n
    X=[]; acc=0.0
    for j in range(n): acc+=yw[j]-alpha-beta*xw[j]; X.append(acc)
    x1=X[:-1]; x2=X[1:]; m1=len(x1)
    S1=sum(x1); S2=sum(x2); S11=sum(a*a for a in x1); S12=sum(x1[j]*x2[j] for j in range(m1))
    d2=m1*S11-S1*S1
    if d2==0: return (beta,None,None)
    b=(m1*S12-S1*S2)/d2; a=(S2-b*S1)/m1
    if not (0<b<1): return (beta,None,None)
    m_eq=a/(1-b)
    vr=sum((x2[j]-(a+b*x1[j]))**2 for j in range(m1))/max(1,m1-2)
    sig=math.sqrt(vr/(1-b*b)) if vr>0 else 0.0
    if sig<=0: return (beta,None,None)
    s=(X[-1]-m_eq)/sig; hl=math.log(2)/(-math.log(b))
    return (beta,s,hl)

# precompute aligned (xs,ys,tms) per coin once
aligned={}
for s in liquid:
    t,hi,lo,c,v,ret=per[s]
    xs=[]; ys=[]; tms=[]
    for i in range(1,len(t)):
        ms=t[i]
        if ms in mkt and ret[i]==ret[i]:
            xs.append(mkt[ms]); ys.append(ret[i]); tms.append(ms)
    if len(xs)>=W+STEP+2: aligned[s]=(xs,ys,tms)

def generate(s_open, s_cl):
    trades=[]
    for s,(xs,ys,tms) in aligned.items():
        pos=0; beta_e=0.0; acc=0.0; k=W
        while k < len(xs)-1:
            r=sscore(xs[k-W:k], ys[k-W:k])
            s_sc = r[1] if r else None; hl = r[2] if r else None; beta = r[0] if r else 0.0
            if pos==0:
                if s_sc is not None and hl is not None and HL_MIN<=hl<=HL_MAX:
                    if s_sc < -s_open: pos=+1; beta_e=beta; acc=0.0
                    elif s_sc >  s_open: pos=-1; beta_e=beta; acc=0.0
            else:
                if (pos>0 and (s_sc is None or s_sc>-s_cl)) or (pos<0 and (s_sc is None or s_sc< s_cl)):
                    trades.append((tms[k], acc-COST)); pos=0
            for j in range(k, min(k+STEP, len(xs)-1)):
                if pos!=0: acc += pos*(ys[j+1]-beta_e*xs[j+1])
            k += STEP
        if pos!=0: trades.append((tms[min(k,len(tms)-1)], acc-COST))
    return trades

def daily_sharpe(rows):
    byd=defaultdict(list)
    for tm,rr in rows: byd[tm//86400000].append(rr)
    ser=[sum(v)/len(v) for _,v in sorted(byd.items())]
    if len(ser)<2: return 0.0,0
    m,sd=moments(ser); return (m/sd*math.sqrt(365) if sd>0 else 0.0), len(ser)

print(f"\nMARKET-NEUTRAL STAT-ARB (W={W}, OU s-score; cost {COST*1e4:.0f}bps rt over 2 legs)")
print(f"  {'open|z|':>7} {'trades':>7} {'gross bps':>9} {'net bps':>8} {'win%':>5} {'annSh':>6} {'holdout':>7} {'total$':>7}")
for s_open,s_cl in ((1.25,0.5),(2.0,0.75),(2.5,1.0),(3.0,1.0)):
    tr=generate(s_open,s_cl)
    if not tr:
        print(f"  {s_open:>7} {'0':>7}"); continue
    rets=[r for _,r in tr]; m,sd=moments(rets); wins=sum(1 for r in rets if r>0)/len(rets)*100
    gross=(m+COST)*1e4
    sh,nd=daily_sharpe(tr); tmax=max(t for t,_ in tr)
    sho,_=daily_sharpe([(t,r) for t,r in tr if t>=tmax-HOLDOUT_DAYS*86400000])
    print(f"  {s_open:>7} {len(tr):>7} {gross:>+9.1f} {m*1e4:>+8.1f} {wins:>5.1f} {sh:>+6.2f} {sho:>+7.2f} {NOT*sum(rets):>+7.0f}")
print("\ngross = before cost; net = after 12bps round-trip (2 legs). Verdict below.")
