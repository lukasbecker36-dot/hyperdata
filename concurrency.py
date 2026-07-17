import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VW=RW=96; HOLD_MS=32*15*60*1000        # 8h hold
NOTIONAL=100; LEV=2; MARGIN=NOTIONAL/LEV
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
sig=sig[(sig['rv24']>=thr)&(sig['brk']*np.sign(sig['fund8'])==1)].copy()
sig=sig.sort_values('open_time_ms')
span_days=(sig['open_time_ms'].max()-sig['open_time_ms'].min())/86400000
print(f"stacked signals: {len(sig)} over {span_days:.0f} days  (~{len(sig)/span_days:.1f}/day)\n")

def concurrency(entries):
    ev=[]
    for t in entries: ev.append((t,1)); ev.append((t+HOLD_MS,-1))
    ev.sort()
    cur=0; series=[]
    for _,x in ev:
        cur+=x; series.append(cur)
    return np.array(series)

# raw: every signal is a position
raw=concurrency(sig['open_time_ms'].values)
# dedup: at most one open position per coin at a time
open_until={}; kept=[]
for _,r in sig.iterrows():
    t=r['open_time_ms']
    if r['symbol'] in open_until and t<open_until[r['symbol']]: continue
    open_until[r['symbol']]=t+HOLD_MS; kept.append(t)
ded=concurrency(np.array(kept))

def report(series,label,n):
    p=lambda q:int(np.percentile(series,q))
    print(f"{label}  ({n} positions taken)")
    print(f"   concurrent positions: mean={series.mean():.1f}  median={int(np.median(series))}  95th={p(95)}  99th={p(99)}  MAX={series.max()}")
    for tag,val in [('95th',p(95)),('99th',p(99)),('MAX',series.max())]:
        print(f"   margin @ {tag:>4s} concurrency: {val} x ${MARGIN:.0f} = ${val*MARGIN:,.0f}   (notional ${val*NOTIONAL:,.0f})")
    print()

print(f"assumptions: ${NOTIONAL} notional/trade, {LEV}x leverage -> ${MARGIN:.0f} initial margin/position, 8h hold\n")
report(raw,"RAW (take every signal, even multiple per coin):",len(sig))
report(ded,"DEDUPED (max 1 open position per coin):",len(kept))
