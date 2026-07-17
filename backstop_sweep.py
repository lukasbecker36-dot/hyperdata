import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VW=RW=96; COST=0.0011; BARH=0.25; CAP=96   # evaluate reclaim up to 24h; fixed signal set needs CAP bars room
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())
store={}; tmp=[]
for sym,g in df.groupby('symbol'):
    if len(g)<1500: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(VW).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VW).median()
    ph=g['high'].shift(1).rolling(RW).max(); pl=g['low'].shift(1).rolling(RW).min()
    g['ph']=ph; g['pl']=pl; g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0)); g['tier']=tier(uni.get(sym,0))
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3600*1000).reset_index(drop=True)
    store[sym]=g; tmp.append(g)
big=pd.concat(tmp); thr=big[(big['vratio']>=5)&(big['brk']!=0)&(big['tier'].isin(['HIGH','MID']))].dropna(subset=['rv24'])['rv24'].quantile(0.60)

# for each signal: k_reclaim (first bar close re-enters range, capped CAP; -1 if never), and fade return path
recl=[]; paths=[]
for sym,g in big.groupby('symbol'):
    c=g['close'].values; ph=g['ph'].values; pl=g['pl'].values
    for i in range(len(g)):
        r=g.iloc[i]
        if r['vratio']<5 or r['brk']==0 or r['tier'] not in('HIGH','MID') or np.isnan(r['rv24']) or r['rv24']<thr: continue
        if pd.isna(r['fund8']) or r['brk']*np.sign(r['fund8'])!=1 or i+CAP>=len(c): continue
        brk=int(r['brk']); d=-brk; e=c[i]
        kR=-1
        for k in range(1,CAP+1):
            if (brk==1 and c[i+k]<ph[i]) or (brk==-1 and c[i+k]>pl[i]): kR=k; break
        recl.append(kR)
        paths.append((sym,i,d,e,g['close'].values))   # keep ref for exit price lookup
recl=np.array(recl)
print(f"stacked signals (24h forward room): {len(recl)}\n")

# reclaim timing distribution
print("cumulative reclaim rate by elapsed time:")
for B in [4,8,16,24,32,48,64,96]:
    frac=((recl>0)&(recl<=B)).mean()
    print(f"  by {B*BARH:4.1f}h: {frac*100:4.1f}% have reclaimed")
neverpct=(recl<0).mean()*100
print(f"  never within 24h: {neverpct:.1f}%\n")

def eval_backstop(B):
    rets=[]; holds=[]; nbackstop=0
    for kR,(sym,i,d,e,c) in zip(recl,paths):
        if kR>0 and kR<=B: k=kR
        else: k=B; nbackstop+=1
        rets.append(d*np.log(c[i+k]/e)-COST); holds.append(k)
    r=np.array(rets); h=np.array(holds)*BARH
    return dict(net=r.mean()*1e4,win=(r>0).mean()*100,hold=h.mean(),sharpe=r.mean()/r.std(),
                nph=r.mean()*1e4/h.mean(),bkpct=nbackstop/len(recl)*100,worst=r.min()*100)

print(f"{'backstop':>9s} {'net bps':>7s} {'win%':>5s} {'avgHold':>7s} {'Sharpe':>7s} {'net/hr':>6s} {'%exit@backstop':>14s} {'worst':>6s}")
for B in [8,16,24,32,48,64,96]:
    s=eval_backstop(B)
    print(f"{B*BARH:6.1f}h  {s['net']:+7.1f} {s['win']:5.1f} {s['hold']:6.1f}h {s['sharpe']:+7.3f} {s['nph']:+6.1f} {s['bkpct']:13.1f}% {s['worst']:+6.1f}%")
