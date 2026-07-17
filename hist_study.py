import pandas as pd, numpy as np

df=pd.read_csv('hyperliquid_1h_history.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VOLWIN=RANGEWIN=24   # 24h on 1h bars
MINBARS=600
COST=0.0011

frames=[]
for sym,g in df.groupby('symbol'):
    if len(g)<MINBARS: continue
    g=g.copy()
    g['ret']=np.log(g['close']/g['close'].shift(1))
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VOLWIN).median()
    ph=g['high'].shift(1).rolling(RANGEWIN).max(); pl=g['low'].shift(1).rolling(RANGEWIN).min()
    g['brk']=np.where(g['close']>ph,1,np.where(g['close']<pl,-1,0))
    for h in [1,2,4]: g[f'f{h}']=np.log(g['close'].shift(-h)/g['close'])
    g['tier']=tier(uni.get(sym,0))
    frames.append(g)
d=pd.concat(frames).replace([np.inf,-np.inf],np.nan).dropna(subset=['vratio','ret'])
d['dt']=pd.to_datetime(d['open_time_ms'],unit='ms')
print(f"symbols={d['symbol'].nunique()} rows={len(d)}  span {d['dt'].min().date()} -> {d['dt'].max().date()}\n")

# overall context check (1h version), fade hold 2h
sp=d[(d['vratio']>=5)&(d['brk']!=0)].copy(); sp['fade']=-sp['brk']*sp['f2']
sp=sp.dropna(subset=['fade'])
print("1h-signal fade (5x spike+breakout), hold 2h, HIGH+MID:")
hm=sp[sp.tier.isin(['HIGH','MID'])]
print(f"  n={len(hm)} gross={hm['fade'].mean()*1e4:+.1f}bps net@11={ (hm['fade'].mean()-COST)*1e4:+.1f}bps win={(hm['fade']>0).mean()*100:.1f}%\n")

print("=== MONTHLY fade edge (HIGH+MID, 5x spike+breakout, hold 2h) ===")
print(f"  {'month':>8s} {'n':>5s} {'gross bps':>9s} {'net@11':>7s} {'win%':>6s}")
for m,s in hm.set_index('dt').groupby(pd.Grouper(freq='MS')):
    if len(s)<20: continue
    f=s['fade']
    print(f"  {m.strftime('%Y-%m'):>8s} {len(f):5d} {f.mean()*1e4:+9.1f} {(f.mean()-COST)*1e4:+7.1f} {(f>0).mean()*100:6.1f}")

print("\n=== same, ALL tiers (incl. LOW) for breadth ===")
allt=sp
for m,s in allt.set_index('dt').groupby(pd.Grouper(freq='MS')):
    if len(s)<20: continue
    f=s['fade']
    print(f"  {m.strftime('%Y-%m'):>8s} {len(f):5d} gross={f.mean()*1e4:+7.1f}bps net={ (f.mean()-COST)*1e4:+7.1f} win={(f>0).mean()*100:5.1f}%")
