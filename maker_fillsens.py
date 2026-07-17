import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VW=RW=96; HOLD=32; FILLWIN=4          # allow 1h to fill, hold 8h from fill
MAKER_IN_TAKER_OUT=0.0006             # ~1.5bps maker in + 4.5bps taker out
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())

store={}; tmp=[]
for sym,g in df.groupby('symbol'):
    if len(g)<1500: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(VW).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VW).median()
    ph=g['high'].shift(1).rolling(RW).max(); pl=g['low'].shift(1).rolling(RW).min()
    g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0))
    g['tier']=tier(uni.get(sym,0))
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3600*1000).reset_index(drop=True)
    store[sym]=(g['close'].values,g['high'].values,g['low'].values)
    tmp.append(g)
big=pd.concat(tmp); rv_thr=big['rv24'].quantile(0.60)
sig=[]
for sym,g in big.groupby('symbol'):
    c,hi,lo=store[sym]
    for i in range(len(g)):
        r=g.iloc[i]
        if np.isnan(r['vratio']) or r['vratio']<5 or r['brk']==0 or r['tier'] not in('HIGH','MID'): continue
        if np.isnan(r['rv24']) or r['rv24']<rv_thr or pd.isna(r['fund8']): continue
        if r['brk']*np.sign(r['fund8'])!=1 or i+FILLWIN+HOLD>=len(c): continue
        sig.append((sym,i,int(r['brk'])))
sig=pd.DataFrame(sig,columns=['sym','i','brk'])
print(f"stacked signals: {len(sig)}   (post limit at signal close to fade; require trade-THROUGH by buffer)\n")
print("Interpretation: bigger buffer = more certain the order truly filled, but the")
print("filled subset is more adversely selected (price ran further before reverting).\n")
print(f"{'fill rule':>22s} {'fill%':>6s} {'maker net':>9s} {'win%':>5s}   {'(taker-at-close ref)':>20s}")

# taker reference: enter at close, hold, taker RT ~11bps
tk=[]
for _,r in sig.iterrows():
    c,_,_=store[r['sym']]; i=r['i']; d=-r['brk']
    tk.append(d*np.log(c[i+HOLD]/c[i])-0.0011)
tk=np.array(tk)

for buf,lbl in [(0.0,'touch (optimistic)'),(0.0005,'through +5bps'),(0.0010,'through +10bps'),(0.0020,'through +20bps'),(0.0040,'through +40bps')]:
    fills=[]; rets=[]
    for _,r in sig.iterrows():
        c,hi,lo=store[r['sym']]; i=r['i']; brk=r['brk']; d=-brk; e=c[i]
        limit=e  # post at last price (join the touch on the fade side)
        filled=False; fj=None
        for k in range(1,FILLWIN+1):
            if brk==1 and hi[i+k]>=limit*(1+buf): filled=True; fj=k; break   # up-brk: sell filled if price trades up through
            if brk==-1 and lo[i+k]<=limit*(1-buf): filled=True; fj=k; break  # down-brk: buy filled if price trades down through
        fills.append(filled)
        if filled:
            exitpx=c[i+fj+HOLD]
            rets.append(d*np.log(exitpx/limit)-MAKER_IN_TAKER_OUT)
    fills=np.array(fills); rets=np.array(rets)
    fr=fills.mean(); net=rets.mean()*1e4 if len(rets) else np.nan; win=(rets>0).mean()*100 if len(rets) else np.nan
    print(f"{lbl:>22s} {fr*100:5.1f}% {net:+8.1f} {win:5.1f}%")
print(f"\n  taker-at-close baseline (fills 100%): net={tk.mean()*1e4:+.1f}bps win={(tk>0).mean()*100:.1f}%")
