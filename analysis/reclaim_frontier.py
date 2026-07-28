#!/usr/bin/env python3
"""Live-accurate slot/capital frontier: ATS sizing + reclaim exits + isolated liquidation.

slot_sweep.py used flat $25 and hold-to-backstop, which overstates both holding time (=> concurrency
=> peak margin) and understates size dispersion. This replays the ACTUAL live rules:
  - size:   notional = $25 * clamp(ats_ratio/2, 0.5, 3.0)   (ats_ratio = (v/n) vs trailing median v/n)
  - exit:   reclaim (close back inside the prior-24h range) OR 8h backstop; intrabar isolated
            liquidation at 1/LEV - MAINT = 28.3% adverse (3x).
Then sweep the concurrency cap (total & per-side) and report P&L, maxDD, ret/DD, peak concurrent, and
peak MARGIN in $ (capital you must keep free at the worst burst), plus avg hold and exit-reason mix so
you can see how much reclaim shortens holds vs the 8h assumption. Pure stdlib. 1h panel. Run from analysis/.
"""
import bisect, csv, math, sys, os
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

MAXH = w.MAXH; COST = w.COST; per = w.per_sym
BASE = 25.0; LEV = 3.0; MAINT = 0.05; LIQ = 1.0/LEV - MAINT
SIZE_REF, SIZE_MIN, SIZE_MAX, WIN = 2.0, 0.5, 3.0, 24
H = 3600*1000

# num_trades per (sym, time_ms)
ntr = {}
with open("../hyperliquid_1h_history.csv") as f:
    for r in csv.DictReader(f):
        ntr[(r['symbol'], int(r['open_time_ms']))] = float(r['num_trades'] or 0)

def ats_mult(sym, t, v, i):
    ni = ntr.get((sym, t[i]), 0)
    if ni <= 0: return 1.0
    pa = sorted(v[j]/ntr[(sym, t[j])] for j in range(i-WIN, i)
                if ntr.get((sym, t[j]), 0) > 0)
    if len(pa) < WIN//2 or pa[len(pa)//2] <= 0: return 1.0
    ar = (v[i]/ni) / pa[len(pa)//2]
    return min(SIZE_MAX, max(SIZE_MIN, ar/SIZE_REF))

# build trades with live rules
trades = []
for sym, i, brk in w.signals:
    t, hi, lo, c, v, ret = per[sym]
    d = -brk; e = c[i]
    prior_h = max(hi[i-WIN:i]); prior_l = min(lo[i-WIN:i])
    mult = ats_mult(sym, t, v, i); notional = BASE*mult
    reason = 'backstop'; exit_k = MAXH; ex_px = c[i+MAXH]
    for k in range(1, MAXH+1):
        Hk, Lk, Ck = hi[i+k], lo[i+k], c[i+k]
        if d < 0 and Hk >= e*(1+LIQ):   reason, exit_k, ex_px = 'liq', k, e*(1+LIQ); break
        if d > 0 and Lk <= e*(1-LIQ):   reason, exit_k, ex_px = 'liq', k, e*(1-LIQ); break
        if d < 0 and Ck < prior_h:      reason, exit_k, ex_px = 'reclaim', k, Ck; break
        if d > 0 and Ck > prior_l:      reason, exit_k, ex_px = 'reclaim', k, Ck; break
    r = -LIQ - COST if reason == 'liq' else d*math.log(ex_px/e) - COST
    trades.append(dict(entry=t[i], exit=t[i+exit_k], side=d, ret=r, notion=notional,
                       margin=notional/LEV, pnl=notional*r, hold=exit_k, reason=reason))
trades.sort(key=lambda x: x['entry'])
ent = [x['entry'] for x in trades]

for k in range(len(trades)):
    t0 = trades[k]['entry']; s = trades[k]['side']; lo = bisect.bisect_left(ent, t0-H)
    trades[k]['clu'] = sum(1 for j in range(lo, k+1) if trades[j]['side'] == s)
CLU_TOTAL = sum(x['pnl'] for x in trades if x['clu'] >= 4)

n = len(trades); avg_hold = sum(x['hold'] for x in trades)/n
rc = sum(1 for x in trades if x['reason'] == 'reclaim'); bs = sum(1 for x in trades if x['reason'] == 'backstop')
lq = sum(1 for x in trades if x['reason'] == 'liq')
print(f"{n} trades | live rules: ats-sized + reclaim + liq | avg notional ${sum(x['notion'] for x in trades)/n:.1f}")
print(f"exit mix: reclaim {rc/n*100:.0f}%  backstop {bs/n*100:.0f}%  liq {lq/n*100:.1f}%  | avg hold {avg_hold:.1f}h "
      f"(vs 8h assumed) | uncapped $P&L {sum(x['pnl'] for x in trades):+.0f}\n")

def run(cap, per_side):
    open_pos = []; kept = []
    for tr in trades:
        open_pos = [(e, sd, mg) for (e, sd, mg) in open_pos if e > tr['entry']]
        used = (sum(1 for (_, sd, _) in open_pos if sd == tr['side']) if per_side else len(open_pos))
        if cap is not None and used >= cap: continue
        kept.append(tr); open_pos.append((tr['exit'], tr['side'], tr['margin']))
    ev = sorted(kept, key=lambda x: x['exit'])
    cum = peak = mdd = 0.0
    for tr in ev:
        cum += tr['pnl']; peak = max(peak, cum); mdd = min(mdd, cum-peak)
    # event sweep: peak count + peak margin ($), tracking real per-name margin
    evs = []
    for tr in kept:
        evs.append((tr['entry'], 1, tr['side'], tr['margin'])); evs.append((tr['exit'], -1, tr['side'], tr['margin']))
    evs.sort(key=lambda x: (x[0], x[1]))
    cn = mx = 0; mg = pkmg = 0.0
    for _, dd, sd, m in evs:
        cn += dd; mx = max(mx, cn); mg += dd*m; pkmg = max(pkmg, mg)
    clu_kept = sum(tr['pnl'] for tr in kept if tr['clu'] >= 4)
    return dict(n=len(kept), cum=cum, mdd=mdd, peak=mx, pkmg=pkmg,
                retdd=cum/abs(mdd) if mdd < 0 else float('inf'),
                clu=clu_kept/CLU_TOTAL*100 if CLU_TOTAL else 0)

for per_side in (False, True):
    kind = "PER-SIDE (per-leg) cap" if per_side else "TOTAL-position cap"
    print(f"=== {kind} ===")
    print(f"  {'cap':>4} {'trades':>6} {'P&L$':>7} {'maxDD$':>7} {'ret/DD':>7} {'peakPos':>7} "
          f"{'peakMargin$':>11} {'clu4+ kept%':>11}")
    for cap in (5, 6, 7, 8, 10, 12, 15, 20, None):
        m = run(cap, per_side); lbl = '∞' if cap is None else str(cap)
        print(f"  {lbl:>4} {m['n']:>6} {m['cum']:>+7.0f} {m['mdd']:>7.0f} {m['retdd']:>7.2f} "
              f"{m['peak']:>7} {m['pkmg']:>11.0f} {m['clu']:>10.0f}%")
    print()
print("peakMargin$ = capital that must be free at the worst simultaneous burst (real per-name margins).")
print("At 3x, a name's margin = notional/3 (=$8.3 base, up to $25 for an ats-3x name).")
