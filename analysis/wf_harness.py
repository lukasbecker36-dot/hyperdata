#!/usr/bin/env python3
"""Phase 0 validity harness — deflate the Sharpe honestly.

Fixes the two biggest validity problems the report (claudeStudy.md) flags:
  1. LOOKAHEAD: the core studies set the realized-vol threshold from the FULL-sample
     60th percentile. Here every signal's threshold is computed CAUSALLY from prior
     signal candidates only (expanding trailing quantile) -> no peeking.
  2. INFLATED SHARPE: reports the Deflated Sharpe Ratio (Bailey-Lopez de Prado 2014),
     which corrects for (a) how many configs were tried and (b) non-normal (skewed,
     fat-tailed) returns; plus a block-bootstrap CI and an untouched final holdout.

Signal = same stacked filter as stop_target.py (5x vol spike + 24h breakout + HIGH/MID
tier + crowd-aligned funding), fade 8h hold, 11 bps round-trip cost.
Reuses wide_stop.py for data loading (run from the analysis/ directory).
"""
import math, bisect
import wide_stop as w

MAXH = w.MAXH; COST = 0.0011; RV_PCT = 0.60
WARMUP = 300          # causal threshold needs this many prior candidates before we trade
HOLDOUT_DAYS = 45     # final untouched OOS slice
GAMMA = 0.5772156649  # Euler-Mascheroni

# ---------- normal CDF / inverse (stdlib only) ----------
def ncdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def nppf(p):
    a=[-3.969683028665376e+01,2.209460984245205e+02,-2.759285104469687e+02,1.383577518672690e+02,-3.066479806614716e+01,2.506628277459239e+00]
    b=[-5.447609879822406e+01,1.615858368580409e+02,-1.556989798598866e+02,6.680131188771972e+01,-1.328068155288572e+01]
    c=[-7.784894002430293e-03,-3.223964580411365e-01,-2.400758277161838e+00,-2.549732539343734e+00,4.374664141464968e+00,2.938163982698783e+00]
    d=[7.784695709041462e-03,3.224671290700398e-01,2.445134137142996e+00,3.754408661907416e+00]
    pl=0.02425; ph=1-pl
    if p<pl:
        q=math.sqrt(-2*math.log(p)); return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5])/((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p<=ph:
        q=p-0.5; r=q*q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q/(((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q=math.sqrt(-2*math.log(1-p)); return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5])/((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)

def moments(xs):
    n=len(xs); m=sum(xs)/n
    v=sum((x-m)**2 for x in xs)/n; sd=v**0.5
    if sd==0: return m,sd,0.0,3.0
    g3=sum((x-m)**3 for x in xs)/n/sd**3
    g4=sum((x-m)**4 for x in xs)/n/sd**4
    return m,sd,g3,g4

def pctile(sorted_vals,q):
    n=len(sorted_vals)
    if n==0: return None
    if n==1: return sorted_vals[0]
    pos=q*(n-1); lo=int(pos); hi=min(lo+1,n-1)
    return sorted_vals[lo]+(sorted_vals[hi]-sorted_vals[lo])*(pos-lo)

# ---------- build candidate signals (BEFORE the rv filter), with timestamps ----------
cands=[]   # (t_ms, rv24, fade_net, sym, brk)
for sym,(t,hi,lo,c,v,ret) in w.per_sym.items():
    for i in range(24, len(c)-MAXH):
        win=sorted(v[i-24:i]); med=win[len(win)//2] if len(win)%2 else (win[len(win)//2-1]+win[len(win)//2])/2
        if med<=0 or v[i]/med<5: continue
        ph=max(hi[i-24:i]); pl=min(lo[i-24:i])
        brk=1 if c[i]>ph else (-1 if c[i]<pl else 0)
        if brk==0: continue
        if w.tier(w.uni.get(sym,0)) not in ('HIGH','MID'): continue
        rv=w.sample_std(ret[i-23:i+1])
        if math.isnan(rv): continue
        f8=w.fund8_at(sym,t[i])
        if f8 is None: continue
        if brk*(1 if f8>0 else (-1 if f8<0 else 0))!=1: continue
        fade=-brk*math.log(c[i+MAXH]/c[i])-COST
        cands.append((t[i],rv,fade,sym,brk))
cands.sort()
print(f"candidate signals (pre-rv-filter): {len(cands)}")

# ---------- (A) biased full-sample threshold ----------
allrv=sorted(x[1] for x in cands)
thr_full=pctile(allrv,RV_PCT)
full=[x[2] for x in cands if x[1]>=thr_full]

# ---------- (B) causal expanding-trailing threshold (no lookahead) ----------
prior=[]; causal=[]   # causal = list of (t_ms, fade_net)
for (tm,rv,fade,sym,brk) in cands:
    if len(prior)>=WARMUP:
        thr=pctile(prior,RV_PCT)
        if rv>=thr: causal.append((tm,fade))
    bisect.insort(prior,rv)

def daily_series(rows):
    """rows=(t_ms,ret). Portfolio daily return = equal-weight mean of that day's trades."""
    byday={}
    for tm,r in rows:
        day=tm//86400000
        byday.setdefault(day,[]).append(r)
    days=sorted(byday)
    return [(d, sum(byday[d])/len(byday[d])) for d in days]

def ann_sharpe(daily):
    rets=[r for _,r in daily]
    m,sd,g3,g4=moments(rets)
    return (m/sd*math.sqrt(365) if sd>0 else 0.0), rets, (m,sd,g3,g4)

full_daily=daily_series([(0,f) for f in full]) if False else None  # full uses no timestamps; approximate below
# full-sample series needs timestamps too:
full_rows=[(x[0],x[2]) for x in cands if x[1]>=thr_full]
sh_full,_,_=ann_sharpe(daily_series(full_rows))
sh_caus,dret,(dm,dsd,dg3,dg4)=ann_sharpe(daily_series(causal))

print(f"\n--- lookahead haircut (annualized daily Sharpe) ---")
print(f"  (A) full-sample rv threshold (BIASED): {sh_full:+.2f}   n_trades={len(full_rows)}")
print(f"  (B) causal trailing threshold (HONEST): {sh_caus:+.2f}   n_trades={len(causal)}")
print(f"  haircut: {sh_full:+.2f} -> {sh_caus:+.2f}  ({(sh_caus-sh_full):+.2f})")

# ---------- Deflated Sharpe on the causal daily series ----------
caus_daily=daily_series(causal); N=len(caus_daily)
sr=dm/dsd if dsd>0 else 0.0    # per-DAY Sharpe (non-annualized) for PSR/DSR

# estimate SR0 (expected max Sharpe under the null) from a config grid = number of trials tried
MIN_TRADES=200   # a config must trade >= this to count as a real trial (drop degenerate ones)
def config_sharpe(vol_mult, rv_pct, hold):
    rows=[]
    for sym,(t,hi,lo,c,v,ret) in w.per_sym.items():
        for i in range(24, len(c)-hold):
            win=sorted(v[i-24:i]); med=win[len(win)//2]
            if med<=0 or v[i]/med<vol_mult: continue
            ph=max(hi[i-24:i]); pl=min(lo[i-24:i]); brk=1 if c[i]>ph else(-1 if c[i]<pl else 0)
            if brk==0 or w.tier(w.uni.get(sym,0)) not in('HIGH','MID'): continue
            rv=w.sample_std(ret[i-23:i+1])
            if math.isnan(rv): continue
            f8=w.fund8_at(sym,t[i])
            if f8 is None or brk*(1 if f8>0 else -1)!=1: continue
            rows.append((rv,t[i],-brk*math.log(c[i+hold]/c[i])-COST))
    rvs=sorted(r[0] for r in rows); thr=pctile(rvs,rv_pct)
    kept=[(t,f) for rv,t,f in rows if rv>=thr]
    dd=daily_series(kept)
    r=[x for _,x in dd]; m,sd,_,_=moments(r)
    return (m/sd if sd>0 else 0.0, len(kept))

grid=[(vm,rp,h) for vm in (3,4,5,7,10) for rp in (0.5,0.6,0.7) for h in (4,6,8,12)]
raw=[(g,config_sharpe(*g)) for g in grid]
trials=[(g,sr) for g,(sr,n) in raw if n>=MIN_TRADES]     # drop degenerate low-sample configs
trial_srs=[sr for _,sr in trials]
K=len(trial_srs)
mt,sd_sr,_,_=moments(trial_srs)         # moments returns SD; sqrt(V)=SD directly
sr0=sd_sr*((1-GAMMA)*nppf(1-1.0/K)+GAMMA*nppf(1-1.0/(K*math.e)))
trial_sorted=sorted(trial_srs)
deployed_sr=next((sr for g,sr in trials if g==(5,0.6,8)), sr)
rank=sum(1 for s in trial_srs if s<=deployed_sr)
print(f"\n  [trial grid] K={K} configs with >={MIN_TRADES} trades  "
      f"(dropped {len(raw)-K} degenerate)")
print(f"  trial per-day SR: min={min(trial_srs):+.3f} mean={mt:+.3f} max={max(trial_srs):+.3f}")
print(f"  deployed (5x/0.6/8h) per-day SR={deployed_sr:+.3f}  ranks {rank}/{K} in the grid")

denom=math.sqrt(1-dg3*sr+((dg4-1)/4)*sr*sr)
psr0=ncdf((sr-0)*math.sqrt(N-1)/denom)          # prob SR>0
dsr =ncdf((sr-sr0)*math.sqrt(N-1)/denom)        # deflated: prob SR>expected-max-under-null

print(f"\n--- Deflated Sharpe (causal series, N={N} days) ---")
print(f"  daily skew={dg3:+.2f}  kurtosis={dg4:.2f}  (normal=3)  <- fat left tail")
print(f"  per-day SR={sr:+.3f}   annualized={sr*math.sqrt(365):+.2f}")
print(f"  trials K={K}  SD(SR across trials)={sd_sr:.4f}  ->  SR0(expected max)={sr0:+.3f}/day")
print(f"  PSR(SR>0)          = {psr0*100:5.1f}%")
print(f"  Deflated Sharpe    = {dsr*100:5.1f}%   (prob the edge beats the best-of-{K}-trials null)")
print(f"  --> {'PASS' if dsr>0.95 else 'MARGINAL' if dsr>0.5 else 'FAIL'} at 95% confidence")

# ---------- block bootstrap CI on annualized Sharpe ----------
import random
random.seed(0)
blk=5; B=2000; boot=[]
ndays=len(caus_daily); rets=[r for _,r in caus_daily]
nblk=max(1,ndays//blk)
for _ in range(B):
    samp=[]
    for _ in range(nblk):
        s=random.randint(0,ndays-blk); samp+=rets[s:s+blk]
    m,sd,_,_=moments(samp); boot.append(m/sd*math.sqrt(365) if sd>0 else 0)
boot.sort()
print(f"\n--- block-bootstrap 95% CI (annualized Sharpe, {blk}-day blocks, B={B}) ---")
print(f"  point={sh_caus:+.2f}   95% CI=[{boot[int(0.025*B)]:+.2f}, {boot[int(0.975*B)]:+.2f}]")

# ---------- untouched final holdout ----------
tmax=max(t for t,_ in causal); cutoff=tmax-HOLDOUT_DAYS*86400000
hold=[(t,r) for t,r in causal if t>=cutoff]; train=[(t,r) for t,r in causal if t<cutoff]
sh_tr,_,_=ann_sharpe(daily_series(train)); sh_ho,_,_=ann_sharpe(daily_series(hold))
print(f"\n--- final {HOLDOUT_DAYS}d holdout (untouched OOS) ---")
print(f"  train Sharpe={sh_tr:+.2f} ({len(train)} tr)   holdout Sharpe={sh_ho:+.2f} ({len(hold)} tr)")
