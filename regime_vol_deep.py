import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_1h_history.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
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
d['dt']=pd.to_datetime(d['open_time_ms'],unit='ms')
sig=d[(d['vratio']>=5)&(d['brk']!=0)&(d['tier'].isin(['HIGH','MID']))].dropna(subset=['fade','rv24']).copy()

# global vol quintiles (cross-sectional over all symbols/times)
sig['vq']=pd.qcut(sig['rv24'],5,labels=['Q1lo','Q2','Q3','Q4','Q5hi'])
print("=== fade net edge by SYMBOL realized-vol quintile (all 8 months) ===")
for q in sig['vq'].cat.categories:
    s=sig[sig['vq']==q]['fade']
    print(f"   {q:6s} n={len(s):5d}  net={ (s.mean()-COST)*1e4:+6.1f}bps  win={(s>0).mean()*100:4.1f}%")

# CRITICAL: monthly edge within top-2 vol quintiles (high-vol subset)
hv=sig[sig['vq'].isin(['Q4','Q5hi'])]
print(f"\n=== HIGH-VOL subset (top-40% rv24): monthly net edge  [n={len(hv)}] ===")
print("   is the edge persistent across months, or still only Jun-Jul?")
for m,s in hv.set_index('dt').groupby(pd.Grouper(freq='MS')):
    if len(s)<20: continue
    print(f"   {m.strftime('%Y-%m')}  n={len(s):4d}  net={ (s['fade'].mean()-COST)*1e4:+6.1f}bps  win={(s['fade']>0).mean()*100:4.1f}%")

# split: pre-June (Dec-May) vs Jun-Jul, within high-vol
hv=hv.copy(); hv['era']=np.where(hv['dt']<'2026-06-01','Dec-May','Jun-Jul')
print("\n=== high-vol subset: pre-June vs Jun-Jul ===")
for e,s in hv.groupby('era'):
    print(f"   {e:8s} n={len(s):5d}  net={ (s['fade'].mean()-COST)*1e4:+6.1f}bps  win={(s['fade']>0).mean()*100:4.1f}%")
print("\n=== same split for LOW/MID vol (bottom 60%) ===")
lv=sig[sig['vq'].isin(['Q1lo','Q2','Q3'])].copy(); lv['era']=np.where(lv['dt']<'2026-06-01','Dec-May','Jun-Jul')
for e,s in lv.groupby('era'):
    print(f"   {e:8s} n={len(s):5d}  net={ (s['fade'].mean()-COST)*1e4:+6.1f}bps  win={(s['fade']>0).mean()*100:4.1f}%")
