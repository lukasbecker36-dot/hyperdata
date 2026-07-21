#!/usr/bin/env python3
"""Map MAX_POSITIONS (the bot's existing knob) to $ cluster-tail risk.

Flat $100 notional. Process signals in entry order; if >= cap positions already
open, skip the new entry (exactly what paper_bot.py does at MAX_POSITIONS).
Report total return, maxDD, worst-48h cluster, peak deployed capital.
This is the robust tail lever: it caps how much correlated exposure can stack up,
without distorting the signal (unlike vol-scaling) and without selling the
overshoot (unlike a stop).
"""
import bisect
import wide_stop as w

MAXH = w.MAXH; NOT = 100.0
base, _ = w.simulate(None, None)
trades = []
for (sym, i, brk), ret in zip(w.signals, base):
    t = w.per_sym[sym][0]
    trades.append({'entry': t[i], 'exit': t[i+MAXH], 'ret': ret})
trades.sort(key=lambda x: x['entry'])

def run(cap):
    open_exits = []   # sorted exit times of open positions
    kept = []
    for tr in trades:
        # expire
        while open_exits and open_exits[0] <= tr['entry']:
            open_exits.pop(0)
        if cap is not None and len(open_exits) >= cap:
            continue
        kept.append(tr)
        bisect.insort(open_exits, tr['exit'])
    # metrics on kept (flat $100)
    ev = sorted(kept, key=lambda x: x['exit'])
    pnls = [NOT*tr['ret'] for tr in ev]; exits = [tr['exit'] for tr in ev]
    cum=peak=mdd=0.0
    for p in pnls:
        cum+=p; peak=max(peak,cum); mdd=min(mdd,cum-peak)
    pre=[0.0]
    for p in pnls: pre.append(pre[-1]+p)
    w48=0.0; W=48*3600*1000
    for a in range(len(exits)):
        b=bisect.bisect_right(exits, exits[a]+W); w48=min(w48, pre[b]-pre[a])
    # peak concurrency of kept
    evs=[]
    for tr in kept:
        evs.append((tr['entry'],1)); evs.append((tr['exit'],-1))
    evs.sort(key=lambda x:(x[0],x[1])); cn=mx=0
    for _,d in evs: cn+=d; mx=max(mx,cn)
    return dict(n=len(kept), total=cum, mdd=mdd, w48=w48, peak=mx, dep=mx*NOT)

print(f"{'MAX_POS':>8s} | {'trades':>6s} {'total $':>8s} {'maxDD $':>8s} {'worst48h $':>10s} {'peakPos':>7s} {'peak$dep':>8s} {'ret/|DD|':>8s}")
for cap in (None, 40, 30, 20, 15, 10, 5):
    m=run(cap); lbl='none(∞)' if cap is None else str(cap)
    print(f"{lbl:>8s} | {m['n']:6d} {m['total']:+8.0f} {m['mdd']:8.0f} {m['w48']:10.0f} "
          f"{m['peak']:7d} {m['dep']:8.0f} {m['total']/abs(m['mdd']):8.2f}")
