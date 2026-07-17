import pandas as pd, numpy as np

df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VOLWIN=RANGEWIN=96; MINBARS=1500
H=8            # hold 2h after fill/entry
FILLWIN=4      # allow up to 1h for the resting limit to fill

# taker vs maker round-trip cost assumptions (bps)
TAKER_RT=0.0011   # ~9bps fees + ~2bps slippage
MAKER_RT=0.0006   # maker in (~1.5) + taker out (~4.5) ballpark

sig=[]   # symbol, arrays
store={}
for sym,g in df.groupby('symbol'):
    if len(g)<MINBARS: continue
    t=tier(uni.get(sym,0))
    if t=='LOW': continue          # focus on executable tiers
    g=g.reset_index(drop=True)
    c=g['close'].values; hi=g['high'].values; lo=g['low'].values
    med=pd.Series(g['volume']).shift(1).rolling(VOLWIN).median().values
    vr=g['volume'].values/med
    ph=pd.Series(g['high']).shift(1).rolling(RANGEWIN).max().values
    pl=pd.Series(g['low']).shift(1).rolling(RANGEWIN).min().values
    brk=np.where(c>ph,1,np.where(c<pl,-1,0))
    store[sym]=(c,hi,lo)
    n=len(g)
    for i in range(n):
        if np.isnan(vr[i]) or vr[i]<5 or brk[i]==0: continue
        if i+FILLWIN+H>=n: continue
        sig.append((sym,i,int(brk[i]),t))
sig=pd.DataFrame(sig,columns=['sym','i','brk','tier'])
print(f"executable signals (HIGH+MID, 5x spike+breakout): {len(sig)}\n")

def eval_offset(o):
    filled=[]; fillret=[]; base_all=[]; fillmask=[]
    for _,r in sig.iterrows():
        c,hi,lo=store[r['sym']]; i=r['i']; brk=r['brk']; d=-brk  # fade dir: +1 long, -1 short
        entryc=c[i]
        # base fade over the whole (fill+hold) window from signal close -> for adverse-selection comparison
        base=d*np.log(c[i+FILLWIN+H]/entryc)
        base_all.append(base)
        # resting limit: short(up-brk) sell above; long(down-brk) buy below
        if brk==1:
            limit=entryc*(1+o); fill=False; fj=None
            for k in range(1,FILLWIN+1):
                if hi[i+k]>=limit: fill=True; fj=k; break
        else:
            limit=entryc*(1-o); fill=False; fj=None
            for k in range(1,FILLWIN+1):
                if lo[i+k]<=limit: fill=True; fj=k; break
        fillmask.append(fill)
        if fill:
            exitpx=c[i+fj+H]
            fillret.append(d*np.log(exitpx/limit))
    fillmask=np.array(fillmask); base_all=np.array(base_all); fillret=np.array(fillret)
    fr=fillmask.mean()
    maker_gross=np.mean(fillret) if len(fillret) else np.nan
    base_fill=base_all[fillmask].mean(); base_miss=base_all[~fillmask].mean() if (~fillmask).any() else np.nan
    return fr,maker_gross,base_fill,base_miss

print(f"{'offset':>7s} {'fill%':>6s} {'maker gross':>11s} {'maker net':>10s} | {'base(filled)':>12s} {'base(missed)':>12s} {'adv.sel':>8s}")
for o in [0.000,0.001,0.002,0.003,0.005]:
    fr,mg,bf,bm=eval_offset(o)
    net=mg-MAKER_RT
    adv=(bm-bf)*1e4 if not np.isnan(bm) else np.nan   # how much better the MISSED trades were (bps)
    print(f"{o*100:6.1f}% {fr*100:5.1f}% {mg*1e4:+10.1f} {net*1e4:+9.1f} | {bf*1e4:+11.1f} {bm*1e4:+11.1f} {adv:+7.1f}")

# taker baseline over same H (from signal close)
base=[]
for _,r in sig.iterrows():
    c,_,_=store[r['sym']]; i=r['i']; d=-r['brk']
    base.append(d*np.log(c[i+H]/c[i]))
base=np.array(base)
print(f"\nTAKER baseline (enter at signal close, hold 2h):")
print(f"  gross={base.mean()*1e4:+.1f}bps  net@11bps={ (base.mean()-TAKER_RT)*1e4:+.1f}bps  win={(base>0).mean()*100:.1f}%  n={len(base)}")
print("\nNotes: 'base(filled/missed)' = fade return from signal close over fill+hold window, split by whether the")
print("limit filled. adv.sel = base(missed) - base(filled): if positive, the trades you MISS are the winners.")
