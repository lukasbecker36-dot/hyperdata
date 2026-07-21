#!/usr/bin/env python3
"""Position-sizing tail-risk quantifier.

Question: what per-trade notional keeps the worst *correlated-cluster* drawdown
under a chosen $ limit — and does vol-scaled sizing let you run bigger for the
same tail?

Uses the same 8-month stacked signal set as the stop sweep (no stop, 8h hold).
Each trade: entry_ms, exit_ms=entry+8h, net return (incl 11bps cost), rv24.
Dollar P&L realized at exit. Measures, at a given sizing:
  - worst single trade $
  - worst realized loss in any rolling 24h / 48h window  (the 'cluster' metric)
  - max drawdown of the realized equity path ($)
  - peak simultaneous open positions -> peak deployed notional
Everything scales linearly with notional, so we invert to hit a $ DD target.
Compares FLAT $100 vs VOL-SCALED (mean $100, size ~ 1/rv24, cap 4x) — matches sizing.py.
"""
import math, bisect
import wide_stop as w

MAXH = w.MAXH
base, _ = w.simulate(None, None)         # baseline hold returns, aligned to w.signals

# ---- build trade records ----
trades = []
for (sym, i, brk), ret in zip(w.signals, base):
    t = w.per_sym[sym][0]; retser = w.per_sym[sym][5]
    rv = w.sample_std(retser[i-23:i+1])
    trades.append({'sym': sym, 'entry': t[i], 'exit': t[i+MAXH], 'ret': ret, 'rv': rv})

# ---- vol-scaled weights (mean 1, cap 4x), matches sizing.py ----
inv = [1.0/tr['rv'] for tr in trades]
mean_inv = sum(inv)/len(inv)
for tr, x in zip(trades, inv):
    tr['w'] = min(x/mean_inv, 4.0)
# note: clipping lowers mean slightly -> vol-scaled deploys marginally LESS avg capital

def metrics(notional, weighted):
    for tr in trades:
        sz = notional * (tr['w'] if weighted else 1.0)
        tr['pnl'] = sz * tr['ret']
    ev = sorted(trades, key=lambda x: x['exit'])
    exits = [tr['exit'] for tr in ev]; pnls = [tr['pnl'] for tr in ev]
    # equity path + max drawdown
    cum = 0.0; peak = 0.0; mdd = 0.0
    for p in pnls:
        cum += p; peak = max(peak, cum); mdd = min(mdd, cum - peak)
    total = cum
    worst_single = min(pnls)
    # worst rolling-window realized loss (sum of pnl of trades exiting within W)
    def worst_window(W_ms):
        pre = [0.0]
        for p in pnls: pre.append(pre[-1] + p)
        worst = 0.0
        for a in range(len(exits)):
            b = bisect.bisect_right(exits, exits[a] + W_ms)   # trades with exit in [a, a+W]
            worst = min(worst, pre[b] - pre[a])
        return worst
    w24 = worst_window(24*3600*1000); w48 = worst_window(48*3600*1000)
    # peak concurrency + deployed notional
    events = []
    for tr in trades:
        sz = notional * (tr['w'] if weighted else 1.0)
        events.append((tr['entry'], 1, sz)); events.append((tr['exit'], -1, -sz))
    events.sort(key=lambda x: (x[0], x[1]))
    cn = 0; mx = 0; dep = 0.0; mxdep = 0.0
    for _, d, sz in events:
        cn += d; dep += sz; mx = max(mx, cn); mxdep = max(mxdep, dep)
    return dict(total=total, mdd=mdd, worst_single=worst_single, w24=w24, w48=w48,
                peak_pos=mx, peak_dep=mxdep)

print(f"signals: {len(trades)}   period: 8 months (Dec 2025 - Jul 2026)\n")
print(f"{'sizing':>18s} | {'total $':>9s} {'maxDD $':>9s} {'worst48h $':>10s} {'worst24h $':>10s} {'worst1 $':>9s} {'peakPos':>7s} {'peak$dep':>9s}")
for label, wt in [("FLAT $100", False), ("VOL-SCALED ~$100", True)]:
    m = metrics(100.0, wt)
    print(f"{label:>18s} | {m['total']:+9.0f} {m['mdd']:9.0f} {m['w48']:10.0f} {m['w24']:10.0f} "
          f"{m['worst_single']:9.0f} {m['peak_pos']:7d} {m['peak_dep']:9.0f}")

# efficiency: DD per $ of total return
mf = metrics(100.0, False); mv = metrics(100.0, True)
print(f"\nrisk-efficiency (8mo, $100 avg notional):")
print(f"  FLAT:       return ${mf['total']:+.0f}  maxDD ${mf['mdd']:.0f}  ->  return/|DD| = {mf['total']/abs(mf['mdd']):.2f}")
print(f"  VOL-SCALED: return ${mv['total']:+.0f}  maxDD ${mv['mdd']:.0f}  ->  return/|DD| = {mv['total']/abs(mv['mdd']):.2f}")
print(f"  vol-scaling cuts worst single trade from ${abs(mf['worst_single']):.0f} to ${abs(mv['worst_single']):.0f}, "
      f"maxDD from ${abs(mf['mdd']):.0f} to ${abs(mv['mdd']):.0f}")

# ---- invert: notional to keep worst-48h cluster (and maxDD) under a $ limit ----
print(f"\nSIZING TO A RISK BUDGET (linear scaling from the $100 run):")
print(f"  {'$ limit on':>12s} | {'FLAT notional':>14s} | {'VOL-SCALED avg notional':>24s}")
print(f"  {'worst 48h':>12s} |  (per-trade)  |      (per-trade)")
for lim in (200, 500, 1000, 2000):
    fl = 100.0 * lim/abs(mf['w48'])
    vl = 100.0 * lim/abs(mv['w48'])
    print(f"  {'$'+str(lim):>12s} | {'$'+format(fl,'.0f'):>14s} | {'$'+format(vl,'.0f'):>24s}")
print(f"\n  (same table keyed to maxDD instead of worst-48h:)")
for lim in (200, 500, 1000, 2000):
    fl = 100.0 * lim/abs(mf['mdd']); vl = 100.0 * lim/abs(mv['mdd'])
    print(f"  maxDD<=${lim:<5d} | FLAT ${fl:>6.0f}/trade | VOL-SCALED ${vl:>6.0f}/trade avg")
