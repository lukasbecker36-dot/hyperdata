import pandas as pd, numpy as np
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
COST=0.0011

# common window = the 5m data's span (~17-20d)
d5=pd.read_csv('hyperliquid_5m.csv'); COMMON_START=d5['open_time_ms'].min()

def run(path,barmin,label):
    d=pd.read_csv(path).sort_values(['symbol','open_time_ms'])
    d=d[d['open_time_ms']>=COMMON_START]
    W=int(24*60/barmin); BACK=int(8*60/barmin)      # 24h windows, 8h backstop
    span_days=(d['open_time_ms'].max()-COMMON_START)/86400000
    rows=[]
    for sym,g in d.groupby('symbol'):
        if len(g)<W+BACK+5: continue
        g=g.reset_index(drop=True); c=g['close'].values
        ret=np.log(c/np.roll(c,1)); ret[0]=np.nan
        rv=pd.Series(ret).rolling(W).std().values
        vr=(g['volume'].values)/pd.Series(g['volume']).shift(1).rolling(W).median().values
        ph=pd.Series(g['high']).shift(1).rolling(W).max().values; pl=pd.Series(g['low']).shift(1).rolling(W).min().values
        brk=np.where(c>ph,1,np.where(c<pl,-1,0))
        t=g['open_time_ms'].values; tl=tier(uni.get(sym,0))
        fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
        fm=pd.merge_asof(pd.DataFrame({'open_time_ms':t}),fg,on='open_time_ms',direction='backward',tolerance=2*3600*1000)['fund8'].values
        for i in range(len(g)):
            if np.isnan(vr[i]) or vr[i]<5 or brk[i]==0 or tl not in('HIGH','MID'): continue
            if np.isnan(rv[i]) or np.isnan(fm[i]) or brk[i]*np.sign(fm[i])!=1 or i+BACK>=len(g): continue
            rows.append((sym,i,int(brk[i]),c,ph[i],pl[i],rv[i]))
    # apply high-vol gate: rv top 40% among signals
    if not rows: print(f"{label}: no signals"); return
    R=pd.DataFrame(rows,columns=['sym','i','brk','c','ph','pl','rv'])
    thr=R['rv'].quantile(0.60); R=R[R['rv']>=thr]
    nets=[]; holds=[]; moves=[]
    for _,r in R.iterrows():
        c=r['c']; i=r['i']; brk=r['brk']; d_=-brk; e=c[i]; k=BACK
        for kk in range(1,BACK+1):
            if (brk==1 and c[i+kk]<r['ph']) or (brk==-1 and c[i+kk]>r['pl']): k=kk; break
        nets.append(d_*np.log(c[i+k]/e)-COST); holds.append(k*barmin/60); moves.append(abs(np.log(c[i+k]/e)))
    n=np.array(nets); h=np.array(holds)
    print(f"{label:4s}: n={len(n):4d} ({len(n)/span_days:4.1f}/day)  net={n.mean()*1e4:+6.1f}bps  win={(n>0).mean()*100:4.1f}%  "
          f"avgHold={h.mean():4.1f}h  grossMove={np.mean(moves)*100:4.2f}%  Sharpe={n.mean()/n.std():+.3f}  net/hr={n.mean()*1e4/h.mean():+5.1f}")

print(f"Stacked strategy (high-vol + crowd + reclaim/8h), SAME window (~{ (d5['open_time_ms'].max()-COMMON_START)/86400000:.0f}d), all perps:\n")
run('hyperliquid_5m.csv',5,'5m')
run('hyperliquid_15m_allperps.csv',15,'15m')
run('hyperliquid_1h_history.csv',60,'1h')
