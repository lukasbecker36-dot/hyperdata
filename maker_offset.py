import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VW=RW=96; HOLD=32; FILLWIN=4; MAKER=0.0006
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())
store={}; tmp=[]
for sym,g in df.groupby('symbol'):
    if len(g)<1500: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(VW).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VW).median()
    ph=g['high'].shift(1).rolling(RW).max(); pl=g['low'].shift(1).rolling(RW).min()
    g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0)); g['tier']=tier(uni.get(sym,0))
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3600*1000).reset_index(drop=True)
    store[sym]=(g['close'].values,g['high'].values,g['low'].values); tmp.append(g)
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
sig=pd.DataFrame(sig,columns=['sym','i','brk']); N=len(sig)
print(f"stacked signals: {N}\n")
print("POST PRICE = reach into the breakout for a better fade entry (short=higher, long=lower)")
print(f"{'post at':>16s} {'fill%':>6s} {'net/filled':>10s} {'win%':>5s} {'CAPTURED/signal':>15s}")
print(f"{'':>16s} {'':>6s} {'':>10s} {'':>5s} {'(= fill% x net)':>15s}")
for off,lbl in [(0.0,'touch (at price)'),(0.0010,'+10bps better'),(0.0020,'+20bps better'),(0.0040,'+40bps better'),(0.0080,'+80bps better')]:
    fills=0; rets=[]
    for _,r in sig.iterrows():
        c,hi,lo=store[r['sym']]; i=r['i']; brk=r['brk']; d=-brk; e=c[i]
        if brk==1: post=e*(1+off); hit=any(hi[i+k]>=post for k in range(1,FILLWIN+1))    # short: sell higher
        else:      post=e*(1-off); hit=any(lo[i+k]<=post for k in range(1,FILLWIN+1))    # long: buy lower
        if not hit: continue
        # find fill bar
        for k in range(1,FILLWIN+1):
            if (brk==1 and hi[i+k]>=post) or (brk==-1 and lo[i+k]<=post): fj=k; break
        fills+=1; rets.append(d*np.log(c[i+fj+HOLD]/post)-MAKER)
    rets=np.array(rets); fr=fills/N; net=rets.mean()*1e4; cap=fr*net
    print(f"{lbl:>16s} {fr*100:5.1f}% {net:+9.1f} {(rets>0).mean()*100:5.1f} {cap:+14.1f}")
print("\n  'captured/signal' = expected bps per SIGNAL (accounts for trades you miss).")
print("  Higher = more total P&L across all signals, i.e. the metric that matters for capacity.")
