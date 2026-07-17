import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VW=RW=96; COST=0.0011; BACKSTOP=32; TR=16   # breadth over trailing 4h
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())
store={}; tmp=[]
for sym,g in df.groupby('symbol'):
    if len(g)<1500: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(VW).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VW).median()
    ph=g['high'].shift(1).rolling(RW).max(); pl=g['low'].shift(1).rolling(RW).min()
    g['ph']=ph; g['pl']=pl; g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0)); g['tier']=tier(uni.get(sym,0))
    g['tr16']=np.log(c/c.shift(TR))    # trailing 4h return
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3600*1000).reset_index(drop=True)
    store[sym]=g; tmp.append(g)
big=pd.concat(tmp)
thr=big[(big['vratio']>=5)&(big['brk']!=0)&(big['tier'].isin(['HIGH','MID']))].dropna(subset=['rv24'])['rv24'].quantile(0.60)

# market-wide aggregates per timestamp
mkt_up=big.assign(up=(big['tr16']>0)).groupby('open_time_ms')['up'].mean().rename('mkt_up_frac')   # frac of universe up over 4h
brk_up=big.groupby('open_time_ms')['brk'].agg(lambda s:(s==1).sum()).rename('nbrk_up')
brk_dn=big.groupby('open_time_ms')['brk'].agg(lambda s:(s==-1).sum()).rename('nbrk_dn')
btc=big[big['symbol']=='BTC'].set_index('open_time_ms')['tr16'].rename('btc_tr16')
agg=pd.concat([mkt_up,brk_up,brk_dn,btc],axis=1)
# rolling co-breakouts over last 4 bars
agg['nbrk_up_4']=agg['nbrk_up'].rolling(4,min_periods=1).sum(); agg['nbrk_dn_4']=agg['nbrk_dn'].rolling(4,min_periods=1).sum()

rows=[]
for sym,g in big.groupby('symbol'):
    c=g['close'].values; ph=g['ph'].values; pl=g['pl'].values
    for i in range(len(g)):
        r=g.iloc[i]
        if r['vratio']<5 or r['brk']==0 or r['tier'] not in('HIGH','MID') or np.isnan(r['rv24']) or r['rv24']<thr: continue
        if pd.isna(r['fund8']) or r['brk']*np.sign(r['fund8'])!=1 or i+BACKSTOP>=len(c): continue
        b=int(r['brk']); d=-b; e=c[i]; t=r['open_time_ms']; k=BACKSTOP
        for kk in range(1,BACKSTOP+1):
            if (b==1 and c[i+kk]<ph[i]) or (b==-1 and c[i+kk]>pl[i]): k=kk; break
        net=d*np.log(c[i+k]/e)-COST
        a=agg.loc[t]
        breadth_dir=a['mkt_up_frac'] if b==1 else 1-a['mkt_up_frac']       # frac of market moving WITH the breakout
        btc_dir=b*a['btc_tr16']                                            # BTC move aligned with breakout
        codir=a['nbrk_up_4'] if b==1 else a['nbrk_dn_4']                   # #coins breaking out same dir (last 1h)
        rows.append(dict(net=net, breadth_dir=breadth_dir, btc_dir=btc_dir, codir=codir))
S=pd.DataFrame(rows).dropna(); S['win']=S['net']>0
print(f"stacked signals: {len(S)}   win {S['win'].mean()*100:.1f}%   base net {S['net'].mean()*1e4:+.1f}bps\n")

print("=== breadth features: winner vs loser, and corr with net ===")
print(f"{'feature':>12s} {'winner':>9s} {'loser':>9s} {'corr(net)':>10s}")
for f in ['breadth_dir','btc_dir','codir']:
    print(f"{f:>12s} {S[S.win][f].mean():+9.3f} {S[~S.win][f].mean():+9.3f} {np.corrcoef(S[f],S['net'])[0,1]:+10.3f}")

print("\n=== fade edge by tercile of each breadth feature ===")
for f in ['breadth_dir','btc_dir','codir']:
    S['b']=pd.qcut(S[f],3,labels=['LOW (idiosyncratic)','MID','HIGH (market-wide)'],duplicates='drop')
    print(f"  by {f}:")
    for lab in S['b'].cat.categories:
        s=S[S['b']==lab]['net']
        print(f"    {lab:22s} n={len(s):4d}  net={s.mean()*1e4:+6.1f}bps  win={(s>0).mean()*100:4.1f}%")

print("\n=== screen: fade only IDIOSYNCRATIC breakouts (drop market-wide) ===")
def rep(mask,label):
    s=S[mask]; print(f"  {label:44s} keep {len(s):4d} ({len(s)/len(S)*100:2.0f}%)  net={s['net'].mean()*1e4:+6.1f}bps  win={s['win'].mean()*100:4.1f}%  Sharpe={s['net'].mean()/s['net'].std():+.3f}")
rep(pd.Series(True,index=S.index),"[baseline]")
rep(S.breadth_dir<S.breadth_dir.median(),"market breadth below median")
rep(S.breadth_dir<S.breadth_dir.quantile(0.33),"market breadth bottom third")
rep(S.btc_dir<S.btc_dir.median(),"BTC-aligned move below median")
rep(S.btc_dir<0,"BTC moving AGAINST the breakout")
rep(S.codir<S.codir.median(),"few co-breakouts (below median)")
