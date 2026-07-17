import pandas as pd, numpy as np

df = pd.read_csv('hyperliquid_1m_48h.csv')
df = df.sort_values(['symbol','open_time_ms']).reset_index(drop=True)

WIN = 60          # trailing baseline window (minutes)
HORIZONS = [1,3,5,15,30]
COST = 0.0005     # ~5 bps round-trip cost assumption (taker fee + spread)

frames = []
for sym, g in df.groupby('symbol'):
    g = g.copy()
    g['ret'] = np.log(g['close'] / g['close'].shift(1))          # candle log return
    g['dir'] = np.sign(g['ret'])
    # trailing baseline volume (exclude current candle)
    med = g['volume'].shift(1).rolling(WIN).median()
    g['vratio'] = g['volume'] / med
    # forward returns from this candle's close
    for h in HORIZONS:
        g[f'fwd{h}'] = np.log(g['close'].shift(-h) / g['close'])
    frames.append(g)
d = pd.concat(frames).dropna(subset=['vratio','ret'])

print(f"Rows analysed: {len(d)}  |  baseline window: {WIN}m\n")

# ---- 1. Volume vs contemporaneous absolute move (volatility relationship) ----
c = np.corrcoef(np.log(d['vratio'].clip(0.01)), d['ret'].abs())[0,1]
print("=== 1. Volume spike -> SAME-candle move size (volatility) ===")
print(f"corr( log volume-ratio , |return| ) = {c:.3f}")
for lo,hi,lbl in [(0,1,'below median'),(1,2,'1-2x'),(2,3,'2-3x'),(3,5,'3-5x'),(5,10,'5-10x'),(10,1e9,'>10x')]:
    m = d[(d['vratio']>=lo)&(d['vratio']<hi)]
    print(f"  vol {lbl:12s} n={len(m):5d}  mean|ret|={m['ret'].abs().mean()*100:6.3f}%")

# ---- 2. Forward returns after a spike, split by spike-candle direction ----
print("\n=== 2. After a volume spike, does price CONTINUE (momentum) or REVERT? ===")
print("    signed by spike-candle direction; >0 = momentum, <0 = reversion\n")
for thr in [3,5,8]:
    sp = d[(d['vratio']>=thr) & (d['dir']!=0)]
    print(f"-- spike = volume >= {thr}x trailing median   (n={len(sp)}) --")
    for h in HORIZONS:
        signed = sp['dir'] * sp[f'fwd{h}']
        signed = signed.dropna()
        n=len(signed); mean=signed.mean(); t = mean/ (signed.std()/np.sqrt(n)) if n>1 else np.nan
        hit = (signed>0).mean()
        print(f"   +{h:2d}m: mean={mean*100:+6.3f}%  t={t:+5.2f}  hit={hit*100:4.1f}%  net(after {COST*100:.2f}% cost)={ (abs(mean)-COST)*100:+6.3f}%")
    print()

# ---- 3. Baseline: forward move regardless of direction (is it just volatility?) ----
print("=== 3. |forward return| : spike vs non-spike (unsigned) ===")
for thr in [3,5,8]:
    sp = d[d['vratio']>=thr]; ns = d[d['vratio']<thr]
    print(f"-- {thr}x --")
    for h in HORIZONS:
        print(f"   +{h:2d}m: |fwd| spike={sp[f'fwd{h}'].abs().mean()*100:5.3f}%  non-spike={ns[f'fwd{h}'].abs().mean()*100:5.3f}%")

# ---- 4. Per-symbol momentum edge at 5x / +5m ----
print("\n=== 4. Per-symbol: signed 5m forward return after 5x spike ===")
for sym,g in d.groupby('symbol'):
    sp=g[(g['vratio']>=5)&(g['dir']!=0)]
    if len(sp)<5:
        print(f"   {sym:12s} n={len(sp):3d}  (too few)"); continue
    signed=(sp['dir']*sp['fwd5']).dropna()
    print(f"   {sym:12s} n={len(sp):3d}  mean5m={signed.mean()*100:+6.3f}%  hit={ (signed>0).mean()*100:4.1f}%")
