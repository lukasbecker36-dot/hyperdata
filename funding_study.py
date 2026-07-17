import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_1h_history.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').sort_values(['symbol','time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
COST=0.0011; HOLD=2

frames=[]
for sym,g in df.groupby('symbol'):
    if len(g)<600: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(24).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(24).median()
    ph=g['high'].shift(1).rolling(24).max(); pl=g['low'].shift(1).rolling(24).min()
    g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0))
    g['fade']=-g['brk']*np.log(c.shift(-HOLD)/c)
    g['tier']=tier(uni.get(sym,0))
    frames.append(g)
d=pd.concat(frames).replace([np.inf,-np.inf],np.nan)

# merge funding (hourly) by nearest past timestamp per symbol
d=d.sort_values(['symbol','open_time_ms'])
fund=fund.rename(columns={'time_ms':'open_time_ms'})
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())
parts=[]
for sym,g in d.groupby('symbol'):
    fg=fund[fund['symbol']==sym]
    if len(fg)==0: g['funding_rate']=np.nan; g['fund8']=np.nan; parts.append(g); continue
    m=pd.merge_asof(g.sort_values('open_time_ms'),fg[['open_time_ms','funding_rate','fund8']].sort_values('open_time_ms'),
                    on='open_time_ms',direction='backward',tolerance=3*3600*1000)
    parts.append(m)
d=pd.concat(parts)
d['dt']=pd.to_datetime(d['open_time_ms'],unit='ms')

sig=d[(d['vratio']>=5)&(d['brk']!=0)&(d['tier'].isin(['HIGH','MID']))].dropna(subset=['fade','fund8']).copy()
print(f"signals w/ funding: {len(sig)}   baseline net={ (sig['fade'].mean()-COST)*1e4:+.1f}bps\n")

# funding features
sig['abs_fund']=sig['fund8'].abs()
sig['crowd']=sig['brk']*np.sign(sig['fund8'])   # +1: breakout in direction of funding pressure (crowded)

print("=== by |funding| (8h avg) terciles ===")
sig['fb']=pd.qcut(sig['abs_fund'],3,labels=['LOW','MID','HIGH'])
for b in sig['fb'].cat.categories:
    s=sig[sig['fb']==b]['fade']
    print(f"   |fund| {b:5s} n={len(s):5d}  net={ (s.mean()-COST)*1e4:+6.1f}bps  win={(s>0).mean()*100:4.1f}%")

print("\n=== breakout vs funding-pressure alignment ===")
for cval,lbl in [(1,'breakout WITH crowd (up-brk & +funding / down-brk & -funding)'),
                 (-1,'breakout AGAINST crowd')]:
    s=sig[sig['crowd']==cval]['fade']
    print(f"   {lbl:56s} n={len(s):5d} net={ (s.mean()-COST)*1e4:+6.1f}bps win={(s>0).mean()*100:4.1f}%")

print("\n=== 2-way: crowded-direction breakout AND high |funding| ===")
hf=sig['abs_fund']>sig['abs_fund'].median()
for cval in [1,-1]:
    s=sig[(sig['crowd']==cval)&hf]['fade']
    lbl='WITH crowd' if cval==1 else 'AGAINST crowd'
    print(f"   high|fund| & {lbl:12s} n={len(s):5d}  net={ (s.mean()-COST)*1e4:+6.1f}bps  win={(s>0).mean()*100:4.1f}%")

# combine with high-vol filter (top 40% rv24) + crowded
print("\n=== STACKED filter: high-vol (top40% rv24) + crowded-direction breakout ===")
sig['vq']=pd.qcut(sig['rv24'],5,labels=[1,2,3,4,5]).astype(int)
stk=sig[(sig['vq']>=4)&(sig['crowd']==1)]
base=sig
print(f"   stacked   n={len(stk):5d}  net={ (stk['fade'].mean()-COST)*1e4:+6.1f}bps  win={(stk['fade']>0).mean()*100:4.1f}%")
stk2=stk.copy(); stk2['era']=np.where(pd.to_datetime(stk2['dt'])<'2026-06-01','Dec-May','Jun-Jul')
for e,s in stk2.groupby('era'):
    print(f"      {e:8s} n={len(s):4d}  net={ (s['fade'].mean()-COST)*1e4:+6.1f}bps  win={(s['fade']>0).mean()*100:4.1f}%")
