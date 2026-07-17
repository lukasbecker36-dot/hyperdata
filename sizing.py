import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_1h_history.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
COST=0.0011; MAXH=8
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())
store={}; tmp=[]
for sym,g in df.groupby('symbol'):
    if len(g)<600: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(24).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(24).median()
    ph=g['high'].shift(1).rolling(24).max(); pl=g['low'].shift(1).rolling(24).min()
    g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0))
    g['tier']=tier(uni.get(sym,0))
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3*3600*1000).reset_index(drop=True)
    store[sym]=(g['close'].values,g['high'].values,g['low'].values,g['open_time_ms'].values)
    tmp.append(g)
big=pd.concat(tmp); rv_thr=big['rv24'].quantile(0.60)
S=[]
for sym,g in big.groupby('symbol'):
    c,hi,lo,tm=store[sym]
    for i in range(len(g)):
        r=g.iloc[i]
        if np.isnan(r['vratio']) or r['vratio']<5 or r['brk']==0 or r['tier'] not in('HIGH','MID'): continue
        if np.isnan(r['rv24']) or r['rv24']<rv_thr or pd.isna(r['fund8']): continue
        if r['brk']*np.sign(r['fund8'])!=1 or i+MAXH>=len(c): continue
        S.append((sym,i,int(r['brk']),tm[i],r['rv24']))
sig=pd.DataFrame(S,columns=['sym','i','brk','t','rv24'])

def sim(stop):
    rets=[]
    for _,r in sig.iterrows():
        c,hi,lo,_=store[r['sym']]; i=r['i']; d=-r['brk']; e=c[i]; out=None
        if stop:
            for k in range(1,MAXH+1):
                if d==-1 and hi[i+k]>=e*(1+stop): out=-stop;break
                if d==1 and lo[i+k]<=e*(1-stop): out=-stop;break
                if k==MAXH: out=d*np.log(c[i+MAXH]/e)
        else: out=d*np.log(c[i+MAXH]/e)
        rets.append(out-COST)
    return np.array(rets)

print("STOP-WIDTH SWEEP (no target, 8h max hold) — does a wider stop recover the edge?")
print(f"  {'stop':>7s} {'net bps':>8s} {'win%':>5s} {'worst%':>7s} {'skew':>5s} {'PTsharpe':>8s}")
for stop in [None,0.10,0.08,0.05,0.03,0.02]:
    r=sim(stop); lbl='none' if not stop else f'{stop*100:.0f}%'
    print(f"  {lbl:>7s} {r.mean()*1e4:+8.1f} {(r>0).mean()*100:5.1f} {r.min()*100:+7.1f} {pd.Series(r).skew():+5.2f} {r.mean()/r.std():+8.3f}")

# ---- proper tail solution: volatility-scaled sizing, NO price stop, 8h hold ----
r=sim(None); sig=sig.assign(net=r)
# weight inversely to rv24, normalized so mean weight = 1 (constant-risk sizing)
w=1.0/sig['rv24']; w=w/w.mean(); w=w.clip(upper=4)     # cap leverage at 4x avg
sig['w']=w; sig['wret']=sig['net']*sig['w']
sig['day']=pd.to_datetime(sig['t'],unit='ms').dt.floor('D')

def equity(col,label):
    daily=sig.groupby('day').apply(lambda x:(x[col]).mean(),include_groups=False)
    days=pd.date_range(daily.index.min(),daily.index.max(),freq='D'); daily=daily.reindex(days,fill_value=0.0)
    eq=daily.cumsum(); sh=daily.mean()/daily.std()*np.sqrt(365); dd=(eq-eq.cummax()).min()
    print(f"  {label:22s} cum={eq.iloc[-1]*100:+6.1f}%  ann={daily.mean()*365*100:+5.0f}%  vol={daily.std()*np.sqrt(365)*100:4.0f}%  Sharpe={sh:4.2f}  maxDD={dd*100:6.1f}%  worstday={daily.min()*100:+.2f}%")
    return eq,daily

print(f"\nPORTFOLIO (no stop, 8h hold, 1 unit/day equal-split), {sig['day'].nunique()} active days:")
eq_eq,d_eq=equity('net','equal-weight')
eq_vs,d_vs=equity('wret','vol-scaled size')

# worst individual trades under each
print(f"\n  tail comparison (per-trade, portfolio-weighted contribution):")
print(f"    equal-weight  worst trade net = {sig['net'].min()*100:+.1f}%")
print(f"    vol-scaled    worst trade contrib = {sig['wret'].min()*100:+.1f}%  (position was {sig.loc[sig['wret'].idxmin(),'w']:.2f}x)")
pd.DataFrame({'day':eq_vs.index,'equal_wt':eq_eq.values,'vol_scaled':eq_vs.values}).to_csv('stacked_equity.csv',index=False)
print("\n  monthly (vol-scaled):")
for m,s in sig.set_index('day').groupby(pd.Grouper(freq='MS')):
    if len(s)<10: continue
    print(f"    {m.strftime('%Y-%m')}  n={len(s):4d}  net(size-wt)={s['wret'].mean()*1e4:+6.1f}bps  raw={s['net'].mean()*1e4:+6.1f}bps")
