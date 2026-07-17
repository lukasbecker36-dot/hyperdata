import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VW=RW=96; HOLD=32; NOTIONAL=100; LEV=2; MARGIN=NOTIONAL/LEV
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())
frames=[]
for sym,g in df.groupby('symbol'):
    if len(g)<1500: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(VW).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VW).median()
    ph=g['high'].shift(1).rolling(RW).max(); pl=g['low'].shift(1).rolling(RW).min()
    g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0)); g['tier']=tier(uni.get(sym,0))
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3600*1000)
    frames.append(g)
d=pd.concat(frames).replace([np.inf,-np.inf],np.nan)
thr=d[(d['vratio']>=5)&(d['brk']!=0)&(d['tier'].isin(['HIGH','MID']))].dropna(subset=['rv24'])['rv24'].quantile(0.60)
sig=d[(d['vratio']>=5)&(d['brk']!=0)&(d['tier'].isin(['HIGH','MID']))].dropna(subset=['rv24','fund8']).copy()
sig=sig[(sig['rv24']>=thr)&(sig['brk']*np.sign(sig['fund8'])==1)].sort_values('open_time_ms')

pivot=d.pivot_table(index='open_time_ms',columns='symbol',values='close')
T=pivot.index.values; idx={t:i for i,t in enumerate(T)}; nT=len(T)

# dedup: one open position per coin
open_until={}; pos=[]
for _,r in sig.iterrows():
    t=r['open_time_ms']; s=r['symbol']
    if s in open_until and t<open_until[s]: continue
    i0=idx[t]; i1=min(i0+HOLD,nT-1); open_until[s]=T[i1]
    pos.append((s,i0,i1,-int(r['brk']),r['close']))   # dir=-brk (fade)

unreal=np.zeros(nT); count=np.zeros(nT)
for s,i0,i1,dr,epx in pos:
    px=pivot[s].values[i0:i1]
    pnl=NOTIONAL*dr*(px/epx-1.0)
    unreal[i0:i1]+=np.nan_to_num(pnl); count[i0:i1]+=1
required=count*MARGIN+np.maximum(0,-unreal)   # cross-margin: initial margin locked + absorb unrealized loss
kbad=int(np.argmax(required))
print(f"DEDUPED book, ${NOTIONAL} notional, {LEV}x lev (${MARGIN:.0f} margin/pos), 8h hold, {len(pos)} positions\n")
print(f"peak concurrent positions           : {int(count.max())}")
print(f"peak INITIAL margin locked          : ${int(count.max())*MARGIN:,.0f}  ({int(count.max())} x ${MARGIN:.0f})")
print(f"worst simultaneous unrealized loss  : ${-unreal.min():,.0f}  (open book marked-to-market)")
print(f"PEAK CAPITAL REQUIRED (margin+MtM)   : ${required.max():,.0f}")
print(f"   at that moment: {int(count[kbad])} open positions, ${-unreal[kbad]:,.0f} unrealized loss")
print(f"\npercentiles of required capital over time: 95th=${np.percentile(required,95):,.0f}  99th=${np.percentile(required,99):,.0f}  max=${required.max():,.0f}")
# how often is required capital near the peak
for lvl in [1000,1500,2000,2500]:
    print(f"   % of time required capital > ${lvl}: {(required>lvl).mean()*100:.1f}%")
