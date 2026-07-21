#!/usr/bin/env python3
"""Prototype a TREND GATE on the 8-month backtest signal set (stacked_trades.csv).

Failure mode seen live: fading a breakout that ALIGNS with a strong prevailing
trend -> the breakout keeps running -> fade rides to the backstop for a big loss.

Gate idea: at signal time, measure trailing return (momentum). Classify each
breakout as:
  ALIGNED  : breakout direction == sign(trailing trend)  (momentum breakout)
  COUNTER  : breakout direction against the trailing trend (exhaustion breakout)
Hypothesis: ALIGNED breakouts are the ones that get run over; COUNTER breakouts
(exhaustion) are what actually reverts. So skip the strongly-aligned ones.

Data: stacked_trades.csv (dt,symbol,brk,fade) joined to 1h closes for the
trailing-return lookup. fade = per-trade return of the fade; cost 11 bps.
"""
import csv, math
from datetime import datetime, timezone

COST = 0.0011

# ---- load signals ----
sig=[]
with open('../stacked_trades.csv') as f:
    for r in csv.DictReader(f):
        ms=int(datetime.strptime(r['dt'],"%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()*1000)
        sig.append({'ms':ms,'sym':r['symbol'],'brk':int(r['brk']),'fade':float(r['fade']),'dt':r['dt']})
syms=set(s['sym'] for s in sig)

# ---- load 1h closes for needed symbols ----
series={}  # sym -> list of (ms, close) sorted
with open('../hyperliquid_1h_history.csv') as f:
    for r in csv.DictReader(f):
        sym=r['symbol']
        if sym not in syms: continue
        series.setdefault(sym,[]).append((int(r['open_time_ms']),float(r['close'])))
for sym in series: series[sym].sort()

def close_at(sym, ms):
    """close of the bar whose open_time == ms (exact), else None."""
    arr=series.get(sym)
    if not arr: return None,None
    # binary search for ms
    lo,hi=0,len(arr)-1; idx=None
    while lo<=hi:
        m=(lo+hi)//2
        if arr[m][0]==ms: idx=m; break
        if arr[m][0]<ms: lo=m+1
        else: hi=m-1
    if idx is None: return None,None
    return idx,arr

def trail_ret(sym, ms, hours):
    idx,arr=close_at(sym,ms)
    if idx is None or idx-hours<0: return None
    c0=arr[idx-hours][1]; c1=arr[idx][1]
    if c0<=0: return None
    return math.log(c1/c0)

# ---- classify each signal by trend alignment over several lookbacks ----
for s in sig:
    for h in (6,12,24):
        tr=trail_ret(s['sym'], s['ms'], h)
        s[f'tr{h}']=tr
        s[f'al{h}']= None if tr is None else (1 if s['brk']*(1 if tr>0 else -1)==1 else -1)

def stats(rows):
    n=len(rows)
    if n==0: return "n=0"
    f=[r['fade'] for r in rows]; net=[x-COST for x in f]
    m=sum(net)/n
    sd=(sum((x-m)**2 for x in net)/n)**0.5
    wins=sum(1 for x in net if x>0)/n*100
    worst=min(f); p05=sorted(f)[max(0,int(0.05*n))]
    tot=sum(net)
    return (f"n={n:5d} net={m*1e4:+6.1f}bps win={wins:4.1f}% "
            f"tot={tot*100:+6.1f}% worst={worst*100:+6.1f}% p05={p05*100:+5.1f}%")

base=[s for s in sig]
print("BASELINE (all stacked signals):")
print("  ", stats(base))

for h in (6,12,24):
    al=[s for s in sig if s.get(f'al{h}')==1]
    co=[s for s in sig if s.get(f'al{h}')==-1]
    print(f"\nTrailing {h}h trend split:")
    print(f"  ALIGNED (fade a momentum breakout): {stats(al)}")
    print(f"  COUNTER (fade an exhaustion break): {stats(co)}")

# ---- the GATE: drop signals aligned with a STRONG trend (|trail_ret| above pct) ----
print("\n"+"="*72)
print("TREND GATE: skip signals where breakout aligns with trend AND |trail_ret| is large")
print("(kept = counter-trend fades + weak-trend aligned fades)")
print("="*72)
for h in (12,24):
    trs=sorted(abs(s[f'tr{h}']) for s in sig if s.get(f'tr{h}') is not None)
    for pct in (0.5,0.6,0.7,0.8):
        thr=trs[int(pct*len(trs))]
        kept=[s for s in sig if not (s.get(f'al{h}')==1 and s.get(f'tr{h}') is not None and abs(s[f'tr{h}'])>=thr)]
        dropped=[s for s in sig if s not in kept]
        print(f"\n  lookback={h}h  drop aligned & |tr|>= {pct:.0%}-pct ({thr*100:.1f}%):")
        print(f"    KEPT    {stats(kept)}")
        print(f"    DROPPED {stats(dropped)}   <- want this net negative / tail-heavy")
