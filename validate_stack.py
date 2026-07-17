import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_1h_history.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
HOLD=2
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())

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
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3*3600*1000)
    frames.append(g)
d=pd.concat(frames).replace([np.inf,-np.inf],np.nan)
d['dt']=pd.to_datetime(d['open_time_ms'],unit='ms')
sig=d[(d['vratio']>=5)&(d['brk']!=0)&(d['tier'].isin(['HIGH','MID']))].dropna(subset=['fade','rv24','fund8']).copy()
sig['vq']=pd.qcut(sig['rv24'],5,labels=[1,2,3,4,5]).astype(int)
sig['crowd']=sig['brk']*np.sign(sig['fund8'])

def report(s,cost=0.0011):
    f=s['fade']; net=f.mean()-cost; t=f.mean()/(f.std()/np.sqrt(len(f)))
    return f"n={len(s):5d} gross={f.mean()*1e4:+6.1f} net@11={net*1e4:+6.1f}bps win={(f>0).mean()*100:4.1f}% t={t:+5.2f}"

stk=sig[(sig['vq']>=4)&(sig['crowd']==1)]
print("STACKED FILTER = high-vol (top40% rv24) + breakout WITH crowded funding\n")
print("Overall:", report(stk))
print(f"Selectivity: {len(stk)}/{len(sig)} = {len(stk)/len(sig)*100:.0f}% of raw signals\n")

print("Monthly (the stability test):")
for m,s in stk.set_index('dt').groupby(pd.Grouper(freq='MS')):
    if len(s)<15: continue
    print(f"  {m.strftime('%Y-%m')}  {report(s)}")

print("\nCost sensitivity (bps net per trade):")
for c in [5,8,11,15,20]:
    print(f"  @{c:2d}bps: {(stk['fade'].mean()-c/1e4)*1e4:+.1f}")

print("\nAblation — remove one filter at a time:")
print("  vol only (top40%, any funding dir):     ", report(sig[sig['vq']>=4]))
print("  funding only (crowd=+1, any vol):        ", report(sig[sig['crowd']==1]))
print("  neither (raw HIGH+MID):                  ", report(sig))
print("  vol top20% + crowd:                      ", report(sig[(sig['vq']==5)&(sig['crowd']==1)]))

print("\nPer-tier within stacked filter:")
for tl in ['HIGH','MID']:
    print(f"  {tl}: {report(stk[stk.tier==tl])}")

# quarterly-ish equity in trade order
stk2=stk.sort_values('open_time_ms').copy()
stk2['cum']=(stk2['fade']-0.0011).cumsum()
print(f"\nCumulative net (sum of per-trade returns, 11bps): {stk2['cum'].iloc[-1]*100:+.1f}% over {len(stk2)} trades, 8 months")
print(f"  worst single trade: {stk2['fade'].min()*100:+.1f}%   best: {stk2['fade'].max()*100:+.1f}%")
stk2[['dt','symbol','brk','rv24','fund8','fade','cum']].to_csv('stacked_trades.csv',index=False)
