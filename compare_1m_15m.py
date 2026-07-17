import pandas as pd, numpy as np
# Raw fade signal (5x vol spike + 24h range breakout, reclaim exit + 8h backstop) on the SAME
# 10 names & SAME 48h window, detected on 1m vs 15m bars. Tests: does 1m produce reversion profit?
COST=0.0011
def run(path,barmin,label):
    d=pd.read_csv(path).sort_values(['symbol','open_time_ms'])
    tmax=d['open_time_ms'].max(); d=d[d['open_time_ms']>=tmax-48*3600*1000]   # last 48h
    perday=24*60/barmin; W=int(24*60/barmin); BACK=int(8*60/barmin)           # 24h window, 8h backstop in bars
    rows=[]
    for sym,g in d.groupby('symbol'):
        g=g.reset_index(drop=True); c=g['close'].values; vol=g['volume'].values
        if len(g)<W+BACK+5: continue
        med=pd.Series(vol).shift(1).rolling(W).median().values
        vr=vol/med
        ph=pd.Series(g['high']).shift(1).rolling(W).max().values
        pl=pd.Series(g['low']).shift(1).rolling(W).min().values
        for i in range(len(g)):
            if np.isnan(vr[i]) or vr[i]<5: continue
            brk=1 if c[i]>ph[i] else (-1 if c[i]<pl[i] else 0)
            if brk==0 or i+BACK>=len(g): continue
            d_=-brk; e=c[i]; k=BACK
            for kk in range(1,BACK+1):
                if (brk==1 and c[i+kk]<ph[i]) or (brk==-1 and c[i+kk]>pl[i]): k=kk; break
            rows.append((d_*np.log(c[i+k]/e), k*barmin/60.0, abs(np.log(c[i+k]/e))))
    r=pd.DataFrame(rows,columns=['fade','holdh','absmove'])
    net=(r['fade']-COST)
    print(f"{label:6s}: signals={len(r):5d} ({len(r)/2:.0f}/day/coin-set)  net={net.mean()*1e4:+6.1f}bps  "
          f"win={(net>0).mean()*100:4.1f}%  avgHold={r['holdh'].mean():4.1f}h  grossMove={r['absmove'].mean()*100:4.2f}%  Sharpe={net.mean()/net.std():+.3f}")

print("Same 10 names, last 48h, RAW fade (5x spike+breakout, reclaim/8h exit):\n")
run('hyperliquid_1m_48h.csv',1,'1m')
run('hyperliquid_15m_60d.csv',15,'15m')
