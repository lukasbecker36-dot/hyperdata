import pandas as pd, numpy as np

df = pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
uni = pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
VOLWIN=RANGEWIN=96
MINBARS=1500
BARS_PER_YEAR=96*365

# tertile tier boundaries on universe volume
qs = uni.quantile([1/3,2/3]).values
def tier_of(v): return 'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')

# ---- build indicators + signal events ----
events=[]      # each: symbol, idx, t, fade_dir, tier, and path handled later
paths={}       # symbol -> arrays
for sym,g in df.groupby('symbol'):
    if len(g)<MINBARS: continue
    g=g.reset_index(drop=True)
    close=g['close'].values
    lret=np.log(close/np.roll(close,1)); lret[0]=np.nan
    med=pd.Series(g['volume']).shift(1).rolling(VOLWIN).median().values
    vr=g['volume'].values/med
    ph=pd.Series(g['high']).shift(1).rolling(RANGEWIN).max().values
    pl=pd.Series(g['low']).shift(1).rolling(RANGEWIN).min().values
    brk=np.where(close>ph,1,np.where(close<pl,-1,0))
    t=tier_of(uni.get(sym,0))
    paths[sym]=(close,g['open_time_ms'].values)
    n=len(g)
    for i in range(n):
        if np.isnan(vr[i]) or vr[i]<5 or brk[i]==0: continue
        events.append({'sym':sym,'i':i,'t':g['open_time_ms'].values[i],'fade':-brk[i],'tier':t})
ev=pd.DataFrame(events)
print(f"signal events (5x spike + breakout): {len(ev)}  | HIGH {sum(ev.tier=='HIGH')} MID {sum(ev.tier=='MID')} LOW {sum(ev.tier=='LOW')}\n")

# ---- A. fixed-hold gross returns + cost sensitivity ----
def fade_ret(row,H):
    close,_=paths[row['sym']]; i=row['i']
    if i+H>=len(close): return np.nan
    return row['fade']*np.log(close[i+H]/close[i])

COSTS=[5,10,15,20,30]  # bps round-trip
print("="*84)
print("A. FIXED-HOLD FADE — mean net return per trade (bps) across round-trip cost assumptions")
print("="*84)
for H,lbl in [(4,'1h'),(8,'2h'),(16,'4h')]:
    ev[f'g{H}']=ev.apply(lambda r:fade_ret(r,H),axis=1)
    print(f"\nHold {lbl}:")
    print(f"  {'tier':5s} {'n':>5s} {'gross':>8s} " + " ".join(f"{c}bps".rjust(8) for c in COSTS))
    for tl in ['HIGH','MID','LOW','ALL']:
        s=ev[f'g{H}'] if tl=='ALL' else ev[ev.tier==tl][f'g{H}']
        s=s.dropna(); gross=s.mean()*1e4
        row=" ".join(f"{gross-c:+8.1f}" for c in COSTS)
        print(f"  {tl:5s} {len(s):5d} {gross:+8.1f} {row}")

# ---- B. stop/target variant (path-dependent), HIGH+MID, 1h max hold ----
print("\n"+"="*84)
print("B. STOP/TARGET vs FIXED HOLD (HIGH+MID tiers, max hold 4h, 12bps cost)")
print("="*84)
def run_st(row,maxH,stop,target):
    close,_=paths[row['sym']]; i=row['i']
    if i+maxH>=len(close): return np.nan,0
    entry=close[i]; d=row['fade']
    for k in range(1,maxH+1):
        r=d*np.log(close[i+k]/entry)
        if r<=-stop: return -stop,k
        if r>=target: return target,k
    return d*np.log(close[i+maxH]/entry),maxH
hm=ev[ev.tier.isin(['HIGH','MID'])].copy()
COST_HM=0.0012
for stop,target,mh in [(None,None,4),(0.010,0.010,16),(0.015,0.015,16),(0.010,0.020,16),(0.020,0.010,16)]:
    if stop is None:
        r=hm.apply(lambda x:fade_ret(x,4),axis=1).dropna(); k=np.full(len(r),4)
        desc="fixed 1h hold"
    else:
        out=hm.apply(lambda x:run_st(x,mh,stop,target),axis=1)
        r=out.apply(lambda x:x[0]).dropna(); k=out.apply(lambda x:x[1])[r.index]
        desc=f"stop {stop*100:.1f}% / target {target*100:.1f}% (max 4h)"
    net=r-COST_HM
    sharpe=net.mean()/net.std()
    print(f"  {desc:34s} n={len(r):5d} gross={r.mean()*1e4:+6.1f}bps net={net.mean()*1e4:+6.1f}bps win={(net>0).mean()*100:4.1f}% avgbars={k.mean():4.1f} PT-sharpe={sharpe:+.3f}")

# ---- C. portfolio equity (HIGH+MID, 1h hold). Capital model: deploy 1 unit/day
#         split equally across that day's signals -> daily return = mean net trade return.
#         This exposes cross-sectional clustering (a market-wide breakout day = all fades move together). ----
print("\n"+"="*84)
print("C. PORTFOLIO EQUITY (HIGH+MID fade, 1h hold, 12bps cost; 1 unit/day equal-split across signals)")
print("="*84)
bt=hm.copy(); bt['net']=bt.apply(lambda r:fade_ret(r,4),axis=1)-COST_HM
bt=bt.dropna(subset=['net']); bt['day']=pd.to_datetime(bt['t'],unit='ms').dt.floor('D')
daily=bt.groupby('day')['net'].mean()             # equal-weight across the day's signals
alldays=pd.date_range(daily.index.min(),daily.index.max(),freq='D')
daily=daily.reindex(alldays,fill_value=0.0)
eq=daily.cumsum()
ann_ret=daily.mean()*365; ann_vol=daily.std()*np.sqrt(365)
sharpe=ann_ret/ann_vol if ann_vol>0 else np.nan
maxdd=(eq-eq.cummax()).min()
avg_sig=bt.groupby('day').size().mean()
print(f"  trading days: {len(daily)}   avg signals/day: {avg_sig:.1f}")
print(f"  final cum return: {eq.iloc[-1]*100:+.1f}%  over ~52d   (~{daily.mean()*100:+.3f}%/day)")
print(f"  annualized return: {ann_ret*100:+.0f}%   ann vol: {ann_vol*100:.0f}%   daily Sharpe(annualized): {sharpe:.2f}")
print(f"  max drawdown: {maxdd*100:.1f}%   positive days: {(daily>0).mean()*100:.0f}%")
print(f"  best day: {daily.max()*100:+.2f}%   worst day: {daily.min()*100:+.2f}%")
pd.DataFrame({'day':eq.index,'equity_cum':eq.values,'daily_ret':daily.values}).to_csv('backtest_equity.csv',index=False)

# ---- D. walk-forward: halves + biweekly ----
print("\n"+"="*84)
print("D. WALK-FORWARD STABILITY (HIGH+MID fade, 1h hold, gross bps per trade)")
print("="*84)
hm=hm.assign(g4=hm.apply(lambda x:fade_ret(x,4),axis=1)).dropna(subset=['g4'])
hm['dt']=pd.to_datetime(hm['t'],unit='ms')
mid=hm['dt'].min()+(hm['dt'].max()-hm['dt'].min())/2
for lbl,sub in [('first half',hm[hm.dt<mid]),('second half',hm[hm.dt>=mid])]:
    s=sub['g4']
    print(f"  {lbl:12s} n={len(s):5d} gross={s.mean()*1e4:+6.1f}bps net12={ (s.mean()-COST_HM)*1e4:+6.1f}bps win={(s>0).mean()*100:4.1f}%")
print("  biweekly buckets:")
for period,s in hm.set_index('dt')['g4'].groupby(pd.Grouper(freq='14D')):
    if len(s)<20: continue
    print(f"    {period.date()}  n={len(s):4d}  gross={s.mean()*1e4:+6.1f}bps  net12={ (s.mean()-COST_HM)*1e4:+6.1f}bps  win={(s>0).mean()*100:4.1f}%")
