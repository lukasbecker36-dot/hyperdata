import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_1h_history.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
COST=0.0011; HOURMS=3600*1000
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())

# ---- build signal table once (features + forward fade returns for each hold) ----
rows=[]
for sym,g in df.groupby('symbol'):
    if len(g)<600: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(24).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(24).median()
    ph=g['high'].shift(1).rolling(24).max(); pl=g['low'].shift(1).rolling(24).min()
    g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0))
    for h in [2,4,8]: g[f'fade{h}']=-g['brk']*np.log(c.shift(-h)/c)
    g['tier']=tier(uni.get(sym,0))
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3*HOURMS)
    sg=g[(g['vratio']>=5)&(g['brk']!=0)&(g['tier'].isin(['HIGH','MID']))].dropna(subset=['rv24','fund8','fade8']).copy()
    sg['crowd']=(sg['brk']*np.sign(sg['fund8'])==1).astype(int)
    rows.append(sg[['open_time_ms','rv24','crowd','fade2','fade4','fade8','tier']])
S=pd.concat(rows); S['dt']=pd.to_datetime(S['open_time_ms'],unit='ms'); S=S.sort_values('dt').reset_index(drop=True)
t0=S['dt'].min(); tN=S['dt'].max()
print(f"total filtered signals: {len(S)}   span {t0.date()} -> {tN.date()}  ({(tN-t0).days}d)\n")

def edge(sub,h):
    f=sub[f'fade{h}']; return (f.mean()-COST), (f.mean()-COST)/(f.std()/np.sqrt(len(f))) if len(f)>1 else 0

# rolling walk-forward
TRAIN=60; TEST=20; STEP=20; PURGE=1  # days
folds=[]; start=t0
oos_fixed=[]; oos_adapt=[]
print(f"{'fold':>4s} {'test window':>23s} {'trainN':>6s} {'testN':>5s} | {'FIXED thr/h':>11s} {'train':>6s} {'test':>6s} | {'ADAPT p/h':>9s} {'train':>6s} {'test':>6s}")
fold=0; ws=t0
while True:
    tr_lo=ws; tr_hi=ws+pd.Timedelta(days=TRAIN)
    te_lo=tr_hi+pd.Timedelta(days=PURGE); te_hi=te_lo+pd.Timedelta(days=TEST)
    if te_lo>=tN: break
    tr=S[(S.dt>=tr_lo)&(S.dt<tr_hi)]; te=S[(S.dt>=te_lo)&(S.dt<te_hi)]
    if len(tr)<100 or len(te)<20: ws=ws+pd.Timedelta(days=STEP); continue
    fold+=1
    # crowd-aligned only (part of frozen structure)
    trc=tr[tr.crowd==1]; tec=te[te.crowd==1]
    # FIXED: vol thr = 60th pctile of train rv24, hold=8
    thr=trc['rv24'].quantile(0.60); h=8
    trs=trc[trc.rv24>=thr]; tes=tec[tec.rv24>=thr]
    ftr,_=edge(trs,h); fte,ftt=edge(tes,h)
    oos_fixed.append(tes.assign(hold=h,ret=tes[f'fade{h}']-COST))
    # ADAPT: grid over pctile & hold, pick best train net edge
    best=None
    for p in [0.5,0.6,0.7,0.8]:
        th=trc['rv24'].quantile(p)
        for hh in [2,4,8]:
            e,_=edge(trc[trc.rv24>=th],hh)
            if best is None or e>best[0]: best=(e,p,hh,th)
    _,bp,bh,bth=best
    atr=trc[trc.rv24>=bth]; ate=tec[tec.rv24>=bth]
    atr_e,_=edge(atr,bh); ate_e,_=edge(ate,bh)
    oos_adapt.append(ate.assign(hold=bh,ret=ate[f'fade{bh}']-COST))
    print(f"{fold:4d} {te_lo.strftime('%m-%d')}->{te_hi.strftime('%m-%d')}  {len(trc):6d} {len(tec):5d} | p60/h8      {ftr*1e4:+6.1f} {fte*1e4:+6.1f} | p{int(bp*100)}/h{bh}   {atr_e*1e4:+6.1f} {ate_e*1e4:+6.1f}")
    ws=ws+pd.Timedelta(days=STEP)

def pooled(chunks,name):
    a=pd.concat(chunks); r=a['ret']
    t=r.mean()/(r.std()/np.sqrt(len(r)))
    print(f"  {name:28s} n={len(r):5d}  net={r.mean()*1e4:+6.1f}bps  win={(r>0).mean()*100:4.1f}%  t={t:+5.2f}  cum={r.sum()*100:+.0f}%")

print("\n=== POOLED OUT-OF-SAMPLE (test windows only, concatenated) ===")
pooled(oos_fixed,"FIXED spec (thr from train)")
pooled(oos_adapt,"ADAPTIVE (grid-searched)")
print("\n  in-sample reference (whole-sample fit, 8h hold): stacked ~ +25bps @8h")
# save OOS equity (fixed)
oe=pd.concat(oos_fixed).sort_values('dt'); oe['cum']=oe['ret'].cumsum()
oe[['dt','tier','hold','ret','cum']].to_csv('walkforward_oos.csv',index=False)
