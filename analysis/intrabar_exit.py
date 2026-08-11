#!/usr/bin/env python3
"""The exit is checked once every 15 minutes. What does that cost?

exit_reason() compares the latest CLOSED 15m bar's close against the prior 24h range, so
the reclaim condition is only ever evaluated at bar boundaries. A position that reclaimed
four minutes into a bar and drifted back out by minute fifteen is not exited -- the bot
never saw it -- and it keeps holding for at least another bar. With 180 of 307 live trades
exiting on reclaim, and the backstop tail at -247bps being the entire loss book, the
granularity of that check is worth measuring rather than assuming.

This is an EXECUTION question, not a forecasting one. No feature has to predict anything;
either the bar-boundary check is leaving money on the table or it is not. That is why it is
worth doing even though the last several prediction batches came up empty.

The comparison, on 1-minute bars rebuilt from the tape:

  BAR-CLOSE (today)  first 15m close satisfying reclaim, else the 32-bar backstop
  1M-CLOSE           first 1m close satisfying reclaim -- realistic, the bot polls every
                     20s and could evaluate the live price
  FIRST-TOUCH        first 1m bar whose high/low crossed the level -- optimistic, bounds
                     the opportunity rather than estimating it

It is genuinely two-sided and not obviously a win: exiting sooner forfeits the cases where
price reclaimed and kept running in our favour. The prize, if there is one, is the third
table -- backstop losers that DID touch their reclaim level during the hold and were simply
never checked at the right moment.

  python3 analysis/intrabar_exit.py spike_events.csv bars_1m.csv.gz
"""
import math, sys
import numpy as np
import pandas as pd

EV = sys.argv[1] if len(sys.argv) > 1 else "spike_events.csv"
BARS = sys.argv[2] if len(sys.argv) > 2 else "bars_1m.csv.gz"
BAR_MS, HOLD_MIN, COST = 900000, 32 * 15, 3.0     # 32 bars = 8h = 480 one-minute bars

ev = pd.read_csv(EV)
full = pd.read_csv("tape_events_featured.csv",
                   usecols=["sym", "t", "entry", "prior_h", "prior_l", "dirn",
                            "signalled", "why", "fade_bps"])
ev = ev.merge(full, on=["sym", "t", "dirn", "signalled"], how="left",
              suffixes=("", "_f"))
print(f"{len(ev):,} events")

bars = pd.read_csv(BARS, dtype={"coin": "category"})
bars = bars.sort_values(["coin", "bar_ms"], kind="mergesort").reset_index(drop=True)
idx = {}
for c, g in bars.groupby("coin", observed=True, sort=False):
    idx[str(c)] = (g.bar_ms.values, g.c.values, g.h.values, g.l.values)
print(f"{len(bars):,} 1m bars, {len(idx)} coins")

rows = []
for r in ev.itertuples():
    p = idx.get(r.sym)
    if p is None or not np.isfinite(r.entry):
        continue
    t, cl, hi, lo = p
    a = np.searchsorted(t, r.t + BAR_MS, "left")       # exit search starts after the signal bar
    b = np.searchsorted(t, r.t + BAR_MS + HOLD_MIN * 60000, "left")
    if b - a < 60:
        continue
    tt, cc, hh, ll = t[a:b], cl[a:b], hi[a:b], lo[a:b]
    d = -float(r.dirn)                                  # position direction
    lvl = r.prior_h if r.dirn > 0 else r.prior_l
    e = float(r.entry)

    def pnl(px):
        return d * (px - e) / e * 1e4 - COST

    # --- reclaim tests. short: close back BELOW prior_h. long: close back ABOVE prior_l.
    ok_close = (cc < lvl) if r.dirn > 0 else (cc > lvl)
    ok_touch = (ll < lvl) if r.dirn > 0 else (hh > lvl)

    # BAR-CLOSE policy: only minutes that end a 15m bar are visible
    is_boundary = ((tt + 60000) % BAR_MS) == 0
    j_bar = np.argmax(ok_close & is_boundary) if (ok_close & is_boundary).any() else -1
    j_1m = np.argmax(ok_close) if ok_close.any() else -1
    j_tc = np.argmax(ok_touch) if ok_touch.any() else -1

    out = dict(sym=r.sym, t=r.t, signalled=r.signalled, why=r.why,
               ever_touched=bool(ok_touch.any()), ever_closed=bool(ok_close.any()))
    out["bar_bps"] = pnl(cc[j_bar]) if j_bar >= 0 else pnl(cc[-1])
    out["bar_min"] = (tt[j_bar] - r.t) / 60000 if j_bar >= 0 else np.nan
    out["m1_bps"] = pnl(cc[j_1m]) if j_1m >= 0 else pnl(cc[-1])
    out["m1_min"] = (tt[j_1m] - r.t) / 60000 if j_1m >= 0 else np.nan
    out["tc_bps"] = pnl(lvl) if j_tc >= 0 else pnl(cc[-1])
    out["tc_min"] = (tt[j_tc] - r.t) / 60000 if j_tc >= 0 else np.nan
    out["bar_exited"] = j_bar >= 0
    rows.append(out)

R = pd.DataFrame(rows)
print(f"{len(R):,} events with a usable 1m path\n")


def st(v):
    v = np.asarray(v, float); v = v[~np.isnan(v)]
    n = len(v)
    return (v.mean(), v.mean()/(v.std(ddof=1)/math.sqrt(n)), n) if n > 1 else (np.nan,)*3


for lab, pop in (("SIGNALLED (gated)", R[R.signalled == 1]), ("ALL SPIKES", R)):
    print(f"{'='*78}\n### {lab} — n={len(pop):,}\n{'='*78}")
    print(f"  {'exit policy':<22} {'mean bps':>10} {'t':>7} {'median hold':>12} "
          f"{'win%':>6} {'total $/35':>11}")
    for pl, c, mc in (("bar-close (today)", "bar_bps", "bar_min"),
                      ("1m-close", "m1_bps", "m1_min"),
                      ("first-touch (bound)", "tc_bps", "tc_min")):
        m, t, n = st(pop[c])
        print(f"  {pl:<22} {m:>+10.1f} {t:>+7.2f} {pop[mc].median():>9.0f} min "
              f"{100*(pop[c] > 0).mean():>5.0f}% {35*pop[c].sum()/1e4:>+11.2f}")
    d1 = pop.m1_bps - pop.bar_bps
    m, t, n = st(d1)
    print(f"\n  1m-close minus bar-close: {m:+.1f} bps/trade (t={t:+.2f}), "
          f"${35*d1.sum()/1e4:+.2f} total on n={n:,}")
    print(f"    helped {100*(d1 > 1).mean():.0f}%   hurt {100*(d1 < -1).mean():.0f}%   "
          f"unchanged {100*(d1.abs() <= 1).mean():.0f}%")

print(f"\n{'='*78}\n### THE PRIZE: backstop losers that DID reach their reclaim level\n{'='*78}")
g = R[R.signalled == 1]
bs = g[~g.bar_exited]
print(f"  gated trades that ran to the backstop: {len(bs):,}")
print(f"    of those, price CLOSED back inside the range at some 1m bar: "
      f"{int(bs.ever_closed.sum()):,} ({100*bs.ever_closed.mean():.0f}%)")
print(f"    of those, price merely TOUCHED the level: "
      f"{int(bs.ever_touched.sum()):,} ({100*bs.ever_touched.mean():.0f}%)")
sub = bs[bs.ever_closed]
if len(sub) > 5:
    m0, _, _ = st(sub.bar_bps)
    m1, t1, n1 = st(sub.m1_bps)
    print(f"\n  those {len(sub):,} trades:")
    print(f"    held to backstop (today)     {m0:>+8.1f} bps   ${35*sub.bar_bps.sum()/1e4:>+8.2f}")
    print(f"    exited at the 1m reclaim     {m1:>+8.1f} bps   ${35*sub.m1_bps.sum()/1e4:>+8.2f}")
    print(f"    difference                   {m1-m0:>+8.1f} bps   "
          f"${35*(sub.m1_bps-sub.bar_bps).sum()/1e4:>+8.2f}   (t={st(sub.m1_bps-sub.bar_bps)[1]:+.2f})")
    print(f"    median minutes to that reclaim: {sub.m1_min.median():.0f}")
print("\n  A backstop loser that closed back inside the range mid-bar is money the current")
print("  check granularity cannot see. Everything else here is a wash by construction.")
