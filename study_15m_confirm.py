import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VW=RW=96          # 24h windows on 15m
COST=0.0011
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())

frames=[]
for sym,g in df.groupby('symbol'):
    if len(g)<1500: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(VW).std()
    g['rv_wk']=g['ret'].rolling(VW*7).std()                    # ~7d realized vol
    g['compress']=g['rv24']/g['rv_wk']                         # <1 = recently quiet / coiling
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VW).median()
    ph=g['high'].shift(1).rolling(RW).max(); pl=g['low'].shift(1).rolling(RW).min()
    g['rangew']=(ph-pl)/c                                      # prior 24h range width (dimensionless)
    g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0))
    for h in [1,2,4,8,16,32]: g[f'f{h}']=np.log(c.shift(-h)/c)  # 15m..8h
    g['tier']=tier(uni.get(sym,0))
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3600*1000)
    frames.append(g)
d=pd.concat(frames).replace([np.inf,-np.inf],np.nan)
d['dt']=pd.to_datetime(d['open_time_ms'],unit='ms')

base=d[(d['vratio']>=5)&(d['brk']!=0)&(d['tier'].isin(['HIGH','MID']))].dropna(subset=['rv24','fund8']).copy()
base['crowd']=base['brk']*np.sign(base['fund8'])
base['vq']=pd.qcut(base['rv24'],5,labels=[1,2,3,4,5]).astype(int)
print(f"15m signals (HIGH+MID, 5x spike+breakout): {len(base)}\n")

def line(s,h,cost=COST):
    f=(s[f'f{h}']* -s['brk']).dropna()   # FADE return (>0 good for fade)
    t=f.mean()/(f.std()/np.sqrt(len(f))) if len(f)>1 else 0
    return f"n={len(f):5d} net={ (f.mean()-cost)*1e4:+6.1f}bps win={(f>0).mean()*100:4.1f}% t={t:+5.2f}"

print("=== PART 1: CONFIRM stacked FADE filter on 15m (vol top40% + crowd-aligned) ===")
stk=base[(base['vq']>=4)&(base['crowd']==1)]
print("  hold  |  raw HIGH+MID              |  STACKED FILTER")
for h,lbl in [(2,'30m'),(4,'1h'),(8,'2h'),(16,'4h'),(32,'8h')]:
    print(f"  {lbl:4s}  | {line(base,h)} | {line(stk,h)}")
print(f"\n  stacked selectivity: {len(stk)}/{len(base)} = {len(stk)/len(base)*100:.0f}%")
# stability across the 52d (halves)
mid=base['dt'].min()+(base['dt'].max()-base['dt'].min())/2
print("  stacked filter, 4h fade, by half:")
for lbl,sub in [('first ~26d',stk[stk.dt<mid]),('second ~26d',stk[stk.dt>=mid])]:
    print(f"    {lbl:11s} {line(sub,16)}")

print("\n=== PART 2: RANGE TIGHTNESS — do breakouts from tight/dead ranges CONTINUE? ===")
print("  signed by breakout direction: net>0 = MOMENTUM (continuation), net<0 = reversion (fade works)")
def mom_line(s,h,cost=COST):
    m=(s['brk']*s[f'f{h}']).dropna()   # MOMENTUM return (breakout direction)
    if len(m)<20: return f"n={len(m):5d} (few)"
    t=m.mean()/(m.std()/np.sqrt(len(m)))
    return f"n={len(m):5d} mom_net={ (m.mean()-cost)*1e4:+6.1f}bps win={(m>0).mean()*100:4.1f}% t={t:+5.2f}"

for var,lbl in [('rangew','prior 24h RANGE WIDTH'),('compress','COMPRESSION (rv24/rv_week, <1=coiled)')]:
    sub=base.dropna(subset=[var]).copy()
    sub['q']=pd.qcut(sub[var],3,labels=['TIGHT/LOW','MID','WIDE/HIGH'])
    print(f"\n  by {lbl}:  (2h & 4h horizons)")
    for b in sub['q'].cat.categories:
        s=sub[sub['q']==b]
        print(f"    {b:10s} 2h: {mom_line(s,8)}")
        print(f"    {'':10s} 4h: {mom_line(s,16)}")

print("\n=== PART 3: 'DEAD COIN SPRINGS TO LIFE' — low prior vol + tight range + volume spike breakout ===")
sub=base.dropna(subset=['rangew','compress']).copy()
tight=sub['rangew']<sub['rangew'].quantile(0.33)
coiled=sub['compress']<sub['compress'].quantile(0.33)
dead=sub[tight&coiled]
print(f"  dead-then-spike breakouts (tight range AND coiled): n={len(dead)}")
for h,lbl in [(4,'1h'),(8,'2h'),(16,'4h'),(32,'8h')]:
    print(f"    {lbl:3s} MOMENTUM: {mom_line(dead,h)}")
print("  (compare: all signals momentum 4h:) ", mom_line(sub,16))
# stack it: dead+spike momentum on HIGH liquidity only (cleaner execution)
print("  dead+spike, HIGH-liq only, 4h momentum:", mom_line(dead[dead.tier=='HIGH'],16))
