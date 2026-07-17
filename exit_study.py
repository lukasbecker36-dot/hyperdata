import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VW=RW=96; COST=0.0011; BARH=0.25   # 15m = 0.25h
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())
store={}; tmp=[]
for sym,g in df.groupby('symbol'):
    if len(g)<1500: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(VW).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VW).median()
    ph=g['high'].shift(1).rolling(RW).max(); pl=g['low'].shift(1).rolling(RW).min()
    g['ph']=ph; g['pl']=pl
    g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0)); g['tier']=tier(uni.get(sym,0))
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3600*1000).reset_index(drop=True)
    store[sym]=(g['close'].values,g['high'].values,g['low'].values,g['ph'].values,g['pl'].values); tmp.append(g)
big=pd.concat(tmp); thr=big[(big['vratio']>=5)&(big['brk']!=0)&(big['tier'].isin(['HIGH','MID']))].dropna(subset=['rv24'])['rv24'].quantile(0.60)
sg=[]
for sym,g in big.groupby('symbol'):
    c=store[sym][0]
    for i in range(len(g)):
        r=g.iloc[i]
        if np.isnan(r['vratio']) or r['vratio']<5 or r['brk']==0 or r['tier'] not in('HIGH','MID'): continue
        if np.isnan(r['rv24']) or r['rv24']<thr or pd.isna(r['fund8']): continue
        if r['brk']*np.sign(r['fund8'])!=1 or i+48>=len(c): continue
        sg.append((sym,i,int(r['brk'])))
sig=pd.DataFrame(sg,columns=['sym','i','brk'])
print(f"stacked signals (all with >=12h forward room): {len(sig)}\n")

def stats(rets,holds):
    r=np.array(rets); h=np.array(holds)*BARH
    net=r.mean()*1e4; sh=r.mean()/r.std(); nph=net/h.mean()
    return f"net={net:+6.1f}bps  win={(r>0).mean()*100:4.1f}%  avgHold={h.mean():4.1f}h  Sharpe={sh:+.3f}  net/hr={nph:+5.1f}bps"

print("=== 1. TIME-BASED HOLD (exit at close after H bars) ===")
for H in [1,2,4,6,8,12,16,24,32,48]:
    rets=[];
    for _,r in sig.iterrows():
        c=store[r['sym']][0]; i=r['i']; d=-r['brk']
        rets.append(d*np.log(c[i+H]/c[i])-COST)
    print(f"  {H*15:4d}m ({H*BARH:4.1f}h): {stats(rets,[H]*len(rets))}")

print("\n=== 2. PROFIT-TARGET exit (no stop; time backstop 8h=32 bars) ===")
MAXT=32
for T in [0.005,0.01,0.02,0.03,0.05]:
    rets=[]; holds=[]
    for _,r in sig.iterrows():
        c,hi,lo,_,_=store[r['sym']]; i=r['i']; brk=r['brk']; d=-brk; e=c[i]; done=None
        for k in range(1,MAXT+1):
            if brk==1 and lo[i+k]<=e*(1-T): done=(T,k); break      # short fade: profit when price falls to -T
            if brk==-1 and hi[i+k]>=e*(1+T): done=(T,k); break     # long fade: profit when price rises +T
        if done is None: done=(d*np.log(c[i+MAXT]/e),MAXT)
        rets.append(done[0]-COST); holds.append(done[1])
    print(f"  target {T*100:.1f}%: {stats(rets,holds)}")

print("\n=== 3. RANGE-RECLAIM exit (exit when close re-enters prior 24h range; backstop 8h) ===")
for MAXB in [16,32,48]:
    rets=[]; holds=[]
    for _,r in sig.iterrows():
        c,hi,lo,ph,pl=store[r['sym']]; i=r['i']; brk=r['brk']; d=-brk; e=c[i]; done=None
        H=min(MAXB, len(c)-1-i)
        for k in range(1,H+1):
            if brk==1 and c[i+k]<ph[i]: done=(d*np.log(c[i+k]/e),k); break   # short: close back below the level it broke
            if brk==-1 and c[i+k]>pl[i]: done=(d*np.log(c[i+k]/e),k); break
        if done is None: done=(d*np.log(c[i+H]/e),H)
        rets.append(done[0]-COST); holds.append(done[1])
    print(f"  reclaim, backstop {MAXB*BARH:.0f}h: {stats(rets,holds)}")
