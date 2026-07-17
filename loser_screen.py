import pandas as pd, numpy as np
df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VW=RW=96; COST=0.0011; BACKSTOP=32
fund['fund8']=fund.groupby('symbol')['funding_rate'].transform(lambda s:s.rolling(8,min_periods=1).mean())

def rsi(c,n=14):
    d=np.diff(c,prepend=c[0]); up=np.where(d>0,d,0); dn=np.where(d<0,-d,0)
    ru=pd.Series(up).ewm(alpha=1/n,adjust=False).mean().values
    rd=pd.Series(dn).ewm(alpha=1/n,adjust=False).mean().values
    rs=ru/np.where(rd==0,np.nan,rd); return 100-100/(1+rs)

store={}; tmp=[]
for sym,g in df.groupby('symbol'):
    if len(g)<1500: continue
    g=g.copy(); c=g['close']; g['ret']=np.log(c/c.shift(1))
    g['rv24']=g['ret'].rolling(VW).std()
    g['vratio']=g['volume']/g['volume'].shift(1).rolling(VW).median()
    ph=g['high'].shift(1).rolling(RW).max(); pl=g['low'].shift(1).rolling(RW).min()
    g['ph']=ph; g['pl']=pl; g['brk']=np.where(c>ph,1,np.where(c<pl,-1,0)); g['tier']=tier(uni.get(sym,0))
    g['ema20']=c.ewm(span=20).mean(); g['ema80']=c.ewm(span=80).mean()
    g['rsi']=rsi(c.values)
    g['mom24']=np.log(c/c.shift(96)); g['mom4']=np.log(c/c.shift(16))
    g['upfrac']=(g['ret']>0).rolling(24).mean()
    fg=fund[fund['symbol']==sym][['open_time_ms','fund8']]
    g=pd.merge_asof(g.sort_values('open_time_ms'),fg.sort_values('open_time_ms'),on='open_time_ms',direction='backward',tolerance=3600*1000).reset_index(drop=True)
    store[sym]=g; tmp.append(g)
big=pd.concat(tmp); thr=big[(big['vratio']>=5)&(big['brk']!=0)&(big['tier'].isin(['HIGH','MID']))].dropna(subset=['rv24'])['rv24'].quantile(0.60)

rows=[]
for sym,g in big.groupby('symbol'):
    c=g['close'].values; ph=g['ph'].values; pl=g['pl'].values
    for i in range(len(g)):
        r=g.iloc[i]
        if r['vratio']<5 or r['brk']==0 or r['tier'] not in('HIGH','MID') or np.isnan(r['rv24']) or r['rv24']<thr: continue
        if pd.isna(r['fund8']) or r['brk']*np.sign(r['fund8'])!=1 or i+BACKSTOP>=len(c): continue
        b=int(r['brk']); d=-b; e=c[i]
        k=BACKSTOP
        for kk in range(1,BACKSTOP+1):
            if (b==1 and c[i+kk]<ph[i]) or (b==-1 and c[i+kk]>pl[i]): k=kk; break
        net=d*np.log(c[i+k]/e)-COST
        # features oriented so LARGER = more extended in the breakout direction (candidate loser signal)
        rows.append(dict(net=net, backstop=(k==BACKSTOP),
            mom24_dir=b*r['mom24'], mom4_dir=b*r['mom4'],
            ema_align=b*np.sign(r['ema20']-r['ema80']),
            ema_dist=b*(e-r['ema80'])/r['ema80'],
            rsi_dir=r['rsi'] if b==1 else 100-r['rsi'],
            brk_mag=(e-(ph[i] if b==1 else pl[i]))/e*b,
            upfrac_dir=(r['upfrac'] if b==1 else 1-r['upfrac']),
            vratio=r['vratio']))
S=pd.DataFrame(rows).dropna()
S['win']=S['net']>0
print(f"stacked signals: {len(S)}   win rate {S['win'].mean()*100:.1f}%   base net {S['net'].mean()*1e4:+.1f}bps\n")

feats=['mom24_dir','mom4_dir','ema_align','ema_dist','rsi_dir','brk_mag','upfrac_dir','vratio']
print("=== do features separate winners from losers? ===")
print(f"{'feature':>12s} {'winner mean':>11s} {'loser mean':>10s} {'corr w/ net':>11s}")
for f in feats:
    wm=S[S.win][f].mean(); lm=S[~S.win][f].mean(); cr=np.corrcoef(S[f],S['net'])[0,1]
    print(f"{f:>12s} {wm:+11.4f} {lm:+10.4f} {cr:+11.3f}")

print("\n=== screen tests (drop the 'trending/extended' tail) — does edge improve? ===")
def rep(mask,label):
    s=S[mask]; base=S
    print(f"  {label:38s} keep {len(s):4d}/{len(S)} ({len(s)/len(S)*100:.0f}%)  net={s['net'].mean()*1e4:+6.1f}bps  win={s['win'].mean()*100:4.1f}%  Sharpe={s['net'].mean()/s['net'].std():+.3f}")
rep(pd.Series(True,index=S.index),"[baseline: no screen]")
rep(S.ema_align<=0,"skip WITH-trend (ema20>ema80 aligned)")
rep(S.ema_align>=0,"skip AGAINST-trend")
for q in [0.9,0.75]:
    rep(S.mom24_dir<S.mom24_dir.quantile(q),f"skip top {int((1-q)*100)}% 24h momentum-aligned")
    rep(S.ema_dist<S.ema_dist.quantile(q),f"skip top {int((1-q)*100)}% EMA-extension")
rep(S.rsi_dir<80,"skip RSI-extended (>80 in brk dir)")
rep(S.rsi_dir<70,"skip RSI>70 in brk dir")
rep(S.brk_mag<S.brk_mag.quantile(0.75),"skip top 25% breakout magnitude")
