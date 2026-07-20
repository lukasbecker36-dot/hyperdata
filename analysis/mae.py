#!/usr/bin/env python3
"""Maximum Adverse Excursion (MAE) -> safe leverage per $100 notional.

The strategy holds through the overshoot with NO stop. So the binding leverage
constraint is: a position must survive its worst intratrade adverse move without
being liquidated, otherwise you eat the loss AND forfeit the reversion the
strategy exists to capture.

For each 8-month signal, walk the 8h hold intrabar and record MAE =
max adverse move vs entry (highs for fade-shorts, lows for fade-longs).
Liquidation (isolated) happens ~ when adverse move >= 1/L - mm.
So leverage that survives an adverse move M is L <= 1/(M + mm).
"""
import wide_stop as w

MAXH = w.MAXH
maes = []          # (mae_frac, final_ret_net, reverted_bool)
base, _ = w.simulate(None, None)
for (sym, i, brk), fin in zip(w.signals, base):
    t, hi, lo, c, v, ret = w.per_sym[sym]
    d = -brk; e = c[i]; mae = 0.0
    for k in range(1, MAXH+1):
        if d == -1:                       # fade short: up moves hurt
            adv = (hi[i+k] - e) / e
        else:                             # fade long: down moves hurt
            adv = (e - lo[i+k]) / e
        if adv > mae: mae = adv
    maes.append((mae, fin, fin > 0))

maes.sort()
n = len(maes)
def pct(p): return maes[min(n-1, int(p*n))][0]
mvals = [m[0] for m in maes]

print(f"n={n} trades   MAE = worst intratrade adverse move over the 8h hold\n")
print("MAE distribution (fraction of notional moved AGAINST the position):")
for p, lbl in [(0.50,'median'),(0.90,'p90'),(0.95,'p95'),(0.99,'p99'),(0.999,'p99.9')]:
    print(f"   {lbl:>6s}: {pct(p)*100:5.1f}%")
print(f"   {'max':>6s}: {max(mvals)*100:5.1f}%")

# how often does a big-MAE trade still end up a winner? (why you must survive it)
big = [m for m in maes if m[0] >= 0.10]
if big:
    rev = sum(1 for m in big if m[2]) / len(big) * 100
    print(f"\n   trades with MAE>=10%: {len(big)}  ({rev:.0f}% STILL closed positive after reverting)")
big2 = [m for m in maes if m[0] >= 0.15]
if big2:
    rev2 = sum(1 for m in big2 if m[2]) / len(big2) * 100
    print(f"   trades with MAE>=15%: {len(big2)}  ({rev2:.0f}% still closed positive)")

# leverage that survives X% of trades, for a couple maintenance-margin assumptions
print(f"\nMax isolated leverage that AVOIDS liquidation on X% of trades:")
print(f"  (L = 1/(MAE_pct + mm);  mm = maintenance-margin fraction)")
print(f"  {'survive':>8s} | {'MAE_pct':>7s} | {'L @mm=0':>8s} {'L @mm=5%':>9s} {'L @mm=10%':>9s}")
for p, lbl in [(0.95,'95%'),(0.99,'99%'),(0.999,'99.9%'),(1.0,'100%(worst)')]:
    M = max(mvals) if p==1.0 else pct(p)
    row = f"  {lbl:>8s} | {M*100:6.1f}% |"
    for mm in (0.0,0.05,0.10):
        row += f" {1.0/(M+mm):8.1f}x"
    print(row)

# fraction of trades liquidated at candidate leverages (isolated, mm=5%)
print(f"\nAt a fixed leverage, what fraction of trades get liquidated (mm=5%)?")
print(f"  and of the equity given up, how much would have reverted to a win?")
for L in (2,3,5,10,20):
    liq_move = 1.0/L - 0.05
    liq = [m for m in maes if m[0] >= liq_move]
    if not liq:
        print(f"   {L:2d}x: liq@{liq_move*100:.0f}% adverse -> 0 liquidations")
        continue
    would_win = sum(1 for m in liq if m[2]) / len(liq) * 100
    print(f"   {L:2d}x: liq@{liq_move*100:4.1f}% adverse -> {len(liq):4d}/{n} trades liquidated "
          f"({len(liq)/n*100:4.1f}%);  {would_win:3.0f}% of them would've reverted to a WIN")
