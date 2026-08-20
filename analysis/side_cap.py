#!/usr/bin/env python3
"""Sizing the per-side cap against DRAWDOWN, not mean bps. Written after 2026-08-19.

What happened: two consecutive 15m bars opened 5 then 7 SHORT positions -- twelve shorts
in thirty minutes, $557 of gross. The book peaked at 21 positions of which 20 were SHORT,
$976 gross against $383 of collateral. A market-wide rally then ran every one of them
over: -$42.59 on the day, 56% backstop rate, and the drawdown took cum from +$5.68 to
-$38.04.

None of the three caps stopped it, and each failed differently:
  --max-per-side 20   bound at EXACTLY 20 shorts. It worked as specified and the
                      specification was wrong.
  --max-gross 2000    peak was $976. Never came close.
  --daily-loss-limit  realised P&L crossed -$15 at 23:16, by which point 17 positions
                      were already open. It blocks new ENTRIES; it cannot close existing
                      ones, so it fired eight hours after the exposure was taken.

The deeper error is in how the cap was justified. cluster_entries.py, cited in the unit
file, found clustered entries earn more per trade, and re-running it on the event set
confirms that strongly: 6+ open earns +59.2bps against -33.3 for isolated, t=+7.50. That
measurement is correct and irrelevant to this failure. It is a per-TRADE mean. Twenty
simultaneous shorts during a market-wide move are not twenty independent draws from that
mean, they are one bet on the market reverting, sized twenty times. Per-trade edge and
portfolio variance are different questions and the cap was set from the wrong one.

So this measures the cap the right way: simulate the book at each per-side limit and score
return/|maxDD|, which is the quantity a correlated burst actually damages.

  python3 analysis/side_cap.py
"""
import math
import numpy as np
import pandas as pd

exec(open("analysis/config_forecast.py").read().split("# ---- book simulation")[0])

SPOT = 383.0


def run(max_side, max_bar=99, max_pos=40, max_gross=2000.0):
    """Book simulation. max_bar caps entries opened in one 15m bar, which is the direct
    brake on a burst; max_side caps standing directional exposure."""
    op, took, ser = [], [], []
    dp, cd = 0.0, None
    bar_count, cur_bar = 0, None
    for r in ev.itertuples():
        bar = r.t - (r.t % 900000)
        if bar != cur_bar:
            cur_bar, bar_count = bar, 0
        op = [q for q in op if q[0] > r.t]
        d = pd.to_datetime(r.t, unit="ms").date()
        if d != cd:
            cd, dp = d, 0.0
        same = sum(1 for q in op if q[1] == r.dirn)
        gross = sum(q[2] for q in op)
        if (len(op) >= max_pos or same >= max_side or bar_count >= max_bar
                or gross + r.ntl > max_gross or dp <= -DAILY_LOSS):
            continue
        pnl = r.ntl * r.net / 1e4
        dp += pnl
        bar_count += 1
        took.append(dict(t=r.t, pnl=pnl, net=r.net, ntl=r.ntl))
        op.append((r.t + r.bars * 900000, r.dirn, r.ntl, r.margin))
        ser.append((sum(q[2] for q in op), sum(1 for q in op if q[1] == r.dirn)))
    T = pd.DataFrame(took)
    if T.empty:
        return None
    cum = np.cumsum(T.pnl.values)
    dd = float(np.min(cum - np.maximum.accumulate(cum)))
    dayp = pd.Series(T.pnl.values,
                     index=pd.to_datetime(T.t.values, unit="ms")).resample("D").sum()
    S = pd.DataFrame(ser, columns=["gross", "same"])
    return dict(n=len(T), bps=T.net.mean(), total=T.pnl.sum(), dd=dd,
                retdd=T.pnl.sum() / abs(dd) if dd < 0 else np.nan,
                sharpe=dayp.mean() / dayp.std() * math.sqrt(365) if dayp.std() else np.nan,
                worst_day=dayp.min(), peak_gross=S.gross.max(), peak_same=int(S["same"].max()))


print(f"\n{'='*86}")
print("### PER-SIDE CAP (current = 20)")
print("=" * 86)
print(f"  {'cap':>5} {'trades':>7} {'bps':>8} {'total $':>9} {'maxDD':>9} {'ret/DD':>7} "
      f"{'Sharpe':>7} {'worst day':>10} {'peak gross':>11} {'peak same':>10}")
for c in (20, 12, 10, 8, 6, 5, 4, 3):
    r = run(c)
    print(f"  {c:>5} {r['n']:>7,} {r['bps']:>+8.1f} {r['total']:>+9.2f} {r['dd']:>+9.2f} "
          f"{r['retdd']:>7.2f} {r['sharpe']:>+7.2f} {r['worst_day']:>+10.2f} "
          f"${r['peak_gross']:>10,.0f} {r['peak_same']:>10}")

print(f"\n{'='*86}")
print("### ENTRIES-PER-BAR CAP (current = unlimited; 08-19 opened 7 in one bar)")
print("=" * 86)
print(f"  {'per bar':>8} {'trades':>7} {'bps':>8} {'total $':>9} {'maxDD':>9} {'ret/DD':>7} "
      f"{'worst day':>10} {'peak gross':>11}")
for b in (99, 5, 4, 3, 2):
    r = run(20, max_bar=b)
    print(f"  {b:>8} {r['n']:>7,} {r['bps']:>+8.1f} {r['total']:>+9.2f} {r['dd']:>+9.2f} "
          f"{r['retdd']:>7.2f} {r['worst_day']:>+10.2f} ${r['peak_gross']:>10,.0f}")

print(f"\n{'='*86}")
print("### BOTH TOGETHER")
print("=" * 86)
print(f"  {'side':>5} {'bar':>4} {'trades':>7} {'bps':>8} {'total $':>9} {'maxDD':>9} "
      f"{'ret/DD':>7} {'worst day':>10} {'peak gross':>11}")
for c, b in ((20, 99), (8, 99), (8, 3), (6, 3), (6, 2), (5, 3)):
    r = run(c, max_bar=b)
    print(f"  {c:>5} {b:>4} {r['n']:>7,} {r['bps']:>+8.1f} {r['total']:>+9.2f} "
          f"{r['dd']:>+9.2f} {r['retdd']:>7.2f} {r['worst_day']:>+10.2f} "
          f"${r['peak_gross']:>10,.0f}")
print("\n  Judge on ret/|maxDD| and worst day, not total $. A cap cannot add edge; the")
print("  question is only how much P&L is given up to bound the correlated tail.")
