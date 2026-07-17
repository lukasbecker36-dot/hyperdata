import pandas as pd, numpy as np

df = pd.read_csv('hyperliquid_15m_60d.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
VOLWIN = 96          # trailing vol baseline = 24h (96 x 15m)
RANGEWIN = 96        # prior range lookback = 24h
HZ = [1,2,4,8,16]    # forward horizons in bars -> 15m,30m,1h,2h,4h
COST = 0.0005        # ~5 bps round-trip

frames=[]
for sym,g in df.groupby('symbol'):
    g=g.copy()
    g['ret']=np.log(g['close']/g['close'].shift(1))
    g['dir']=np.sign(g['ret'])
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VOLWIN).median()
    phigh=g['high'].shift(1).rolling(RANGEWIN).max()
    low =g['low'].shift(1).rolling(RANGEWIN).min()
    g['brk']=np.where(g['close']>phigh,1,np.where(g['close']<low,-1,0))  # +1 up-breakout,-1 down,0 in-range
    for h in HZ:
        g[f'f{h}']=np.log(g['close'].shift(-h)/g['close'])
    frames.append(g)
d=pd.concat(frames).replace([np.inf,-np.inf],np.nan).dropna(subset=['vratio','ret'])

def stat(sub, signcol):
    """signed forward returns; sign given by signcol (breakout dir or candle dir)"""
    print(f"      n={len(sub)}")
    for h in HZ:
        s=(sub[signcol]*sub[f'f{h}']).dropna()
        if len(s)<10:
            print(f"      +{h*15:3d}m: (too few)"); continue
        m=s.mean(); t=m/(s.std()/np.sqrt(len(s)))
        print(f"      +{h*15:3d}m: mean={m*100:+6.3f}%  t={t:+5.2f}  hit={(s>0).mean()*100:4.1f}%  net={ (m-COST)*100:+6.3f}%")

for THR in [3,5]:
    print(f"\n{'='*70}\nSPIKE THRESHOLD = volume >= {THR}x trailing 24h median\n{'='*70}")
    spike=d[d['vratio']>=THR]

    print(f"\n[A] SPIKE + RANGE BREAKOUT  (signed by breakout direction -> momentum test)")
    a=spike[spike['brk']!=0].copy(); a['sig']=a['brk']
    stat(a,'sig')

    print(f"\n[B] BREAKOUT WITHOUT volume spike  (control: does volume add anything?)")
    b=d[(d['vratio']<THR)&(d['brk']!=0)].copy(); b['sig']=b['brk']
    stat(b,'sig')

    print(f"\n[C] SPIKE INSIDE RANGE (no breakout)  (signed by candle direction -> fade test)")
    c=spike[spike['brk']==0].copy(); c['sig']=c['dir']
    stat(c,'sig')

# breakdown of how spikes distribute across context
print(f"\n{'='*70}\nContext distribution of 5x spikes")
sp=d[d['vratio']>=5]
print(f"  total 5x spikes: {len(sp)}")
print(f"  ... that are range breakouts: {(sp['brk']!=0).sum()}  ({(sp['brk']!=0).mean()*100:.1f}%)")
print(f"  ... that fire inside range:   {(sp['brk']==0).sum()}  ({(sp['brk']==0).mean()*100:.1f}%)")

# per-symbol: spike+breakout, 1h forward (h=4)
print(f"\nPer-symbol: SPIKE(5x)+BREAKOUT, signed 1h forward return")
for sym,g in d.groupby('symbol'):
    a=g[(g['vratio']>=5)&(g['brk']!=0)]
    if len(a)<8: print(f"  {sym:12s} n={len(a):3d} (too few)"); continue
    s=(a['brk']*a['f4']).dropna()
    print(f"  {sym:12s} n={len(a):3d}  mean1h={s.mean()*100:+6.3f}%  t={s.mean()/(s.std()/np.sqrt(len(s))):+5.2f}  hit={(s>0).mean()*100:4.1f}%")
