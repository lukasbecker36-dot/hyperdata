#!/usr/bin/env python3
"""Should we raise the slot cap (currently 5) to capture the profitable clustered bursts?

cluster_entries.py showed the crowded 4+ entries carry the edge; a 5-slot cap skips exactly those.
Here we sweep the concurrency cap two ways and price the trade-off in P&L, drawdown, and the capital
(margin) needed at peak concurrency, at the live sizing ($25 base notional, 3x isolated => ~$8.3 margin
per slot). We report both a TOTAL-position cap and a PER-SIDE (per-leg) cap, since the live cap is
per leg. For each cap: kept trades, total P&L, maxDD, ret/DD, peak concurrent, peak margin, and the
share of the clustered (trailing same-side 4+) edge that survives the cap.
Pure stdlib. 1h panel. Run from analysis/.
"""
import bisect, sys, os
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

MAXH = w.MAXH; per = w.per_sym
BASE = 25.0; LEV = 3.0; MARGIN = BASE/LEV       # ~$8.33 margin per slot at live sizing
base_ret, _ = w.simulate(None, None)

trades = []
for (sym, i, brk), ret in zip(w.signals, base_ret):
    t = per[sym][0]
    trades.append(dict(entry=t[i], exit=t[i+MAXH], side=-brk, ret=ret, idx=len(trades)))
trades.sort(key=lambda x: x['entry'])
ent = [x['entry'] for x in trades]; H = 3600*1000

# tag each trade's trailing same-side crowd (for "clustered edge captured")
def crowd(k):
    t0 = trades[k]['entry']; s = trades[k]['side']; lo = bisect.bisect_left(ent, t0-H)
    return sum(1 for j in range(lo, k+1) if trades[j]['side'] == s)
for k in range(len(trades)): trades[k]['clu'] = crowd(k)
CLU_TOTAL = sum(t['ret'] for t in trades if t['clu'] >= 4)   # total clustered-4+ edge available

def run(cap, per_side):
    open_pos = []   # (exit_ms, side)
    kept = []
    for tr in trades:
        open_pos = [(e, sd) for (e, sd) in open_pos if e > tr['entry']]
        if per_side:
            used = sum(1 for (_, sd) in open_pos if sd == tr['side'])
        else:
            used = len(open_pos)
        if cap is not None and used >= cap:
            continue
        kept.append(tr); open_pos.append((tr['exit'], tr['side']))
    # metrics
    ev = sorted(kept, key=lambda x: x['exit'])
    cum = peak = mdd = 0.0; pre = [0.0]
    for tr in ev:
        cum += BASE*tr['ret']; peak = max(peak, cum); mdd = min(mdd, cum-peak); pre.append(cum)
    # peak concurrency (total, and per-side max)
    evs = []
    for tr in kept:
        evs.append((tr['entry'], 1, tr['side'])); evs.append((tr['exit'], -1, tr['side']))
    evs.sort(key=lambda x: (x[0], x[1]))
    cn = mx = 0; cs = {1: 0, -1: 0}; mxs = 0
    for _, d, sd in evs:
        cn += d; mx = max(mx, cn); cs[sd] += d; mxs = max(mxs, cs[sd])
    clu_kept = sum(tr['ret'] for tr in kept if tr['clu'] >= 4)
    return dict(n=len(kept), cum=cum, mdd=mdd, peak=mx, peak_side=mxs,
                retdd=cum/abs(mdd) if mdd < 0 else float('inf'),
                clu_cap=clu_kept/CLU_TOTAL*100)

for per_side in (False, True):
    kind = "PER-SIDE (per-leg) cap" if per_side else "TOTAL-position cap"
    caps = [5, 6, 7, 8, 10, 12, 15, 20, None]
    print(f"\n=== {kind} === (base ${BASE:.0f}/slot, {LEV:.0f}x => ${MARGIN:.1f} margin/slot)")
    print(f"  {'cap':>5} {'trades':>6} {'total$':>8} {'maxDD$':>8} {'ret/DD':>7} {'peakTot':>7} "
          f"{'peakSide':>8} {'peakMargin$':>11} {'clu4+ kept%':>11}")
    for cap in caps:
        m = run(cap, per_side)
        lbl = '∞' if cap is None else str(cap)
        pm = m['peak']*MARGIN
        print(f"  {lbl:>5} {m['n']:>6} {m['cum']:>+8.0f} {m['mdd']:>8.0f} {m['retdd']:>7.2f} "
              f"{m['peak']:>7} {m['peak_side']:>8} {pm:>11.0f} {m['clu_cap']:>10.0f}%")
print("\ntotal$/maxDD$ at flat $25/trade (ats sizing would scale ~1x avg, up to 3x/name).")
print("peakMargin$ = peak concurrent positions x $8.3 margin = capital you must have free at the worst burst.")
