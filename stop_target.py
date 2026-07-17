import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_1h_history.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
COST=0.0011; MAXH=8
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())

store={}; sigs=[]
allrv=[]
tmp=[]
for sym,g in df.groupby('symbol'):
    if len(g)<600: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(24).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(24).median()
    ph=g['high'].shift(1).rolling(24).max(); pl=g['low'].shift(1).rolling(24).min()
    g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0))
    g['tier']=tier(uni.get(sym,0))
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3*3600*1000)
    g=g.reset_index(drop=True)
    store[sym]=(g['close'].values,g['high'].values,g['low'].values,g['open_time_ms'].values)
    tmp.append(g)
big=pd.concat(tmp)
# global vol-quintile threshold (top-40% => rv24 >= 60th pct). NOTE: uses full-sample pctile (mild lookahead).
rv_thr=big['rv24'].quantile(0.60)

for sym,g in big.groupby('symbol'):
    c,hi,lo,tm=store[sym]
    for i in range(len(g)):
        r=g.iloc[i]
        if np.isnan(r['vratio']) or r['vratio']<5 or r['brk']==0: continue
        if r['tier'] not in ('HIGH','MID'): continue
        if np.isnan(r['rv24']) or r['rv24']<rv_thr: continue          # high-vol filter
        if pd.isna(r['fund8']): continue
        if r['brk']*np.sign(r['fund8'])!=1: continue                  # crowd-aligned only
        if i+MAXH>=len(c): continue
        sigs.append((sym,i,int(r['brk']),r['tier'],tm[i]))
sig=pd.DataFrame(sigs,columns=['sym','i','brk','tier','t'])
print(f"stacked-filter signals: {len(sig)}\n")

def simulate(stop,target,maxh=MAXH):
    rets=[]; bars=[]; hit=[]
    for _,r in sig.iterrows():
        c,hi,lo,_=store[r['sym']]; i=r['i']; d=-r['brk']; e=c[i]
        outcome=None
        for k in range(1,maxh+1):
            H=hi[i+k]; L=lo[i+k]
            if d==-1:  # fade short (up-breakout): profit if price falls
                if stop and H>=e*(1+stop): outcome=(-stop,k,'stop'); break
                if target and L<=e*(1-target): outcome=(target,k,'tgt'); break
            else:      # fade long (down-breakout): profit if price rises
                if stop and L<=e*(1-stop): outcome=(-stop,k,'stop'); break
                if target and H>=e*(1+target): outcome=(target,k,'tgt'); break
        if outcome is None:
            outcome=(d*np.log(c[i+maxh]/e),maxh,'time')
        rets.append(outcome[0]-COST); bars.append(outcome[1]); hit.append(outcome[2])
    return np.array(rets),np.array(bars),np.array(hit)

def stats(rets):
    m=rets.mean(); sd=rets.std(); sk=pd.Series(rets).skew()
    return dict(n=len(rets),net=m*1e4,win=(rets>0).mean()*100,worst=rets.min()*100,
               p5=np.percentile(rets,5)*100,skew=sk,sharpe=m/sd,cum=rets.sum()*100)

print(f"{'stop':>5s} {'tgt':>5s} | {'net bps':>7s} {'win%':>5s} {'worst%':>7s} {'p5%':>6s} {'skew':>5s} {'PTsharpe':>8s} {'cum%':>7s}")
configs=[(None,None),(0.03,None),(0.02,None),(0.015,None),(0.02,0.03),(0.015,0.02),(0.02,0.04),(0.03,0.05),(0.01,0.02)]
best=None
for stop,tgt in configs:
    rets,bars,hit=simulate(stop,tgt)
    s=stats(rets)
    lbl_s='none' if not stop else f'{stop*100:.1f}%'; lbl_t='hold' if not tgt else f'{tgt*100:.1f}%'
    print(f"{lbl_s:>5s} {lbl_t:>5s} | {s['net']:+7.1f} {s['win']:5.1f} {s['worst']:+7.1f} {s['p5']:+6.1f} {s['skew']:+5.2f} {s['sharpe']:+8.3f} {s['cum']:+7.1f}")

# pick config: stop 2% / target 4% (asymmetric, lets reversion run, caps tail)
STOP,TGT=0.02,0.04
rets,bars,hit=simulate(STOP,TGT)
sig=sig.assign(net=rets,bars=bars,exit=hit)
print(f"\n--- chosen config: stop {STOP*100:.0f}% / target {TGT*100:.0f}% / max {MAXH}h ---")
print(f"  exit breakdown: {pd.Series(hit).value_counts().to_dict()}   avg hold: {bars.mean():.1f}h")
print(f"  vs NO-STOP baseline worst trade was -42.6%; now worst={rets.min()*100:+.1f}%")

# ---- portfolio equity: 1 unit/day equal-split across that day's stacked signals ----
sig['day']=pd.to_datetime(sig['t'],unit='ms').dt.floor('D')
daily=sig.groupby('day')['net'].mean()
alldays=pd.date_range(daily.index.min(),daily.index.max(),freq='D')
daily=daily.reindex(alldays,fill_value=0.0)
eq=daily.cumsum()
sharpe=daily.mean()/daily.std()*np.sqrt(365)
maxdd=(eq-eq.cummax()).min()
print(f"\n  PORTFOLIO (1 unit/day, {len(daily)} days, avg {sig.groupby('day').size().mean():.1f} sig/day):")
print(f"    cum return: {eq.iloc[-1]*100:+.1f}%   ann: {daily.mean()*365*100:+.0f}%   ann vol: {daily.std()*np.sqrt(365)*100:.0f}%   Sharpe: {sharpe:.2f}")
print(f"    max drawdown: {maxdd*100:.1f}%   positive days: {(daily>0).mean()*100:.0f}%   worst day: {daily.min()*100:+.2f}%")
pd.DataFrame({'day':eq.index,'equity_cum':eq.values,'daily_ret':daily.values}).to_csv('stacked_equity.csv',index=False)
print("\n  monthly net (chosen config):")
for m,s in sig.set_index('day').groupby(pd.Grouper(freq='MS')):
    if len(s)<10: continue
    print(f"    {m.strftime('%Y-%m')}  n={len(s):4d}  net={s['net'].mean()*1e4:+6.1f}bps  win={(s['net']>0).mean()*100:4.1f}%")
