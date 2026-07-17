import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VW=RW=96; HOLD=32; COST=0.0011; NOTIONAL=100.0
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())

frames=[]
for sym,g in df.groupby('symbol'):
    if len(g)<1500: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(VW).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VW).median()
    ph=g['high'].shift(1).rolling(RW).max(); pl=g['low'].shift(1).rolling(RW).min()
    g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0))
    g['fade']=-g['brk']*np.log(c.shift(-HOLD)/c)      # 8h hold
    g['tier']=tier(uni.get(sym,0))
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3600*1000)
    frames.append(g)
d=pd.concat(frames).replace([np.inf,-np.inf],np.nan)
d['dt']=pd.to_datetime(d['open_time_ms'],unit='ms')

sig=d[(d['vratio']>=5)&(d['brk']!=0)&(d['tier'].isin(['HIGH','MID']))].dropna(subset=['rv24','fund8','fade']).copy()
sig['crowd']=(sig['brk']*np.sign(sig['fund8'])==1).astype(int)

# past-month window
tmax=d['dt'].max(); mstart=tmax-pd.Timedelta(days=30)
# vol threshold from data BEFORE the month (no lookahead)
train=sig[sig['dt']<mstart]
rv_thr=train['rv24'].quantile(0.60)
print(f"window: {mstart.date()} -> {tmax.date()}  | vol threshold (60th pct, from pre-month data) = {rv_thr:.4f}\n")

month=sig[(sig['dt']>=mstart)&(sig['dt']<=tmax)].copy()
stk=month[(month['rv24']>=rv_thr)&(month['crowd']==1)].copy()
ndays=(tmax-mstart).days

def summarize(s,label):
    gross=NOTIONAL*s['fade'].sum(); net=NOTIONAL*(s['fade']-COST).sum()
    print(f"{label}")
    print(f"  trades: {len(s)}   trades/day: {len(s)/ndays:.1f}")
    print(f"  P&L @ {int(NOTIONAL)} notional/trade:  gross={gross:+.2f}   net(after 11bps rt)={net:+.2f}")
    if len(s):
        print(f"  avg/trade: gross={NOTIONAL*s['fade'].mean():+.3f}  net={NOTIONAL*(s['fade'].mean()-COST):+.3f}   win={(s['fade']>0).mean()*100:.1f}%")
        print(f"  best/worst trade P&L: {NOTIONAL*s['fade'].max():+.2f} / {NOTIONAL*s['fade'].min():+.2f}")
    print()

print("=== PAST MONTH — STACKED STRATEGY (high-vol + crowd-aligned, 8h hold) ===\n")
summarize(stk,"STACKED (what we'd actually trade):")
summarize(month,"[reference] RAW HIGH+MID spike-breakouts (no vol/funding filter):")

# concurrency: max simultaneous open positions (each held 8h=32 bars)
stk=stk.sort_values('open_time_ms')
ev=[]
for _,r in stk.iterrows():
    ev.append((r['open_time_ms'],1)); ev.append((r['open_time_ms']+HOLD*15*60*1000,-1))
ev.sort(); cur=mx=0
for _,x in ev: cur+=x; mx=max(mx,cur)
print(f"concurrency: up to {mx} positions open at once -> peak notional deployed ≈ {mx*int(NOTIONAL)}")

# per-day breakdown (stacked)
print("\nper-day stacked trades & net P&L:")
stk['day']=stk['dt'].dt.floor('D')
for day,s in stk.groupby('day'):
    print(f"  {day.date()}  n={len(s):2d}  netP&L={NOTIONAL*(s['fade']-COST).sum():+7.2f}")
