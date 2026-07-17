import pandas as pd, numpy as np

df = pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
uni = pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
VOLWIN, RANGEWIN = 96, 96
HZ=[1,2,4,8,16]; COST=0.0005
MINBARS=1500   # require a reasonable history so rolling windows are valid

frames=[]
for sym,g in df.groupby('symbol'):
    if len(g)<MINBARS: continue
    g=g.copy()
    g['ret']=np.log(g['close']/g['close'].shift(1))
    g['dir']=np.sign(g['ret'])
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VOLWIN).median()
    ph=g['high'].shift(1).rolling(RANGEWIN).max(); pl=g['low'].shift(1).rolling(RANGEWIN).min()
    g['brk']=np.where(g['close']>ph,1,np.where(g['close']<pl,-1,0))
    for h in HZ: g[f'f{h}']=np.log(g['close'].shift(-h)/g['close'])
    frames.append(g)
d=pd.concat(frames).replace([np.inf,-np.inf],np.nan).dropna(subset=['vratio','ret'])
nsym=d['symbol'].nunique()
print(f"Symbols analysed: {nsym}   rows: {len(d)}\n")

def block(sub, signcol, label):
    print(f"  {label}  (n={len(sub)})")
    for h in HZ:
        s=(sub[signcol]*sub[f'f{h}']).dropna()
        if len(s)<20: print(f"    +{h*15:3d}m: (too few)"); continue
        m=s.mean(); t=m/(s.std()/np.sqrt(len(s)))
        print(f"    +{h*15:3d}m: mean={m*100:+6.3f}%  t={t:+6.2f}  hit={(s>0).mean()*100:4.1f}%")

for THR in [3,5]:
    print(f"{'='*66}\nSPIKE >= {THR}x trailing-24h median volume  (ALL {nsym} perps pooled)\n{'='*66}")
    sp=d[d['vratio']>=THR]
    a=sp[sp['brk']!=0].copy(); a['sig']=a['brk']
    block(a,'sig',"[A] SPIKE + BREAKOUT  (signed by breakout dir; <0 = reverts)")
    b=d[(d['vratio']<THR)&(d['brk']!=0)].copy(); b['sig']=b['brk']
    block(b,'sig',"[B] BREAKOUT, normal volume  (control)")
    c=sp[sp['brk']==0].copy(); c['sig']=c['dir']
    block(c,'sig',"[C] SPIKE inside range  (signed by candle dir)")
    print()

# Liquidity tiers by 24h notional volume
print(f"{'='*66}\nFADE the 5x-volume breakout, hold 1h -- by LIQUIDITY TIER\n{'='*66}")
d['vol24']=d['symbol'].map(uni)
sp=d[(d['vratio']>=5)&(d['brk']!=0)].dropna(subset=['f4','vol24']).copy()
sp['fade']=-sp['brk']*sp['f4']
qs=sp['vol24'].quantile([1/3,2/3]).values
def tier(v): return 'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
sp['tier']=sp['vol24'].apply(tier)
for tl in ['HIGH','MID','LOW','ALL']:
    s=sp['fade'] if tl=='ALL' else sp[sp['tier']==tl]['fade']
    t=s.mean()/(s.std()/np.sqrt(len(s)))
    print(f"  {tl:4s} n={len(s):5d}  mean={s.mean()*100:+6.3f}%  median={s.median()*100:+6.3f}%  win={(s>0).mean()*100:4.1f}%  t={t:+5.2f}  net5bps={(s.mean()-COST)*100:+6.3f}%")

# consistency across symbols: fraction of symbols with negative momentum (i.e. fade works)
print(f"\nCross-symbol consistency (5x spike+breakout, 1h):")
res=[]
for sym,g in sp.groupby('symbol'):
    if len(g)<10: continue
    res.append((sym,g['fade'].mean(),len(g)))
res=pd.DataFrame(res,columns=['sym','fade','n'])
print(f"  symbols with >=10 events: {len(res)}")
print(f"  fraction with POSITIVE fade edge: {(res['fade']>0).mean()*100:.1f}%")
print(f"  median across symbols: {res['fade'].median()*100:+.3f}%")
