import pandas as pd, numpy as np, time as _t
df=pd.read_csv('hyperliquid_15m_allperps.csv').sort_values(['symbol','open_time_ms']).reset_index(drop=True)
fund=pd.read_csv('hyperliquid_funding.csv').rename(columns={'time_ms':'open_time_ms'}).sort_values(['symbol','open_time_ms'])
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
VW=RW=96; COST=0.0011; NOTIONAL=100; LEV=2
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

def reclaim_exit(g,i,brk):
    c=g['close'].values; ph=g['ph'].values[i]; pl=g['pl'].values[i]
    for k in range(1,min(33,len(c)-1-i)+1):
        if brk==1 and c[i+k]<ph: return k,'reclaim'
        if brk==-1 and c[i+k]>pl: return k,'reclaim'
    return min(32,len(c)-1-i),'8h backstop'

# collect candidate signals (up-breakouts = short fades, easy to narrate)
cands=[]
for sym,g in big.groupby('symbol'):
    for i in range(len(g)):
        r=g.iloc[i]
        if r['vratio']<5 or r['brk']!=1 or r['tier']!='MID' or np.isnan(r['rv24']) or r['rv24']<thr: continue
        if pd.isna(r['fund8']) or r['brk']*np.sign(r['fund8'])!=1 or i+33>=len(g): continue
        k,why=reclaim_exit(g,i,1); d=-1
        fade=d*np.log(g['close'].values[i+k]/r['close'])-COST
        cands.append((sym,i,k,why,fade,uni.get(sym,0),r['vratio']))
cd=pd.DataFrame(cands,columns=['sym','i','k','why','fade','vol','vr'])
# pick a clean illustrative winner: reclaim exit, hold 4-16 bars, decent gain, recognizable (high dayvol)
pick=cd[(cd.why=='reclaim')&(cd.k.between(4,16))&(cd.fade.between(0.015,0.05))].sort_values('vol',ascending=False).iloc[0]
sym=pick['sym']; g=store[sym]; i=int(pick['i']); k=int(pick['k'])
r=g.iloc[i]; entry=r['close']; ph=r['ph']; pl=r['pl']; exitpx=g['close'].values[i+k]
def ts(ms): return _t.strftime('%Y-%m-%d %H:%M',_t.gmtime(ms/1000))
print(f"================ EXAMPLE TRADE: {sym} (MID tier) ================\n")
print("--- GATES (all must pass) ---")
print(f"  signal bar         : {ts(r['open_time_ms'])} UTC")
print(f"  prior 24h high     : {ph:.5f}   prior 24h low: {pl:.5f}")
print(f"  this bar close     : {entry:.5f}  -> closes ABOVE prior high  => UP-BREAKOUT (fade = SHORT)")
print(f"  volume spike       : {r['vratio']:.1f}x  (need >=5x trailing-24h median)  [PASS]")
print(f"  realized vol rv24  : {r['rv24']:.4f}  vs threshold {thr:.4f}  => high-vol  [PASS]")
print(f"  funding (8h avg)   : {r['fund8']*100:.4f}%  (positive = crowded longs; up-brk aligned)  [PASS]")
print(f"  liquidity tier     : MID  [tradeable]\n")
print("--- ENTRY ---")
print(f"  post a SELL (maker) at ~{entry:.5f}, {NOTIONAL} notional @ {LEV}x = ${NOTIONAL/LEV:.0f} margin\n")
print("--- PRICE PATH (15m closes) ---")
for j in range(i-1,i+k+1):
    b=g.iloc[j]; tag=''
    if j==i: tag='  <== SIGNAL/ENTRY (short here)'
    elif j==i+k: tag=f'  <== EXIT ({pick["why"]}): close back below prior high {ph:.5f}'
    inout='ABOVE range' if b['close']>ph else ('below range' if b['close']<pl else 'in range')
    print(f"  {ts(b['open_time_ms'])}  close={b['close']:.5f}  ({inout}){tag}")
gain=-(np.log(exitpx/entry))  # short fade raw return
print(f"\n--- RESULT ---")
print(f"  held {k} bars = {k*0.25:.1f}h")
print(f"  entry {entry:.5f} -> exit {exitpx:.5f}  (price fell {(1-exitpx/entry)*100:.2f}%)")
print(f"  fade gross = {gain*100:+.2f}%   net (after 11bps) = {(gain-COST)*100:+.2f}%")
print(f"  P&L on {NOTIONAL} notional: ${NOTIONAL*(gain-COST):+.2f}  (on ${NOTIONAL/LEV:.0f} margin = {(gain-COST)*LEV*100:+.1f}% return on margin)")
