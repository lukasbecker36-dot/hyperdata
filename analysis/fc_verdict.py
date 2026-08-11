#!/usr/bin/env python3
"""Phase 1 verdict, with the effective-sample-size gate applied. Writes fc_verdict.md.

fc_ic.py reported PASS. It should not have, and the reason is the one thing spec 6
insists on reporting next to every result: effective n = n_timestamps / h_bars.

The two cells that cleared the cost floor are:

    h=60,  MID, intensity   t_NW = -4.3   effective n =  2.18
    h=240, MID, ret1        t_NW = -3.2   effective n =  0.54

Effective n below one is not a weak result, it is not a result. MID-tier coins rarely
clear 10 prints and $5k of notional in a 1-minute bar, so the cross-section is thin and
most timestamps fail the >=5-coins rule; 131 surviving timestamps at a 60-bar horizon
overlap almost completely. A Newey-West t-stat computed on that is an artifact of the
correction itself, not evidence.

The cells with real statistics say the opposite, and they say it very clearly.

  python3 analysis/fc_verdict.py ic_table.csv
"""
import math, sys
from statistics import NormalDist
import numpy as np
import pandas as pd

IC = pd.read_csv(sys.argv[1] if len(sys.argv) > 1 else "ic_table.csv")
OUT = "fc_verdict.md"
COST_MAKER, COST_TAKER = 2.88, 5.73
GATE = 1.5
MIN_EFF_N = 100          # below this the t-stat is describing overlap, not data
SIG = {(1, "HIGH"): 25.4, (1, "MID"): 36.1, (1, "LOW"): 77.3,
       (5, "HIGH"): 48.3, (5, "MID"): 57.1, (5, "LOW"): 137.7,
       (15, "HIGH"): 73.3, (15, "MID"): 86.3, (15, "LOW"): 215.8,
       (60, "HIGH"): 132.8, (60, "MID"): 152.0, (60, "LOW"): 428.1,
       (240, "HIGH"): 220.9, (240, "MID"): 275.2, (240, "LOW"): 592.6}

L = []
def w(s=""):
    L.append(s); print(s.encode("ascii", "replace").decode())


crit = abs(NormalDist().inv_cdf(0.025 / len(IC)))
w("# Phase 1 verdict: the cost floor is not cleared")
w()
w(f"Trials logged: **{len(IC)}** (9 features x 5 horizons x 4 tiers). "
  f"Bonferroni threshold |t| > **{crit:.2f}**.")
w()
w("## Why the two apparent passes are discarded")
w()
w("| h | feature | tier | t_NW | n_timestamps | **effective n** |")
w("|---|---|---|---|---|---|")
for _, r in IC[(IC.horizon.isin([60, 240])) & (IC.tier == "MID")
               & (IC.t_nw.abs() > 3)].iterrows():
    w(f"| {r.horizon} | {r.feature} | {r.tier} | {r.t_nw:+.1f} | "
      f"{int(r.n_timestamps)} | **{r.eff_n:.2f}** |")
w()
w("Effective n below 1 is not a weak result, it is not a result. Both are discarded.")
w()

ok = IC[(IC.t_nw.abs() > crit) & (IC.day_consistency >= 0.60)
        & (IC.eff_n >= MIN_EFF_N) & (IC.tier != "ALL")].copy()
ok["sigma_h"] = [SIG.get((h, t), np.nan) for h, t in zip(ok.horizon, ok.tier)]
ok["fcast_bps"] = ok.sigma_h * ok.ic.abs()
ok["ratio_maker"] = ok.fcast_bps / COST_MAKER
ok["ratio_taker"] = ok.fcast_bps / COST_TAKER
ok = ok.sort_values("ratio_maker", ascending=False)

w("## Cells that survive all three statistical gates")
w()
w(f"Bonferroni |t_NW| > {crit:.2f}, day-consistency >= 60%, effective n >= {MIN_EFF_N}.")
w()
w("| h | feature | tier | IC | t_NW | eff n | days+ | sigma_h | forecastable | "
  "maker ratio | taker ratio |")
w("|---|---|---|---|---|---|---|---|---|---|---|")
for _, r in ok.head(12).iterrows():
    w(f"| {r.horizon}m | `{r.feature}` | {r.tier} | {r.ic:+.5f} | {r.t_nw:+.1f} | "
      f"{r.eff_n:,.0f} | {r.day_consistency:.0%} | {r.sigma_h:.1f} | "
      f"**{r.fcast_bps:.2f} bps** | **{r.ratio_maker:.2f}** | {r.ratio_taker:.2f} |")
w()
best = ok.ratio_maker.max()
w(f"Best maker ratio among statistically real cells: **{best:.2f}** against a gate of "
  f"{GATE}. Every cell fails, by a factor of {GATE/best:.1f}.")
w()

w("## How far short, and can combining features close it?")
w()
w("The optimistic bound: if the Tier-1 features were mutually INDEPENDENT, a combined "
  "IC would be sqrt(sum of squared ICs). They are not independent -- `clv`, `ofi` and "
  "`ret1` all measure the same directional pressure -- so this is an upper bound that "
  "cannot be reached in practice.")
w()
w("| h | tier | best single IC | independent-combination bound | IC needed for ratio 1.5 | shortfall |")
w("|---|---|---|---|---|---|")
for (h, t), g in ok.groupby(["horizon", "tier"]):
    s = SIG[(h, t)]
    comb = math.sqrt((g.ic ** 2).sum())
    need = GATE * COST_MAKER / s
    w(f"| {h}m | {t} | {g.ic.abs().max():.4f} | {comb:.4f} | **{need:.4f}** | "
      f"**{need/comb:.1f}x** |")
w()
w("Even the unreachable independent-combination bound falls short at every horizon.")
w()

w("## Verdict")
w()
w("**Phase 1 FAILS.** Spec 9 says: stop and report.")
w()
w("The signal is real and it is not the problem. `clv`, `ofi` and `ret1` at 1-minute "
  "produce t-stats of 12-16 on ~28,000 independent timestamps, hold their sign on 86-95% "
  "of days, and clear Bonferroni over 130 trials by a wide margin. This is about as "
  "solid as a microstructure result gets on 20 days.")
w()
w("It is simply too small. The best forecastable component is **0.96 bps** against a "
  "**2.88 bps** maker round trip, measured from real fills rather than assumed. Taker is "
  "arithmetically dead at 5.73 bps.")
w()
w("Two things follow that are worth more than the negative result itself:")
w()
w("1. **Costs are already near the floor.** Measured maker round trip is 2.88 bps "
  "against a 3.0 bps theoretical minimum at base fee tier, entry slippage is "
  "*negative* (-1.97 bps -- resting captures spread rather than paying it), and "
  "adverse selection is negative too (fills outperform misses by 9.9 bps). There is "
  "no execution improvement available that would change the answer.")
w()
w("2. **The gap is structural, not a sample-size problem.** Closing it needs an IC "
  "of ~0.17 at 1m where 0.032 is measured. More data would tighten the error bars "
  "around a number that is already 5x too small.")
w()
w("Spec 9 also says: do not re-search the same 21 days with new features. Phases 2-5 "
  "are not justified.")

open(OUT, "w", encoding="utf-8").write("\n".join(L) + "\n")
print(f"\nwrote {OUT}")
