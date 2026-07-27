#!/usr/bin/env python3
"""Cross immediately when the spread is CHEAP, rest when it is expensive.

Reconciling two facts that look contradictory but are not:

  1. Signals that never fill are worth having. Priced as immediate takers -- paying the
     full spread and the taker fee -- the 106 unfilled trades of 400 earned +$0.313 each,
     t=+2.7 (analysis/taker_now.py). Price moved toward the reclaim zone without us.
  2. Crossing EVERYTHING is only a wash. It also pays the spread on the 74% that would
     have filled passively for free, and that cost cancels the gain.

Both are true, so the question is not whether to cross but WHEN. The cost of crossing is
the spread, and the spread is known at placement time and already logged by the live bot.
So: cross when it is cheap, rest when it is not.

    if spread <= threshold : cross immediately, guaranteed fill at the far touch
    else                   : rest as a maker, abandon if unfilled (today's behaviour)

Scored on the 400 audited paper trades using the bid/ask the bot actually recorded, so
spreads are observed, not assumed. Exit priced both ways since ~25% of exits do not fill
passively either.

  python3 analysis/spread_gate.py [report.csv] [arm_glob]
"""
import csv, glob, math, os, sys
from collections import defaultdict

REPORT = sys.argv[1] if len(sys.argv) > 1 else "live/shadow_fill_report_v2.csv"
ARMS = sys.argv[2] if len(sys.argv) > 2 else "live/paper_*.csv"
MAKER_BPS, TAKER_BPS = 1.5, 4.5

info = {}
for path in sorted(glob.glob(ARMS)):
    for r in csv.DictReader(open(path)):
        try:
            net = float(r["net_bps"]); pnl = float(r["pnl_usd"])
            if abs(net) < 1e-12:
                continue
            info[(r["symbol"], r["side"], r["reason"], round(pnl, 4))] = dict(
                notional=pnl/(net/1e4), e_bid=float(r["entry_bid"]),
                e_ask=float(r["entry_ask"]), exit=float(r["exit_px"]))
        except Exception:
            pass

T = []
for r in csv.DictReader(open(REPORT)):
    d = info.get((r["sym"], r["side"], r["reason"], round(float(r["pnl"]), 4)))
    if not d:
        continue
    mid = 0.5*(d["e_bid"] + d["e_ask"])
    if mid <= 0:
        continue
    T.append(dict(arm=r["arm"], side=r["side"], pnl=float(r["pnl"]),
                  filled=(r["entry_fill_300"] == "through"),
                  spread=(d["e_ask"]-d["e_bid"])/mid*1e4, **d))
print(f"joined {len(T)} audited trades\n")


def st(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


def taker_pnl(t, exit_bps):
    d = 1 if t["side"] == "LONG" else -1
    px = t["e_bid"] if d < 0 else t["e_ask"]     # short sells the bid, long buys the ask
    if px <= 0:
        return 0.0
    return t["notional"] * (d*(t["exit"]-px)/px - (TAKER_BPS+exit_bps)/1e4)


print("=== spread distribution on real signals (bps) ===")
sp = sorted(t["spread"] for t in T)
for q in (10, 25, 50, 75, 90):
    print(f"  p{q:<3} {sp[int(q/100*len(sp))]:>7.1f}")
print()

print("=== policy: cross if spread <= X, else rest-and-abandon ===")
print(f"  {'threshold':>12} {'crossed':>8} {'rested':>7} {'filled':>7} | "
      f"{'total mk':>9} {'total tk':>9} {'vs base':>9}")
base_mk = sum(t["pnl"] for t in T if t["filled"])
print(f"  {'0 (never)':>12} {0:>8} {len(T):>7} "
      f"{sum(1 for t in T if t['filled']):>7} | {base_mk:>+9.2f} {base_mk:>+9.2f} {'base':>9}")
best = None
for thr in (0.5, 1, 2, 3, 5, 8, 12, 20, 1e9):
    cross = [t for t in T if t["spread"] <= thr]
    rest = [t for t in T if t["spread"] > thr]
    tot_mk = sum(taker_pnl(t, MAKER_BPS) for t in cross) + sum(t["pnl"] for t in rest if t["filled"])
    tot_tk = sum(taker_pnl(t, TAKER_BPS) for t in cross) + sum(t["pnl"] for t in rest if t["filled"])
    nfill = len(cross) + sum(1 for t in rest if t["filled"])
    lbl = "all (always)" if thr > 1e8 else f"<= {thr:g} bps"
    print(f"  {lbl:>12} {len(cross):>8} {len(rest):>7} {nfill:>7} | "
          f"{tot_mk:>+9.2f} {tot_tk:>+9.2f} {tot_tk-base_mk:>+9.2f}")
    if best is None or tot_tk > best[1]:
        best = (lbl, tot_tk, len(cross), nfill)
print(f"\n  best by taker-exit total: {best[0]}  ->  ${best[1]:+.2f} "
      f"({best[2]} crossed, {best[3]}/{len(T)} signals traded vs "
      f"{sum(1 for t in T if t['filled'])} today)")

print("\n=== why it works (or does not): cost vs benefit by spread bucket ===")
print(f"  {'spread':>14} {'n':>5} {'would fill%':>12} {'cross-if-miss':>14} "
      f"{'cross-if-fill':>14}")
edges = [0, 1, 2, 4, 8, 1e9]
for a, b in zip(edges, edges[1:]):
    g = [t for t in T if a <= t["spread"] < b]
    if len(g) < 10:
        continue
    miss = [t for t in g if not t["filled"]]
    fill = [t for t in g if t["filled"]]
    # gain from crossing a would-be miss (it becomes a trade instead of nothing)
    gm = st([taker_pnl(t, TAKER_BPS) for t in miss])[0] if len(miss) >= 3 else float("nan")
    # cost of crossing something that would have filled anyway
    cf = (st([taker_pnl(t, TAKER_BPS) for t in fill])[0] - st([t["pnl"] for t in fill])[0]) \
        if len(fill) >= 3 else float("nan")
    lab = f"{a:g}-{b:g}b" if b < 1e8 else f"{a:g}b+"
    print(f"  {lab:>14} {len(g):>5} {len(fill)/len(g)*100:>11.0f}% "
          f"{gm:>+14.3f} {cf:>+14.3f}")
print()
print("  'cross-if-miss' is $/trade gained by crossing a signal that would NOT have")
print("  filled. 'cross-if-fill' is $/trade LOST by crossing one that would have. A")
print("  spread bucket is worth crossing only where the first, times the miss rate,")
print("  exceeds the second times the fill rate.")
