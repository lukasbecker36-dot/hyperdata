import pandas as pd, numpy as np

df=pd.read_csv('hyperliquid_1h_history.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VOLWIN=RANGEWIN=24; MINBARS=600; COST=0.0011; HOLD=2

frames=[]
for sym,g in df.groupby('symbol'):
    if len(g)<MINBARS: continue
    g=g.copy()
    c=g['close']
    g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(24).std()                      # realized vol (trailing 24h)
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VOLWIN).median()
    ph=g['high'].shift(1).rolling(RANGEWIN).max(); pl=g['low'].shift(1).rolling(RANGEWIN).min()
    g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0))
    ef=c.ewm(span=10).mean(); es=c.ewm(span=40).mean()
    g['trend']=np.sign(ef-es)                                  # +1 up-trend, -1 down-trend
    num=(c-c.shift(24)).abs(); den=c.diff().abs().rolling(24).sum()
    g['er']=num/den                                            # efficiency ratio: ~1 trending, ~0 choppy
    g['fade']=-g['brk']*np.log(c.shift(-HOLD)/c)               # fade return, 2h hold
    g['align']=g['brk']*g['trend']                            # +1 breakout WITH trend, -1 AGAINST
    g['tier']=tier(uni.get(sym,0))
    frames.append(g)
d=pd.concat(frames).replace([np.inf,-np.inf],np.nan)
d['dt']=pd.to_datetime(d['open_time_ms'],unit='ms')

# market-wide vol index = cross-sectional median rv24 per timestamp
mkt=d.groupby('open_time_ms')['rv24'].median().rename('mkt_vol')
d=d.join(mkt,on='open_time_ms')

sig=d[(d['vratio']>=5)&(d['brk']!=0)&(d['tier'].isin(['HIGH','MID']))].dropna(subset=['fade','rv24','er','mkt_vol']).copy()
print(f"signals (1h, HIGH+MID, 5x spike+breakout, 2h fade hold): {len(sig)}")
print(f"baseline: net={ (sig['fade'].mean()-COST)*1e4:+.1f}bps  win={(sig['fade']>0).mean()*100:.1f}%\n")

def buckets(col,label,q=3):
    print(f"--- by {label} ({'terciles' if q==3 else 'quantiles'}) ---")
    lab=['LOW','MID','HIGH'] if q==3 else [f'Q{i+1}' for i in range(q)]
    sig['_b']=pd.qcut(sig[col],q,labels=lab,duplicates='drop')
    for b in sig['_b'].cat.categories:
        s=sig[sig['_b']==b]['fade']
        print(f"   {b:5s} n={len(s):5d}  net={ (s.mean()-COST)*1e4:+6.1f}bps  win={(s>0).mean()*100:4.1f}%")
    print()

buckets('rv24','SYMBOL realized vol')
buckets('mkt_vol','MARKET vol index')
buckets('er','efficiency ratio (trend/chop)')

print("--- by breakout vs EMA trend alignment ---")
for a,lbl in [(-1,'AGAINST trend (counter-trend breakout)'),(0,'neutral'),(1,'WITH trend (trend-continuation breakout)')]:
    s=sig[sig['align']==a]['fade']
    if len(s)<20: continue
    print(f"   {lbl:42s} n={len(s):5d}  net={ (s.mean()-COST)*1e4:+6.1f}bps  win={(s>0).mean()*100:4.1f}%")

# 2-way: does a filter recover a persistent edge? choppy (low ER) AND against-trend
print("\n--- combined filter: choppy (ER < median) AND counter-trend (align<=0) ---")
med_er=sig['er'].median()
filt=sig[(sig['er']<med_er)&(sig['align']<=0)]
rest=sig[~((sig['er']<med_er)&(sig['align']<=0))]
print(f"   FILTERED IN : n={len(filt):5d}  net={ (filt['fade'].mean()-COST)*1e4:+6.1f}bps  win={(filt['fade']>0).mean()*100:4.1f}%")
print(f"   filtered out: n={len(rest):5d}  net={ (rest['fade'].mean()-COST)*1e4:+6.1f}bps  win={(rest['fade']>0).mean()*100:4.1f}%")

# does any regime var explain the Jun-Jul switch? monthly regime averages + monthly edge
print("\n--- monthly: regime averages vs fade edge (does a variable flag Jun-Jul?) ---")
print(f"  {'month':>7s} {'net bps':>8s} {'mktVol':>7s} {'ER':>6s} {'%againstTrend':>13s}")
for m,s in sig.set_index('dt').groupby(pd.Grouper(freq='MS')):
    if len(s)<20: continue
    print(f"  {m.strftime('%Y-%m'):>7s} {(s['fade'].mean()-COST)*1e4:+8.1f} {s['mkt_vol'].mean()*100:6.2f}% {s['er'].mean():6.2f} {(s['align']<=0).mean()*100:12.1f}%")
