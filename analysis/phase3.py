#!/usr/bin/env python3
"""Phase 3 — exploratory signal constructions (claudeStudy.md Section B).

Judged on the causal series + 45d holdout, same bar as Phase 1/2 (beat baseline
holdout Sharpe +4.31 / r-DD 2.35). Universe HIGH+MID to isolate the trigger change.

  A) Bollinger / price z-score   fade |z|>=thr (replace breakout, and as extra gate)
  B) Volume log-z-score gate     replace 5x median with z_logvol>=thr
  C) RSI(2) extreme              fade overbought/oversold
  D) Cross-sectional reversal    rank universe by past return, fade extremes (market-neutral)

NOT run: VPIN/order-flow (needs historical trade tape — unavailable, HANDOFF.md),
stat-arb pairs sleeve (separate project), frequency sweep (only 1h has the full 8mo).
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
def vol_logz(v,i,n=24):
    seg=[math.log(v[j]) for j in range(i-n,i) if v[j]>0]
    if len(seg)<n//2 or v[i]<=0: return None
    m=sum(seg)/len(seg); sd=(sum((x-m)**2 for x in seg)/len(seg))**0.5
    return (math.log(v[i])-m)/sd if sd>0 else None
def rsi(c,i,n=2):
    g=l=0.0
    for k in range(i-n+1,i+1):
        d=c[k]-c[k-1]
        if d>0: g+=d
        else: l+=-d
    if l==0: return 100.0
    rs=(g/n)/(l/n); return 100-100/(1+rs)

def build(trigger='breakout', tthr=None, volgate='mult', vthr=5.0, boll_extra=False, hold=8):
    cn=[]
    for sym,(t,hi,lo,c,v,ret) in per.items():
        if w.tier(w.uni.get(sym,0)) not in ('HIGH','MID'): continue
        for i in range(24,len(c)-hold):
            if volgate=='mult':
                win=sorted(v[i-24:i]); med=win[len(win)//2] if len(win)%2 else (win[len(win)//2-1]+win[len(win)//2])/2
                if med<=0 or v[i]/med<vthr: continue
            else:
                z=vol_logz(v,i)
                if z is None or z<vthr: continue
            if trigger=='breakout':
                ph=max(hi[i-24:i]); pl=min(lo[i-24:i]); bexp=1 if c[i]>ph else(-1 if c[i]<pl else 0)
            elif trigger=='bollinger':
                z=price_z(c,i); bexp=1 if z>=tthr else(-1 if z<=-tthr else 0)
            else:  # rsi
                r=rsi(c,i); bexp=1 if r>=tthr[0] else(-1 if r<=tthr[1] else 0)
            if bexp==0: continue
            if boll_extra:
                z=price_z(c,i)
                if not((bexp==1 and z>=2) or (bexp==-1 and z<=-2)): continue
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
    print(f"  {lbl:26s} n={m['n']:5d} bps/t={m['bps']:+5.1f} Sh={m['sh']:+5.2f} hold={m['sho']:+6.2f} "
          f"tot=${m['tot']:+6.0f} maxDD=${m['mdd']:6.0f} r/DD={m['rdd']:5.2f}")

bm=metrics(build())
print("BASELINE (breakout, 5x, causal rv60, 8h, HIGH+MID):"); row("baseline",bm); print()
print("A) BOLLINGER / PRICE Z-SCORE:")
for thr in (2.0,2.5):
    row(f"replace |z|>= {thr}", metrics(build(trigger='bollinger',tthr=thr)))
row("breakout + |z|>=2 gate", metrics(build(boll_extra=True)))
print("\nB) VOLUME LOG-Z-SCORE GATE (replaces 5x median):")
for thr in (2.0,2.5,3.0):
    row(f"vol z>= {thr}", metrics(build(volgate='logz',vthr=thr)))
print("\nC) RSI(2) EXTREME:")
for ob,os_ in ((90,10),(95,5)):
    row(f"rsi {ob}/{os_}", metrics(build(trigger='rsi',tthr=(ob,os_))))

# ---------- D) CROSS-SECTIONAL REVERSAL (market-neutral) ----------
print("\nD) CROSS-SECTIONAL REVERSAL (rank by past return, fade extremes, market-neutral):")
maps={sym:{ms:k for k,ms in enumerate(t)} for sym,(t,hi,lo,c,v,ret) in per.items()}
grid=per['BTC'][0] if 'BTC' in per else max(per.values(),key=lambda x:len(x[0]))[0]
def xsec(lookback=24, hold=8, step=8, dec=0.1):
    ser=[]
    for g in range(lookback,len(grid)-hold,step):
        ms=grid[g]; rows=[]
        for sym,(t,hi,lo,c,v,ret) in per.items():
            if w.tier(w.uni.get(sym,0)) not in('HIGH','MID'): continue
            k=maps[sym].get(ms)
            if k is None or k<lookback or k+hold>=len(c): continue
            rows.append((math.log(c[k]/c[k-lookback]), math.log(c[k+hold]/c[k])))
        if len(rows)<20: continue
        rows.sort(); nd=max(1,int(len(rows)*dec))
        leg=[ r[1]-COST for r in rows[:nd] ]+[ -r[1]-COST for r in rows[-nd:] ]  # long losers, short winners
        ser.append((ms,sum(leg)/len(leg)))
    return ser
for lb in (12,24,48):
    s=xsec(lookback=lb)
    if len(s)<10: continue
    rets=[r for _,r in s]; m,sd=moments(rets)
    ppy=365*24/8; ann=m/sd*math.sqrt(ppy) if sd>0 else 0
    cut=max(t for t,_ in s)-HOLDOUT_DAYS*86400000
    ho=[r for t,r in s if t>=cut]; mh,sh_=moments(ho); annh=mh/sh_*math.sqrt(ppy) if sh_>0 else 0
    cum=peak=mdd=0.0
    for _,r in s:
        cum+=r*NOT; peak=max(peak,cum); mdd=min(mdd,cum-peak)
    print(f"  lookback {lb}h  rebals={len(s)} bps/leg={m*1e4:+5.1f} annSh={ann:+5.2f} hold={annh:+5.2f} "
          f"tot=${cum*NOT if False else cum:+6.0f} maxDD=${mdd:6.0f}")

print(f"\nADOPT only if holdout Sharpe > {bm['sho']:+.2f} AND r/DD >= {bm['rdd']:.2f} (baseline)")
print("(exploratory — every new trial raises the deflated-Sharpe bar; treat wins skeptically)")
